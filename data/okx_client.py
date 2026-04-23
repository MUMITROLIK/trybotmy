"""
okx_client.py — клиент OKX Linear Futures (USDT Swap).

Интерфейс идентичен bybit_client.py / binance_client.py:
  - get_top_futures_async(n, exclude)  → list[str]
  - get_full_data(symbol)              → dict  (те же ключи)
  - get_cached_price(symbol)           → float | None

Особенности OKX:
  - Символы в формате BTC-USDT-SWAP → конвертируем в BTCUSDT для совместимости
  - Свечи: новейшие сверху → разворачиваем
  - Bar: "1m","5m","15m","30m","1H","4H","1D"
  - OI: /api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP
"""

import asyncio
import time
from typing import Optional

import aiohttp
import numpy as np
import pandas as pd

import config
from utils import logger

OKX_BASE = "https://www.okx.com"

_INTERVAL_MAP = {
    "1m":  "1m",
    "3m":  "3m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1H",
    "2h":  "2H",
    "4h":  "4H",
    "1d":  "1D",
}

# Монеты с некорректными объёмами (контракты не в базовой монете или мусор)
_OKX_BLACKLIST = {"PEPEUSDT", "SHIBUSDT", "BONKUSDT", "SATSUSDT", "FLOKIUSDT",
                  "CLUSDT", "OKBUSDT", "XPTUSDT", "XPDUSDT", "GIGGLEUSDT"}

_symbols_cache: list[str] = []       # в формате BTCUSDT (совместимо с остальными)
_inst_id_map:  dict[str, str] = {}   # BTCUSDT → BTC-USDT-SWAP
_symbols_cache_time = 0.0
_ws_prices: dict[str, float] = {}
_session: Optional[aiohttp.ClientSession] = None
# Глобальный семафор — не более 3 параллельных запросов к OKX (20 req/sec лимит)
_semaphore: Optional[asyncio.Semaphore] = None

def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(3)
    return _semaphore


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            base_url=OKX_BASE,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Mozilla/5.0"},
        )
    return _session


async def _get(path: str, params: dict = None) -> dict:
    async with _get_semaphore():
        session = await _get_session()
        async with session.get(path, params=params or {}) as resp:
            resp.raise_for_status()
            data = await resp.json()
        code = str(data.get("code", "0"))
        if code != "0":
            raise ValueError(f"OKX API error code={code} msg={data.get('msg')} | {path}")
        return data


def _okx_to_usdt(inst_id: str) -> str:
    """BTC-USDT-SWAP → BTCUSDT"""
    return inst_id.replace("-USDT-SWAP", "USDT")


def _usdt_to_okx(symbol: str) -> str:
    """BTCUSDT → BTC-USDT-SWAP"""
    return _inst_id_map.get(symbol, symbol.replace("USDT", "-USDT-SWAP"))


# ── Индикаторы (те же что в bybit_client) ─────────────────────────────────────

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def _macd(close: pd.Series, fast=12, slow=26, signal=9):
    ml = _ema(close, fast) - _ema(close, slow)
    sl = _ema(ml, signal)
    return ml, sl, ml - sl


def _bbands(close: pd.Series, period: int = 20, mult: float = 2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return mid + mult * std, mid, mid - mult * std


def _atr(high, low, close, period=14):
    prev = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev).abs(), (low - prev).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    df[f"RSI_{config.RSI_PERIOD}"] = _rsi(c, config.RSI_PERIOD)
    macd, sig, hist = _macd(c)
    df["MACD"], df["MACD_sig"], df["MACD_hist"] = macd, sig, hist
    bb_u, bb_m, bb_l = _bbands(c, config.BB_PERIOD)
    df["BB_upper"], df["BB_mid"], df["BB_lower"] = bb_u, bb_m, bb_l
    df["EMA_20"]    = _ema(c, 20)
    df["EMA_50"]    = _ema(c, 50)
    df["ATR_14"]    = _atr(df["high"], df["low"], c, 14)
    df["vol_ma"]    = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma"]
    if "taker_buy_base" not in df.columns:
        df["taker_buy_base"] = df["volume"] * 0.5  # fallback
    return df


# ── Список монет ──────────────────────────────────────────────────────────────

async def get_top_futures_async(n: int = None, exclude: set = None) -> list:
    """
    Возвращает топ-N USDT Swap фьючерсов по объёму за 24ч.
    exclude — символы уже сканируемые Binance/Bybit (без дублей).
    Возвращает символы в формате BTCUSDT (совместимо с остальным кодом).
    """
    global _symbols_cache, _symbols_cache_time, _inst_id_map
    n   = n or config.TOP_FUTURES_COUNT
    now = time.time()

    if _symbols_cache and (now - _symbols_cache_time) < 600:
        result = _symbols_cache
        if exclude:
            result = [s for s in result if s not in exclude]
        return result[:n]

    try:
        data    = await _get("/api/v5/market/tickers", {"instType": "SWAP"})
        tickers = data.get("data", [])

        usdt = []
        for t in tickers:
            inst_id = t.get("instId", "")
            if not inst_id.endswith("-USDT-SWAP"):
                continue
            try:
                # volCcy24h = объём в базовой монете (BTC, ETH, YFI...)
                # last = текущая цена
                # volCcy24h × last = объём в USDT
                vol_ccy = float(t.get("volCcy24h") or 0)
                last_price = float(t.get("last") or 0)
                vol_usdt = vol_ccy * last_price
            except Exception:
                vol_usdt = 0.0
            okx_min_vol = float(getattr(config, "OKX_MIN_VOLUME_USDT", config.MIN_VOLUME_USDT))
            if vol_usdt < okx_min_vol:
                continue
            sym = _okx_to_usdt(inst_id)
            if sym in _OKX_BLACKLIST:
                continue
            usdt.append((sym, inst_id, vol_usdt))

        usdt.sort(key=lambda x: x[2], reverse=True)

        _inst_id_map   = {sym: inst_id for sym, inst_id, _ in usdt}
        _symbols_cache = [sym for sym, _, _ in usdt]
        _symbols_cache_time = now

        result = _symbols_cache
        if exclude:
            result = [s for s in result if s not in exclude]

        logger.info("OKX", f"{len(_symbols_cache)} USDT Swap, "
              f"уникальных (не в Binance/Bybit): {len(result)}")
        return result[:n]

    except Exception as e:
        logger.err("OKX", f"Ошибка тикеров: {e}")
        result = _symbols_cache or []
        if exclude:
            result = [s for s in result if s not in exclude]
        return result[:n]


# ── Свечи ─────────────────────────────────────────────────────────────────────

async def get_candles_async(symbol: str, interval: str = "15m",
                             limit: int = 100) -> pd.DataFrame:
    inst_id = _usdt_to_okx(symbol)
    bar     = _INTERVAL_MAP.get(interval, "15m")
    try:
        data = await _get("/api/v5/market/candles", {
            "instId": inst_id,
            "bar":    bar,
            "limit":  str(min(limit, 300)),
        })
        rows = data.get("data", [])
        if not rows:
            return pd.DataFrame()

        # OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        # Новейшие сверху → разворачиваем
        rows = list(reversed(rows))
        df = pd.DataFrame(rows, columns=[
            "timestamp", "open", "high", "low", "close",
            "volume", "volCcy", "volCcyQuote", "confirm"
        ])
        for col in ["open", "high", "low", "close", "volume", "volCcy"]:
            df[col] = df[col].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms")
        df.set_index("timestamp", inplace=True)

        # taker_buy_base — OKX не отдаёт в candles, используем fallback
        df["taker_buy_base"]  = df["volume"] * 0.5
        df["taker_buy_quote"] = df["volCcy"] * 0.5

        return _add_indicators(df)

    except Exception as e:
        logger.err("OKX", f"Свечи {symbol}/{interval}: {e}")
        return pd.DataFrame()


# ── Стакан ────────────────────────────────────────────────────────────────────

async def get_orderbook_async(symbol: str) -> dict:
    inst_id = _usdt_to_okx(symbol)
    try:
        data   = await _get("/api/v5/market/books", {
            "instId": inst_id,
            "sz":     str(config.ORDERBOOK_DEPTH),
        })
        result = data.get("data", [{}])[0]
        bids   = [(float(p), float(q)) for p, q, *_ in result.get("bids", [])]
        asks   = [(float(p), float(q)) for p, q, *_ in result.get("asks", [])]

        bid_vol = sum(q for _, q in bids)
        ask_vol = sum(q for _, q in asks)
        total   = bid_vol + ask_vol
        imb     = (bid_vol - ask_vol) / total if total > 0 else 0

        return {
            "bids":      bids,
            "asks":      asks,
            "bid_vol":   bid_vol,
            "ask_vol":   ask_vol,
            "imbalance": round(imb, 4),
        }
    except Exception as e:
        logger.err("OKX", f"Стакан {symbol}: {e}")
        return {}


# ── Цена и статистика 24ч ─────────────────────────────────────────────────────

async def get_price_info_async(symbol: str) -> dict:
    inst_id = _usdt_to_okx(symbol)
    try:
        data = await _get("/api/v5/market/ticker", {"instId": inst_id})
        t    = data.get("data", [{}])[0]

        price      = float(t.get("last") or t.get("askPx") or 0)
        open_24h   = float(t.get("open24h") or price)
        change_pct = (price - open_24h) / open_24h * 100 if open_24h else 0
        # volCcy24h = объём в базовой монете, нужно умножить на цену для USDT
        vol_ccy    = float(t.get("volCcy24h") or 0)
        vol_usdt   = vol_ccy * price
        # Funding rate — OKX отдаёт через отдельный endpoint
        funding    = 0.0

        _ws_prices[symbol] = price
        return {
            "price":          price,
            "change_24h_pct": round(change_pct, 2),
            "volume_24h":     vol_usdt,
            "funding_rate":   funding,
        }
    except Exception as e:
        logger.err("OKX", f"Цена {symbol}: {e}")
        return {"price": 0, "change_24h_pct": 0, "volume_24h": 0, "funding_rate": 0}


# ── Open Interest ─────────────────────────────────────────────────────────────

async def get_open_interest(symbol: str) -> dict:
    inst_id = _usdt_to_okx(symbol)
    try:
        data = await _get("/api/v5/public/open-interest", {
            "instType": "SWAP",
            "instId":   inst_id,
        })
        rows = data.get("data", [])
        if not rows:
            return {"oi": 0, "oi_change_pct": 0, "oi_growing": False, "oi_falling": False}

        oi_now = float(rows[0].get("oi") or rows[0].get("oiCcy") or 0)

        # OKX /public/open-interest возвращает только текущее значение.
        # Для change_pct нужен history endpoint — используем нулевое изменение как fallback.
        return {
            "oi":            oi_now,
            "oi_change_pct": 0.0,
            "oi_growing":    False,
            "oi_falling":    False,
        }
    except Exception as e:
        logger.err("OKX", f"OI {symbol}: {e}")
        return {"oi": 0, "oi_change_pct": 0, "oi_growing": False, "oi_falling": False}


# ── get_full_data — идентичный интерфейс ──────────────────────────────────────

async def get_full_data(symbol: str) -> dict:
    """
    Собирает все данные по монете. Возвращает тот же формат что binance/bybit.
    signal_generator.py работает без изменений.
    """
    c5, c15, c1h, c4h, ob, pi, oi, btc_1h = await asyncio.gather(
        get_candles_async(symbol, "5m",  50),
        get_candles_async(symbol, "15m", 100),
        get_candles_async(symbol, "1h",  100),
        get_candles_async(symbol, "4h",  50),
        get_orderbook_async(symbol),
        get_price_info_async(symbol),
        get_open_interest(symbol),
        get_candles_async("BTCUSDT", "1h", 50),
    )
    return {
        "symbol":         symbol,
        "candles_5m":     c5,
        "candles_15m":    c15,
        "candles_1h":     c1h,
        "candles_4h":     c4h,
        "orderbook":      ob,
        "price_info":     pi,
        "open_interest":  oi,
        "btc_candles_1h": btc_1h,
        "_exchange":      "okx",
    }


def get_cached_price(symbol: str) -> float | None:
    return _ws_prices.get(symbol)
