"""
ml_auto_trainer.py — автоматическое переобучение ML без остановки бота.

Логика:
  - Каждые CHECK_INTERVAL_SEC секунд проверяем сколько НОВЫХ размеченных строк
    появилось с момента последнего обучения.
  - Если накопилось >= RETRAIN_EVERY_N_TRADES новых строк → переобучаем.
  - Обучение выполняется в отдельном потоке (ThreadPoolExecutor),
    чтобы не блокировать asyncio event loop и не останавливать сканер/трекер.
  - После обучения перезагружаем модель прямо в памяти (load_model()).
  - Отправляем уведомление в Telegram с метриками до/после.
  - Если качество упало (новый AUC хуже старого на > 0.03) — откатываемся.
"""

import asyncio
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from utils import logger

# ── Настройки ─────────────────────────────────────────────────────────────────
CHECK_INTERVAL_SEC   = 300        # проверяем каждые 5 минут
RETRAIN_EVERY_N_TRADES = 100      # переобучаем каждые 100 новых размеченных строк
MIN_ROWS_TO_TRAIN    = 200        # минимум строк в датасете чтобы вообще обучать
MAX_AUC_REGRESSION   = 0.03       # если новый AUC хуже на 0.03+ — откатываемся

DATA_PATH  = Path("docs/ml_dataset_clean3.json")  # ← ИЗМЕНЕНО: используем clean3 (только с market_regime)
MODEL_PATH = Path("ml/model.joblib")
META_PATH  = Path("ml/model_meta.json")
BACKUP_MODEL = Path("ml/model_backup.joblib")
BACKUP_META  = Path("ml/model_meta_backup.json")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ml_train")
_notify_cb = None          # callback для отправки в Telegram
_last_trained_count = 0    # сколько строк было при последнем обучении
_last_train_ts = 0.0       # timestamp последнего обучения

# Файл для сохранения состояния между перезапусками
_STATE_FILE = Path("ml/auto_trainer_state.json")


def _save_state():
    """Сохраняет счётчик на диск чтобы не сбрасывался при перезапуске."""
    try:
        _STATE_FILE.parent.mkdir(exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps({"last_trained_count": _last_trained_count,
                        "last_train_ts": _last_train_ts}),
            encoding="utf-8"
        )
    except Exception:
        pass


def _load_state():
    """Загружает счётчик с диска при старте."""
    try:
        if _STATE_FILE.exists():
            s = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            return s.get("last_trained_count", 0), s.get("last_train_ts", 0.0)
    except Exception:
        pass
    return 0, 0.0


def set_notify_callback(fn):
    """Устанавливает callback для отправки сообщений в Telegram."""
    global _notify_cb
    _notify_cb = fn


def _get_labeled_count() -> int:
    """Возвращает количество размеченных clean3 строк в БД (только с market_regime)."""
    try:
        from database.db import get_conn
        conn = get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*) FROM ml_features
            WHERE labeled = 1
              AND market_regime IS NOT NULL
              AND COALESCE(target,'') NOT IN ('EXPIRED', 'NOT_FILLED')
        """)
        count = c.fetchone()[0] or 0
        conn.close()
        return count
    except Exception as e:
        logger.err("MLAuto", f"Ошибка подсчёта строк: {e}")
        return 0


def _get_current_auc() -> float | None:
    """Читает текущий ROC-AUC из model_meta.json."""
    try:
        if META_PATH.exists():
            meta = json.loads(META_PATH.read_text(encoding="utf-8"))
            return meta.get("cv_auc_mean")
    except Exception:
        pass
    return None


def _get_current_meta() -> dict:
    """Читает полный model_meta.json."""
    try:
        if META_PATH.exists():
            return json.loads(META_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _train_model() -> dict:
    """
    Запускается в отдельном потоке.
    Экспортирует датасет, обучает модель, сохраняет файлы.
    Возвращает словарь с результатами.
    """
    result = {
        "success": False,
        "rows": 0,
        "auc_old": None,
        "auc_new": None,
        "error": None,
        "rolled_back": False,
    }

    try:
        # 1. Экспортируем свежий датасет из БД (clean3 - только с market_regime)
        from database.db import export_ml_dataset_clean3
        rows = export_ml_dataset_clean3(str(DATA_PATH))
        result["rows"] = rows

        if rows < MIN_ROWS_TO_TRAIN:
            result["error"] = f"Мало данных: {rows} < {MIN_ROWS_TO_TRAIN}"
            return result

        # 2. Запоминаем старый AUC и модель
        result["auc_old"]        = _get_current_auc()
        result["model_name_old"] = _get_current_meta().get("model_name", "?")

        # 3. Бэкапим текущую модель перед перезаписью
        if MODEL_PATH.exists():
            shutil.copy2(MODEL_PATH, BACKUP_MODEL)
        if META_PATH.exists():
            shutil.copy2(META_PATH, BACKUP_META)

        # 4. Обучаем через ml_train.train() — там авто-выбор модели по размеру датасета
        from ml_train import train as _ml_train_fn
        trained_meta = _ml_train_fn(DATA_PATH)

        result["auc_new"]        = trained_meta.get("cv_auc_mean")
        result["model_name_new"] = trained_meta.get("model_name", "?")

        # 5. Проверяем на регрессию качества
        if (result["auc_old"] is not None
                and result["auc_new"] is not None
                and result["auc_new"] < result["auc_old"] - MAX_AUC_REGRESSION):
            if BACKUP_MODEL.exists():
                shutil.copy2(BACKUP_MODEL, MODEL_PATH)
            if BACKUP_META.exists():
                shutil.copy2(BACKUP_META, META_PATH)
            result["rolled_back"] = True
            result["error"] = (
                f"AUC упал {result['auc_old']:.3f} → {result['auc_new']:.3f} "
                f"(> {MAX_AUC_REGRESSION}), откат"
            )
            return result

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        logger.err("MLAuto", f"Ошибка обучения: {e}")

    return result


def _build_notify_msg(result: dict, new_count: int) -> str:
    """Формирует сообщение в Telegram о результатах переобучения."""
    if result.get("rolled_back"):
        return (
            f"⚠️ <b>ML: переобучение отменено</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📉 AUC упал: {result['auc_old']:.3f} → {result['auc_new']:.3f}\n"
            f"↩️ Модель откатана к предыдущей версии\n"
            f"📦 Строк в датасете: {result['rows']}"
        )
    if not result["success"]:
        return (
            f"❌ <b>ML: ошибка переобучения</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💬 {result.get('error', 'неизвестная ошибка')}"
        )

    auc_old = result.get("auc_old")
    auc_new = result.get("auc_new")
    model_old = result.get("model_name_old", "?")
    model_new = result.get("model_name_new", "?")

    auc_arrow = ""
    if auc_old and auc_new:
        diff = auc_new - auc_old
        auc_arrow = f" ({'+' if diff >= 0 else ''}{diff:.3f})"

    # Строка о смене модели (если изменилась)
    model_line = ""
    if model_old and model_new and model_old != model_new:
        model_line = f"\n🔄 Модель обновлена: <b>{model_old}</b> → <b>{model_new}</b> 🆙"
    else:
        model_line = f"\n🤖 Модель: <b>{model_new}</b>"

    return (
        f"🧠 <b>ML переобучена автоматически</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Строк в датасете: <b>{result['rows']}</b>\n"
        f"📊 ROC-AUC: <b>{auc_new:.3f}</b>{auc_arrow}"
        f"{model_line}\n"
        f"🆕 Новых сделок: <b>{new_count}</b>\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )


async def run_auto_trainer():
    """
    Главный async loop.
    Добавь в main.py: asyncio.gather(..., run_auto_trainer(), ...)
    """
    global _last_trained_count, _last_train_ts

    # Загружаем счётчик с диска (переживает перезапуск бота)
    saved_count, saved_ts = _load_state()
    current_db_count = _get_labeled_count()

    if saved_count > 0 and saved_count <= current_db_count:
        # Есть сохранённое состояние — используем его
        _last_trained_count = saved_count
        _last_train_ts = saved_ts
        logger.ml("MLAuto", f"Запущен. Восстановлено из файла: {_last_trained_count} строк. "
              f"В БД сейчас: {current_db_count}. "
              f"Новых с последнего обучения: {current_db_count - _last_trained_count}.")
    else:
        # Нет файла или данные устарели — стартуем от текущего значения БД
        _last_trained_count = current_db_count
        _save_state()
        logger.ml("MLAuto", f"Запущен. Текущих размеченных строк: {_last_trained_count}. "
              f"Переобучение каждые {RETRAIN_EVERY_N_TRADES} новых сделок.")

    loop = asyncio.get_event_loop()

    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        try:
            current_count = _get_labeled_count()
            new_since_last = current_count - _last_trained_count

            logger.ml("MLAuto", f"Размечено строк: {current_count} "
                  f"(+{new_since_last} с последнего обучения, "
                  f"порог: {RETRAIN_EVERY_N_TRADES})")

            if new_since_last < RETRAIN_EVERY_N_TRADES:
                continue  # ещё не накопилось

            logger.ml("MLAuto", f"🔄 Запускаем переобучение ({current_count} строк)...")

            # Обучаем в отдельном потоке — не блокируем event loop
            result = await loop.run_in_executor(_executor, _train_model)

            if result["success"]:
                # Перезагружаем модель в памяти — без остановки бота
                from ml.model import load_model
                load_model()
                _last_trained_count = current_count
                _last_train_ts = time.time()
                _save_state()
                logger.ml("MLAuto", f"✅ Модель переобучена и перезагружена. "
                      f"AUC: {result.get('auc_old')} → {result.get('auc_new')}")
            elif result.get("rolled_back"):
                # При откате обновляем счётчик чтобы не пытаться снова сразу
                _last_trained_count = current_count
                _save_state()
                logger.ml("MLAuto", f"↩️ Откат: {result.get('error')}")
            else:
                logger.err("MLAuto", f"Ошибка: {result.get('error')}")
                # При ошибке не обновляем счётчик — попробуем при следующей проверке

            # Уведомление в Telegram
            if _notify_cb:
                msg = _build_notify_msg(result, new_since_last)
                try:
                    await _notify_cb(msg)
                except Exception:
                    pass

        except Exception as e:
            logger.err("MLAuto", f"Ошибка цикла: {e}")