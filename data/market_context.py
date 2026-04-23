"""
Рыночный контекст — BTC как барометр всего рынка.

Логика:
  - Смотрим BTC на 4h и 1h
  - Если BTC в сильном нисходящем тренде → не давать LONG на альты
  - Если BTC в сильном восходящем тренде → не давать SHORT на альты
  - Нейтральный BTC → сигналы в любую сторону

Время суток (UTC):
  - 00:00-07:00 → азиатская сессия, низкий объём, фильтруем слабые сигналы
  - 07:00-22:00 → европейская + американская, полная активность
"""

import asyncio
import time
from datetime import datetime, timezone
import pandas as pd
from data.binance_client import get_candles_async

_cache = {"context": None, "fetched_at": 0}
CACHE_TTL = 300  # обновляем каждые 5 минут


async def get_btc_context() -> dict:
    """
    Возвращает рыночный контекст BTC:
    {
        trend_4h: 'bull' | 'bear' | 'neutral',
        trend_1h: 'bull' | 'bear' | 'neutral',
        change_4h_pct: float,
        change_1h_pct: float,
        session: 'asian' | 'european' | 'american',
        block_long: bool,   # не давать LONG
        block_short: bool,  # не давать SHORT
        reason: str,
    }
    """
    global _cache
    now = time.time()

    if _cache["context"] and (now - _cache["fetched_at"]) < CACHE_TTL:
        return _cache["context"]

    try:
        df_4h, df_1h = await asyncio.gather(
            get_candles_async("BTCUSDT", "4h",  20),
            get_candles_async("BTCUSDT", "1h",  20),
        )

        # 4h тренд
        trend_4h = _trend(df_4h)
        change_4h = _price_change(df_4h, 3)  # последние 3 свечи = 12 часов

        # 1h тренд
        trend_1h = _trend(df_1h)
        change_1h = _price_change(df_1h, 1)

        # Время суток (UTC)
        hour = datetime.now(timezone.utc).hour
        if 0 <= hour < 7:
            session = "asian"
        elif 7 <= hour < 16:
            session = "european"
        else:
            session = "american"

        # Блокируем направления при сильном BTC тренде
        block_long  = False
        block_short = False
        reason      = ""

        # Сильный медвежий рынок — не давать LONG на альты
        if trend_4h == "bear" and trend_1h == "bear" and change_4h < -3.0:
            block_long = True
            reason = f"BTC сильный нисходящий тренд ({change_4h:.1f}% за 12ч)"

        # Сильный бычий рынок — не давать SHORT на альты
        elif trend_4h == "bull" and trend_1h == "bull" and change_4h > 3.0:
            block_short = True
            reason = f"BTC сильный восходящий тренд (+{change_4h:.1f}% за 12ч)"

        # Азиатская сессия — повышаем минимальную силу сигнала
        asian_penalty = session == "asian"

        ctx = {
            "trend_4h":      trend_4h,
            "trend_1h":      trend_1h,
            "change_4h_pct": round(change_4h, 2),
            "change_1h_pct": round(change_1h, 2),
            "session":       session,
            "block_long":    block_long,
            "block_short":   block_short,
            "asian_penalty": asian_penalty,
            "reason":        reason,
        }

        _cache = {"context": ctx, "fetched_at": now}
        return ctx

    except Exception as e:
        print(f"[Context] Ошибка: {e}")
        return _empty_context()


def _trend(df: pd.DataFrame) -> str:
    if df.empty or "EMA_20" not in df.columns or "EMA_50" not in df.columns:
        return "neutral"
    e20 = df["EMA_20"].iloc[-1]
    e50 = df["EMA_50"].iloc[-1]
    close = df["close"].iloc[-1]

    if e20 > e50 and close > e20:  return "bull"
    if e20 < e50 and close < e20:  return "bear"
    return "neutral"


def _price_change(df: pd.DataFrame, candles_back: int) -> float:
    if df.empty or len(df) < candles_back + 1:
        return 0.0
    current = df["close"].iloc[-1]
    past    = df["close"].iloc[-(candles_back+1)]
    return (current - past) / past * 100


def _empty_context() -> dict:
    return {
        "trend_4h": "neutral", "trend_1h": "neutral",
        "change_4h_pct": 0, "change_1h_pct": 0,
        "session": "european",
        "block_long": False, "block_short": False,
        "asian_penalty": False, "reason": "",
    }


def get_cached_context() -> dict:
    return _cache["context"] if _cache["context"] else _empty_context()