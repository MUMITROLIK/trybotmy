"""
Бэктест v2 — исправленная логика
Изменения vs v1:
  - ATR-based SL/TP вместо фиксированных процентов
  - Подтверждение разворота для RSI (свеча закрылась выше/ниже предыдущей)
  - Тест на нескольких периодах: bull / bear / sideways
  - Правильная обработка "свеча пробила и TP и SL" (смотрим направление открытия)
  - Combo-тест встроен, запускается флагом --combo
  - Меньше сигналов но намного чище

Запуск:
  py -3.11 analysis/backtest.py              # базовый бэктест
  py -3.11 analysis/backtest.py --combo      # тест комбинаций
  py -3.11 analysis/backtest.py --period all # все периоды подряд
"""

import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from data.binance_client import get_candles_async, _rsi, _ema, _macd, _bbands, _atr
import config

# ── Параметры ─────────────────────────────────────────────────────────────────

SYMBOLS  = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
            "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "ADAUSDT", "DOTUSDT"]
INTERVAL = "15m"
LIMIT    = 500   # ~500 свечей на период

# TP/SL множители от ATR
ATR_TP1  = 2.0   # TP1 = 2x ATR
ATR_TP2  = 4.0   # TP2 = 4x ATR
ATR_SL   = 1.2   # SL  = 1.2x ATR

# Минимальные значения если ATR слишком мал
MIN_TP1  = 1.5
MIN_SL   = 0.8

HOLD_MAX = 32    # максимум свечей удержания (~8 часов на 15m)

# Исторические периоды для тестирования
# endTime в миллисекундах (Unix timestamp * 1000)
PERIODS = {
    "bull_2024":     {"label": "📈 Бычий (окт-ноя 2024)",   "endTime": 1733000000000},
    "sideways_2024": {"label": "➡️  Боковик (сен 2024)",     "endTime": 1727000000000},
    "bear_2025":     {"label": "📉 Медвежий (фев-мар 2025)", "endTime": 1741000000000},
    "current":       {"label": "🔴 Текущий момент",           "endTime": None},
}


# ── Загрузка свечей с нужным периодом ─────────────────────────────────────────

async def get_candles_period(symbol: str, end_time=None) -> pd.DataFrame:
    """Загружает свечи заканчивающиеся на end_time (или текущие если None)."""
    import data.binance_client as bc
    session = await bc._get_session()
    params = {"symbol": symbol, "interval": INTERVAL, "limit": LIMIT}
    if end_time:
        params["endTime"] = end_time
    try:
        async with session.get("/fapi/v1/klines", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        df = pd.DataFrame(data, columns=[
            "timestamp","open","high","low","close","volume",
            "close_time","quote_vol","trades","taker_buy_base","taker_buy_quote","ignore",
        ])
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)

        # Добавляем индикаторы
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
    except Exception as e:
        print(f"[Binance] {symbol}: {e}")
        return pd.DataFrame()


# ── Проверка исхода сделки (ATR-based) ────────────────────────────────────────

def _check_outcome(df: pd.DataFrame, idx: int, direction: str) -> tuple[str, float]:
    """
    Проверяет исход сделки с ATR-based TP/SL.
    Возвращает (статус, pnl_pct).
    Статус: 'TP1', 'TP2', 'SL', 'HOLD'

    Улучшение vs v1: если свеча пробила и TP и SL — смотрим на open свечи
    чтобы определить что было раньше (heuristic).
    """
    entry = df["close"].iloc[idx]
    atr   = df["ATR_14"].iloc[idx]

    if pd.isna(atr) or atr == 0:
        atr = entry * 0.005  # fallback: 0.5%

    atr_pct = atr / entry * 100
    tp1_p   = max(MIN_TP1, atr_pct * ATR_TP1)
    tp2_p   = max(MIN_TP1 * 2, atr_pct * ATR_TP2)
    sl_p    = max(MIN_SL,  atr_pct * ATR_SL)

    if direction == "LONG":
        tp1 = entry * (1 + tp1_p / 100)
        tp2 = entry * (1 + tp2_p / 100)
        sl  = entry * (1 - sl_p  / 100)
    else:
        tp1 = entry * (1 - tp1_p / 100)
        tp2 = entry * (1 - tp2_p / 100)
        sl  = entry * (1 + sl_p  / 100)

    future = df.iloc[idx+1 : idx+1+HOLD_MAX]
    for _, row in future.iterrows():
        h, l, o = row["high"], row["low"], row["open"]

        if direction == "LONG":
            # Если свеча пробила и TP и SL — смотрим открытие
            if h >= tp2 and l <= sl:
                # Открытие ближе к SL → SL сработал первым
                if abs(o - sl) < abs(o - tp2):
                    return "SL", round(-sl_p, 2)
                return "TP2", round(tp2_p, 2)
            if h >= tp2: return "TP2", round(tp2_p, 2)
            if h >= tp1 and l <= sl:
                if abs(o - sl) < abs(o - tp1):
                    return "SL", round(-sl_p, 2)
                return "TP1", round(tp1_p, 2)
            if h >= tp1: return "TP1", round(tp1_p, 2)
            if l  <= sl: return "SL",  round(-sl_p, 2)
        else:
            if l <= tp2 and h >= sl:
                if abs(o - sl) < abs(o - tp2):
                    return "SL", round(-sl_p, 2)
                return "TP2", round(tp2_p, 2)
            if l  <= tp2: return "TP2", round(tp2_p, 2)
            if l  <= tp1 and h >= sl:
                if abs(o - sl) < abs(o - tp1):
                    return "SL", round(-sl_p, 2)
                return "TP1", round(tp1_p, 2)
            if l  <= tp1: return "TP1", round(tp1_p, 2)
            if h  >= sl:  return "SL",  round(-sl_p, 2)

    return "HOLD", 0.0


# ── Индикаторы с улучшенной логикой ───────────────────────────────────────────

def test_rsi(df: pd.DataFrame) -> dict:
    """
    RSI v2: требует подтверждение разворота.
    LONG: RSI < 30 И текущая свеча закрылась ВЫШЕ предыдущей (первый зелёный)
    SHORT: RSI > 70 И текущая свеча закрылась НИЖЕ предыдущей
    """
    results = {"LONG": [], "SHORT": []}
    col = f"RSI_{config.RSI_PERIOD}"
    if col not in df.columns:
        return results

    for i in range(50, len(df) - HOLD_MAX):
        rsi   = df[col].iloc[i]
        close = df["close"].iloc[i]
        prev  = df["close"].iloc[i-1]

        # LONG: RSI перепродан + разворотная свеча вверх
        if rsi < 30 and close > prev:
            outcome, pnl = _check_outcome(df, i, "LONG")
            results["LONG"].append((outcome, pnl))

        # SHORT: RSI перекуплен + разворотная свеча вниз
        elif rsi > 70 and close < prev:
            outcome, pnl = _check_outcome(df, i, "SHORT")
            results["SHORT"].append((outcome, pnl))

    return results


def test_macd(df: pd.DataFrame) -> dict:
    """
    MACD v2: только чистые кроссоверы (пересечение произошло на ЭТОЙ свече).
    """
    results = {"LONG": [], "SHORT": []}
    if "MACD" not in df.columns:
        return results

    for i in range(50, len(df) - HOLD_MAX):
        hist      = df["MACD_hist"].iloc[i]
        hist_prev = df["MACD_hist"].iloc[i-1]
        macd      = df["MACD"].iloc[i]
        sig       = df["MACD_sig"].iloc[i]

        # Чистый кроссовер вверх: гистограмма только что стала положительной
        if hist > 0 and hist_prev <= 0 and macd > sig:
            outcome, pnl = _check_outcome(df, i, "LONG")
            results["LONG"].append((outcome, pnl))

        # Чистый кроссовер вниз
        elif hist < 0 and hist_prev >= 0 and macd < sig:
            outcome, pnl = _check_outcome(df, i, "SHORT")
            results["SHORT"].append((outcome, pnl))

    return results


def test_bb(df: pd.DataFrame) -> dict:
    """
    BB v2: пробой полосы + возврат внутрь (отскок подтверждён).
    Не входим при касании — ждём закрытия свечи обратно внутри полосы.
    """
    results = {"LONG": [], "SHORT": []}
    if "BB_lower" not in df.columns:
        return results

    for i in range(50, len(df) - HOLD_MAX):
        close  = df["close"].iloc[i]
        lower  = df["BB_lower"].iloc[i]
        upper  = df["BB_upper"].iloc[i]
        prev_c = df["close"].iloc[i-1]

        # Предыдущая свеча была ниже нижней полосы, текущая вернулась внутрь
        if prev_c < lower and close > lower:
            outcome, pnl = _check_outcome(df, i, "LONG")
            results["LONG"].append((outcome, pnl))

        # Предыдущая свеча была выше верхней полосы, текущая вернулась внутрь
        elif prev_c > upper and close < upper:
            outcome, pnl = _check_outcome(df, i, "SHORT")
            results["SHORT"].append((outcome, pnl))

    return results


def test_ema_cross(df: pd.DataFrame) -> dict:
    """EMA 20/50 кроссовер — логика та же, уже была нормальная."""
    results = {"LONG": [], "SHORT": []}
    if "EMA_20" not in df.columns:
        return results

    for i in range(55, len(df) - HOLD_MAX):
        e20      = df["EMA_20"].iloc[i]
        e50      = df["EMA_50"].iloc[i]
        e20_prev = df["EMA_20"].iloc[i-1]
        e50_prev = df["EMA_50"].iloc[i-1]

        if e20 > e50 and e20_prev <= e50_prev:
            outcome, pnl = _check_outcome(df, i, "LONG")
            results["LONG"].append((outcome, pnl))
        elif e20 < e50 and e20_prev >= e50_prev:
            outcome, pnl = _check_outcome(df, i, "SHORT")
            results["SHORT"].append((outcome, pnl))

    return results


def test_volume_spike(df: pd.DataFrame) -> dict:
    """
    Volume v2: объёмный спайк + направление определяется по телу свечи,
    не просто close > prev_close. Требуем тело свечи > 0.3 ATR.
    """
    results = {"LONG": [], "SHORT": []}
    if "vol_ratio" not in df.columns:
        return results

    for i in range(25, len(df) - HOLD_MAX):
        vr    = df["vol_ratio"].iloc[i]
        close = df["close"].iloc[i]
        open_ = df["open"].iloc[i]
        atr   = df["ATR_14"].iloc[i]

        if vr < 2.0:
            continue

        body = abs(close - open_)
        if pd.isna(atr) or body < atr * 0.3:  # слабая свеча — пропускаем
            continue

        if close > open_:  # бычья свеча
            outcome, pnl = _check_outcome(df, i, "LONG")
            results["LONG"].append((outcome, pnl))
        else:              # медвежья свеча
            outcome, pnl = _check_outcome(df, i, "SHORT")
            results["SHORT"].append((outcome, pnl))

    return results


# ── Статистика ────────────────────────────────────────────────────────────────

def _stats(outcomes: list) -> dict:
    """Считает winrate, avg pnl, expectancy."""
    total = len(outcomes)
    if total == 0:
        return {"wr": 0, "wins": 0, "total": 0, "avg_pnl": 0, "expectancy": 0}

    wins   = sum(1 for o, _ in outcomes if o in ("TP1", "TP2"))
    pnls   = [p for _, p in outcomes]
    avg    = round(sum(pnls) / total, 2)
    win_pnl  = [p for o, p in outcomes if o in ("TP1","TP2")]
    loss_pnl = [p for o, p in outcomes if o == "SL"]
    avg_win  = round(sum(win_pnl)/len(win_pnl), 2) if win_pnl else 0
    avg_loss = round(sum(loss_pnl)/len(loss_pnl), 2) if loss_pnl else 0
    exp = round((wins/total) * avg_win + ((total-wins)/total) * avg_loss, 2) if total > 0 else 0

    return {
        "wr":         round(wins / total * 100, 1),
        "wins":       wins,
        "total":      total,
        "avg_pnl":    avg,
        "avg_win":    avg_win,
        "avg_loss":   avg_loss,
        "expectancy": exp,   # ожидаемый P&L на сделку
    }


def _print_indicator(name: str, results: dict):
    l = _stats(results.get("LONG", []))
    s = _stats(results.get("SHORT", []))
    all_out = results.get("LONG", []) + results.get("SHORT", [])
    a = _stats(all_out)

    status = "✅" if a["wr"] >= 50 else "⚠️ " if a["wr"] >= 38 else "❌"
    exp_s  = f"{a['expectancy']:+.2f}%" if a["total"] > 0 else "—"

    print(f"\n  {name}  {status}")
    print(f"    LONG:   {l['wr']:5.1f}% WR  avg={l['avg_pnl']:+.2f}%  ({l['wins']}/{l['total']})")
    print(f"    SHORT:  {s['wr']:5.1f}% WR  avg={s['avg_pnl']:+.2f}%  ({s['wins']}/{s['total']})")
    print(f"    ИТОГО:  {a['wr']:5.1f}% WR  expectancy={exp_s}  ({a['total']} сигналов)")
    return {**a, "name": name}


# ── Основной бэктест ──────────────────────────────────────────────────────────

async def run_backtest(period_key: str = "current"):
    import data.binance_client as bc
    bc._session = None

    period  = PERIODS.get(period_key, PERIODS["current"])
    end_ts  = period.get("endTime")
    label   = period.get("label", period_key)

    print("=" * 65)
    print(f"БЭКТЕСТ v2  |  {INTERVAL}  |  {LIMIT} баров  |  ATR-based TP/SL")
    print(f"Период: {label}")
    print(f"Монеты: {', '.join(SYMBOLS)}")
    print(f"Параметры: TP1={ATR_TP1}xATR  TP2={ATR_TP2}xATR  SL={ATR_SL}xATR")
    print("=" * 65)

    all_results = {k: {"LONG": [], "SHORT": []} for k in ["RSI","MACD","BB","EMA","Volume"]}
    tests = [("RSI", test_rsi), ("MACD", test_macd), ("BB", test_bb),
             ("EMA", test_ema_cross), ("Volume", test_volume_spike)]

    for symbol in SYMBOLS:
        print(f"\n[{symbol}] загружаем...", end=" ", flush=True)
        df = await get_candles_period(symbol, end_ts)
        if df.empty:
            print("пусто, пропускаем")
            continue

        # Показываем контекст периода
        price_start = df["close"].iloc[0]
        price_end   = df["close"].iloc[-1]
        period_chg  = (price_end - price_start) / price_start * 100
        print(f"{len(df)} свечей  {period_chg:+.1f}% за период", end="  ")

        for key, fn in tests:
            res = fn(df)
            all_results[key]["LONG"].extend(res.get("LONG", []))
            all_results[key]["SHORT"].extend(res.get("SHORT", []))
        print("✓")

    print("\n" + "=" * 65)
    print("РЕЗУЛЬТАТЫ")
    print("=" * 65)

    summaries = []
    for name, results in all_results.items():
        s = _print_indicator(name, results)
        summaries.append(s)

    # Рейтинг по expectancy (реальный P&L на сделку)
    summaries.sort(key=lambda x: x["expectancy"], reverse=True)
    print("\n" + "=" * 65)
    print("РЕЙТИНГ по EXPECTANCY (реальный P&L на сделку):")
    print("=" * 65)
    for i, s in enumerate(summaries, 1):
        bar    = "█" * max(0, int((s["wr"]) / 5))
        status = "✅" if s["expectancy"] > 0 else "⚠️ " if s["expectancy"] > -0.3 else "❌"
        exp_s  = f"{s['expectancy']:+.3f}%"
        print(f"  {i}. {s['name']:<10} WR={s['wr']:5.1f}%  exp={exp_s}/сделку  "
              f"{bar} {status}  ({s['total']} сигналов)")

    total_signals = sum(s["total"] for s in summaries)
    print(f"\n  Всего сигналов проверено: {total_signals}")
    print("  Breakeven WR при avg TP=2xSL: ~33%")
    print("=" * 65)


# ── Combo бэктест ─────────────────────────────────────────────────────────────

async def run_combo_backtest(period_key: str = "current"):
    import data.binance_client as bc
    bc._session = None

    period = PERIODS.get(period_key, PERIODS["current"])
    end_ts = period.get("endTime")
    label  = period.get("label", period_key)

    print("\n" + "=" * 65)
    print(f"КОМБО-БЭКТЕСТ  |  Период: {label}")
    print("=" * 65)

    combos = {
        "RSI+BB_reversal":    [],
        "MACD_cross+Volume":  [],
        "Volume+EMA_align":   [],
        "RSI+MACD+Volume":    [],
        "All_3_agree":        [],
    }

    col_rsi = f"RSI_{config.RSI_PERIOD}"

    for symbol in SYMBOLS[:6]:
        print(f"[{symbol}]...", end=" ", flush=True)
        df = await get_candles_period(symbol, end_ts)
        if df.empty:
            print("пусто")
            continue

        for i in range(60, len(df) - HOLD_MAX):
            rsi      = df[col_rsi].iloc[i]   if col_rsi in df.columns else 50
            hist     = df["MACD_hist"].iloc[i]   if "MACD_hist" in df.columns else 0
            hist_p   = df["MACD_hist"].iloc[i-1] if "MACD_hist" in df.columns else 0
            macd     = df["MACD"].iloc[i]        if "MACD" in df.columns else 0
            sig      = df["MACD_sig"].iloc[i]    if "MACD_sig" in df.columns else 0
            vr       = df["vol_ratio"].iloc[i]   if "vol_ratio" in df.columns else 1
            close    = df["close"].iloc[i]
            open_    = df["open"].iloc[i]
            prev_c   = df["close"].iloc[i-1]
            bb_lower = df["BB_lower"].iloc[i]    if "BB_lower" in df.columns else 0
            bb_upper = df["BB_upper"].iloc[i]    if "BB_upper" in df.columns else 999999
            e20      = df["EMA_20"].iloc[i]      if "EMA_20" in df.columns else 0
            e50      = df["EMA_50"].iloc[i]      if "EMA_50" in df.columns else 0
            atr      = df["ATR_14"].iloc[i]      if "ATR_14" in df.columns else 0
            body     = abs(close - open_)

            # Условия для LONG
            rsi_long    = rsi < 32 and close > prev_c
            macd_cross  = hist > 0 and hist_p <= 0   # свежий кроссовер
            bb_reversal = df["close"].iloc[i-1] < bb_lower and close > bb_lower
            vol_strong  = vr >= 2.0 and close > open_ and (not atr or body > atr * 0.3)
            ema_bull    = e20 > e50 and close > e20

            # RSI отскок + BB подтверждение
            if rsi_long and bb_reversal:
                combos["RSI+BB_reversal"].append(_check_outcome(df, i, "LONG"))

            # MACD кроссовер + объём
            if macd_cross and vr >= 1.5:
                combos["MACD_cross+Volume"].append(_check_outcome(df, i, "LONG"))

            # Volume спайк + EMA тренд согласен
            if vol_strong and ema_bull:
                combos["Volume+EMA_align"].append(_check_outcome(df, i, "LONG"))

            # RSI + MACD + Volume все согласны
            if rsi_long and macd_cross and vr >= 1.5:
                combos["RSI+MACD+Volume"].append(_check_outcome(df, i, "LONG"))

            # Всё согласно
            if rsi_long and macd_cross and vol_strong and ema_bull:
                combos["All_3_agree"].append(_check_outcome(df, i, "LONG"))

        print("✓")

    print("\n" + "=" * 65)
    print("РЕЗУЛЬТАТЫ КОМБИНАЦИЙ (LONG):")
    print("=" * 65)

    for name, outcomes in combos.items():
        s = _stats(outcomes)
        if s["total"] == 0:
            print(f"  {name:<25} нет сигналов")
            continue
        status = "✅" if s["expectancy"] > 0 else "⚠️ " if s["expectancy"] > -0.3 else "❌"
        bar    = "█" * max(0, int(s["wr"] / 5))
        print(f"  {name:<25} WR={s['wr']:5.1f}%  "
              f"exp={s['expectancy']:+.3f}%/сд  "
              f"{bar} {status}  ({s['wins']}/{s['total']})")

    print("\nВывод: комбинации дают меньше сигналов, но более чистые.")
    print("=" * 65)


# ── Multi-period ──────────────────────────────────────────────────────────────

async def run_all_periods():
    """Прогоняет базовый бэктест на всех периодах для сравнения."""
    import data.binance_client as bc

    print("\n" + "█" * 65)
    print("МУЛЬТИ-ПЕРИОД БЭКТЕСТ — сравниваем bull/bear/sideways")
    print("█" * 65)

    for key in PERIODS:
        bc._session = None
        await run_backtest(key)
        print()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--period" in args:
        idx = args.index("--period")
        period = args[idx+1] if idx+1 < len(args) else "current"
    else:
        period = "current"

    if "all" in args or "--period" in args and period == "all":
        asyncio.run(run_all_periods())
    elif "--combo" in args:
        asyncio.run(run_combo_backtest(period))
    elif "--both" in args:
        async def _both():
            await run_backtest(period)
            await run_combo_backtest(period)
        asyncio.run(_both())
    else:
        asyncio.run(run_backtest(period))