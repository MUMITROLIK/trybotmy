"""
Signal Generator v5 — Чёткие сигналы
Изменения vs v4:
  - Фильтр "тяжёлых" индикаторов (нужно 1+ из mtf/volume/oi/cvd)
  - CVD дивергенция = жёсткий стоп (не просто штраф)
  - 4h тренд обязателен для направления LONG/SHORT
  - Повышены пороги Volume (x1.2 больше не считается)
  - Фильтр боковика (ATR + BB ширина)
  - Проверка "здоровья" входной свечи
  - Динамический MIN_SIGNAL_STRENGTH (рынок, время суток, F&G)
  - Лог ВСЕХ оценённых сигналов в БД (для анализа)
"""

import pandas as pd
from datetime import datetime, timezone

import config
from data.news_parser import get_coin_sentiment
from data.fear_greed import get_cached as get_fg
from data.market_context import get_cached_context
from data.binance_client import (
    calc_cvd, detect_candle_patterns,
    calc_btc_correlation, get_funding_timer,
    is_consolidating,
)

# ── Веса (обновлены по бэктесту 16.03.2026) ───────────────────────────────────
WEIGHTS = {
    "rsi":       12,
    "macd":      12,
    "bb":         8,
    "ema":        5,
    "volume":    16,
    "orderbook":  9,
    "news":       6,
    "mtf":       15,
    "oi":         5,
    "cvd":        8,
    "patterns":   6,
    "btc_corr":   4,
    "levels":     7,  # ← новый индикатор уровней
}

# Индикаторы которые реально предсказывают движение
# Нужно минимум 1 из них с силой >= 0.5
HEAVY_INDICATORS = ["mtf", "volume", "oi", "cvd"]
HEAVY_MIN_SCORE = 0.5


# ── Индикаторы ────────────────────────────────────────────────────────────────

def _rsi_score(df, direction):
    col = f"RSI_{config.RSI_PERIOD}"
    if col not in df.columns or df[col].isna().all():
        return 0.0, None
    rsi = df[col].iloc[-1]
    
    # ← Проверка дивергенции RSI
    div_bonus = 0.0
    if len(df) >= 15 and "close" in df.columns:
        rsi_recent = df[col].tail(5).mean()
        rsi_prev = df[col].iloc[-15:-10].mean() if len(df) >= 15 else df[col].iloc[-10]
        price_recent = df["close"].tail(5).mean()
        price_prev = df["close"].iloc[-15:-10].mean() if len(df) >= 15 else df["close"].iloc[-10]
        
        # Bullish divergence: цена ниже, RSI выше
        if direction == "LONG" and price_recent < price_prev and rsi_recent > rsi_prev:
            div_bonus = 0.2
        # Bearish divergence: цена выше, RSI ниже
        elif direction == "SHORT" and price_recent > price_prev and rsi_recent < rsi_prev:
            div_bonus = 0.2
    
    if direction == "LONG":
        if rsi < 30:   return min(1.0, 1.0 + div_bonus), f"RSI={rsi:.0f} (сильная перепроданность)"
        if rsi < 40:   return min(1.0, 0.75 + div_bonus), f"RSI={rsi:.0f} (перепроданность)"
        if rsi < 50:   return 0.4 + div_bonus, f"RSI={rsi:.0f} (нейтральный↓)"
    if direction == "SHORT":
        if rsi > 70:   return min(1.0, 1.0 + div_bonus), f"RSI={rsi:.0f} (сильная перекупленность)"
        if rsi > 60:   return min(1.0, 0.75 + div_bonus), f"RSI={rsi:.0f} (перекупленность)"
        if rsi > 50:   return 0.4 + div_bonus, f"RSI={rsi:.0f} (нейтральный↑)"
    return 0.0 + div_bonus, None


def _macd_score(df, direction):
    if "MACD" not in df.columns:
        return 0.0, None
    macd      = df["MACD"].iloc[-1]
    sig       = df["MACD_sig"].iloc[-1]
    hist      = df["MACD_hist"].iloc[-1]
    hist_prev = df["MACD_hist"].iloc[-2]
    if direction == "LONG":
        if macd > sig and hist > 0:
            return min(1.0, 0.7 + (0.3 if hist > hist_prev else 0)), f"MACD бычий (hist={hist:.5f})"
        if hist > hist_prev and hist > 0: return 0.4, "MACD гистограмма растёт"
        if macd > sig:                    return 0.3, "MACD выше сигнала"
    if direction == "SHORT":
        if macd < sig and hist < 0:
            return min(1.0, 0.7 + (0.3 if hist < hist_prev else 0)), f"MACD медвежий (hist={hist:.5f})"
        if hist < hist_prev and hist < 0: return 0.4, "MACD гистограмма падает"
        if macd < sig:                    return 0.3, "MACD ниже сигнала"
    return 0.0, None


def _bb_score(df, direction):
    if "BB_upper" not in df.columns:
        return 0.0, None
    close = df["close"].iloc[-1]
    upper = df["BB_upper"].iloc[-1]
    lower = df["BB_lower"].iloc[-1]
    mid   = df["BB_mid"].iloc[-1]
    bb_pos = (close - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
    if direction == "LONG":
        if close <= lower:  return 1.0,  "BB нижняя полоса (пробой)"
        if bb_pos < 0.25:   return 0.75, f"BB нижняя зона ({bb_pos:.0%})"
        if close < mid:     return 0.4,  "BB ниже середины"
    if direction == "SHORT":
        if close >= upper:  return 1.0,  "BB верхняя полоса (пробой)"
        if bb_pos > 0.75:   return 0.75, f"BB верхняя зона ({bb_pos:.0%})"
        if close > mid:     return 0.4,  "BB выше середины"
    return 0.0, None


def _ema_score(df, direction):
    if "EMA_20" not in df.columns or "EMA_50" not in df.columns:
        return 0.0, None
    e20  = df["EMA_20"].iloc[-1]
    e50  = df["EMA_50"].iloc[-1]
    e20p = df["EMA_20"].iloc[-2]
    e50p = df["EMA_50"].iloc[-2]
    close = df["close"].iloc[-1]
    if direction == "LONG":
        if e20 > e50 and e20p <= e50p:  return 1.0,  "EMA20 > EMA50 (golden cross) 🌟"
        if e20 > e50 and close > e20:   return 0.7,  "Цена > EMA20 > EMA50"
        if e20 > e50:                   return 0.5,  "EMA20 > EMA50"
        if close > e20:                 return 0.25, "Цена выше EMA20"
    if direction == "SHORT":
        if e20 < e50 and e20p >= e50p:  return 1.0,  "EMA20 < EMA50 (death cross) 💀"
        if e20 < e50 and close < e20:   return 0.7,  "Цена < EMA20 < EMA50"
        if e20 < e50:                   return 0.5,  "EMA20 < EMA50"
        if close < e20:                 return 0.25, "Цена ниже EMA20"
    return 0.0, None


def _volume_score(df):
    """
    Повышены пороги — x1.2 теперь не считается.
    Только значимые всплески объёма.
    + Проверка пробоя локального хай/лоя.
    """
    if "vol_ratio" not in df.columns:
        return 0.0, None
    vr = df["vol_ratio"].iloc[-1]
    
    # Базовый скор
    if vr >= 4.0:  base_score, base_label = 1.0,  f"Объём x{vr:.1f} 🔥🔥🔥"
    elif vr >= 2.5: base_score, base_label = 0.75, f"Объём x{vr:.1f} 🔥🔥"
    elif vr >= 1.8: base_score, base_label = 0.4,  f"Объём x{vr:.1f} 🔥"
    else: return 0.0, None
    
    # ← Проверка пробоя уровня
    breakout_bonus = 1.0
    if len(df) >= 20 and "high" in df.columns and "low" in df.columns:
        local_high = df["high"].tail(20).max()
        local_low = df["low"].tail(20).min()
        close = df["close"].iloc[-1]
        
        # Пробой вверх с объёмом
        if vr >= 1.8 and close > local_high:
            breakout_bonus = 1.3
        # Пробой вниз с объёмом
        elif vr >= 1.8 and close < local_low:
            breakout_bonus = 1.3
    
    score = min(1.0, base_score * breakout_bonus)
    label = base_label
    if breakout_bonus > 1.0:
        label += " + пробой уровня"
    
    return score, label


def _orderbook_score(ob, direction):
    if not ob:
        return 0.0, None
    imb = ob.get("imbalance", 0)
    if direction == "LONG":
        if imb > 0.25:  return 1.0,  f"Стакан: сильное давление покупки ({imb:+.2f})"
        if imb > 0.10:  return 0.6,  f"Стакан: давление покупки ({imb:+.2f})"
        if imb > 0.05:  return 0.3,  f"Стакан: перевес bid ({imb:+.2f})"
    if direction == "SHORT":
        if imb < -0.25: return 1.0,  f"Стакан: сильное давление продажи ({imb:+.2f})"
        if imb < -0.10: return 0.6,  f"Стакан: давление продажи ({imb:+.2f})"
        if imb < -0.05: return 0.3,  f"Стакан: перевес ask ({imb:+.2f})"
    return 0.0, None


def _news_score(coin_base, direction):
    s = get_coin_sentiment(coin_base)
    if s["count"] == 0:
        return 0.0, None
    val = s["score"]
    top = s.get("top_news", "")
    label = f"📰 {top[:65]}..." if top and len(top) > 65 else f"📰 {top}"
    if direction == "LONG" and val > 0.05:
        return min(1.0, abs(val) + (0.1 if s["count"] >= 3 else 0)), label
    if direction == "SHORT" and val < -0.05:
        return min(1.0, abs(val) + (0.1 if s["count"] >= 3 else 0)), label
    return 0.0, None


def _tf_bias(df: pd.DataFrame) -> str:
    if df.empty or len(df) < 3:
        return "neutral"
    long_s = short_s = 0
    rsi_col = f"RSI_{config.RSI_PERIOD}"
    if "EMA_20" in df.columns and "EMA_50" in df.columns:
        if df["EMA_20"].iloc[-1] > df["EMA_50"].iloc[-1]: long_s  += 2
        else:                                               short_s += 2
    if rsi_col in df.columns:
        rsi = df[rsi_col].iloc[-1]
        if rsi < 45:    long_s  += 2
        elif rsi > 55:  short_s += 2
    if "MACD_hist" in df.columns:
        if df["MACD_hist"].iloc[-1] > 0: long_s  += 1
        else:                             short_s += 1
    if "close" in df.columns and "EMA_20" in df.columns:
        if df["close"].iloc[-1] > df["EMA_20"].iloc[-1]: long_s  += 1
        else:                                              short_s += 1
    if long_s > short_s:   return "long"
    if short_s > long_s:   return "short"
    return "neutral"


def _mtf_score(df_5m, df_15m, df_1h, df_4h, direction: str):
    biases = {
        "5m":  _tf_bias(df_5m),
        "15m": _tf_bias(df_15m),
        "1h":  _tf_bias(df_1h),
        "4h":  _tf_bias(df_4h),
    }
    target  = direction.lower()
    matches = sum(1 for b in biases.values() if b == target)
    if matches == 4: return 1.0,  "МТФ: все 4 таймфрейма согласны ✅✅"
    if matches == 3:
        agreed = [tf for tf, b in biases.items() if b == target]
        return 0.75, f"МТФ: {', '.join(agreed)} согласны"
    if matches == 2:
        agreed = [tf for tf, b in biases.items() if b == target]
        return 0.45, f"МТФ: {', '.join(agreed)} согласны"
    if matches == 1: return 0.2,  "МТФ: только 1 таймфрейм согласен"
    return 0.0, None


def _oi_score(oi: dict, direction: str, price_change: float):
    if not oi or oi.get("oi") == 0:
        return 0.0, None
    oi_change = oi.get("oi_change_pct", 0)
    growing   = oi.get("oi_growing", False)
    falling   = oi.get("oi_falling", False)
    if direction == "LONG":
        if growing and price_change > 0: return 1.0, f"OI растёт +{oi_change:.1f}% + цена ↑"
        if growing:                      return 0.5, f"OI растёт +{oi_change:.1f}%"
        if falling and price_change < 0: return 0.3, "OI падает (шорты закрываются)"
    if direction == "SHORT":
        if growing and price_change < 0: return 1.0, f"OI растёт +{oi_change:.1f}% + цена ↓"
        if growing:                      return 0.5, f"OI растёт +{oi_change:.1f}%"
        if falling and price_change > 0: return 0.3, "OI падает (лонги закрываются)"
    return 0.0, None


def _cvd_score(df: pd.DataFrame, direction: str):
    """
    CVD подтверждает или БЛОКИРУЕТ сигнал.
    Дивергенция = жёсткий стоп.
    """
    cvd = calc_cvd(df, periods=20)

    if direction == "LONG":
        if cvd["divergence_bearish"]:
            return -1.0, None
        if cvd["cvd_rising"] and cvd["cvd_norm"] > 5:
            return 1.0,  f"CVD растёт ({cvd['cvd_norm']:+.1f}%) — покупки усиливаются 📈"
        if cvd["cvd_rising"]:
            return 0.5,  f"CVD положительный ({cvd['cvd_norm']:+.1f}%)"
        if cvd["divergence_bullish"]:
            return 0.7,  "CVD дивергенция: цена ↓ но покупки растут 🔄"

    if direction == "SHORT":
        if cvd["divergence_bullish"]:
            return -1.0, None
        if cvd["cvd_falling"] and cvd["cvd_norm"] < -5:
            return 1.0,  f"CVD падает ({cvd['cvd_norm']:+.1f}%) — продажи усиливаются 📉"
        if cvd["cvd_falling"]:
            return 0.5,  f"CVD отрицательный ({cvd['cvd_norm']:+.1f}%)"
        if cvd["divergence_bearish"]:
            return 0.7,  "CVD дивергенция: цена ↑ но продажи растут 🔄"

    return 0.0, None


def _patterns_score(df_1h: pd.DataFrame, direction: str):
    if df_1h.empty:
        return 0.0, None
    p = detect_candle_patterns(df_1h)
    if direction == "LONG":
        if p["bullish_engulfing"]: return 1.0,  "Bullish Engulfing (1h) 🕯️"
        if p["hammer"]:            return 0.8,  "Hammer (1h) 🔨"
        if p["doji"]:              return 0.2,  "Doji (1h) — нерешительность"
        if p["bearish_pattern"]:   return -0.2, None
    if direction == "SHORT":
        if p["bearish_engulfing"]: return 1.0,  "Bearish Engulfing (1h) 🕯️"
        if p["shooting_star"]:     return 0.8,  "Shooting Star (1h) ⭐"
        if p["doji"]:              return 0.2,  "Doji (1h) — нерешительность"
        if p["bullish_pattern"]:   return -0.2, None
    return 0.0, None


def _btc_corr_score(df_symbol: pd.DataFrame, df_btc: pd.DataFrame, direction: str):
    if df_symbol.empty or df_btc.empty:
        return 0.0, None
    corr_data = calc_btc_correlation(df_symbol, df_btc, periods=20)
    corr      = corr_data.get("correlation", 0)
    high_corr = corr_data.get("high_corr", False)
    if not high_corr:
        return 0.0, None
    if len(df_btc) < 5:
        return 0.0, None
    btc_change = (df_btc["close"].iloc[-1] - df_btc["close"].iloc[-5]) / df_btc["close"].iloc[-5] * 100
    if direction == "LONG" and btc_change > 0.3:
        return min(1.0, btc_change / 2), f"BTC корреляция {corr:.2f}, BTC +{btc_change:.1f}% 📈"
    if direction == "SHORT" and btc_change < -0.3:
        return min(1.0, abs(btc_change) / 2), f"BTC корреляция {corr:.2f}, BTC {btc_change:.1f}% 📉"
    return 0.0, None


def _levels_score(df_15m: pd.DataFrame, direction: str):
    """
    Оценивает близость цены к локальным уровням (хай/лой за 20 свечей).
    Пробой уровня с близостью → высокий скор.
    """
    if df_15m.empty or len(df_15m) < 20:
        return 0.0, None
    if "high" not in df_15m.columns or "low" not in df_15m.columns:
        return 0.0, None
    
    close = df_15m["close"].iloc[-1]
    local_high = df_15m["high"].tail(20).max()
    local_low = df_15m["low"].tail(20).min()
    
    # Расстояние до уровней в %
    dist_to_high = (local_high - close) / close * 100 if close > 0 else 100
    dist_to_low = (close - local_low) / close * 100 if close > 0 else 100
    
    # Была ли цена за уровнем на предыдущей свече
    prev_close = df_15m["close"].iloc[-2] if len(df_15m) >= 2 else close
    
    # LONG: цена у поддержки (local_low) или пробила её
    if direction == "LONG":
        if dist_to_low <= 0.5:
            if prev_close < local_low:
                return 1.0, f"Пробой поддержки {local_low:.4f}"
            return 0.5, f"Цена у поддержки {local_low:.4f}"
    # SHORT: цена у сопротивления (local_high) или пробила его
    elif direction == "SHORT":
        if dist_to_high <= 0.5:
            if prev_close > local_high:
                return 1.0, f"Пробой сопротивления {local_high:.4f}"
            return 0.5, f"Цена у сопротивления {local_high:.4f}"
    
    return 0.0, None


def _apply_fg_filter(direction: str, strength: int) -> int:
    fg  = get_fg()
    val = fg.get("value", 50)
    if direction == "LONG":
        if val <= 24:   strength += 5
        elif val >= 76: strength -= 5
    if direction == "SHORT":
        if val >= 76:   strength += 5
        # Убираем штраф для SHORT при Extreme Fear —
        # низкий F&G это хорошее время для SHORT, не плохое
    return max(0, min(100, strength))


def _calc_dynamic_min_strength(ctx: dict, fg: dict) -> int:
    """
    Динамический порог силы сигнала.
    Адаптируется к состоянию рынка, времени суток и F&G.
    """
    base = config.MIN_SIGNAL_STRENGTH  # из .env

    # Нейтральный рынок — закомментировано чтобы ML мог видеть больше сигналов
    # if ctx["trend_4h"] == "neutral" and ctx["trend_1h"] == "neutral":
    #     base += 5

    # Экстрим F&G → чуть мягче
    fg_val = fg.get("value", 50)
    if fg_val <= 15 or fg_val >= 85:
        base -= 5

    # Ночь UTC (02:00-06:00) — низкая ликвидность → строже
    hour = datetime.now(timezone.utc).hour
    if 2 <= hour <= 6:
        base += 5

    return max(config.MIN_SIGNAL_STRENGTH, min(90, base))


def _direction(df_15m, df_1h, df_4h):
    """
    Определяет направление сигнала.
    v5: 4h тренд обязателен — без него нет сигнала.
    """
    ema4h_bull = None
    if not df_4h.empty and "EMA_20" in df_4h.columns and "EMA_50" in df_4h.columns:
        e20_4h = df_4h["EMA_20"].iloc[-1]
        e50_4h = df_4h["EMA_50"].iloc[-1]
        ema4h_bull = e20_4h > e50_4h
    else:
        return None

    ema1h_bull = None
    if not df_1h.empty and "EMA_20" in df_1h.columns and "EMA_50" in df_1h.columns:
        ema1h_bull = df_1h["EMA_20"].iloc[-1] > df_1h["EMA_50"].iloc[-1]

    rsi_col = f"RSI_{config.RSI_PERIOD}"
    rsi = df_15m[rsi_col].iloc[-1] if rsi_col in df_15m.columns else None

    macd_neg          = False
    macd_turning_down = False
    macd_turning_up   = False
    if "MACD_hist" in df_15m.columns:
        macd_hist = df_15m["MACD_hist"].iloc[-1]
        macd_prev = df_15m["MACD_hist"].iloc[-2] if len(df_15m) > 2 else macd_hist
        macd_neg  = macd_hist < 0
        macd_turning_down = macd_prev > 0 and macd_hist < 0
        macd_turning_up   = macd_prev < 0 and macd_hist > 0

    # Контртрендовый SHORT (перекупленность на бычьем рынке)
    if ema4h_bull and ema1h_bull is True and rsi is not None:
        price_15m  = df_15m["close"].iloc[-1]
        e20_1h_val = (df_1h["EMA_20"].iloc[-1]
                      if not df_1h.empty and "EMA_20" in df_1h.columns else None)
        overextended = False
        if price_15m and e20_1h_val and e20_1h_val > 0:
            dist_pct = (price_15m - e20_1h_val) / e20_1h_val * 100
            overextended = dist_pct > 2.0
        if rsi > 72 and overextended and macd_neg:
            return "SHORT"

    # Контртрендовый LONG (перепроданность на медвежьем рынке)
    if (ema4h_bull is False) and (ema1h_bull is True or ema1h_bull is None) and rsi is not None:
        if rsi < 28 and macd_turning_up:
            return "LONG"

    if ema4h_bull is True:
        return "LONG"
    else:
        return "SHORT"


def _entry_candle_ok(df: pd.DataFrame, mult: float = None) -> bool:
    if "ATR_14" not in df.columns:
        return True
    last_body = abs(df["close"].iloc[-1] - df["open"].iloc[-1])
    atr       = df["ATR_14"].iloc[-1]
    if atr == 0:
        return True
    if mult is None:
        mult = float(getattr(config, "ENTRY_CANDLE_MAX_ATR_MULT", 1.1))
    return last_body < atr * mult


def _adaptive_tp_sl(df: pd.DataFrame, entry: float, direction: str) -> tuple:
    atr = df["ATR_14"].iloc[-1] if "ATR_14" in df.columns else None
    if atr:
        atr_pct = atr / entry * 100
        tp1_p   = max(config.TP1_PERCENT, atr_pct * 1.5)
        tp2_p   = max(config.TP2_PERCENT, atr_pct * 3.0)
        sl_p    = max(config.SL_PERCENT,  atr_pct * 1.2)
        tp1_p   = min(tp1_p, 8.0)
        tp2_p   = min(tp2_p, 15.0)
        sl_p    = min(sl_p,  4.0)
    else:
        tp1_p = config.TP1_PERCENT
        tp2_p = config.TP2_PERCENT
        sl_p  = config.SL_PERCENT

    if direction == "LONG":
        tp1 = entry * (1 + tp1_p / 100)
        tp2 = entry * (1 + tp2_p / 100)
        sl  = entry * (1 - sl_p  / 100)
    else:
        tp1 = entry * (1 - tp1_p / 100)
        tp2 = entry * (1 - tp2_p / 100)
        sl  = entry * (1 + sl_p  / 100)

    return (round(tp1, 6), round(tp2, 6), round(sl, 6),
            round(tp1_p, 2), round(tp2_p, 2), round(sl_p, 2))


# ── Главная функция ───────────────────────────────────────────────────────────

def generate_signal(data: dict):
    from database.db import log_signal_attempt

    symbol  = data["symbol"]
    exchange = data.get("exchange", "binance")  # биржа: binance/bybit/okx
    df_5m   = data["candles_5m"]
    df_15m  = data["candles_15m"]
    df_1h   = data["candles_1h"]
    df_4h   = data.get("candles_4h", pd.DataFrame())
    df_btc  = data.get("btc_candles_1h", pd.DataFrame())
    ob      = data["orderbook"]
    pi      = data["price_info"]
    oi      = data.get("open_interest", {})

    if df_15m.empty or not pi.get("price"):
        return None

    price = pi.get("price", 1)

    # ── Фильтр высокой волатильности (ATR) — адаптивный ─────────────────
    # При сильном падении рынка (BTC -5%+) ATR растёт у всех монет.
    # Жёсткий порог 3% блокирует всё — поднимаем до 5% в медвежьем контексте.
    if "ATR_14" in df_15m.columns and not df_15m["ATR_14"].isna().all():
        atr_pct = df_15m["ATR_14"].iloc[-1] / price * 100 if price > 0 else 0
        _ctx_early = get_cached_context()
        _btc_drop = abs(min(_ctx_early.get("change_4h_pct", 0), 0))
        # При BTC -3%+ разрешаем ATR до 5%, при -5%+ до 6%
        _atr_limit = 3.0
        if _btc_drop >= 5.0:
            _atr_limit = 6.0
        elif _btc_drop >= 3.0:
            _atr_limit = 5.0
        if atr_pct > _atr_limit:
            log_signal_attempt(symbol, "N/A", 0, f"ATR {atr_pct:.1f}% > {_atr_limit:.0f}% (волатильно)")
            return None

    # ── Направление ───────────────────────────────────────────────────────
    anchor_dir = _direction(df_15m, df_1h, df_4h)
    if anchor_dir is None:
        log_signal_attempt(symbol, "N/A", 0, "Нет данных 4h для направления")
        return None

    # ── Рыночный контекст BTC ─────────────────────────────────────────────
    ctx = get_cached_context()

    if anchor_dir == "LONG"  and ctx.get("block_long"):
        log_signal_attempt(symbol, "LONG", 0, f"BTC блок LONG: {ctx.get('reason','')}")
        return None
    if anchor_dir == "SHORT" and ctx.get("block_short"):
        log_signal_attempt(symbol, "SHORT", 0, f"BTC блок SHORT: {ctx.get('reason','')}")
        return None

    def _short_boost_for_ctx(ctx_: dict) -> int:
        trend_4h = ctx_.get("trend_4h", "neutral")
        trend_1h = ctx_.get("trend_1h", "neutral")
        if trend_4h == "bear" and trend_1h == "bear":       return 0
        if trend_4h == "bear" or trend_1h == "bear":        return 5
        if trend_4h == "neutral" and trend_1h == "neutral": return 6
        return 10

    asian_boost  = 5 if ctx.get("asian_penalty") else 0
    coin_base    = symbol.replace("USDT", "")
    price_change = pi.get("change_24h_pct", 0)

    # ── Funding Rate — ОТКЛЮЧЕНО (для скальпинга нужно много сигналов) ────
    # funding = get_funding_timer()
    # if funding.get("avoid_entry"):
    #     log_signal_attempt(symbol, anchor_dir, 0, f"Funding через {funding['minutes_to_funding']}м")
    #     return None

    # ── Проверка входной свечи — адаптивная ──────────────────────────────
    # При падающем рынке все свечи большие. Используем динамический мультипликатор.
    _btc_drop_pct = abs(min(ctx.get("change_4h_pct", 0), 0))
    if _btc_drop_pct >= 4.0:
        _candle_mult = 3.5   # сильное падение — очень мягко
    elif _btc_drop_pct >= 2.0:
        _candle_mult = 2.5   # умеренное падение — мягче
    else:
        _candle_mult = float(getattr(config, "ENTRY_CANDLE_MAX_ATR_MULT", 1.1))
    if not _entry_candle_ok(df_15m, _candle_mult):
        log_signal_attempt(symbol, anchor_dir, 0,
                           f"Входная свеча > {_candle_mult:.1f}x ATR (уже проехали)")
        return None

    def _score_for_direction(direction: str):
        scores, reasons = {}, []
        blocked = False
        block_reason = None
        for key, fn, args in [
            ("rsi",       _rsi_score,       (df_15m, direction)),
            ("macd",      _macd_score,      (df_15m, direction)),
            ("bb",        _bb_score,        (df_15m, direction)),
            ("ema",       _ema_score,       (df_15m, direction)),
            ("volume",    _volume_score,    (df_15m,)),
            ("orderbook", _orderbook_score, (ob, direction)),
            ("news",      _news_score,      (coin_base, direction)),
            ("mtf",       _mtf_score,       (df_5m, df_15m, df_1h, df_4h, direction)),
            ("oi",        _oi_score,        (oi, direction, price_change)),
            ("cvd",       _cvd_score,       (df_15m, direction)),
            ("patterns",  _patterns_score,  (df_1h, direction)),
            ("btc_corr",  _btc_corr_score,  (df_1h, df_btc, direction)),
            ("levels",    _levels_score,    (df_15m, direction)),  # ← новый индикатор
        ]:
            score, reason = fn(*args)
            scores[key] = score
            if reason and score > 0:
                reasons.append(reason)

        if scores.get("cvd") == -1.0:
            blocked = True
            block_reason = "CVD дивергенция (hard stop)"

        heavy_confirmed = sum(
            1 for k in HEAVY_INDICATORS
            if scores.get(k, 0) >= HEAVY_MIN_SCORE
        )

        strength = int(round(sum(
            max(scores[k], 0) * WEIGHTS[k]
            for k in scores if k in WEIGHTS
        )))
        strength = _apply_fg_filter(direction, strength)
        return strength, scores, reasons, heavy_confirmed, blocked, block_reason

    strength_long,  scores_long,  reasons_long,  heavy_long,  blocked_long,  block_reason_long  = _score_for_direction("LONG")
    strength_short, scores_short, reasons_short, heavy_short, blocked_short, block_reason_short = _score_for_direction("SHORT")

    if blocked_long and blocked_short:
        log_signal_attempt(symbol, "N/A", 0, "CVD hard-stop в обе стороны")
        return None

    DIR_MARGIN = 8
    if (not blocked_long) and (blocked_short or strength_long >= strength_short + DIR_MARGIN):
        direction = "LONG"
        strength, scores, reasons, heavy_confirmed = strength_long, scores_long, reasons_long, heavy_long
    elif (not blocked_short) and (blocked_long or strength_short >= strength_long + DIR_MARGIN):
        direction = "SHORT"
        strength, scores, reasons, heavy_confirmed = strength_short, scores_short, reasons_short, heavy_short
    else:
        direction = anchor_dir
        if direction == "LONG":
            if blocked_long:
                log_signal_attempt(symbol, "LONG", strength_long, block_reason_long or "CVD hard-stop")
                return None
            strength, scores, reasons, heavy_confirmed = strength_long, scores_long, reasons_long, heavy_long
        else:
            if blocked_short:
                log_signal_attempt(symbol, "SHORT", strength_short, block_reason_short or "CVD hard-stop")
                return None
            strength, scores, reasons, heavy_confirmed = strength_short, scores_short, reasons_short, heavy_short

    # ── Динамический порог ────────────────────────────────────────────────
    fg = get_fg()
    short_boost   = _short_boost_for_ctx(ctx) if direction == "SHORT" else 0
    effective_min = (_calc_dynamic_min_strength(ctx, fg) + asian_boost + short_boost)
    effective_min = min(effective_min, 85)

    # ── Heavy фильтр отключён — ML берёт на себя фильтрацию ─────────────
    # Индикаторы всё равно считаются и влияют на strength и ML фичи.
    # if heavy_confirmed < 1:
    #     log_signal_attempt(symbol, direction, strength,
    #                        f"Heavy подтверждений <1 (есть {heavy_confirmed})")
    #     return None

    if strength < effective_min:
        log_signal_attempt(symbol, direction, strength,
                           f"Сила {strength} < порог {effective_min}")
        return None

    # ── Предупреждение о funding ──────────────────────────────────────────
    if funding.get("near_funding"):
        reasons.append(f"⚠️ Funding через {funding['minutes_to_funding']}м")

    # ── BTC контекст в причины ────────────────────────────────────────────
    btc_change_4h = ctx.get("change_4h_pct", 0)
    if direction == "LONG"  and btc_change_4h > 1.5:
        reasons.append(f"BTC контекст: +{btc_change_4h:.1f}% за 12ч 📈")
    elif direction == "SHORT" and btc_change_4h < -1.5:
        reasons.append(f"BTC контекст: {btc_change_4h:.1f}% за 12ч 📉")

    current_price = pi["price"]

    # ── Вычисляем ATR percentile (для ML) ──────────────────────────────────
    # ATR percentile — насколько текущая волатильность выше нормы (0.0-1.0)
    # 1.0 = ATR сейчас максимальный за 20 свечей, 0.0 = минимальный
    _atr_percentile = 0.5  # дефолт — середина (нейтрально)
    try:
        if "ATR_14" in df_15m.columns:
            _atr_s = df_15m["ATR_14"].tail(20).dropna()
            if len(_atr_s) >= 5:
                _atr_mn, _atr_mx = _atr_s.min(), _atr_s.max()
                if _atr_mx > _atr_mn:
                    _atr_percentile = round(
                        (_atr_s.iloc[-1] - _atr_mn) / (_atr_mx - _atr_mn), 4
                    )
    except Exception:
        pass

    # ── Вычисляем BTC drawdown (для ML) ────────────────────────────────────
    # BTC drawdown от локального хая за последние 24ч (%)
    # 0.0 = BTC на хае, 5.0 = BTC просел на 5% от хая (паника)
    _btc_drawdown_pct = 0.0  # дефолт — BTC на хае
    try:
        if not df_btc.empty and "close" in df_btc.columns:
            _btc_r = df_btc["close"].tail(24)
            if len(_btc_r) >= 5:
                _btc_high = _btc_r.max()
                if _btc_high > 0:
                    _btc_drawdown_pct = round(
                        (_btc_high - _btc_r.iloc[-1]) / _btc_high * 100, 3
                    )
    except Exception:
        pass

    # Режим рынка — вычисляем ДО ML фильтра
    _fg_val  = fg.get("value", 50)
    _trend4h = ctx.get("trend_4h", "neutral")
    if _trend4h == "bull" and _fg_val >= 45:
        _market_regime = "bull"
    elif _trend4h == "bear" and _fg_val <= 55:
        _market_regime = "bear"
    elif _trend4h == "bull" and _fg_val < 40:
        _market_regime = "fear_bull"
    elif _trend4h == "bear" and _fg_val > 60:
        _market_regime = "greed_bear"
    else:
        _market_regime = "flat"

    # ── Умный лимитный вход (откат к EMA20) ──────────────────────────────
    entry      = current_price
    entry_type = "market"

    if "EMA_20" in df_15m.columns and not df_15m["EMA_20"].isna().all():
        ema20 = df_15m["EMA_20"].iloc[-1]
        if ema20 and ema20 > 0:
            dist_pct = abs(current_price - ema20) / current_price * 100

            # Логика входа:
            # SHORT — всегда рыночный (цена уже идёт вниз, ждать откат = пропустить)
            # LONG сильный (MTF >= 0.45, 2+ таймфрейма согласны) — рыночный
            # LONG слабый (MTF < 0.45) — лимитный откат к EMA20 (ждём подтверждения)
            if direction == "SHORT":
                entry_type = "market"
            elif direction == "LONG":
                mtf_score = scores.get("mtf", 0)
                if mtf_score >= 0.45:
                    # Достаточно подтверждений — входим по рынку немедленно
                    entry_type = "market"
                    if mtf_score >= 0.75:
                        reasons.append("⚡ Рыночный вход (MTF сильный)")
                    else:
                        reasons.append("⚡ Рыночный вход (MTF 2+ ТФ)")
                elif current_price > ema20 and dist_pct > 0.3:
                    # MTF слабый — ждём откат к EMA20
                    entry      = round(ema20 * 1.0005, 8)
                    entry_type = "limit"

    if entry_type == "limit":
        reasons.append(f"📌 Лимитный вход @ {entry:.6g} (откат к EMA20)")

    tp1, tp2, sl, tp1_p, tp2_p, sl_p = _adaptive_tp_sl(df_15m, entry, direction)

    # ── Anti-EXPIRED фильтр ───────────────────────────────────────────────
    if "ATR_14" in df_15m.columns and current_price > 0:
        try:
            atr_pct_now = float(df_15m["ATR_14"].iloc[-1] / current_price * 100)
        except Exception:
            atr_pct_now = 0.0
        if atr_pct_now and atr_pct_now < config.MIN_ATR_PCT_FOR_SIGNAL:
            log_signal_attempt(symbol, direction, strength,
                               f"ATR {atr_pct_now:.2f}% < {config.MIN_ATR_PCT_FOR_SIGNAL:.2f}%")
            return None
        if atr_pct_now and tp1_p > atr_pct_now * config.MAX_TP1_ATR_MULT_FOR_SIG:
            log_signal_attempt(symbol, direction, strength,
                               f"TP1 {tp1_p:.2f}% > ATR*{config.MAX_TP1_ATR_MULT_FOR_SIG:.2f}")
            return None

    # ── Логируем успешный сигнал ──────────────────────────────────────────
    log_signal_attempt(symbol, direction, strength, "✅ Сигнал сгенерирован", passed=True)

    # ── Числовые фичи для ML ─────────────────────────────────────────────
    rsi_col = f"RSI_{config.RSI_PERIOD}"
    feat_rsi          = float(df_15m[rsi_col].iloc[-1]) if rsi_col in df_15m.columns else None
    feat_macd_hist    = float(df_15m["MACD_hist"].iloc[-1]) if "MACD_hist" in df_15m.columns else None
    feat_bb_position  = None
    if "BB_upper" in df_15m.columns:
        upper = df_15m["BB_upper"].iloc[-1]
        lower = df_15m["BB_lower"].iloc[-1]
        if (upper - lower) > 0:
            feat_bb_position = round((current_price - lower) / (upper - lower), 4)
    feat_ema_ratio    = None
    if "EMA_20" in df_15m.columns and "EMA_50" in df_15m.columns:
        e50 = df_15m["EMA_50"].iloc[-1]
        if e50 > 0:
            feat_ema_ratio = round(df_15m["EMA_20"].iloc[-1] / e50, 6)
    feat_volume_ratio = float(df_15m["vol_ratio"].iloc[-1]) if "vol_ratio" in df_15m.columns else None
    feat_atr_pct      = None
    if "ATR_14" in df_15m.columns and current_price > 0:
        feat_atr_pct = round(df_15m["ATR_14"].iloc[-1] / current_price * 100, 4)

    from data.binance_client import calc_cvd as _calc_cvd_raw
    _cvd_raw      = _calc_cvd_raw(df_15m, periods=20)
    feat_cvd_norm = _cvd_raw.get("cvd_norm", 0)

    feat_ob_imbalance = ob.get("imbalance", 0) if ob else 0

    from data.binance_client import calc_btc_correlation as _calc_corr
    _corr         = _calc_corr(df_1h, df_btc, periods=20)
    feat_btc_corr = _corr.get("correlation", 0)

    # ── ML-фильтр ────────────────────────────────────────────────────────
    if getattr(config, "ML_FILTER_ENABLED", True):
        try:
            from ml.model import predict_win_prob
            from database.db import get_conn as _ml_gc

            _ml_min_rows = int(getattr(config, "ML_FILTER_MIN_ROWS", 200))
            _ml_conn = _ml_gc()
            _ml_cur  = _ml_conn.cursor()
            _ml_cur.execute("""
                SELECT COUNT(*) FROM ml_features
                WHERE labeled = 1
                  AND COALESCE(target,'') NOT IN ('EXPIRED','NOT_FILLED')
            """)
            _ml_rows = _ml_cur.fetchone()[0] or 0
            _ml_conn.close()

            if _ml_rows >= _ml_min_rows:
                _now = datetime.utcnow()
                _ml_features = {
                    "rsi":             feat_rsi,
                    "macd_hist":       feat_macd_hist,
                    "bb_position":     feat_bb_position,
                    "ema20_50_ratio":  feat_ema_ratio,
                    "volume_ratio":    feat_volume_ratio,
                    "atr_pct":         feat_atr_pct,
                    "cvd_norm":        feat_cvd_norm,
                    "oi_change_pct":   oi.get("oi_change_pct", 0),
                    "ob_imbalance":    feat_ob_imbalance,
                    "btc_correlation": feat_btc_corr,
                    "score_rsi":       scores.get("rsi"),
                    "score_macd":      scores.get("macd"),
                    "score_bb":        scores.get("bb"),
                    "score_ema":       scores.get("ema"),
                    "score_volume":    scores.get("volume"),
                    "score_orderbook": scores.get("orderbook"),
                    "score_news":      scores.get("news"),
                    "score_mtf":       scores.get("mtf"),
                    "score_oi":        scores.get("oi"),
                    "score_cvd":       scores.get("cvd"),
                    "score_patterns":  scores.get("patterns"),
                    "score_btc_corr":  scores.get("btc_corr"),
                    "score_levels":    scores.get("levels"),  # ← новый индикатор
                    "strength":        strength,
                    "heavy_confirmed": heavy_confirmed,
                    "fear_greed":      fg.get("value", 50),
                    "funding_rate":    pi.get("funding_rate"),
                    "btc_change_4h":   ctx.get("change_4h_pct", 0),
                    "hour_of_day":     _now.hour,
                    "day_of_week":     _now.weekday(),
                    "atr_percentile":  _atr_percentile or 0.5,
                    "btc_drawdown_pct": _btc_drawdown_pct or 0.0,
                    "direction":       direction,
                    "session":         ctx.get("session", ""),
                    "btc_trend_4h":    ctx.get("trend_4h", "neutral"),
                    "btc_trend_1h":    ctx.get("trend_1h", "neutral"),
                    "entry_type":      entry_type,
                    "market_regime":   _market_regime,
                }
                _ml_min_prob = float(getattr(config, "ML_FILTER_MIN_PROB", 0.52))
                # При сильном падении рынка модель занижает вероятности SHORT-сигналов
                # (обучена на спокойном рынке). Временно снижаем порог для SHORT.
                _btc_drop_now = abs(min(ctx.get("change_4h_pct", 0), 0))
                if direction == "SHORT" and _btc_drop_now >= 3.0:
                    _ml_min_prob = max(0.38, _ml_min_prob - 0.12)
                elif _btc_drop_now >= 2.0:
                    _ml_min_prob = max(0.40, _ml_min_prob - 0.08)
                _win_prob    = predict_win_prob(_ml_features)

                if _win_prob is not None:
                    if _win_prob < _ml_min_prob:
                        log_signal_attempt(
                            symbol, direction, strength,
                            f"ML: вероятность {_win_prob:.2f} < порог {_ml_min_prob:.2f}"
                        )
                        print(f"[Scanner] 🤖 ML отклонил {symbol} {direction} "
                              f"(prob={_win_prob:.2f} < {_ml_min_prob:.2f})")
                        return None
                    else:
                        reasons.append(f"🤖 ML: вероятность {_win_prob:.0%}")
                        print(f"[Scanner] 🤖 ML одобрил {symbol} {direction} "
                              f"(prob={_win_prob:.2f})")
                else:
                    print(f"[ML-filter] {symbol}: predict вернул None — пропускаем фильтр")
            else:
                print(f"[ML-filter] Мало данных: {_ml_rows} < {_ml_min_rows} — фильтр не активен")

        except Exception as _ml_err:
            import traceback
            print(f"[ML-filter] Ошибка: {_ml_err}")
            traceback.print_exc()

    # ── НОВЫЕ фичи v2: время + рыночный контекст ──────────────────────────────
    _now_utc = datetime.now(timezone.utc)

    # UTC час (0-23) — ликвидность сильно зависит от времени суток
    _hour_of_day = _now_utc.hour

    # День недели (0=пн, 4=пт, 6=вс)
    _day_of_week = _now_utc.weekday()

    # Режим рынка уже вычислен выше (перед ML фильтром)
    # _market_regime готов к использованию

    return {
        "symbol":            symbol,
        "exchange":          exchange,
        "direction":         direction,
        "entry_price":       entry,
        "current_price":     current_price,
        "entry_type":        entry_type,
        "tp1":               tp1,
        "tp2":               tp2,
        "sl":                sl,
        "strength":          strength,
        "reasons":           reasons,
        "news_title":        get_coin_sentiment(coin_base).get("top_news"),
        "tp1_pct":           tp1_p,
        "tp2_pct":           tp2_p,
        "sl_pct":            sl_p,
        "volume_24h":        pi.get("volume_24h"),
        "change_24h":        pi.get("change_24h_pct"),
        "funding_rate":      pi.get("funding_rate"),
        "oi_change_pct":     oi.get("oi_change_pct", 0),
        "fear_greed":        fg.get("value", 50),
        "fear_greed_label":  fg.get("label", ""),
        "session":           ctx.get("session", ""),
        "btc_trend":         ctx.get("trend_4h", "neutral"),
        "btc_trend_1h":      ctx.get("trend_1h", "neutral"),
        "btc_change_4h":     ctx.get("change_4h_pct", 0),
        "scores":            {k: round(v, 3) for k, v in scores.items()},
        "heavy_confirmed":   heavy_confirmed,
        "rsi":               feat_rsi,
        "macd_hist":         feat_macd_hist,
        "bb_position":       feat_bb_position,
        "ema20_50_ratio":    feat_ema_ratio,
        "volume_ratio":      feat_volume_ratio,
        "atr_pct":           feat_atr_pct,
        "cvd_norm":          feat_cvd_norm,
        "ob_imbalance":      feat_ob_imbalance,
        "btc_correlation":   feat_btc_corr,
        "score_rsi":         scores.get("rsi"),
        "score_macd":        scores.get("macd"),
        "score_bb":          scores.get("bb"),
        "score_ema":         scores.get("ema"),
        "score_volume":      scores.get("volume"),
        "score_orderbook":   scores.get("orderbook"),
        "score_news":        scores.get("news"),
        "score_mtf":         scores.get("mtf"),
        "score_oi":          scores.get("oi"),
        "score_cvd":         scores.get("cvd"),
        "score_patterns":    scores.get("patterns"),
        "score_btc_corr":    scores.get("btc_corr"),
        "score_levels":      scores.get("levels"),  # ← новый индикатор
        "btc_trend_4h":      ctx.get("trend_4h", "neutral"),
        "btc_trend_1h":      ctx.get("trend_1h", "neutral"),
        "feat_rsi":          feat_rsi,
        "feat_macd_hist":    feat_macd_hist,
        "feat_bb_position":  feat_bb_position,
        "feat_ema_ratio":    feat_ema_ratio,
        "feat_volume_ratio": feat_volume_ratio,
        "feat_atr_pct":      feat_atr_pct,
        "feat_cvd_norm":     feat_cvd_norm,
        "feat_ob_imbalance": feat_ob_imbalance,
        "feat_btc_corr":     feat_btc_corr,
        # ── Новые фичи v2 ─────────────────────────────────────────────────────
        "hour_of_day":       _hour_of_day,
        "day_of_week":       _day_of_week,
        "market_regime":     _market_regime,
        "atr_percentile":    _atr_percentile,
        "btc_drawdown_pct":  _btc_drawdown_pct,
    }