"""
bybit_client.py — клиент Bybit Linear Futures (USDT Perp).

Интерфейс идентичен binance_client.py:
  - get_top_futures_async(n)  → list[str]
  - get_full_data(symbol)     → dict  (те же ключи что у Binance)
  - get_cached_price(symbol)  → float | None

signal_generator.py не требует изменений — получает тот же формат.
"""

import asyncio
import time
from typing import Optional

import aiohttp
import numpy as np
import pandas as pd

import config
from utils import logger

BYBIT_BASE = "https://api.bybit.com"

# Интервалы Bybit отличаются от Binance
_INTERVAL_MAP = {
    "1m":  "1",
    "3m":  "3",
    "5m":  "5",
    "15m": "15",
    "30m": "30",
    "1h":  "60",
    "2h":  "120",
    "4h":  "240",
    "1d":  "D",
}

_symbols_cache      = []
_symbols_cache_time = 0.0
_ws_prices: dict[str, float] = {}
_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            base_url=BYBIT_BASE,
            timeout=aiohttp.ClientTimeout(total=10),
        )
    return _session


async def _get(path: str, params: dict = None) -> dict:
    session = await _get_session()
    async with session.get(path, params=params or {}) as resp:
        resp.raise_for_status()
        data = await resp.json()
    if data.get("retCode", 0) != 0:
        raise ValueError(f"Bybit API error: {data.get('retMsg')} | {path} {params}")
    return data


# ── Индикаторы (те же что в binance_client) ───────────────────────────────────

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
    tr   = pd.concat(
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
    # taker_buy_base нужен для CVD — у Bybit это otherVolume (sell side)
    # Приблизительно: taker_buy ≈ turnover * buyRatio если есть, иначе volume/2
    if "taker_buy_base" not in df.columns:
        df["taker_buy_base"] = df["volume"] * 0.5  # fallback
    return df


# ── Список монет ──────────────────────────────────────────────────────────────

async def get_top_futures_async(n: int = None, exclude: set = None) -> list:
    """
    Возвращает топ-N USDT Linear фьючерсов по объёму за 24ч.
    exclude — символы которые уже сканирует Binance (чтобы не дублировать).
    """
    global _symbols_cache, _symbols_cache_time
    n   = n or config.TOP_FUTURES_COUNT
    now = time.time()

    if _symbols_cache and (now - _symbols_cache_time) < 600:
        result = _symbols_cache
        if exclude:
            result = [s for s in result if s not in exclude]
        return result[:n]

    try:
        data    = await _get("/v5/market/tickers", {"category": "linear"})
        tickers = data["result"]["list"]
        usdt = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and "_" not in t["symbol"]
            and float(t.get("turnover24h", 0)) >= config.BYBIT_MIN_VOLUME_USDT
        ]
        usdt.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)
        _symbols_cache      = [t["symbol"] for t in usdt]
        _symbols_cache_time = now

        result = _symbols_cache
        if exclude:
            result = [s for s in result if s not in exclude]

        logger.info("Bybit", f"{len(_symbols_cache)} USDT фьючерсов, "
              f"уникальных (не в Binance): {len(result)}")
        return result[:n]

    except Exception as e:
        logger.err("Bybit", f"Ошибка тикеров: {e}")
        result = _symbols_cache or []
        if exclude:
            result = [s for s in result if s not in exclude]
        return result[:n]


# ── Свечи ─────────────────────────────────────────────────────────────────────

async def get_candles_async(symbol: str, interval: str = "15m",
                             limit: int = 100) -> pd.DataFrame:
    try:
        bybit_interval = _INTERVAL_MAP.get(interval, "15")
        data = await _get("/v5/market/kline", {
            "category": "linear",
            "symbol":   symbol,
            "interval": bybit_interval,
            "limit":    limit,
        })
        rows = data["result"]["list"]
        if not rows:
            return pd.DataFrame()

        # Bybit возвращает: [startTime, open, high, low, close, volume, turnover]
        # Порядок: новейшие сверху → разворачиваем
        rows = list(reversed(rows))
        df = pd.DataFrame(rows, columns=[
            "timestamp", "open", "high", "low", "close", "volume", "turnover"
        ])
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = df[col].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms")
        df.set_index("timestamp", inplace=True)

        # taker_buy_base: Bybit не даёт это в kline напрямую
        # используем volume/2 как нейтральный fallback (CVD будет менее точный)
        df["taker_buy_base"]  = df["volume"] * 0.5
        df["taker_buy_quote"] = df["turnover"] * 0.5

        return _add_indicators(df)

    except Exception as e:
        logger.err("Bybit", f"Свечи {symbol}/{interval}: {e}")
        return pd.DataFrame()


# ── Стакан ────────────────────────────────────────────────────────────────────

async def get_orderbook_async(symbol: str) -> dict:
    try:
        data  = await _get("/v5/market/orderbook", {
            "category": "linear",
            "symbol":   symbol,
            "limit":    config.ORDERBOOK_DEPTH,
        })
        result = data["result"]
        bids = [(float(p), float(q)) for p, q in result.get("b", [])]
        asks = [(float(p), float(q)) for p, q in result.get("a", [])]

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
        logger.err("Bybit", f"Стакан {symbol}: {e}")
        return {}


# ── Цена и статистика 24ч ─────────────────────────────────────────────────────

async def get_price_info_async(symbol: str) -> dict:
    try:
        data   = await _get("/v5/market/tickers", {
            "category": "linear",
            "symbol":   symbol,
        })
        t = data["result"]["list"][0]
        # Bybit иногда отдаёт lastPrice/volume24h как 0 для части инструментов.
        # Для корректного "топа" и логов используем fallback на mark/index,
        # а объём берём как turnover24h (USDT-оборот), который Bybit отдаёт стабильно.
        price = float(
            t.get("lastPrice")
            or t.get("markPrice")
            or t.get("indexPrice")
            or 0
        )
        prev_price = float(t.get("prevPrice24h", price) or price)
        change_pct = (price - prev_price) / prev_price * 100 if prev_price else 0
        volume_24h = float(t.get("turnover24h") or t.get("volume24h") or 0)
        funding    = float(t.get("fundingRate", 0))

        _ws_prices[symbol] = price
        return {
            "price":          price,
            "change_24h_pct": round(change_pct, 2),
            "volume_24h":     volume_24h,
            "funding_rate":   funding,
        }
    except Exception as e:
        logger.err("Bybit", f"Цена {symbol}: {e}")
        return {"price": 0, "change_24h_pct": 0, "volume_24h": 0, "funding_rate": 0}


# ── Open Interest ─────────────────────────────────────────────────────────────

async def get_open_interest(symbol: str) -> dict:
    try:
        data = await _get("/v5/market/open-interest", {
            "category":     "linear",
            "symbol":       symbol,
            "intervalTime": "5min",
            "limit":        3,
        })
        rows = data["result"]["list"]
        if len(rows) < 2:
            return {"oi": 0, "oi_change_pct": 0, "oi_growing": False, "oi_falling": False}

        oi_now  = float(rows[0]["openInterest"])
        oi_prev = float(rows[-1]["openInterest"])
        change  = (oi_now - oi_prev) / oi_prev * 100 if oi_prev else 0
        return {
            "oi":            oi_now,
            "oi_change_pct": round(change, 2),
            "oi_growing":    change > 0.5,
            "oi_falling":    change < -0.5,
        }
    except Exception as e:
        logger.err("Bybit", f"OI {symbol}: {e}")
        return {"oi": 0, "oi_change_pct": 0, "oi_growing": False, "oi_falling": False}


# ── get_full_data — идентичный интерфейс с binance_client ─────────────────────

async def get_full_data(symbol: str) -> dict:
    """
    Собирает все данные по монете. Возвращает тот же формат что binance_client.
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
        "_exchange":      "bybit",   # маркер биржи (для логов)
    }


def get_cached_price(symbol: str) -> float | None:
    return _ws_prices.get(symbol)