import asyncio
import json
import time
from typing import Optional, Callable
import aiohttp
import websockets
import pandas as pd
import numpy as np
import config

from utils import logger

FAPI_BASE = "https://fapi.binance.com"

_symbols_cache      = []
_symbols_cache_time = 0.0
_ws_prices          = {}
_session: Optional[aiohttp.ClientSession] = None

# Чёрный список тикеров, которые не работают на фьючерсах (дают 400 Bad Request)
_BLACKLIST_SYMBOLS = {
    "MNTUSDT",      # не работает на фьючерсах
    "XAUTUSDT",     # золото, нет funding API
    "PUMPFUNUSDT",  # мем-коин, нестабильный API
}


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            base_url=FAPI_BASE,
            timeout=aiohttp.ClientTimeout(total=10),
        )
    return _session


async def _get(path: str, params: dict = None):
    session = await _get_session()
    async with session.get(path, params=params) as resp:
        resp.raise_for_status()
        return await resp.json()


# ── Индикаторы ────────────────────────────────────────────────────────────────

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
    tr   = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
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
    return df


# ── Список монет ──────────────────────────────────────────────────────────────

async def get_top_futures_async(n: int = None) -> list:
    global _symbols_cache, _symbols_cache_time
    n   = n or config.TOP_FUTURES_COUNT
    now = time.time()
    if _symbols_cache and (now - _symbols_cache_time) < 600:
        return _symbols_cache[:n]
    try:
        tickers = await _get("/fapi/v1/ticker/24hr")
        usdt = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and "_" not in t["symbol"]
            and t["symbol"] not in _BLACKLIST_SYMBOLS
            and float(t.get("quoteVolume", 0)) >= config.MIN_VOLUME_USDT
        ]
        usdt.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
        _symbols_cache      = [t["symbol"] for t in usdt]
        _symbols_cache_time = now
        logger.info("Binance", f"{len(_symbols_cache)} USDT фьючерсов (vol≥{config.MIN_VOLUME_USDT/1e6:.0f}M)")
        return _symbols_cache[:n]
    except Exception as e:
        logger.err("Binance", f"Ошибка тикеров: {e}")
        return _symbols_cache[:n] if _symbols_cache else []


# ── Свечи ─────────────────────────────────────────────────────────────────────

async def get_candles_async(symbol: str, interval: str = "15m", limit: int = 100) -> pd.DataFrame:
    try:
        data = await _get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        df = pd.DataFrame(data, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return _add_indicators(df)
    except Exception as e:
        logger.err("Binance", f"Свечи {symbol}/{interval}: {e}")
        return pd.DataFrame()


# ── Стакан ────────────────────────────────────────────────────────────────────

async def get_orderbook_async(symbol: str) -> dict:
    try:
        book    = await _get("/fapi/v1/depth", {"symbol": symbol, "limit": config.ORDERBOOK_DEPTH})
        bids    = [(float(p), float(q)) for p, q in book["bids"]]
        asks    = [(float(p), float(q)) for p, q in book["asks"]]
        bid_vol = sum(q for _, q in bids)
        ask_vol = sum(q for _, q in asks)
        total   = bid_vol + ask_vol
        imbalance  = (bid_vol - ask_vol) / total if total > 0 else 0
        best_bid   = bids[0][0] if bids else 0
        best_ask   = asks[0][0] if asks else 0
        spread_pct = ((best_ask - best_bid) / best_bid * 100) if best_bid > 0 else 0
        avg        = total / (len(bids) + len(asks)) if (bids or asks) else 0
        return {
            "bid_volume":   bid_vol,
            "ask_volume":   ask_vol,
            "imbalance":    round(imbalance, 4),
            "spread_pct":   round(spread_pct, 4),
            "best_bid":     best_bid,
            "best_ask":     best_ask,
            "big_bid_walls": [(p, q) for p, q in bids if q > avg * 3],
            "big_ask_walls": [(p, q) for p, q in asks if q > avg * 3],
        }
    except Exception as e:
        logger.err("Binance", f"Стакан {symbol}: {e}")
        return {}


# ── Цена + Funding ────────────────────────────────────────────────────────────

async def get_price_info_async(symbol: str) -> dict:
    # Символы из чёрного списка (например, MNTUSDT) на фьючерсах Binance
    # могут возвращать 400 Bad Request. Для них просто не ходим в API.
    if symbol in _BLACKLIST_SYMBOLS:
        return {}
    try:
        ticker, premium = await asyncio.gather(
            _get("/fapi/v1/ticker/24hr",    {"symbol": symbol}),
            _get("/fapi/v1/premiumIndex",   {"symbol": symbol}),
        )
        return {
            "price":          float(ticker["lastPrice"]),
            "mark_price":     float(premium.get("markPrice", ticker["lastPrice"])),
            "change_24h_pct": float(ticker["priceChangePercent"]),
            "volume_24h":     float(ticker["quoteVolume"]),
            "funding_rate":   float(premium.get("lastFundingRate", 0)),
        }
    except Exception as e:
        logger.err("Binance", f"Цена {symbol}: {e}")
        return {}


# ── Open Interest ─────────────────────────────────────────────────────────────

async def get_open_interest(symbol: str) -> dict:
    try:
        oi_now  = await _get("/fapi/v1/openInterest", {"symbol": symbol})
        oi_val  = float(oi_now["openInterest"])
        oi_hist = await _get("/futures/data/openInterestHist", {
            "symbol": symbol, "period": "1h", "limit": 5
        })
        if oi_hist and len(oi_hist) >= 2:
            oi_old        = float(oi_hist[0]["sumOpenInterest"])
            oi_new        = float(oi_hist[-1]["sumOpenInterest"])
            oi_change_pct = (oi_new - oi_old) / oi_old * 100 if oi_old > 0 else 0
        else:
            oi_change_pct = 0.0
        return {
            "oi":            oi_val,
            "oi_change_pct": round(oi_change_pct, 2),
            "oi_growing":    oi_change_pct > 1.0,
            "oi_falling":    oi_change_pct < -1.0,
        }
    except Exception:
        return {"oi": 0, "oi_change_pct": 0, "oi_growing": False, "oi_falling": False}


# ── Все данные монеты ─────────────────────────────────────────────────────────

async def get_full_data(symbol: str) -> dict:
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
    }


# ── Фильтр боковика ───────────────────────────────────────────────────────────

def is_consolidating(df: pd.DataFrame,
                     atr_threshold: float = 0.002,
                     bb_threshold:  float = 0.008) -> bool:
    """
    Возвращает True если монета в ЖЁСТКОМ боковике (мёртвый рынок).

    Критерии (оба должны выполняться):
    - ATR < 0.2% от цены  — почти нет движения
    - BB ширина < 0.8%    — полосы сжаты до минимума

    Специально очень строгие пороги — блокируем только совсем мёртвые монеты.
    Нормальная консолидация (1-2% ATR) не блокируется.
    """
    if df.empty or "ATR_14" not in df.columns or "BB_upper" not in df.columns:
        return False
    try:
        price    = df["close"].iloc[-1]
        if price == 0:
            return False
        atr_pct  = df["ATR_14"].iloc[-1] / price
        bb_width = (df["BB_upper"].iloc[-1] - df["BB_lower"].iloc[-1]) / price
        return atr_pct < atr_threshold and bb_width < bb_threshold
    except Exception:
        return False


# ── CVD (Cumulative Volume Delta) ─────────────────────────────────────────────

def calc_cvd(df: pd.DataFrame, periods: int = 20) -> dict:
    if df.empty or "taker_buy_base" not in df.columns:
        return _empty_cvd()
    try:
        df = df.copy()
        df["taker_buy_base"] = pd.to_numeric(df["taker_buy_base"], errors="coerce").fillna(0)
        df["buy_vol"]  = df["taker_buy_base"]
        df["sell_vol"] = df["volume"] - df["taker_buy_base"]
        df["delta"]    = df["buy_vol"] - df["sell_vol"]

        recent   = df.tail(periods)
        cvd      = recent["delta"].sum()
        cvd_prev = df.tail(periods * 2).head(periods)["delta"].sum()

        cvd_rising  = cvd > cvd_prev * 1.05
        cvd_falling = cvd < cvd_prev * 0.95

        price_rising  = df["close"].iloc[-1] > df["close"].iloc[-periods]
        price_falling = df["close"].iloc[-1] < df["close"].iloc[-periods]

        divergence_bearish = cvd_falling and price_rising
        divergence_bullish = cvd_rising  and price_falling

        avg_vol  = df["volume"].tail(periods).mean()
        cvd_norm = (cvd / (avg_vol * periods) * 100) if avg_vol > 0 else 0

        return {
            "cvd":                round(cvd, 2),
            "cvd_norm":           round(cvd_norm, 2),
            "cvd_rising":         cvd_rising,
            "cvd_falling":        cvd_falling,
            "divergence_bearish": divergence_bearish,
            "divergence_bullish": divergence_bullish,
        }
    except Exception:
        return _empty_cvd()


def _empty_cvd() -> dict:
    return {
        "cvd": 0, "cvd_norm": 0,
        "cvd_rising": False, "cvd_falling": False,
        "divergence_bearish": False, "divergence_bullish": False,
    }


# ── Свечные паттерны ──────────────────────────────────────────────────────────

def detect_candle_patterns(df: pd.DataFrame) -> dict:
    if df.empty or len(df) < 3:
        return _empty_patterns()
    try:
        c  = df.iloc[-1]
        p  = df.iloc[-2]

        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        po, pc      = p["open"], p["close"]

        body        = abs(cl - o)
        candle_size = h - l
        upper_wick  = h - max(o, cl)
        lower_wick  = min(o, cl) - l

        if candle_size == 0:
            return _empty_patterns()

        body_pct = body / candle_size
        is_green = cl > o
        is_red   = cl < o

        hammer = (
            lower_wick >= body * 2 and
            upper_wick <= body * 0.3 and
            body_pct < 0.4 and
            p["close"] < p["open"]
        )
        shooting_star = (
            upper_wick >= body * 2 and
            lower_wick <= body * 0.3 and
            body_pct < 0.4 and
            p["close"] > p["open"]
        )
        bullish_engulfing = (
            is_green and
            p["close"] < p["open"] and
            o < p["close"] and
            cl > p["open"]
        )
        bearish_engulfing = (
            is_red and
            p["close"] > p["open"] and
            o > p["close"] and
            cl < p["open"]
        )
        doji = body_pct < 0.1

        return {
            "hammer":            hammer,
            "shooting_star":     shooting_star,
            "bullish_engulfing": bullish_engulfing,
            "bearish_engulfing": bearish_engulfing,
            "doji":              doji,
            "bullish_pattern":   hammer or bullish_engulfing,
            "bearish_pattern":   shooting_star or bearish_engulfing,
        }
    except Exception:
        return _empty_patterns()


def _empty_patterns() -> dict:
    return {
        "hammer": False, "shooting_star": False,
        "bullish_engulfing": False, "bearish_engulfing": False,
        "doji": False, "bullish_pattern": False, "bearish_pattern": False,
    }


# ── Корреляция с BTC ──────────────────────────────────────────────────────────

def calc_btc_correlation(df_symbol: pd.DataFrame, df_btc: pd.DataFrame,
                          periods: int = 20) -> dict:
    if df_symbol.empty or df_btc.empty:
        return {"correlation": 0.0, "high_corr": False, "low_corr": True}
    try:
        sym_ret = df_symbol["close"].pct_change().tail(periods).dropna()
        btc_ret = df_btc["close"].pct_change().tail(periods).dropna()
        min_len = min(len(sym_ret), len(btc_ret))
        if min_len < 5:
            return {"correlation": 0.0, "high_corr": False, "low_corr": True, "neg_corr": False}
        sym_arr = sym_ret.values[-min_len:]
        btc_arr = btc_ret.values[-min_len:]
        if sym_arr.std() == 0 or btc_arr.std() == 0:
            return {"correlation": 0.0, "high_corr": False, "low_corr": True, "neg_corr": False}
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            corr = float(np.corrcoef(sym_arr, btc_arr)[0, 1])
        if np.isnan(corr) or np.isinf(corr):
            corr = 0.0
        return {
            "correlation": round(corr, 3),
            "high_corr":   corr > 0.65,
            "low_corr":    corr < 0.3,
            "neg_corr":    corr < -0.3,
        }
    except Exception:
        return {"correlation": 0.0, "high_corr": False, "low_corr": True, "neg_corr": False}


# ── Funding Rate таймер ───────────────────────────────────────────────────────

def get_funding_timer() -> dict:
    from datetime import datetime, timezone
    now_utc  = datetime.now(timezone.utc)
    hour     = now_utc.hour
    minute   = now_utc.minute
    funding_hours = [0, 8, 16, 24]
    next_funding  = next(h for h in funding_hours if h > hour)
    if next_funding == 24:
        next_funding = 0
        hours_left   = 24 - hour
    else:
        hours_left = next_funding - hour
    minutes_to_funding = hours_left * 60 - minute
    return {
        "minutes_to_funding": minutes_to_funding,
        "near_funding":       minutes_to_funding < 90,
        "avoid_entry":        minutes_to_funding < 30,
        "next_funding_hour":  next_funding,
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────

async def ws_price_stream(symbols: list, on_price: Callable):
    """
    WebSocket стрим цен с улучшенным переподключением.
    - Разбиваем на части по 50 символов (Binance лимит ~100 потоков на подключение)
    - Экспоненциальная задержка при ошибках
    - Авто-ребалансировка при обрыве
    """
    MAX_SYMBOLS_PER_CONN = 50  # Binance стабильнее с меньшим числом потоков
    reconnect_delay = 5
    max_reconnect_delay = 60
    
    # Разбиваем на батчи
    batches = [symbols[i:i+MAX_SYMBOLS_PER_CONN] for i in range(0, len(symbols), MAX_SYMBOLS_PER_CONN)]
    
    async def ws_batch(batch_symbols: list, batch_idx: int):
        """WS подключение для одного батча символов."""
        nonlocal reconnect_delay
        while True:
            try:
                streams = "/".join(f"{s.lower()}@bookTicker" for s in batch_symbols)
                url = f"wss://fstream.binance.com/stream?streams={streams}"
                logger.info("WS", f"[{batch_idx}] Подключаемся ({len(batch_symbols)} монет)...")

                async with websockets.connect(
                    url,
                    ping_interval=25,
                    ping_timeout=15,
                    close_timeout=10,
                ) as ws:
                    logger.ok("WS", f"[{batch_idx}] Подключено ✅")
                    reconnect_delay = 5  # сброс задержки после успешного подключения

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            data = msg.get("data", msg)
                            sym = data.get("s")
                            bid = float(data.get("b", 0))
                            ask = float(data.get("a", 0))
                            if sym and bid and ask:
                                _ws_prices[sym] = (bid + ask) / 2
                                on_price(sym, _ws_prices[sym])
                        except Exception:
                            pass

            except websockets.exceptions.ConnectionClosed as e:
                logger.warn("WS", f"[{batch_idx}] Обрыв: {e}. Переподключение через {reconnect_delay}с...")
            except Exception as e:
                logger.warn("WS", f"[{batch_idx}] Ошибка: {e}. Переподключение через {reconnect_delay}с...")
            
            await asyncio.sleep(reconnect_delay)
            # Экспоненциальная задержка (но не больше максимума)
            reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)
    
    # Запускаем все батчи параллельно
    tasks = [asyncio.create_task(ws_batch(batch, i)) for i, batch in enumerate(batches)]
    await asyncio.gather(*tasks, return_exceptions=True)


def get_cached_price(symbol: str):
    return _ws_prices.get(symbol)