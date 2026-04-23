"""
Fear & Greed Index — бесплатный публичный API alternative.me
Значения: 0-24 Extreme Fear, 25-49 Fear, 50-74 Greed, 75-100 Extreme Greed
"""

import aiohttp
import time

_cache = {"value": None, "label": None, "fetched_at": 0}
CACHE_TTL = 3600  # обновляем раз в час


async def get_fear_greed() -> dict:
    global _cache
    now = time.time()

    if _cache["value"] is not None and (now - _cache["fetched_at"]) < CACHE_TTL:
        return _cache

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get("https://api.alternative.me/fng/?limit=1") as resp:
                data = await resp.json()

        item  = data["data"][0]
        value = int(item["value"])
        label = item["value_classification"]

        _cache = {
            "value":      value,
            "label":      label,
            "fetched_at": now,
            "emoji":      _emoji(value),
            "signal":     _signal(value),
        }
        print(f"[FearGreed] {value} — {label}")
        return _cache

    except Exception as e:
        print(f"[FearGreed] Ошибка: {e}")
        return _cache if _cache["value"] else {"value": 50, "label": "Neutral", "emoji": "😐", "signal": "neutral"}


def _emoji(v: int) -> str:
    if v <= 24:  return "😱"
    if v <= 49:  return "😨"
    if v <= 74:  return "😏"
    return "🤑"


def _signal(v: int) -> str:
    """
    Контрарианская логика:
    Extreme Fear  → хорошо покупать (LONG bias)
    Extreme Greed → хорошо продавать (SHORT bias)
    """
    if v <= 24:  return "strong_long"
    if v <= 39:  return "long"
    if v <= 60:  return "neutral"
    if v <= 79:  return "short"
    return "strong_short"


def get_cached() -> dict:
    return _cache if _cache["value"] else {"value": 50, "label": "Neutral", "emoji": "😐", "signal": "neutral"}