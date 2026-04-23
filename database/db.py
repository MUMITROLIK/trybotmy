import sqlite3
import json
from datetime import datetime
from config import DB_PATH

# ── Runtime настройки (переживают рестарт через БД) ──────────────────────────
_settings_cache = {}   # кэш в памяти чтобы не долбить БД каждый скан


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT NOT NULL,
            direction   TEXT NOT NULL,
            entry_price REAL NOT NULL,
            tp1         REAL NOT NULL,
            tp2         REAL NOT NULL,
            sl          REAL NOT NULL,
            strength    INTEGER NOT NULL,
            reasons     TEXT NOT NULL,
            news_title  TEXT,
            status      TEXT DEFAULT 'ACTIVE',
            tp1_hit_at  TEXT,
            tp2_hit_at  TEXT,
            sl_hit_at   TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            telegram_msg_id INTEGER,
            exchange    TEXT DEFAULT 'binance'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS news_cache (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT UNIQUE,
            url        TEXT,
            source     TEXT,
            sentiment  REAL,
            coins      TEXT,
            fetched_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Взятые сделки — то что пользователь нажал "Взять" (или AUTO_TAKE)
    c.execute("""
        CREATE TABLE IF NOT EXISTS taken_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id   INTEGER,
            symbol      TEXT NOT NULL,
            direction   TEXT NOT NULL,
            entry_price REAL NOT NULL,
            tp1         REAL NOT NULL,
            tp2         REAL NOT NULL,
            sl          REAL NOT NULL,
            strength    INTEGER,
            reasons     TEXT,
            status      TEXT DEFAULT 'OPEN',
            pnl_pct     REAL DEFAULT 0,
            close_price REAL,
            taken_at    TEXT DEFAULT (datetime('now')),
            closed_at   TEXT,
            telegram_msg_id INTEGER,
            auto_taken  INTEGER DEFAULT 0,   -- 1 если взята автоматически
            -- исходные уровни при входе (для фронта, чтобы не прыгали точки)
            orig_entry_price REAL,
            orig_tp1         REAL,
            orig_tp2         REAL,
            orig_sl          REAL
        )
    """)

    # Лог ВСЕХ попыток генерации сигналов — для анализа и отладки
    # Сюда пишется каждый символ: прошёл фильтры или нет и почему
    c.execute("""
        CREATE TABLE IF NOT EXISTS signal_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol     TEXT NOT NULL,
            direction  TEXT,
            strength   INTEGER DEFAULT 0,
            passed     INTEGER DEFAULT 0,   -- 1 если сигнал прошёл все фильтры
            reason     TEXT,                -- причина блокировки или "✅ Сгенерирован"
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Runtime-настройки бота
    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # Читаем дефолт AUTO_TAKE из .env (только при первом запуске — INSERT OR IGNORE)
    import os
    auto_take_default = 'true' if os.getenv('AUTO_TAKE_SIGNALS','false').lower() == 'true' else 'false'
    c.execute(f"INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('auto_take', '{auto_take_default}')")
    c.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('collect_ml_data', 'true')")


    # ML фичи — числовые значения всех индикаторов в момент сигнала
    # Каждая строка = один сигнал + все фичи + target (исход после закрытия)
    c.execute("""
        CREATE TABLE IF NOT EXISTS ml_features (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id        INTEGER,   -- ссылка на taken_trades.id (NULL если не взяли)
            signal_id       INTEGER,   -- ссылка на signals.id
            symbol          TEXT NOT NULL,
            direction       TEXT NOT NULL,
            exchange        TEXT DEFAULT 'binance',  -- биржа: binance/bybit/okx
            created_at      TEXT DEFAULT (datetime('now')),

            -- Рыночный контекст
            session         TEXT,      -- asian / european / american
            fear_greed      INTEGER,   -- 0-100
            btc_trend_4h    TEXT,      -- bull / bear / neutral
            btc_trend_1h    TEXT,
            btc_change_4h   REAL,      -- % изменение BTC за 12ч
            funding_rate    REAL,      -- текущий funding rate

            -- Индикаторы (числовые значения на 15m)
            rsi             REAL,      -- RSI значение
            macd_hist       REAL,      -- MACD histogram
            bb_position     REAL,      -- 0.0=нижняя полоса, 1.0=верхняя
            ema20_50_ratio  REAL,      -- EMA20/EMA50 (>1 бычий, <1 медвежий)
            volume_ratio    REAL,      -- объём / средний объём (20 свечей)
            atr_pct         REAL,      -- ATR в % от цены
            cvd_norm        REAL,      -- нормализованный CVD
            oi_change_pct   REAL,      -- изменение Open Interest %
            ob_imbalance    REAL,      -- дисбаланс стакана (-1 до +1)
            btc_correlation REAL,      -- корреляция Пирсона с BTC

            -- Скоры индикаторов (0.0-1.0, результат функций из signal_generator)
            score_rsi       REAL,
            score_macd      REAL,
            score_bb        REAL,
            score_ema       REAL,
            score_volume    REAL,
            score_orderbook REAL,
            score_news      REAL,
            score_mtf       REAL,
            score_oi        REAL,
            score_cvd       REAL,
            score_patterns  REAL,
            score_btc_corr  REAL,

            -- Итоговая сила и метаданные
            strength        INTEGER,
            heavy_confirmed INTEGER,   -- сколько "тяжёлых" индикаторов подтвердили
            entry_type      TEXT,      -- market / limit

            -- Target (заполняется трекером когда сделка закрывается)
            target          TEXT,      -- TP1 / TP2 / SL / SL_AFTER_TP1 / TIMEOUT / EXPIRED / NOT_FILLED / NULL
            pnl_pct         REAL,      -- итоговый P&L %
            duration_min    INTEGER,   -- время в сделке (минуты)
            labeled         INTEGER DEFAULT 0   -- 1 когда target заполнен
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_ml_symbol ON ml_features(symbol)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ml_labeled ON ml_features(labeled)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ml_trade_id ON ml_features(trade_id)")

    # Индексы для быстрых запросов
    c.execute("CREATE INDEX IF NOT EXISTS idx_signal_log_symbol ON signal_log(symbol)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_signal_log_created ON signal_log(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_taken_trades_status ON taken_trades(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")

    conn.commit()
    conn.close()
    print("[DB] Инициализирована (v4 — ML features table)")


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, ddl: str):
    """
    Лёгкая миграция схемы: добавляет колонку если её нет.
    ddl пример: "INTEGER DEFAULT 0"
    """
    try:
        c = conn.cursor()
        c.execute(f"PRAGMA table_info({table})")
        cols = {r[1] for r in c.fetchall()}  # (cid, name, type, notnull, dflt, pk)
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            conn.commit()
            print(f"[DB] Миграция: добавлена колонка {table}.{col}")
    except Exception as e:
        print(f"[DB] ensure_column error for {table}.{col}: {e}")


def migrate_db():
    """
    Миграции для существующих signals.db (чтобы не требовать удалять файл).
    Вызывается при запуске после init_db().
    """
    conn = get_conn()
    # taken_trades: флаг авто-входа и вспомогательные колонки
    _ensure_column(conn, "taken_trades", "auto_taken", "INTEGER DEFAULT 0")
    _ensure_column(conn, "taken_trades", "telegram_msg_id", "INTEGER")
    # исходные уровни при входе (фиксируются один раз и больше не трогаются)
    _ensure_column(conn, "taken_trades", "orig_entry_price", "REAL")
    _ensure_column(conn, "taken_trades", "orig_tp1", "REAL")
    _ensure_column(conn, "taken_trades", "orig_tp2", "REAL")
    _ensure_column(conn, "taken_trades", "orig_sl", "REAL")
    _ensure_column(conn, "signals", "telegram_msg_id", "INTEGER")
    # ── Мультибиржевая поддержка ──────────────────────────────────────────────
    _ensure_column(conn, "signals", "exchange", "TEXT DEFAULT 'binance'")
    _ensure_column(conn, "taken_trades", "exchange", "TEXT DEFAULT 'binance'")
    _ensure_column(conn, "ml_features", "exchange", "TEXT DEFAULT 'binance'")
    
    # score_levels для нового индикатора уровней
    _ensure_column(conn, "ml_features", "score_levels", "REAL")
    
    # atr_pct для ATR-based трейлинг стопа
    _ensure_column(conn, "taken_trades", "atr_pct", "REAL")

    # ── Новые фичи v2 (время + рыночный контекст) ────────────────────────────
    _ensure_column(conn, "ml_features", "hour_of_day",     "INTEGER")
    _ensure_column(conn, "ml_features", "day_of_week",     "INTEGER")
    _ensure_column(conn, "ml_features", "market_regime",   "TEXT")
    _ensure_column(conn, "ml_features", "atr_percentile",  "REAL")
    _ensure_column(conn, "ml_features", "btc_drawdown_pct","REAL")

    # Backfill старых строк разумными дефолтами
    try:
        conn.execute("""
            UPDATE ml_features
            SET
                hour_of_day     = CAST(strftime('%H', created_at) AS INTEGER),
                day_of_week     = CAST(strftime('%w', created_at) AS INTEGER),
                atr_percentile  = 0.5,
                btc_drawdown_pct = 0.0
            WHERE hour_of_day IS NULL
        """)
        conn.commit()
    except Exception as e:
        print(f"[DB] backfill new features error: {e}")

    # ML разметка (backfill):
    # Раньше paper-сигналы, которые просто истекли, размечались как TIMEOUT.
    # TIMEOUT должен относиться только к взятым сделкам (trade_id != NULL).
    try:
        conn.execute("""
            UPDATE ml_features
            SET target='EXPIRED'
            WHERE COALESCE(target,'')='TIMEOUT'
              AND (trade_id IS NULL OR trade_id = 0)
        """)
        conn.commit()
    except Exception as e:
        print(f"[DB] migrate ml_features labels error: {e}")

    # Backfill: если есть старые PENDING без taken_at — ставим now,
    # иначе TTL-отмена лимиток не сможет посчитать возраст.
    try:
        conn.execute("""
            UPDATE taken_trades
            SET taken_at = datetime('now')
            WHERE status='PENDING' AND (taken_at IS NULL OR taken_at = '')
        """)
        conn.commit()
    except Exception as e:
        print(f"[DB] migrate pending taken_at error: {e}")
    conn.close()


# ── Signal Log ────────────────────────────────────────────────────────────────

def log_signal_attempt(symbol: str, direction: str, strength: int,
                       reason: str, passed: bool = False):
    """
    Записывает каждую попытку генерации сигнала.
    passed=True только если сигнал реально сгенерирован.
    """
    try:
        conn = get_conn()
        conn.execute("""
            INSERT INTO signal_log (symbol, direction, strength, passed, reason)
            VALUES (?, ?, ?, ?, ?)
        """, (symbol, direction, strength, int(passed), reason))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] log_signal_attempt error: {e}")


def get_signal_log(hours: int = 24, symbol: str = None) -> list:
    """Возвращает лог попыток за последние N часов."""
    conn = get_conn()
    c = conn.cursor()
    if symbol:
        c.execute("""
            SELECT * FROM signal_log
            WHERE datetime(created_at) > datetime('now', ?)
              AND symbol = ?
            ORDER BY created_at DESC
        """, (f"-{hours} hours", symbol))
    else:
        c.execute("""
            SELECT * FROM signal_log
            WHERE datetime(created_at) > datetime('now', ?)
            ORDER BY created_at DESC
        """, (f"-{hours} hours",))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_filter_stats(hours: int = 24) -> dict:
    """Статистика фильтрации за последние N часов."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT reason, COUNT(*) as cnt
        FROM signal_log
        WHERE datetime(created_at) > datetime('now', ?)
          AND passed = 0
        GROUP BY reason
        ORDER BY cnt DESC
        LIMIT 15
    """, (f"-{hours} hours",))
    blocks = [{"reason": r[0], "count": r[1]} for r in c.fetchall()]

    c.execute("""
        SELECT COUNT(*) as total,
               SUM(passed) as passed
        FROM signal_log
        WHERE datetime(created_at) > datetime('now', ?)
    """, (f"-{hours} hours",))
    row = c.fetchone()
    conn.close()
    total  = row[0] or 0
    passed = row[1] or 0
    return {
        "total":      total,
        "passed":     passed,
        "blocked":    total - passed,
        "pass_rate":  round(passed / total * 100, 1) if total > 0 else 0,
        "top_blocks": blocks,
    }


# ── Signals ───────────────────────────────────────────────────────────────────

def save_signal(signal: dict) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO signals
            (symbol, direction, entry_price, tp1, tp2, sl, strength,
             reasons, news_title, telegram_msg_id, exchange)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal["symbol"], signal["direction"], signal["entry_price"],
        signal["tp1"], signal["tp2"], signal["sl"], signal["strength"],
        json.dumps(signal.get("reasons", []), ensure_ascii=False),
        signal.get("news_title"), signal.get("telegram_msg_id"),
        signal.get("exchange", "binance"),
    ))
    sig_id = c.lastrowid
    conn.commit()
    conn.close()
    return sig_id


def get_active_signals() -> list:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM signals WHERE status = 'ACTIVE'")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["reasons"] = json.loads(r["reasons"])
    return rows


def close_signal(signal_id: int, status: str):
    col_map = {"TP1": "tp1_hit_at", "TP2": "tp2_hit_at", "SL": "sl_hit_at"}
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    c = conn.cursor()
    if status in col_map:
        c.execute(f"UPDATE signals SET status=?, {col_map[status]}=? WHERE id=?",
                  (status, now, signal_id))
    else:
        c.execute("UPDATE signals SET status=? WHERE id=?", (status, signal_id))
    conn.commit()
    conn.close()


def update_telegram_msg_id(signal_id: int, msg_id: int):
    conn = get_conn()
    conn.execute("UPDATE signals SET telegram_msg_id=? WHERE id=?", (msg_id, signal_id))
    conn.commit()
    conn.close()


def signal_exists_recent(symbol: str, direction: str, minutes: int = 30) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT 1 FROM signals
        WHERE symbol=? AND direction=? AND status='ACTIVE'
          AND datetime(created_at) > datetime('now', ?)
        LIMIT 1
    """, (symbol, direction, f"-{minutes} minutes"))
    found = c.fetchone() is not None
    conn.close()
    return found


def get_signal_by_msg_id(msg_id: int) -> dict | None:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM signals WHERE telegram_msg_id=? LIMIT 1", (msg_id,))
    row = c.fetchone()
    conn.close()
    if row:
        d = dict(row)
        d["reasons"] = json.loads(d["reasons"])
        return d
    return None


def clear_signal_history():
    conn = get_conn()
    conn.execute("DELETE FROM signals WHERE status != 'ACTIVE'")
    conn.execute("DELETE FROM signals WHERE status = 'ACTIVE'")
    conn.commit()
    conn.close()


# ── Taken Trades ──────────────────────────────────────────────────────────────

def save_taken_trade(signal: dict, auto_taken: bool = False,
                     status: str = "OPEN",
                     taken_at: str | None = None) -> int:
    """
    Сохраняет взятую сделку.
    auto_taken=True если взята автоматически (AUTO_TAKE_SIGNALS=True).
    """
    # Важно: если передать taken_at=None, SQLite дефолт НЕ сработает (вставится NULL).
    # Для PENDING лимиток нам нужен taken_at как время создания заявки, иначе TTL-отмена не сработает.
    if taken_at is None:
        taken_at = datetime.utcnow().isoformat()

    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO taken_trades
            (signal_id, symbol, direction, exchange,
             entry_price, tp1, tp2, sl, atr_pct,
             strength, reasons, status, taken_at, telegram_msg_id, auto_taken,
             orig_entry_price, orig_tp1, orig_tp2, orig_sl)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal.get("id"), signal["symbol"], signal["direction"],
        signal.get("exchange", "binance"),  # биржа (binance/bybit/okx)
        signal["entry_price"], signal["tp1"], signal["tp2"], signal["sl"],
        signal.get("atr_pct"),  # ATR% для трейлинг стопа
        signal.get("strength", 0),
        json.dumps(signal.get("reasons", []), ensure_ascii=False),
        status,
        taken_at,
        signal.get("telegram_msg_id"),
        int(auto_taken),
        # исходные уровни — фиксируем один раз при входе
        signal["entry_price"], signal["tp1"], signal["tp2"], signal["sl"],
    ))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def get_open_trades(include_pending: bool = True) -> list:
    conn = get_conn()
    c = conn.cursor()
    if include_pending:
        c.execute("SELECT * FROM taken_trades WHERE status IN ('PENDING', 'OPEN', 'TP1_HIT')")
    else:
        c.execute("SELECT * FROM taken_trades WHERE status IN ('OPEN', 'TP1_HIT')")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        if r.get("reasons"):
            try:    r["reasons"] = json.loads(r["reasons"])
            except: r["reasons"] = []
    return rows


def activate_pending_trade(trade_id: int, taken_at: str | None = None):
    """
    Переводит PENDING лимитку в OPEN (исполнено).
    taken_at если не задан — ставим текущее время SQLite.
    """
    conn = get_conn()
    if taken_at:
        conn.execute("UPDATE taken_trades SET status='OPEN', taken_at=? WHERE id=?", (taken_at, trade_id))
    else:
        conn.execute("UPDATE taken_trades SET status='OPEN', taken_at=datetime('now') WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()


def hit_tp1_trade(trade_id: int, new_sl: float, pnl_tp1: float):
    conn = get_conn()
    conn.execute("""
        UPDATE taken_trades
        SET status='TP1_HIT', pnl_pct=?, sl=?
        WHERE id=?
    """, (round(pnl_tp1, 2), new_sl, trade_id))
    conn.commit()
    conn.close()


def update_trade_entry(trade_id: int, new_entry: float):
    conn = get_conn()
    conn.execute("UPDATE taken_trades SET entry_price=? WHERE id=?", (new_entry, trade_id))
    conn.commit()
    conn.close()


def update_trade_sl(trade_id: int, new_sl: float):
    conn = get_conn()
    conn.execute("UPDATE taken_trades SET sl=? WHERE id=?", (new_sl, trade_id))
    conn.commit()
    conn.close()


def update_trade_levels(trade_id: int, new_entry: float,
                        new_tp1: float, new_tp2: float, new_sl: float):
    """
    Пересчитывает все уровни сделки при market fallback.
    Вызывается когда лимитка не исполнилась и входим по рынку.
    """
    conn = get_conn()
    conn.execute("""
        UPDATE taken_trades
        SET entry_price=?, tp1=?, tp2=?, sl=?, status='OPEN'
        WHERE id=?
    """, (new_entry, new_tp1, new_tp2, new_sl, trade_id))
    conn.commit()
    conn.close()


def symbol_in_cooldown(symbol: str, minutes: int = 60) -> bool:
    """
    True если по символу был стоп (SL) в последние N минут.
    Защита от повторного входа сразу после потери.
    """
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT 1 FROM taken_trades
        WHERE symbol=?
          AND status = 'SL'
          AND datetime(COALESCE(closed_at, taken_at)) > datetime('now', ?)
        LIMIT 1
    """, (symbol, f"-{minutes} minutes"))
    found = c.fetchone() is not None
    conn.close()
    return found


def close_trade(trade_id: int, status: str, close_price: float, pnl_pct: float):
    conn = get_conn()
    conn.execute("""
        UPDATE taken_trades
        SET status=?, close_price=?, pnl_pct=?, closed_at=datetime('now')
        WHERE id=?
    """, (status, close_price, pnl_pct, trade_id))
    conn.commit()
    conn.close()


def get_all_trades() -> list:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM taken_trades ORDER BY taken_at DESC LIMIT 100")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def trade_exists(symbol: str, direction: str) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT 1 FROM taken_trades
        WHERE symbol=? AND direction=? AND status IN ('OPEN', 'TP1_HIT') LIMIT 1
    """, (symbol, direction))
    found = c.fetchone() is not None
    conn.close()
    return found


def trade_exists_symbol(symbol: str) -> bool:
    """
    True если есть ЛЮБАЯ активная запись по символу (LONG или SHORT):
    - PENDING (лимитка ждёт исполнения)
    - OPEN / TP1_HIT (активная позиция)
    Нужно, чтобы бот не открывал хедж (противоположное направление) автоматически/вручную.
    """
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT 1 FROM taken_trades
        WHERE symbol=? AND status IN ('PENDING', 'OPEN', 'TP1_HIT') LIMIT 1
    """, (symbol,))
    found = c.fetchone() is not None
    conn.close()
    return found


def cancel_pending_trade(trade_id: int, reason: str = "LIMIT_TIMEOUT"):
    """
    Отменяет зависшую лимитку (PENDING), ставит закрытие.
    """
    conn = get_conn()
    conn.execute("""
        UPDATE taken_trades
        SET status=?, closed_at=datetime('now')
        WHERE id=? AND status='PENDING'
    """, (reason, trade_id))
    conn.commit()
    conn.close()


# ── News ──────────────────────────────────────────────────────────────────────

def save_news(title, url, source, sentiment, coins):
    conn = get_conn()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO news_cache (title, url, source, sentiment, coins)
            VALUES (?, ?, ?, ?, ?)
        """, (title, url, source, sentiment, json.dumps(coins)))
        conn.commit()
    except Exception:
        pass
    conn.close()


def get_recent_news(coin_base=None, minutes=60) -> list:
    conn = get_conn()
    c = conn.cursor()
    if coin_base:
        c.execute("""
            SELECT * FROM news_cache
            WHERE datetime(fetched_at) > datetime('now', ?)
              AND coins LIKE ?
            ORDER BY fetched_at DESC
        """, (f"-{minutes} minutes", f"%{coin_base}%"))
    else:
        c.execute("""
            SELECT * FROM news_cache
            WHERE datetime(fetched_at) > datetime('now', ?)
            ORDER BY fetched_at DESC
        """, (f"-{minutes} minutes",))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


# ── Bot Settings (runtime toggle) ─────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    """Читает настройку из БД (с кэшем в памяти)."""
    if key in _settings_cache:
        return _settings_cache[key]
    try:
        conn = get_conn()
        c    = conn.cursor()
        c.execute("SELECT value FROM bot_settings WHERE key=?", (key,))
        row  = c.fetchone()
        conn.close()
        val  = row[0] if row else default
        _settings_cache[key] = val
        return val
    except Exception:
        return default


def set_setting(key: str, value: str):
    """Сохраняет настройку в БД и обновляет кэш."""
    _settings_cache[key] = value
    try:
        conn = get_conn()
        conn.execute("""
            INSERT INTO bot_settings (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                           updated_at=excluded.updated_at
        """, (key, value))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] set_setting error: {e}")


def is_auto_take() -> bool:
    """True если авто-вход включён."""
    return get_setting("auto_take", "false").lower() == "true"


def toggle_auto_take() -> bool:
    """Переключает авто-вход. Возвращает новое состояние."""
    current = is_auto_take()
    new_val = "false" if current else "true"
    set_setting("auto_take", new_val)
    return not current


def is_collect_ml_data() -> bool:
    """True если сбор ML-фич включён (для paper/auto/manual)."""
    return get_setting("collect_ml_data", "true").lower() == "true"


def toggle_collect_ml_data() -> bool:
    """Переключает сбор ML-фич. Возвращает новое состояние."""
    current = is_collect_ml_data()
    new_val = "false" if current else "true"
    set_setting("collect_ml_data", new_val)
    return not current


def get_ml_stats() -> dict:
    """
    Статистика для ML датасета:
    - Количество сигналов с результатами
    - Распределение по направлениям и исходам
    """
    conn = get_conn()
    c    = conn.cursor()

    # Сигналы с финальным статусом (не ACTIVE/OPEN)
    c.execute("""
        SELECT direction, status, strength,
               COUNT(*) as cnt,
               AVG(pnl_pct) as avg_pnl
        FROM taken_trades
        WHERE status NOT IN ('OPEN', 'TP1_HIT')
        GROUP BY direction, status
        ORDER BY direction, status
    """)
    rows = c.fetchall()

    # Общее количество записей в signal_log (для ML)
    c.execute("SELECT COUNT(*), SUM(passed) FROM signal_log")
    log_row = c.fetchone()

    conn.close()

    breakdown = [{"direction": r[0], "status": r[1],
                  "strength": round(r[2] or 0),
                  "count": r[3], "avg_pnl": round(r[4] or 0, 2)}
                 for r in rows]

    total_log   = log_row[0] or 0
    passed_log  = log_row[1] or 0
    total_trades = sum(r["count"] for r in breakdown)

    wins   = sum(r["count"] for r in breakdown
                 if r["status"] in ("TP1","TP2","SL_AFTER_TP1"))
    losses = sum(r["count"] for r in breakdown if r["status"] == "SL")

    return {
        "total_labeled":   total_trades,        # сделок с исходом
        "wins":            wins,
        "losses":          losses,
        "winrate":         round(wins / total_trades * 100, 1) if total_trades else 0,
        "signal_log_rows": total_log,           # строк в лог-таблице (все попытки)
        "signals_passed":  passed_log,          # из них прошли фильтры
        "breakdown":       breakdown,
    }


# ── ML Features ───────────────────────────────────────────────────────────────

def save_ml_features(signal: dict, trade_id: int = None) -> int:
    """
    Версия 2 — сохраняет все старые + новые фичи.
    """
    try:
        scores = signal.get("scores", {})
        conn = get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT INTO ml_features (
                trade_id, signal_id, symbol, direction, exchange,
                session, fear_greed, btc_trend_4h, btc_trend_1h,
                btc_change_4h, funding_rate,
                rsi, macd_hist, bb_position, ema20_50_ratio,
                volume_ratio, atr_pct, cvd_norm, oi_change_pct,
                ob_imbalance, btc_correlation,
                score_rsi, score_macd, score_bb, score_ema, score_volume,
                score_orderbook, score_news, score_mtf, score_oi,
                score_cvd, score_patterns, score_btc_corr, score_levels,
                strength, heavy_confirmed, entry_type,
                hour_of_day, day_of_week, market_regime,
                atr_percentile, btc_drawdown_pct
            ) VALUES (
                ?,?,?,?, ?,  ?,?,?,?,  ?,?,
                ?,?,?,?,  ?,?,?,?,  ?,?,
                ?,?,?,?,?, ?,?,?,?,  ?,?,?,
                ?,?,?,?,
                ?,?,?,?,?
            )
        """, (
            trade_id,
            signal.get("signal_id") or signal.get("id"),
            signal["symbol"],
            signal["direction"],
            signal.get("exchange", "binance"),  # ✅ Фикс: сохраняем биржу

            signal.get("session"),
            signal.get("fear_greed"),
            signal.get("btc_trend"),
            signal.get("btc_trend_1h"),
            signal.get("btc_change_4h"),
            signal.get("funding_rate"),

            signal.get("feat_rsi"),
            signal.get("feat_macd_hist"),
            signal.get("feat_bb_position"),
            signal.get("feat_ema_ratio"),
            signal.get("feat_volume_ratio"),
            signal.get("feat_atr_pct"),
            signal.get("feat_cvd_norm"),
            signal.get("oi_change_pct"),
            signal.get("feat_ob_imbalance"),
            signal.get("feat_btc_corr"),

            scores.get("rsi"),
            scores.get("macd"),
            scores.get("bb"),
            scores.get("ema"),
            scores.get("volume"),
            scores.get("orderbook"),
            scores.get("news"),
            scores.get("mtf"),
            scores.get("oi"),
            scores.get("cvd"),
            scores.get("patterns"),
            scores.get("btc_corr"),
            scores.get("levels"),  # ← новый индикатор

            signal.get("strength"),
            signal.get("heavy_confirmed"),
            signal.get("entry_type"),

            # ── Новые фичи v2 ─────────────────────────────
            signal.get("hour_of_day"),
            signal.get("day_of_week"),
            signal.get("market_regime"),
            signal.get("atr_percentile"),
            signal.get("btc_drawdown_pct"),
        ))
        ml_id = c.lastrowid
        conn.commit()
        conn.close()
        return ml_id
    except Exception as e:
        print(f"[DB] save_ml_features error: {e}")
        return 0


def update_ml_target(trade_id: int, target: str, pnl_pct: float,
                     duration_min: int):
    """
    Вызывается трекером когда сделка закрывается.
    Заполняет target, pnl_pct, duration_min — делает строку размеченной.
    """
    try:
        conn = get_conn()
        conn.execute("""
            UPDATE ml_features
            SET target=?, pnl_pct=?, duration_min=?, labeled=1
            WHERE trade_id=?
        """, (target, round(pnl_pct, 4), duration_min, trade_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] update_ml_target error: {e}")


def update_ml_target_by_signal(signal_id: int, target: str, pnl_pct: float,
                              duration_min: int):
    """
    Разметка paper-сигналов: вызывается трекером сигналов (signals),
    когда не было реального входа (taken_trades отсутствует).
    """
    try:
        conn = get_conn()
        conn.execute("""
            UPDATE ml_features
            SET target=?, pnl_pct=?, duration_min=?, labeled=1
            WHERE signal_id=?
              AND (trade_id IS NULL OR trade_id = 0)
        """, (target, round(pnl_pct, 4), duration_min, signal_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] update_ml_target_by_signal error: {e}")


def export_ml_dataset(path: str = "docs/ml_dataset.json") -> int:
    """
    Экспортирует размеченные строки в JSON для обучения ML.
    Возвращает количество строк.
    """
    import json, os
    conn = get_conn()
    c    = conn.cursor()
    c.execute("""
        SELECT * FROM ml_features
        WHERE labeled = 1
        ORDER BY created_at DESC
    """)
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    print(f"[ML] Экспортировано {len(rows)} строк → {path}")
    return len(rows)


def export_ml_dataset_clean(path: str = "docs/ml_dataset_clean.json") -> int:
    """
    Экспортирует "чистый" датасет для обучения:
    - только размеченные строки
    - исключает EXPIRED (сигнал истёк без TP/SL)
    """
    import json, os
    conn = get_conn()
    c    = conn.cursor()
    c.execute("""
        SELECT * FROM ml_features
        WHERE labeled = 1
          AND COALESCE(target,'') != 'EXPIRED'
        ORDER BY created_at DESC
    """)
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    print(f"[ML] Экспортировано {len(rows)} строк (clean) → {path}")
    return len(rows)


def export_ml_dataset_clean2(path: str = "docs/ml_dataset_clean2.json") -> int:
    """
    Экспортирует "ещё чище" датасет для обучения:
    - только размеченные строки
    - исключает EXPIRED (движения не было) и NOT_FILLED (лимитка не исполнилась)
    """
    import json, os
    conn = get_conn()
    c    = conn.cursor()
    c.execute("""
        SELECT * FROM ml_features
        WHERE labeled = 1
          AND COALESCE(target,'') NOT IN ('EXPIRED','NOT_FILLED')
        ORDER BY created_at DESC
    """)
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    print(f"[ML] Экспортировано {len(rows)} строк (clean2) → {path}")
    return len(rows)


def export_ml_dataset_clean3(path: str = "docs/ml_dataset_clean3.json") -> int:
    """
    Самый чистый датасет для обучения - только с market_regime.
    
    Фильтры:
      1. labeled = 1 (размечено)
      2. market_regime IS NOT NULL (только новые данные после фикса)
      3. target NOT IN ('EXPIRED','NOT_FILLED') (только реальные исходы)
      4. Все ключевые фичи заполнены
    
    Returns:
        Количество экспортированных строк
    """
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT * FROM ml_features
        WHERE labeled = 1
          AND market_regime IS NOT NULL
          AND COALESCE(target,'') NOT IN ('EXPIRED','NOT_FILLED')
          AND target IN ('TP1','TP2','SL','SL_AFTER_TP1','TIMEOUT','MANUAL')
          AND rsi IS NOT NULL
          AND volume_ratio IS NOT NULL
          AND atr_pct IS NOT NULL
        ORDER BY created_at DESC
    """)
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()
    
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    
    print(f"[ML] Экспортировано {len(rows)} строк (clean3 - только с market_regime) → {path}")
    
    # Статистика
    if rows:
        wins = sum(1 for r in rows if r.get("target") in ("TP1","TP2","SL_AFTER_TP1"))
        losses = sum(1 for r in rows if r.get("target") == "SL")
        total = wins + losses
        wr = round(wins / total * 100, 1) if total > 0 else 0
        
        regimes = {}
        for r in rows:
            regime = r.get("market_regime") or "unknown"
            regimes[regime] = regimes.get(regime, 0) + 1
        
        print(f"  WIN: {wins}, LOSS: {losses}, Winrate: {wr}%")
        print(f"  Режимы: {regimes}")
    
    return len(rows)


def get_ml_dataset_stats() -> dict:
    """Подробная статистика датасета для /mlstats."""
    conn = get_conn()
    c    = conn.cursor()

    c.execute("""
        SELECT direction, target, COUNT(*) as cnt,
               AVG(pnl_pct) as avg_pnl,
               AVG(strength) as avg_strength,
               AVG(duration_min) as avg_dur
        FROM ml_features
        WHERE labeled = 1
        GROUP BY direction, target
        ORDER BY direction, target
    """)
    breakdown = [{
        "direction": r[0], "target": r[1], "count": r[2],
        "avg_pnl": round(r[3] or 0, 2),
        "avg_strength": round(r[4] or 0, 1),
        "avg_dur_min": round(r[5] or 0, 0),
    } for r in c.fetchall()]

    c.execute("SELECT COUNT(*), SUM(labeled) FROM ml_features")
    tot_row = c.fetchone()

    c.execute("""
        SELECT AVG(rsi), AVG(volume_ratio), AVG(fear_greed),
               AVG(score_mtf), AVG(score_cvd)
        FROM ml_features WHERE labeled=1
    """)
    avg_row = c.fetchone()

    conn.close()

    total   = tot_row[0] or 0
    labeled = tot_row[1] or 0
    expired = sum(r["count"] for r in breakdown if r["target"] == "EXPIRED")
    not_filled = sum(r["count"] for r in breakdown if r["target"] == "NOT_FILLED")
    wins    = sum(r["count"] for r in breakdown
                 if r["target"] in ("TP1","TP2","SL_AFTER_TP1"))
    losses  = sum(r["count"] for r in breakdown if r["target"] == "SL")
    labeled_no_expired = max(0, labeled - expired)
    labeled_clean2 = max(0, labeled - expired - not_filled)

    # 24h rates (quality control)
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT
              SUM(CASE WHEN labeled=1 THEN 1 ELSE 0 END) as labeled_24h,
              SUM(CASE WHEN labeled=1 AND target='EXPIRED' THEN 1 ELSE 0 END) as expired_24h,
              SUM(CASE WHEN labeled=1 AND target='NOT_FILLED' THEN 1 ELSE 0 END) as not_filled_24h
            FROM ml_features
            WHERE datetime(created_at) >= datetime('now','-24 hours')
        """)
        r = c.fetchone()
        conn.close()
        labeled_24h = int(r[0] or 0)
        expired_24h = int(r[1] or 0)
        not_filled_24h = int(r[2] or 0)
    except Exception:
        labeled_24h = expired_24h = not_filled_24h = 0

    return {
        "total":         total,
        "labeled":       labeled,
        "unlabeled":     total - labeled,
        "expired":       expired,
        "not_filled":    not_filled,
        "labeled_clean": labeled_no_expired,
        "labeled_clean2": labeled_clean2,
        "wins":          wins,
        "losses":        losses,
        "winrate":       round(wins / labeled * 100, 1) if labeled else 0,
        "winrate_clean": round(wins / labeled_no_expired * 100, 1) if labeled_no_expired else 0,
        "winrate_clean2": round(wins / labeled_clean2 * 100, 1) if labeled_clean2 else 0,
        "labeled_24h":   labeled_24h,
        "expired_24h":   expired_24h,
        "not_filled_24h": not_filled_24h,
        "expired_rate_24h": round(expired_24h / labeled_24h * 100, 1) if labeled_24h else 0,
        "not_filled_rate_24h": round(not_filled_24h / labeled_24h * 100, 1) if labeled_24h else 0,
        "breakdown":     breakdown,
        "avg_rsi":       round(avg_row[0] or 0, 1) if avg_row else 0,
        "avg_vol_ratio": round(avg_row[1] or 0, 2) if avg_row else 0,
        "avg_fg":        round(avg_row[2] or 0, 1) if avg_row else 0,
        "avg_mtf_score": round(avg_row[3] or 0, 3) if avg_row else 0,
        "avg_cvd_score": round(avg_row[4] or 0, 3) if avg_row else 0,
        "ready_for_ml":  labeled >= 200,
    }


def get_ml_quality_report(hours: int = 24, goal_clean2: int = 200) -> dict:
    """
    Короткий отчёт качества за последние N часов:
    - доля мусора (EXPIRED / NOT_FILLED)
    - сколько clean2 строк за период
    - high-confidence subset и его winrate
    """
    conn = get_conn()
    c = conn.cursor()

    # labelled rows in last N hours
    c.execute("""
        SELECT
          COUNT(*) as labeled,
          SUM(CASE WHEN target='EXPIRED' THEN 1 ELSE 0 END) as expired,
          SUM(CASE WHEN target='NOT_FILLED' THEN 1 ELSE 0 END) as not_filled,
          SUM(CASE WHEN target IN ('TP1','TP2','SL_AFTER_TP1') THEN 1 ELSE 0 END) as wins,
          SUM(CASE WHEN target='SL' THEN 1 ELSE 0 END) as losses
        FROM ml_features
        WHERE labeled=1
          AND datetime(created_at) >= datetime('now', ?)
    """, (f"-{hours} hours",))
    r = c.fetchone()
    labeled = int(r[0] or 0)
    expired = int(r[1] or 0)
    not_filled = int(r[2] or 0)
    wins = int(r[3] or 0)
    losses = int(r[4] or 0)

    clean2 = max(0, labeled - expired - not_filled)
    winrate_clean2 = round(wins / clean2 * 100, 1) if clean2 else 0
    expired_rate = round(expired / labeled * 100, 1) if labeled else 0
    not_filled_rate = round(not_filled / labeled * 100, 1) if labeled else 0

    # high-confidence (practical): show a subset you can реально использовать
    c.execute("""
        SELECT
          COUNT(*) as n,
          SUM(CASE WHEN target IN ('TP1','TP2','SL_AFTER_TP1') THEN 1 ELSE 0 END) as w
        FROM ml_features
        WHERE labeled=1
          AND datetime(created_at) >= datetime('now', ?)
          AND COALESCE(target,'') NOT IN ('EXPIRED','NOT_FILLED')
          AND COALESCE(strength,0) >= 80
          AND COALESCE(heavy_confirmed,0) >= 2
          AND COALESCE(score_mtf,0) >= 0.45
    """, (f"-{hours} hours",))
    hr = c.fetchone()
    hi_n = int(hr[0] or 0)
    hi_w = int(hr[1] or 0)
    hi_wr = round(hi_w / hi_n * 100, 1) if hi_n else 0

    # mid-confidence: чуть мягче, чтобы видеть “рабочий” набор
    c.execute("""
        SELECT
          COUNT(*) as n,
          SUM(CASE WHEN target IN ('TP1','TP2','SL_AFTER_TP1') THEN 1 ELSE 0 END) as w
        FROM ml_features
        WHERE labeled=1
          AND datetime(created_at) >= datetime('now', ?)
          AND COALESCE(target,'') NOT IN ('EXPIRED','NOT_FILLED')
          AND COALESCE(strength,0) >= 70
          AND COALESCE(heavy_confirmed,0) >= 1
          AND COALESCE(score_mtf,0) >= 0.35
    """, (f"-{hours} hours",))
    mr = c.fetchone()
    mid_n = int(mr[0] or 0)
    mid_w = int(mr[1] or 0)
    mid_wr = round(mid_w / mid_n * 100, 1) if mid_n else 0

    # suspiciously fast outcomes (duration too small)
    # This often indicates either too-tight targets or timestamp issues.
    c.execute("""
        SELECT
          COUNT(*) as n,
          SUM(CASE WHEN target='TP1' THEN 1 ELSE 0 END) as tp1,
          SUM(CASE WHEN target='TP2' THEN 1 ELSE 0 END) as tp2,
          SUM(CASE WHEN target='SL' THEN 1 ELSE 0 END) as sl
        FROM ml_features
        WHERE labeled=1
          AND datetime(created_at) >= datetime('now', ?)
          AND COALESCE(target,'') NOT IN ('EXPIRED','NOT_FILLED')
          AND COALESCE(duration_min, 999999) <= 2
    """, (f"-{hours} hours",))
    fr = c.fetchone()
    fast_n = int(fr[0] or 0)
    fast_tp1 = int(fr[1] or 0)
    fast_tp2 = int(fr[2] or 0)
    fast_sl = int(fr[3] or 0)
    fast_rate = round(fast_n / clean2 * 100, 1) if clean2 else 0

    # overall progress
    c.execute("""
        SELECT
          SUM(labeled) as labeled_total,
          SUM(CASE WHEN labeled=1 AND target='EXPIRED' THEN 1 ELSE 0 END) as expired_total,
          SUM(CASE WHEN labeled=1 AND target='NOT_FILLED' THEN 1 ELSE 0 END) as not_filled_total
        FROM ml_features
    """)
    tr = c.fetchone()
    labeled_total = int(tr[0] or 0)
    expired_total = int(tr[1] or 0)
    not_filled_total = int(tr[2] or 0)
    clean2_total = max(0, labeled_total - expired_total - not_filled_total)
    left = max(0, int(goal_clean2) - clean2_total)

    conn.close()
    return {
        "hours": hours,
        "goal_clean2": int(goal_clean2),
        "clean2_total": clean2_total,
        "left_to_goal": left,
        "labeled": labeled,
        "clean2": clean2,
        "expired_rate": expired_rate,
        "not_filled_rate": not_filled_rate,
        "winrate_clean2": winrate_clean2,
        "high_conf_n": hi_n,
        "high_conf_wr": hi_wr,
        "mid_conf_n": mid_n,
        "mid_conf_wr": mid_wr,
        "fast_n": fast_n,
        "fast_rate": fast_rate,
        "fast_tp1": fast_tp1,
        "fast_tp2": fast_tp2,
        "fast_sl": fast_sl,
    }