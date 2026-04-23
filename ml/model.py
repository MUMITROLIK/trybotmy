"""
ml/model.py — загрузка модели и предсказание.

Ключевое отличие от v1:
  - predict_win_prob использует оптимальный порог из model_meta.json
    вместо хардкоженного 0.5.
  - Порог подбирается в ml_train.py по максимальному F1.
  - Функция predict_win_prob возвращает вероятность (0.0-1.0),
    а сравнение с порогом делается в signal_generator через config.ML_FILTER_MIN_PROB.
  - get_threshold() отдаёт порог из meta — используй его если нужен бинарный ответ.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np

_MODEL = None
_META: dict = {}

_MODEL_PATH = Path("ml/model.joblib")
_META_PATH  = Path("ml/model_meta.json")


def load_model(path: str | Path = None):
    global _MODEL, _META
    p = Path(path) if path else _MODEL_PATH
    if not p.exists():
        _MODEL = None
        return None
    _MODEL = joblib.load(p)

    # Загружаем мету рядом с моделью
    meta_p = p.parent / "model_meta.json"
    if meta_p.exists():
        try:
            _META = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            _META = {}

    return _MODEL


def _ensure_model():
    global _MODEL
    if _MODEL is None:
        load_model()
    return _MODEL


def get_threshold() -> float:
    """
    Возвращает оптимальный порог из model_meta.json.
    Если meta нет — возвращает 0.5 (старое поведение).
    Используй этот порог вместо config.ML_FILTER_MIN_PROB если хочешь
    автоматически адаптировать порог к каждому переобучению.
    """
    return float(_META.get("threshold", 0.5))


def get_model_info() -> dict:
    """Возвращает информацию о текущей модели для логов/Telegram."""
    return {
        "model_name": _META.get("model_name", "unknown"),
        "roc_auc":    _META.get("roc_auc"),
        "cv_auc":     _META.get("cv_auc_mean"),
        "threshold":  get_threshold(),
        "rows":       _META.get("rows", 0),
        "calibrated": _META.get("calibrated", False),
        "trained_at": _META.get("trained_at"),
        "top_features": _META.get("feature_importance", [])[:5],
    }


def predict_win_prob(features: dict[str, Any]) -> float | None:
    """
    Возвращает вероятность WIN (TP1/TP2/SL_AFTER_TP1).
    Если модель не загружена — возвращает None.

    Вероятность откалибрована (CalibratedClassifierCV в ml_train.py v3),
    т.е. 0.6 означает примерно 60% шанс WIN, а не просто "больше 0.5".

    Сравнение с порогом:
      - config.ML_FILTER_MIN_PROB — фиксированный порог из .env
      - get_threshold() — оптимальный порог из model_meta.json (рекомендуется)
    """
    m = _ensure_model()
    if m is None:
        return None
    try:
        import pandas as pd
        
        # Всегда используем порядок признаков из model_meta
        expected_features = _META.get("features", [])
        if not expected_features:
            return None
        
        # Категориальные категории (для правильного кодирования)
        CATEGORICAL_MAPPING = {
            "direction": ["LONG", "SHORT"],
            "session": ["american", "asian", "european"],
            "btc_trend_4h": ["bear", "bull", "neutral"],
            "btc_trend_1h": ["bear", "bull", "neutral"],
            "entry_type": ["market", "limit"],
            "market_regime": ["bull", "bear", "flat", "fear_bull", "greed_bear", "unknown"],
        }
        
        # Создаём строку с нулями для всех ожидаемых столбцов
        X_dict = {f: 0.0 for f in expected_features}
        
        # Заполняем числовые признаки
        numeric_cols = _META.get("numeric_features", [])
        for col in numeric_cols:
            if col in features:
                X_dict[col] = features[col]
        
        # Кодируем категориальные в нужный формат (с нулями по дефоулту)
        for col_name, categories in CATEGORICAL_MAPPING.items():
            if col_name in features:
                col_value = str(features[col_name]) if features[col_name] is not None else "unknown"
                for cat in categories:
                    col_encoded = f"{col_name}_{cat}"
                    if col_encoded in X_dict:
                        X_dict[col_encoded] = 1 if col_value == cat else 0
        
        # Создаём DataFrame с правильным порядком столбцов
        X_final = pd.DataFrame([[X_dict[f] for f in expected_features]], columns=expected_features)
        
        # Предсказание
        p = float(m.predict_proba(X_final)[:, 1][0])
        if np.isnan(p):
            return None
        return max(0.0, min(1.0, p))
    except Exception as e:
        print(f"[ML WARNING] predict_win_prob error: {type(e).__name__}: {e}")
        return None