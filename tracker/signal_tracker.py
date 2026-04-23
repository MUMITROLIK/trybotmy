"""
Трекер v3 — следит за активными сигналами И взятыми сделками.

Новое:
  - Уведомление когда цена близко к SL (50% пути от входа до SL)
  - Трейлинг стоп после TP1: SL подтягивается каждые 15 минут за ценой
  - Daily Summary в 20:00 UTC
  - Фильтр высокой волатильности при генерации (ATR > 3%)
"""

import asyncio
from datetime import datetime, timedelta, timezone

import config
from utils import logger
from database.db import (get_active_signals, close_signal,
                          get_open_trades, close_trade, trade_exists,
                          hit_tp1_trade, update_trade_sl, get_all_trades,
                          update_ml_target, update_ml_target_by_signal,
                          activate_pending_trade, cancel_pending_trade)
from data.binance_client import get_cached_price, get_price_info_async
from data.bybit_client import (get_cached_price as bybit_cached_price,
                                get_price_info_async as bybit_price)
from data.okx_client import (get_cached_price as okx_cached_price,
                              get_price_info_async as okx_price)
from export_data import export_to_json
from github_push import push_data_json

MAX_TRADE_HOURS  = 8     # таймаут сделки
SL_WARN_RATIO    = 0.5   # предупреждение когда цена прошла 50% пути к SL

_notify_callback = None
_sl_warned       = set()   # trade_id у которых уже отправили предупреждение
_last_summary_day = None   # день последнего Daily Summary
_trailing_sl      = {}     # trade_id → текущий trailing SL (персистентен через БД)
_dead_symbols_tracker: set[str] = set()  # делистированные — пропускаем в трекере


def _get_atr_trailing_step(trade: dict) -> float:
    """
    Возвращает ATR-based trailing step для сделки.
    trailing_pct = 1.5x ATR%, но не меньше 0.8% и не больше 3%.
    """
    atr_pct = trade.get("atr_pct")
    if atr_pct is None or atr_pct <= 0:
        atr_pct = 1.5  # дефолт 1.5% если ATR не сохранён
    
    # 1.5x ATR, ограниченный диапазоном [0.8%, 3.0%]
    trailing_pct = max(0.008, min(0.03, atr_pct / 100 * 1.5))
    return trailing_pct, atr_pct


def set_notify_callback(fn):
    global _notify_callback
    _notify_callback = fn


def _load_trailing_sl_state():
    """
    При старте трекера восстанавливает _trailing_sl из БД.
    Для всех TP1_HIT позиций берём текущий sl (уже самый свежий trailing SL,
    т.к. update_trade_sl сохраняет его туда при каждом шаге).
    После этого trailing продолжается с правильного уровня, а не сбрасывается.
    """
    global _trailing_sl
    try:
        trades = get_open_trades(include_pending=False)
        restored = 0
        for t in trades:
            if t.get("status") == "TP1_HIT" and t.get("sl"):
                _trailing_sl[t["id"]] = t["sl"]
                restored += 1
        if restored:
            logger.info("Tracker", f"Trailing SL восстановлен для {restored} позиций из БД")
    except Exception as e:
        logger.err("Tracker", f"Ошибка восстановления trailing SL: {e}")


async def _price(symbol: str, exchange: str = "binance"):
    """Возвращает цену с нужной биржи. Если не нашли — пробуем другие."""
    if symbol in _dead_symbols_tracker:
        return None

    try:
        if exchange == "bybit":
            p = bybit_cached_price(symbol)
            if p: return p
            info = await bybit_price(symbol)
            p = info.get("price")
            if p: return p
            p = get_cached_price(symbol)
            if p: return p
            info = await get_price_info_async(symbol)
            return info.get("price")
        elif exchange == "okx":
            p = okx_cached_price(symbol)
            if p: return p
            info = await okx_price(symbol)
            p = info.get("price")
            if p: return p
            p = get_cached_price(symbol)
            if p: return p
            info = await get_price_info_async(symbol)
            return info.get("price")
        else:
            p = get_cached_price(symbol)
            if p: return p
            info = await get_price_info_async(symbol)
            return info.get("price")
    except Exception as e:
        if "400" in str(e):
            # Пробуем другие биржи перед блокировкой
            if exchange != "binance":
                try:
                    p = get_cached_price(symbol)
                    if p: return p
                    info = await get_price_info_async(symbol)
                    p = info.get("price")
                    if p: return p
                except Exception:
                    pass
            if exchange != "bybit":
                try:
                    p = bybit_cached_price(symbol)
                    if p: return p
                    info = await bybit_price(symbol)
                    p = info.get("price")
                    if p: return p
                except Exception:
                    pass
            # Нигде нет — делистирован
            _dead_symbols_tracker.add(symbol)
            logger.warn("Tracker", f"{symbol} делистирован на всех биржах — исключён из трекинга")
            return None
        raise


def _signal_result_msg(signal: dict, status: str, current_price: float) -> str:
    symbol, direction = signal["symbol"], signal["direction"]
    entry, created    = signal["entry_price"], signal["created_at"]
    emoji_dir = "🟢" if direction == "LONG" else "🔴"
    if status == "TP1":
        pnl = ((current_price-entry)/entry*100) if direction=="LONG" else ((entry-current_price)/entry*100)
        line, emoji = f"🎯 TP1 достигнут! +{pnl:.2f}%", "✅"
    elif status == "TP2":
        pnl = ((current_price-entry)/entry*100) if direction=="LONG" else ((entry-current_price)/entry*100)
        line, emoji = f"🎯🎯 TP2 достигнут! +{pnl:.2f}%", "✅✅"
    elif status == "SL":
        pnl = abs((current_price-entry)/entry*100)
        line, emoji = f"🛑 Стоп-лосс. -{pnl:.2f}%", "❌"
    else:
        ttl_h = max(1, int(round(config.SIGNAL_TTL_MINUTES / 60)))
        line, emoji = f"⏰ Сигнал истёк ({ttl_h}ч)", "⌛"
    return (f"{emoji} <b>СИГНАЛ ОТРАБОТАЛ: {symbol}</b> {emoji_dir} {direction}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Вход: <code>{entry:.6g}</code>\n"
            f"📊 Текущая: <code>{current_price:.6g}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{line}\n"
            f"ℹ️ Сделка не была взята (только трекинг сигнала)\n"
            f"🕐 Открыт: {created[:16]}")


def _tp1_hit_msg(trade: dict, pnl: float, price: float) -> str:
    """Уведомление о достижении TP1 — сделка продолжается до TP2."""
    symbol    = trade["symbol"]
    direction = trade["direction"]
    entry     = trade["entry_price"]
    tp2       = trade["tp2"]
    emoji_dir = "🟢" if direction == "LONG" else "🔴"
    return (
        f"🎯 <b>TP1 достигнут: {symbol}</b> {emoji_dir}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Вход:  <code>{entry:.6g}</code>\n"
        f"✅ TP1:   <code>{price:.6g}</code>  <b>+{pnl:.2f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔒 SL → <code>{entry:.6g}</code> (буфер ATR×0.3)\n"
        f"🎯 Цель TP2: <code>{tp2:.6g}</code>\n"
        f"⏳ Удерживаем позицию..."
    )


def _trade_result_msg(trade: dict, status: str, pnl: float, price: float) -> str:
    symbol, direction = trade["symbol"], trade["direction"]
    entry    = trade["entry_price"]
    taken_at = trade.get("taken_at", "")

    # Считаем время в сделке
    duration_str = ""
    if taken_at:
        try:
            opened = datetime.fromisoformat(taken_at.replace(" ", "T"))
            delta  = datetime.utcnow() - opened
            hours  = int(delta.total_seconds() // 3600)
            mins   = int((delta.total_seconds() % 3600) // 60)
            if hours == 0 and mins == 0:
                duration_str = "<1м"
            else:
                duration_str = f"{hours}ч {mins}м" if hours else f"{mins}м"
        except Exception:
            pass

    emoji_dir = "🟢" if direction == "LONG" else "🔴"

    if status in ("TP1", "TP2"):
        emoji = "✅✅"
        result_line = f"🎯 {status} достигнут! +{pnl:.2f}%"
    elif status == "SL":
        emoji = "❌"
        result_line = f"🛑 Стоп-лосс. -{abs(pnl):.2f}%"
    elif status == "SL_AFTER_TP1":
        emoji = "🔒"
        result_line = f"🔒 Закрыто в безубытке после TP1. {pnl:+.2f}%"
    elif status == "TIMEOUT":
        emoji = "⏰"
        result_line = f"⏰ Таймаут {MAX_TRADE_HOURS}ч — закрыто по времени. {pnl:+.2f}%"
    else:
        emoji = "➖"
        result_line = f"Закрыто: {pnl:+.2f}%"

    return (
        f"{emoji} <b>ПОЗИЦИЯ ЗАКРЫТА: {symbol}</b> {emoji_dir}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Вход:    <code>{entry:.6g}</code>\n"
        f"📊 Закрыто: <code>{price:.6g}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 P&L: <b>{pnl:+.2f}%</b>  ({status})\n"
        + (f"🕐 Время в сделке: {duration_str}\n" if duration_str else "")
    )



def _sl_warning_msg(trade: dict, pnl: float, price: float, pct_to_sl: float) -> str:
    symbol    = trade["symbol"]
    direction = trade["direction"]
    sl        = trade["sl"]
    emoji_dir = "🟢" if direction == "LONG" else "🔴"
    return (
        f"⚠️ <b>Приближение к стопу: {symbol}</b> {emoji_dir}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Текущая: <code>{price:.6g}</code>\n"
        f"🛑 SL:      <code>{sl:.6g}</code>\n"
        f"📉 P&L:     <b>{pnl:+.2f}%</b>\n"
        f"⚡ До стопа: <b>{pct_to_sl:.1f}%</b>\n"
        f"\nМожешь закрыть вручную через /positions"
    )


def _daily_summary_msg(trades_today: list, all_trades: list) -> str:
    closed = [t for t in all_trades if t["status"] not in ("OPEN", "TP1_HIT")]
    # Сделки закрытые сегодня
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    today_closed = [
        t for t in closed
        if t.get("closed_at", "")[:10] == today_str
    ]
    wins   = [t for t in today_closed if t["status"] in ("TP1","TP2","SL_AFTER_TP1") or
              (t["status"] in ("MANUAL","TIMEOUT") and (t.get("pnl_pct") or 0) >= 0)]
    losses = [t for t in today_closed if t["status"] == "SL" or
              (t["status"] in ("MANUAL","TIMEOUT") and (t.get("pnl_pct") or 0) < 0)]
    open_  = [t for t in all_trades if t["status"] in ("OPEN", "TP1_HIT")]
    pnl    = sum(t.get("pnl_pct", 0) or 0 for t in today_closed)
    wr     = round(len(wins)/len(today_closed)*100,1) if today_closed else 0

    lines = [f"📊 <b>Daily Summary — {today_str}</b>\n━━━━━━━━━━━━━━━━━━━"]
    lines.append(f"📈 Закрыто сделок: <b>{len(today_closed)}</b>")
    lines.append(f"✅ Профит: <b>{len(wins)}</b>   ❌ Стоп: <b>{len(losses)}</b>")
    lines.append(f"🏆 Winrate: <b>{wr}%</b>")
    lines.append(f"💰 P&L за день: <b>{pnl:+.2f}%</b>")
    lines.append(f"🔓 Открытых позиций: <b>{len(open_)}</b>")
    if open_:
        lines.append("\nОткрытые:")
        for t in open_[:5]:
            lg  = t["direction"] == "LONG"
            lines.append(f"  {'🟢' if lg else '🔴'} {t['symbol']} {t['direction']}")
    return "\n".join(lines)


async def check_daily_summary():
    """Отправляет Daily Summary раз в день в 20:00 UTC."""
    global _last_summary_day
    now = datetime.now(timezone.utc)
    if now.hour == 20 and now.minute < 1:
        today = now.date()
        if _last_summary_day != today:
            _last_summary_day = today
            try:
                from database.db import get_all_trades
                all_t = get_all_trades()
                msg   = _daily_summary_msg([], all_t)
                if _notify_callback:
                    await _notify_callback(msg)
                print("[Tracker] 📊 Daily Summary отправлен")
            except Exception as e:
                logger.err("Tracker", f"Daily Summary ошибка: {e}")


async def check_signals_once():
    # ── Обычные сигналы ──────────────────────────────────────────────────────
    signals = get_active_signals()
    for signal in signals:
        symbol    = signal["symbol"]
        direction = signal["direction"]
        tp1, tp2, sl = signal["tp1"], signal["tp2"], signal["sl"]
        created   = datetime.fromisoformat(signal["created_at"])
        exchange  = signal.get("exchange", "binance")  # биржа сигнала

        # Автоистечение через TTL (для paper/ML)
        if datetime.utcnow() - created > timedelta(minutes=config.SIGNAL_TTL_MINUTES):
            close_signal(signal["id"], "EXPIRED")
            if trade_exists(symbol, direction):
                # Сделка взята — тихое закрытие сигнала, трекер сделок следит дальше
                logger.info("Tracker", f"{symbol} сигнал закрыт (сделка активна)")
            else:
                logger.info("Tracker", f"{symbol} — сигнал истёк ({config.SIGNAL_TTL_MINUTES} мин, не взят)")
                # ML (paper): это НЕ TIMEOUT (TIMEOUT только для взятых сделок),
                # а отдельный класс EXPIRED — сигнал истёк без достижения TP/SL.
                try:
                    dur_min = _duration_min(signal.get("created_at"))
                    update_ml_target_by_signal(signal["id"], "EXPIRED", 0.0, dur_min)
                except Exception:
                    pass
            continue

        p = await _price(symbol, exchange)
        if not p: continue

        status = None
        if direction == "LONG":
            if p >= tp2:   status = "TP2"
            elif p >= tp1: status = "TP1"
            elif p <= sl:  status = "SL"
        elif direction == "SHORT":
            if p <= tp2:   status = "TP2"
            elif p <= tp1: status = "TP1"
            elif p >= sl:  status = "SL"

        if status:
            # Если по этому символу/направлению уже есть взятая сделка —
            # не шлём "результат сигнала", чтобы не создавалось ощущение,
            # что бот сам вошёл. Результат придёт из блока taken_trades.
            if trade_exists(symbol, direction):
                close_signal(signal["id"], status)
                logger.info("Tracker", f"{symbol} сигнал закрыт {status} (сделка активна, уведомление не отправляется)")
                continue
            
            # Сигнал не был взят в сделку - закрываем и уведомляем
            close_signal(signal["id"], status)
            logger.trade_result(symbol, direction, status, 0.0, p)
            
            # ML (paper): размечаем исход для сигналов без входа
            try:
                pnl = ((p - signal["entry_price"]) / signal["entry_price"] * 100
                       if direction == "LONG"
                       else (signal["entry_price"] - p) / signal["entry_price"] * 100)
                dur_min = _duration_min(signal.get("created_at"))
                update_ml_target_by_signal(signal["id"], status, round(pnl, 2), dur_min)
            except Exception:
                pass
            if _notify_callback:
                await _notify_callback(
                    _signal_result_msg(signal, status, p),
                    signal.get("telegram_msg_id"),
                )

    # ── Взятые сделки ─────────────────────────────────────────────────────────
    trades = get_open_trades(include_pending=True)

    # Лог количества открытых сделок
    if trades:
        summary = "  |  ".join(
            f"{t['symbol']} {t['direction']}" for t in trades
        )
        logger.positions_summary(len(trades), summary)

    for trade in trades:
        symbol    = trade["symbol"]
        direction = trade["direction"]
        entry     = trade["entry_price"]
        tp1, tp2, sl = trade["tp1"], trade["tp2"], trade["sl"]
        taken_at  = trade.get("taken_at", "")
        exchange  = trade.get("exchange", "binance")  # биржа сделки

        p = await _price(symbol, exchange)
        if not p: continue

        # ── Исполнение лимитки (PENDING → OPEN) ─────────────────────────────
        if trade.get("status") == "PENDING":
            # Таймаут лимитки: если долго не исполняется — отменяем.
            try:
                ttl_min = int(getattr(config, "LIMIT_ORDER_TTL_MINUTES", 60))
                created_iso = trade.get("taken_at")
                if created_iso:
                    opened = datetime.fromisoformat(created_iso.replace(" ", "T"))
                    age_min = (datetime.utcnow() - opened).total_seconds() / 60
                    if age_min >= ttl_min:
                        cancel_pending_trade(trade["id"], reason="LIMIT_TIMEOUT")
                        try:
                            update_ml_target(trade["id"], "NOT_FILLED", 0.0, int(age_min))
                        except Exception:
                            pass
                        logger.warn("Tracker", f"Limit timeout: {symbol} {direction} (>{ttl_min}м) → LIMIT_TIMEOUT")
                        continue
            except Exception:
                pass

            filled = False
            try:
                if direction == "LONG"  and p <= entry: filled = True
                elif direction == "SHORT" and p >= entry: filled = True
            except Exception:
                filled = False

            if filled:
                activate_pending_trade(trade["id"])
                logger.ok("Tracker", f"Limit filled: {symbol} {direction} @ {entry} (p={p})")
            else:
                # ── Market fallback ───────────────────────────────────────────
                # Лимитка не исполнилась — проверяем ушла ли цена в нашу сторону.
                # Если ушла не дальше MAX_CHASE_PCT — входим по рынку с пересчётом уровней.
                # Если ушла дальше — не гонимся (перегрето), ждём дальше.
                try:
                    from database.db import update_trade_levels
                    MAX_CHASE_PCT  = float(getattr(config, "LIMIT_CHASE_MAX_PCT", 2.0))
                    MIN_WAIT_MIN   = float(getattr(config, "LIMIT_CHASE_MIN_WAIT_MIN", 5.0))

                    # Сначала проверяем минимальное время ожидания
                    _age_min = 0.0
                    try:
                        _created = trade.get("taken_at")
                        if _created:
                            _opened  = datetime.fromisoformat(_created.replace(" ", "T"))
                            _age_min = (datetime.utcnow() - _opened).total_seconds() / 60
                    except Exception:
                        pass

                    if _age_min < MIN_WAIT_MIN:
                        # Ещё рано — ждём минимальное время
                        logger.info("Tracker", f"{symbol} лимитка ждёт "
                              f"{_age_min:.1f}м / {MIN_WAIT_MIN:.0f}м до fallback")
                    else:
                        # Сколько % цена ушла от лимитной цены в нужную сторону
                        if direction == "LONG"  and p > entry:
                            moved_pct = (p - entry) / entry * 100
                        elif direction == "SHORT" and p < entry:
                            moved_pct = (entry - p) / entry * 100
                        else:
                            moved_pct = 0.0

                        if 0 < moved_pct <= MAX_CHASE_PCT:
                            # Пересчитываем TP/SL от новой цены с теми же процентами
                            tp1_pct = abs(trade["tp1"] - entry) / entry * 100
                            tp2_pct = abs(trade["tp2"] - entry) / entry * 100
                            sl_pct  = abs(trade["sl"]  - entry) / entry * 100

                            if direction == "LONG":
                                new_tp1 = round(p * (1 + tp1_pct / 100), 8)
                                new_tp2 = round(p * (1 + tp2_pct / 100), 8)
                                new_sl  = round(p * (1 - sl_pct  / 100), 8)
                            else:
                                new_tp1 = round(p * (1 - tp1_pct / 100), 8)
                                new_tp2 = round(p * (1 - tp2_pct / 100), 8)
                                new_sl  = round(p * (1 + sl_pct  / 100), 8)

                            update_trade_levels(trade["id"], p, new_tp1, new_tp2, new_sl)

                            now_utc = datetime.utcnow().strftime("%H:%M:%S UTC")
                            logger.info("Tracker", f"Market fallback: {symbol} {direction} "
                                  f"лимит={entry:.6g} → рынок={p:.6g} "
                                  f"(+{moved_pct:.1f}%, ждали {_age_min:.1f}м) | "
                                  f"TP1={new_tp1:.6g} TP2={new_tp2:.6g} SL={new_sl:.6g}")
                            if _notify_callback:
                                await _notify_callback(
                                    f"🔄 <b>Лимитка → Рынок: {symbol}</b> "
                                    f"{'🟢' if direction == 'LONG' else '🔴'}\n"
                                    f"━━━━━━━━━━━━━━━━━━━\n"
                                    f"📌 Лимит был: <code>{entry:.6g}</code>\n"
                                    f"📍 Вошли по рынку: <code>{p:.6g}</code> "
                                    f"(+{moved_pct:.1f}%)\n"
                                    f"🎯 TP1: <code>{new_tp1:.6g}</code>\n"
                                    f"🛑 SL:  <code>{new_sl:.6g}</code>\n"
                                    f"⏱ Ждали: {_age_min:.1f}м\n"
                                    f"🕐 {now_utc}"
                                )
                        elif moved_pct > MAX_CHASE_PCT:
                            logger.info("Tracker", f"{symbol} не гонимся: "
                                  f"цена ушла на {moved_pct:.1f}% > {MAX_CHASE_PCT:.1f}%")
                except Exception as _fb_err:
                    logger.err("Tracker", f"Market fallback ошибка {symbol}: {_fb_err}")

            # Пока не исполнено — не считаем TP/SL/timeout
            continue

        # P&L текущий
        if direction == "LONG":
            pnl = (p - entry) / entry * 100
        else:
            pnl = (entry - p) / entry * 100

        # ── Уведомление о приближении к SL ───────────────────────────────────
        # Предупреждаем когда цена прошла 50% пути от входа до SL
        tp1_already_hit = trade.get("status") == "TP1_HIT"  # ← определяем ДО использования
        if trade["id"] not in _sl_warned:
            if not tp1_already_hit and entry and sl and p:
                dist_entry_sl = abs(entry - sl)
                dist_cur_sl   = abs(p - sl)
                if dist_entry_sl > 0:
                    pct_to_sl = dist_cur_sl / dist_entry_sl * 100
                    if pct_to_sl <= (1 - SL_WARN_RATIO) * 100:
                        _sl_warned.add(trade["id"])
                        remaining_pct = (dist_cur_sl / entry * 100) if entry else 0
                        logger.warn("Tracker", f"{symbol} близко к SL ({remaining_pct:.2f}% до стопа)")
                        if _notify_callback:
                            await _notify_callback(
                                _sl_warning_msg(trade, round(pnl, 2), p, remaining_pct),
                                trade.get("telegram_msg_id")
                            )

        # ── Трейлинг стоп после TP1 отключён ─────────────────────────────────
        # SL остаётся на уровне TP1 до достижения TP2 или отката
        # Трейлинг закрывал позиции слишком рано не давая дойти до TP2
        tp1_already_hit = trade.get("status") == "TP1_HIT"

        # ── Таймаут ───────────────────────────────────────────────────────────
        # Если за MAX_TRADE_HOURS не было TP — закрываем по рынку.
        # Трейдерская логика: зависшая позиция хуже небольшого минуса —
        # капитал заморожен и пропускаем другие сигналы.
        if taken_at:
            try:
                opened  = datetime.fromisoformat(taken_at.replace(" ", "T"))
                age_hrs = (datetime.utcnow() - opened).total_seconds() / 3600
                if age_hrs >= MAX_TRADE_HOURS:
                    close_trade(trade["id"], "TIMEOUT", p, round(pnl, 2))
                    dur_min = _duration_min(trade.get("taken_at"))
                    update_ml_target(trade["id"], "TIMEOUT", round(pnl, 2), dur_min)
                    logger.trade_result(symbol, direction, "TIMEOUT", round(pnl, 2), p)
                    if _notify_callback:
                        await _notify_callback(
                            _trade_result_msg(trade, "TIMEOUT", round(pnl, 2), p),
                            trade.get("telegram_msg_id")
                        )
                    continue
            except Exception:
                pass

        # ── TP / SL ───────────────────────────────────────────────────────────
        tp1_already_hit = trade.get("status") == "TP1_HIT"

        if direction == "LONG":
            hit_tp2 = p >= tp2
            hit_tp1 = p >= tp1 and not tp1_already_hit
            hit_sl  = p <= sl
        else:
            hit_tp2 = p <= tp2
            hit_tp1 = p <= tp1 and not tp1_already_hit
            hit_sl  = p >= sl

        if hit_tp2:
            # Финальное закрытие на TP2
            close_trade(trade["id"], "TP2", p, round(pnl, 2))
            dur_min = _duration_min(trade.get("taken_at"))
            update_ml_target(trade["id"], "TP2", round(pnl, 2), dur_min)
            logger.trade_result(symbol, direction, "TP2", round(pnl, 2), p)
            if _notify_callback:
                await _notify_callback(
                    _trade_result_msg(trade, "TP2", round(pnl, 2), p),
                    trade.get("telegram_msg_id")
                )
            export_to_json()
            push_data_json()

        elif hit_tp1:
            # TP1 достигнут — закрываем сразу (скальп стратегия)
            # Стабильный +2% лучше редкого +5% с риском SL_AFTER_TP1
            close_trade(trade["id"], "TP1", p, round(pnl, 2))
            dur_min = _duration_min(trade.get("taken_at"))
            update_ml_target(trade["id"], "TP1", round(pnl, 2), dur_min)
            logger.trade_result(symbol, direction, "TP1", round(pnl, 2), p)
            if _notify_callback:
                await _notify_callback(
                    _trade_result_msg(trade, "TP1", round(pnl, 2), p),
                    trade.get("telegram_msg_id")
                )
            export_to_json()
            push_data_json()

        elif hit_sl:
            # SL сработал
            status_label = "SL" if not tp1_already_hit else "SL_AFTER_TP1"
            close_trade(trade["id"], status_label, p, round(pnl, 2))
            dur_min = _duration_min(trade.get("taken_at"))
            update_ml_target(trade["id"], status_label, round(pnl, 2), dur_min)
            logger.trade_result(symbol, direction, status_label, round(pnl, 2), p)
            if _notify_callback:
                await _notify_callback(
                    _trade_result_msg(trade, status_label, round(pnl, 2), p),
                    trade.get("telegram_msg_id")
                )
            export_to_json()
            push_data_json()



def _duration_min(ts: str) -> int:
    """Вычисляет длительность в минутах (ISO или SQLite datetime)."""
    if not ts:
        return 0
    try:
        from datetime import datetime
        # taken_at обычно ISO; created_at из SQLite часто "YYYY-MM-DD HH:MM:SS"
        opened = datetime.fromisoformat(ts.replace(" ", "T"))
        delta  = datetime.utcnow() - opened
        return int(delta.total_seconds() / 60)
    except Exception:
        return 0


async def run_tracker(interval_seconds: int = 15):
    logger.info("Tracker", f"Запущен, интервал {interval_seconds}с")
    # Восстанавливаем trailing SL из БД — перед первым циклом
    _load_trailing_sl_state()
    while True:
        try:
            await check_signals_once()
            await check_daily_summary()
        except Exception as e:
            logger.err("Tracker", f"Ошибка: {e}")
        await asyncio.sleep(interval_seconds)