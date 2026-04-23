import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY", "")

NETLIFY_TOKEN   = os.getenv("NETLIFY_TOKEN", "")
NETLIFY_SITE_ID = os.getenv("NETLIFY_SITE_ID", "")

# ── Локальный UI/API сервер ───────────────────────────────────────────────────
# Куда бот POST'ит обновления (для WS broadcast + записи data.json на сервере).
# Если запускаете uvicorn на другом порту (например 8001) — выставьте в .env:
# WEB_SERVER_URL=http://localhost:8001
WEB_SERVER_URL = os.getenv("WEB_SERVER_URL", "http://localhost:8000")

# ── Параметры сигналов ────────────────────────────────────────────────────────
MIN_SIGNAL_STRENGTH = int(os.getenv("MIN_SIGNAL_STRENGTH", "80"))
SCAN_INTERVAL       = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
TOP_FUTURES_COUNT   = int(os.getenv("TOP_FUTURES_COUNT", "100"))

# Сколько живёт сигнал для paper-трекинга/ML (минуты).
# 10 минут дают много TIMEOUT и мусорную разметку; для TP 2-4% обычно лучше 1-4 часа.
SIGNAL_TTL_MINUTES  = int(os.getenv("SIGNAL_TTL_MINUTES", "240"))

# Минимальный объём торгов за 24ч в USDT для включения в скан
# 50M отсекает мусорные монеты с тонким стаканом
# При топ-100: монеты 50-100 обычно имеют 50M-500M объёма — это норм
MIN_VOLUME_USDT = float(os.getenv("MIN_VOLUME_USDT", "50_000_000"))  # 50M USDT

# Для Bybit можно задать более мягкий порог (там объёмы обычно ниже).
# По умолчанию = MIN_VOLUME_USDT, но можно переопределить BYBIT_MIN_VOLUME_USDT в .env.
BYBIT_MIN_VOLUME_USDT = float(os.getenv("BYBIT_MIN_VOLUME_USDT", str(MIN_VOLUME_USDT)))

# ── Лимиты на позиции ────────────────────────────────────────────────────────
# Максимум одновременно открытых позиций.
# 0 = без лимита (режим сбора ML данных, высокий риск!)
# 10-20 = безопасный режим
# 30-50 = режим сбора ML (больше данных, но выше риск)
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "0"))

# Максимум сигналов одного направления за один скан
# 0 = без лимита (режим сбора ML данных)
# 2-5 = обычный режим (защита от коррелированных сигналов)
MAX_LONG_PER_SCAN  = int(os.getenv("MAX_LONG_PER_SCAN",  "0"))
MAX_SHORT_PER_SCAN = int(os.getenv("MAX_SHORT_PER_SCAN", "0"))

# Автоматически брать все сигналы в taken_trades без нажатия кнопки
# True  = все сигналы автоматически трекаются и считаются реальными сделками
# False = пользователь сам нажимает "Взять" в Telegram
AUTO_TAKE_SIGNALS = os.getenv("AUTO_TAKE_SIGNALS", "false").lower() == "true"

# ── Индикаторы ────────────────────────────────────────────────────────────────
TIMEFRAMES        = ["5m", "15m", "1h", "4h"]
RSI_PERIOD        = 14
RSI_OVERSOLD      = 35
RSI_OVERBOUGHT    = 65
VOLUME_SPIKE_MULT = 2.5   # было 2.0 — повышено
BB_PERIOD         = 20
ORDERBOOK_DEPTH   = 20

# ── TP / SL (базовые, перекрываются ATR-адаптацией) ──────────────────────────
TP1_PERCENT = 2.0
TP2_PERCENT = 4.0
SL_PERCENT  = 2.0

# ── Entry candle filter ──────────────────────────────────────────────────────
# Не входим, если последняя свеча слишком "импульсная" (body > ATR * mult).
# Это снижает долю сделок, которые закрываются за 0-2 минуты (шум/спайки).
ENTRY_CANDLE_MAX_ATR_MULT = float(os.getenv("ENTRY_CANDLE_MAX_ATR_MULT", "0.9"))

# ── Anti-EXPIRED фильтры для ML качества ─────────────────────────────────────
# Идея: если ATR слишком маленький, TP1 может быть "слишком далеко" и сигнал
# часто истекает без достижения TP/SL → мусор для ML.
# Эти пороги мягко снижают долю EXPIRED, не меняя основную логику индикаторов.
MIN_ATR_PCT_FOR_SIGNAL   = float(os.getenv("MIN_ATR_PCT_FOR_SIGNAL", "0.35"))   # ATR% минимум
MAX_TP1_ATR_MULT_FOR_SIG = float(os.getenv("MAX_TP1_ATR_MULT_FOR_SIG", "3.50")) # TP1% <= ATR% * mult

# ── Limit orders ─────────────────────────────────────────────────────────────
# Сколько минут ждать исполнения лимитного входа (PENDING).
# Если не исполнилось — снимаем лимитку, чтобы не копить дубли/мусор.
LIMIT_ORDER_TTL_MINUTES = int(os.getenv("LIMIT_ORDER_TTL_MINUTES", "60"))

# ── Новости ───────────────────────────────────────────────────────────────────
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://cryptonews.com/news/feed/",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/feed",
    "https://theblock.co/rss.xml",
    "https://www.newsbtc.com/feed/",
    "https://ambcrypto.com/feed/",
    "https://cryptopotato.com/feed/",
    "https://coinjournal.net/feed/",
]
NEWS_FETCH_INTERVAL = 300

# ── База данных ───────────────────────────────────────────────────────────────
DB_PATH = "signals.db"

# ── Bybit интеграция ──────────────────────────────────────────────────────────
# Сколько уникальных монет сканировать на Bybit (которых нет на Binance).
# 0 = Bybit отключён
BYBIT_TOP_COUNT = int(os.getenv("BYBIT_TOP_COUNT", "100"))

# ── OKX интеграция ────────────────────────────────────────────────────────────
# Сколько уникальных монет сканировать на OKX (которых нет на Binance+Bybit).
# 0 = OKX отключён
OKX_TOP_COUNT = int(os.getenv("OKX_TOP_COUNT", "0"))

# Пауза между батчами OKX (OKX лимит: 20 req/sec на публичный endpoint).
# Больше пауза = меньше риск 429 ошибок, но скан медленнее.
OKX_BATCH_PAUSE = float(os.getenv("OKX_BATCH_PAUSE", "2.0"))

# ── Market fallback для лимитных входов ──────────────────────────────────────
# Если лимитка не исполнилась и цена ушла в нужную сторону не дальше чем на X% —
# входим по рынку с пересчётом TP/SL. Если ушла дальше — не гонимся.
# 0.0 = fallback отключён (только лимитные входы)
LIMIT_CHASE_MAX_PCT = float(os.getenv("LIMIT_CHASE_MAX_PCT", "2.0"))

# Минимальное время ожидания перед market fallback (минуты).
# Даём лимитке шанс исполниться прежде чем догонять по рынку.
LIMIT_CHASE_MIN_WAIT_MIN = float(os.getenv("LIMIT_CHASE_MIN_WAIT_MIN", "5.0"))

# ── Параллельный скан (батчи) ─────────────────────────────────────────────────
# Сколько монет сканируем параллельно внутри одного батча.
# 5 = безопасно для обоих бирж, скан ~1.5 мин вместо 5 мин.
# Не ставить > 10 — можно получить rate limit бан.
SCAN_BATCH_SIZE     = int(os.getenv("SCAN_BATCH_SIZE", "5"))
SCAN_BATCH_PAUSE_BN = float(os.getenv("SCAN_BATCH_PAUSE_BN", "0.5"))  # пауза между батчами Binance
SCAN_BATCH_PAUSE_BY = float(os.getenv("SCAN_BATCH_PAUSE_BY", "1.0"))  # пауза между батчами Bybit

# ── Лимиты запросов (req/min) ─────────────────────────────────────────────────
# Считаем в "запросах в минуту" для сканера. Оставляем резерв, чтобы не упираться
# в лимиты при пиках/повторах/переподключениях.
BINANCE_REQ_PER_MIN_LIMIT = int(os.getenv("BINANCE_REQ_PER_MIN_LIMIT", "1100"))
BYBIT_REQ_PER_MIN_LIMIT   = int(os.getenv("BYBIT_REQ_PER_MIN_LIMIT",   "500"))
REQ_PER_MIN_RESERVE       = int(os.getenv("REQ_PER_MIN_RESERVE",       "50"))

# Оценка "запросов на монету" в одном скане:
# 4 таймфрейма свечей + стакан + цена + OI + BTC 1h = ~8
SCAN_REQ_PER_COIN_ESTIMATE = int(os.getenv("SCAN_REQ_PER_COIN_ESTIMATE", "8"))

# ── Cooldown после стопа ──────────────────────────────────────────────────────
# Сколько минут не входить по монете после SL.
# 60 минут = даём рынку остыть, не ловим "падающий нож" дважды.
SL_COOLDOWN_MINUTES = int(os.getenv("SL_COOLDOWN_MINUTES", "60"))

# ── ML-фильтр сигналов ────────────────────────────────────────────────────────
# Если модель обучена и predict_win_prob(signal) < ML_FILTER_MIN_PROB — сигнал отклоняется.
# Активируется только когда в датасете >= ML_FILTER_MIN_ROWS строк (защита от слабой модели).
# Установите ML_FILTER_ENABLED=false если хотите отключить фильтрацию по ML.
ML_FILTER_ENABLED  = os.getenv("ML_FILTER_ENABLED", "true").lower() == "true"  # ✅ ВКЛЮЧЕНА (538 примеров, ROC-AUC=0.84)
ML_FILTER_MIN_PROB = float(os.getenv("ML_FILTER_MIN_PROB", "0.52"))  # мин. вероятность WIN
ML_FILTER_MIN_ROWS = int(os.getenv("ML_FILTER_MIN_ROWS", "200"))     # мин. строк для активации