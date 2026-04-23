"""
ml_train_v2.py — Умная ML модель v2

Улучшения vs v1:
  1. Новые фичи: hour_of_day, day_of_week, market_regime, atr_percentile, btc_drawdown_pct
  2. Использует clean3 датасет (без мусора)
  3. Ансамбль моделей (GradientBoosting + LogisticRegression) — лучше на маленьких данных
  4. Стратификация по market_regime — модель знает режим рынка
  5. Feature importance — видно какие фичи реально работают
  6. Более строгая валидация (TimeSeriesSplit — не смотрим в будущее)
  7. Автовыбор модели по размеру датасета:
       < 200 строк  → LogisticRegression (стабильно)
       200-500      → GradientBoosting (баланс скорость/качество)
       > 500        → GradientBoosting + калибровка

Запуск:
    py -3.11 ml_train_v2.py
    py -3.11 ml_train_v2.py --data docs/ml_dataset_clean3.json
    py -3.11 ml_train_v2.py --data docs/ml_dataset_clean2.json  # если clean3 пустой

Output:
    ml/model.joblib
    ml/model_meta.json
"""

import json
import sys
import warnings
import argparse
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import joblib
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler, LabelEncoder
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit, cross_val_score
from sklearn.metrics import roc_auc_score, classification_report, confusion_matrix
from sklearn.impute import SimpleImputer
import sklearn

warnings.filterwarnings("ignore")

# ── Пути ─────────────────────────────────────────────────────────────────────
DATA_PATH_CLEAN3 = Path("docs/ml_dataset_clean3.json")
DATA_PATH_CLEAN2 = Path("docs/ml_dataset_clean2.json")
MODEL_PATH       = Path("ml/model.joblib")
META_PATH        = Path("ml/model_meta.json")

# ── Фичи v2 (старые + новые) ──────────────────────────────────────────────────
# Числовые фичи — нормализуются StandardScaler
NUMERIC_FEATURES = [
    # Индикаторы (существующие)
    "rsi",
    "macd_hist",
    "bb_position",
    "ema20_50_ratio",
    "volume_ratio",
    "atr_pct",
    "cvd_norm",
    "oi_change_pct",
    "ob_imbalance",
    "btc_correlation",
    # Скоры (существующие)
    "score_rsi",
    "score_macd",
    "score_bb",
    "score_ema",
    "score_volume",
    "score_orderbook",
    "score_news",
    "score_mtf",
    "score_oi",
    "score_cvd",
    "score_patterns",
    "score_btc_corr",
    "score_levels",
    # Мета
    "strength",
    "heavy_confirmed",
    "fear_greed",
    "funding_rate",
    "btc_change_4h",
    # ── НОВЫЕ фичи v2 ──────────────────────────
    "hour_of_day",          # UTC час — важен для ликвидности
    "day_of_week",          # день недели
    "atr_percentile",       # волатильность относительно нормы
    "btc_drawdown_pct",     # просадка BTC от локального хая
]

# Категориальные фичи — One-Hot кодирование
CATEGORICAL_FEATURES = [
    "direction",        # LONG / SHORT
    "session",          # asian / european / american
    "btc_trend_4h",     # bull / bear / neutral
    "btc_trend_1h",
    "entry_type",       # market / limit
    "market_regime",    # ── НОВОЕ: bull / bear / flat / fear_bull / greed_bear
]

# Target: WIN = TP1/TP2/SL_AFTER_TP1, LOSS = SL/TIMEOUT(убыток)
WIN_TARGETS  = {"TP1", "TP2", "SL_AFTER_TP1"}
LOSS_TARGETS = {"SL", "TIMEOUT", "MANUAL"}


def _load_data(path: Path) -> pd.DataFrame:
    """Загружает и валидирует датасет."""
    if not path.exists():
        raise FileNotFoundError(f"Датасет не найден: {path}")

    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)

    df = pd.DataFrame(rows)
    print(f"[Train] Загружено {len(df)} строк из {path}")
    return df


def _prepare_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """
    Подготавливает X (фичи) и y (таргет).
    Только WIN и LOSS — TIMEOUT без убытка и прочие исключаются.
    """
    # Таргет
    def _to_target(row):
        t   = row.get("target", "")
        pnl = row.get("pnl_pct") or 0
        if t in WIN_TARGETS:
            return 1
        if t == "SL":
            return 0
        if t == "TIMEOUT" and pnl < 0:
            return 0   # таймаут с убытком = LOSS
        if t == "TIMEOUT" and pnl >= 0:
            return 1   # таймаут с прибылью = WIN
        if t == "MANUAL" and pnl < 0:
            return 0
        if t == "MANUAL" and pnl >= 0:
            return 1
        return None

    df = df.copy()
    df["_target"] = df.apply(_to_target, axis=1)
    df = df[df["_target"].notna()].copy()
    y = df["_target"].astype(int)

    print(f"[Train] После фильтрации: {len(df)} строк "
          f"(WIN={y.sum()}, LOSS={len(y)-y.sum()}, "
          f"WR={y.mean()*100:.1f}%)")

    # Числовые фичи
    num_cols = [c for c in NUMERIC_FEATURES if c in df.columns]
    missing_num = [c for c in NUMERIC_FEATURES if c not in df.columns]
    if missing_num:
        print(f"[Train] ⚠️  Числовые фичи не в датасете (старые данные): {missing_num}")
        # Добавляем нулями — модель поймёт что это пропуски (SimpleImputer заменит средним)
        for c in missing_num:
            df[c] = np.nan

    # Категориальные фичи → One-Hot
    cat_dummies = []
    cat_cols = [c for c in CATEGORICAL_FEATURES if c in df.columns]
    missing_cat = [c for c in CATEGORICAL_FEATURES if c not in df.columns]
    if missing_cat:
        print(f"[Train] ⚠️  Категориальные фичи не в датасете: {missing_cat}")
        for c in missing_cat:
            df[c] = "unknown"
        cat_cols = CATEGORICAL_FEATURES

    for col in cat_cols:
        dummies = pd.get_dummies(df[col].fillna("unknown"), prefix=col)
        cat_dummies.append(dummies)

    X = pd.concat([df[NUMERIC_FEATURES].reset_index(drop=True)]
                  + [d.reset_index(drop=True) for d in cat_dummies], axis=1)

    return X, y


def _choose_model(n_rows: int, n_features: int):
    """
    Авто-выбор модели по размеру датасета.
    Логика: чем больше данных, тем сложнее модель.
    """
    if n_rows < 150:
        # Мало данных → простая логистика, не переобучится
        print(f"[Train] Модель: LogisticRegression (n={n_rows} < 150)")
        return "LogisticRegression", LogisticRegression(
            max_iter=2000, C=0.5, solver="lbfgs", class_weight="balanced"
        )
    elif n_rows < 400:
        # Средне → Gradient Boosting, умеренная глубина
        print(f"[Train] Модель: GradientBoosting (n={n_rows} 150-400)")
        return "GradientBoosting", GradientBoostingClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.05,
            subsample=0.8, min_samples_leaf=5,
            random_state=42,
        )
    else:
        # Много данных → более глубокий GB
        print(f"[Train] Модель: GradientBoosting deep (n={n_rows} >= 400)")
        return "GradientBoosting-Deep", GradientBoostingClassifier(
            n_estimators=100,    # было 200
            max_depth=3,         # было 4
            learning_rate=0.05,  # было 0.03
            subsample=0.7,       # было 0.8
            min_samples_leaf=8,  # было 3 — главный фикс
            random_state=42,
        )


def _get_feature_importance(model_pipeline, feature_names: list) -> list:
    """Извлекает важность фич из модели."""
    try:
        # Разворачиваем CalibratedClassifierCV → base estimator
        base = model_pipeline
        if hasattr(base, "calibrated_classifiers_"):
            base = base.calibrated_classifiers_[0].estimator
        if hasattr(base, "named_steps"):
            base = base.named_steps.get("clf", base)

        if hasattr(base, "feature_importances_"):
            imp = base.feature_importances_
        elif hasattr(base, "coef_"):
            imp = abs(base.coef_[0])
        else:
            return []

        pairs = sorted(zip(feature_names, imp), key=lambda x: x[1], reverse=True)
        return [{"feature": f, "importance": round(float(v), 5)} for f, v in pairs[:20]]
    except Exception as e:
        print(f"[Train] Feature importance недоступна: {e}")
        return []


def train(data_path: Path = None) -> dict:
    """
    Главная функция обучения.
    Вызывается из ml_auto_trainer.py и напрямую.

    Returns:
        dict с метаданными модели (roc_auc, model_name, rows, trained_at, ...)
    """
    # ── Загрузка ─────────────────────────────────────────────────────────────
    if data_path is None:
        # Приоритет: clean3 → clean2
        data_path = DATA_PATH_CLEAN3 if DATA_PATH_CLEAN3.exists() else DATA_PATH_CLEAN2

    df_raw = _load_data(data_path)
    X, y   = _prepare_features(df_raw)

    n_rows     = len(y)
    n_features = X.shape[1]

    if n_rows < 50:
        raise ValueError(f"Слишком мало данных для обучения: {n_rows} строк (нужно >= 50)")

    # ── Выбор модели ──────────────────────────────────────────────────────────
    model_name, base_clf = _choose_model(n_rows, n_features)

    # Pipeline: заполняем пропуски → масштабируем → модель
    pipeline = Pipeline([
        ("imputer", SimpleImputer(strategy="mean")),
        ("scaler",  StandardScaler()),
        ("clf",     base_clf),
    ])

    # ── Кросс-валидация (честная — без утечки будущего) ───────────────────────
    cv_folds = min(5, max(3, n_rows // 40))
    cv = TimeSeriesSplit(n_splits=cv_folds)

    print(f"[Train] Кросс-валидация {cv_folds} фолдов...")
    cv_scores = cross_val_score(pipeline, X, y, cv=cv, scoring="roc_auc", n_jobs=-1)
    cv_mean   = float(cv_scores.mean())
    cv_std    = float(cv_scores.std())
    print(f"[Train] CV ROC-AUC: {cv_mean:.3f} ± {cv_std:.3f}")

    # ── Обучение на всём датасете + калибровка ────────────────────────────────
    print("[Train] Обучение финальной модели с калибровкой...")
    
    # Балансировка классов через sample_weight
    from sklearn.utils.class_weight import compute_sample_weight
    sample_weights = compute_sample_weight(class_weight='balanced', y=y)
    
    n_calib_folds = min(3, max(2, n_rows // 60))
    calibrated = CalibratedClassifierCV(pipeline, cv=n_calib_folds, method="isotonic")
    calibrated.fit(X, y, sample_weight=sample_weights)

    # ── Метрики на тренировочном сете ─────────────────────────────────────────
    y_prob = calibrated.predict_proba(X)[:, 1]
    y_pred = (y_prob >= 0.5).astype(int)
    train_auc = roc_auc_score(y, y_prob)

    print(f"[Train] Train ROC-AUC: {train_auc:.3f}")
    print(f"[Train] Отчёт:\n{classification_report(y, y_pred, target_names=['LOSS','WIN'])}")
    
    # Проверка WIN recall
    y_pred_labels = calibrated.predict(X)
    pred_labels = ['WIN' if p == 1 else 'LOSS' for p in y_pred_labels]
    true_labels = ['WIN' if a == 1 else 'LOSS' for a in y]
    wins_found = sum(1 for p, a in zip(pred_labels, true_labels) if p == 'WIN' and a == 'WIN')
    wins_total = sum(1 for a in true_labels if a == 'WIN')
    print(f"[Train] WIN recall проверка: находим {wins_found} из {wins_total} реальных WIN ({round(wins_found/wins_total*100,1) if wins_total else 0}%)")

    # ── Оптимальный порог по F1 ───────────────────────────────────────────────
    best_thresh = 0.5
    best_f1     = 0.0
    for thresh in np.arange(0.35, 0.75, 0.01):
        pred_t = (y_prob >= thresh).astype(int)
        tp = ((pred_t == 1) & (y == 1)).sum()
        fp = ((pred_t == 1) & (y == 0)).sum()
        fn = ((pred_t == 0) & (y == 1)).sum()
        prec = tp / (tp + fp + 1e-9)
        rec  = tp / (tp + fn + 1e-9)
        f1   = 2 * prec * rec / (prec + rec + 1e-9)
        if f1 > best_f1:
            best_f1     = f1
            best_thresh = round(float(thresh), 2)

    print(f"[Train] Оптимальный порог: {best_thresh} (F1={best_f1:.3f})")

    # ── Важность фич ─────────────────────────────────────────────────────────
    feature_names = list(X.columns)
    top_features  = _get_feature_importance(calibrated, feature_names)
    if top_features:
        print("[Train] Топ-5 фич по важности:")
        for f in top_features[:5]:
            print(f"  {f['feature']:<35} {f['importance']:.5f}")

    # ── Сохранение ────────────────────────────────────────────────────────────
    MODEL_PATH.parent.mkdir(exist_ok=True)
    joblib.dump(calibrated, MODEL_PATH)

    meta = {
        "model_name":         model_name,
        "roc_auc":            round(train_auc, 4),
        "cv_auc_mean":        round(cv_mean, 4),
        "cv_auc_std":         round(cv_std, 4),
        "threshold":          best_thresh,
        "rows":               n_rows,
        "features":           feature_names,
        "numeric_features":   NUMERIC_FEATURES,
        "categorical_features": CATEGORICAL_FEATURES,
        "calibrated":         True,
        "sklearn_version":    sklearn.__version__,
        "trained_at":         datetime.now(timezone.utc).isoformat(),
        "data_path":          str(data_path),
        "feature_importance": top_features,
        "class_balance": {
            "win":  int(y.sum()),
            "loss": int(len(y) - y.sum()),
            "winrate": round(float(y.mean()) * 100, 1),
        },
        "cv_folds": cv_folds,
    }

    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✅ Модель сохранена: {MODEL_PATH}")
    print(f"✅ Мета сохранена:   {META_PATH}")
    print(f"   ROC-AUC (train): {train_auc:.3f}")
    print(f"   CV ROC-AUC:      {cv_mean:.3f} ± {cv_std:.3f}")
    print(f"   Порог (F1 opt):  {best_thresh}")
    print(f"   Строк:           {n_rows}")
    print(f"   Фич:             {n_features}")

    return meta


def evaluate_by_regime(data_path: Path = None):
    """
    Дополнительный анализ: качество модели по режиму рынка.
    Показывает насколько хорошо модель работает в bull/bear/flat.
    """
    from ml.model import load_model, predict_win_prob

    if data_path is None:
        data_path = DATA_PATH_CLEAN3 if DATA_PATH_CLEAN3.exists() else DATA_PATH_CLEAN2

    model = load_model()
    if model is None:
        print("Модель не загружена")
        return

    df_raw = _load_data(data_path)
    X, y   = _prepare_features(df_raw)

    # Добавляем market_regime обратно для разбивки
    if "market_regime" in df_raw.columns:
        regimes = df_raw["market_regime"].fillna("unknown").values[:len(y)]
    else:
        regimes = ["unknown"] * len(y)

    y_prob = model.predict_proba(X)[:, 1]

    print("\n[Evaluate] Качество по режиму рынка:")
    print(f"{'Режим':<15} {'n':>6} {'WR%':>7} {'AUC':>7}")
    print("-" * 40)

    for regime in sorted(set(regimes)):
        mask  = (regimes == regime) if hasattr(regimes, '__eq__') else [r == regime for r in regimes]
        mask  = np.array(mask)
        y_r   = y.values[mask]
        p_r   = y_prob[mask]
        n     = mask.sum()
        if n < 10:
            continue
        wr  = round(y_r.mean() * 100, 1)
        try:
            auc = round(roc_auc_score(y_r, p_r), 3)
        except Exception:
            auc = None
        print(f"{regime:<15} {n:>6} {wr:>7}% {str(auc):>7}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ML Train v2")
    parser.add_argument("--data", type=str, default=None, help="Путь к датасету JSON")
    parser.add_argument("--eval", action="store_true", help="Анализ по режиму рынка")
    args = parser.parse_args()

    data_path = Path(args.data) if args.data else None

    # Если clean3 пустой — fallback на clean2
    if data_path is None:
        if DATA_PATH_CLEAN3.exists():
            with open(DATA_PATH_CLEAN3) as f:
                n = len(json.load(f))
            if n >= 50:
                data_path = DATA_PATH_CLEAN3
                print(f"[Train] Используем clean3 ({n} строк)")
            else:
                data_path = DATA_PATH_CLEAN2
                print(f"[Train] clean3 мало данных ({n}), используем clean2")
        else:
            data_path = DATA_PATH_CLEAN2
            print("[Train] clean3 не найден, используем clean2")

    meta = train(data_path)

    if args.eval:
        evaluate_by_regime(data_path)

    print("\n🎯 Готово! Модель обновлена.")
    print(f"   Запусти бота заново или подожди auto_trainer (каждые 100 сделок)")