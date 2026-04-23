"""
Telegram Bot v4 — Auto-Take Toggle
- Кнопка 🤖 Авто-вход: ВКЛ/ВЫКЛ прямо в боте (без перезапуска)
- Статус авто-входа виден в главной клавиатуре
- ML-режим: /mlstats — статистика накопленного датасета
- /filters  — статистика фильтров
- Полный контроль над позициями
"""

from datetime import datetime
from html import escape
from telegram import (Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup,
                       ReplyKeyboardMarkup, KeyboardButton)
from telegram.ext import (ApplicationBuilder, CommandHandler, CallbackQueryHandler,
                           ContextTypes, ConversationHandler, MessageHandler, filters)
from telegram.constants import ParseMode
import config
from ml.model import predict_win_prob
from database.db import (
    get_active_signals, get_conn, get_signal_by_msg_id,
    save_taken_trade, trade_exists, get_all_trades,
    get_open_trades, clear_signal_history,
    close_trade, update_trade_entry, get_filter_stats,
    is_auto_take, toggle_auto_take, get_ml_dataset_stats,
    export_ml_dataset, export_ml_dataset_clean, export_ml_dataset_clean2,
    get_ml_quality_report,
    trade_exists_symbol,
    is_collect_ml_data,
)
from export_data import export_to_json
from github_push import push_data_json

WAITING_ENTRY = 1

_bot_instance = None


def get_bot() -> Bot:
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = Bot(token=config.TELEGRAM_BOT_TOKEN)
    return _bot_instance


def _main_kb() -> ReplyKeyboardMarkup:
    """
    Динамическая клавиатура — кнопка авто-входа показывает текущий статус.
    """
    auto = is_auto_take()
    auto_btn = "🤖 Авто-вход: ВКЛ 🟢" if auto else "🤖 Авто-вход: ВЫКЛ 🔴"
    return ReplyKeyboardMarkup([
        [KeyboardButton("📡 Сигналы"),  KeyboardButton("📊 Позиции")],
        [KeyboardButton("💰 P&L"),      KeyboardButton("📈 Статистика")],
        [KeyboardButton("🔍 Фильтры"), KeyboardButton("📦 ML Данные")],
        [KeyboardButton(auto_btn)],
    ], resize_keyboard=True)


def _bar(strength: int) -> str:
    filled = round(strength / 10)
    return "█" * filled + "░" * (10 - filled)


def _trade_duration(taken_at: str) -> str:
    if not taken_at:
        return ""
    try:
        opened = datetime.fromisoformat(taken_at)
        delta  = datetime.utcnow() - opened
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        return f"{h}ч {m}м" if h else f"{m}м"
    except Exception:
        return ""


def _export_and_push():
    try:
        export_to_json()
        push_data_json()
    except Exception as e:
        print(f"[Bot] Export error: {e}")


# ── Форматирование сигнала ────────────────────────────────────────────────────

def format_signal_message(signal: dict) -> str:
    d         = signal["direction"]
    exchange  = signal.get("exchange", "binance")
    exch_icon = "🟦" if exchange == "binance" else ("🟨" if exchange == "bybit" else ("🟪" if exchange == "okx" else "⬜"))
    emoji_dir = "🟢 LONG  🚀" if d == "LONG" else "🔴 SHORT 📉"
    # Telegram ParseMode.HTML: любые пользовательские строки надо экранировать,
    # иначе символы вида "<" ломают парсер ("unsupported start tag").
    reasons   = "\n".join(f"  • {escape(str(r))}" for r in signal.get("reasons", [])[:5])
    news      = escape(str(signal.get("news_title", "") or ""))
    news_block = f"\n📰 <i>{news[:80]}</i>\n" if news else ""
    funding   = signal.get("funding_rate", 0) or 0
    change    = signal.get("change_24h", 0) or 0
    s         = signal["strength"]
    heavy     = signal.get("heavy_confirmed", 0)
    quality   = "⭐⭐" if heavy >= 3 else ("⭐" if heavy >= 2 else "")
    entry_type  = signal.get("entry_type", "market")
    entry_label = "📌 Лимит" if entry_type == "limit" else "🔴 Рынок"
    auto_label  = "🤖 Авто" if is_auto_take() else "👤 Ожидает"

    p = predict_win_prob(signal)
    if p is None:
        ml_line = ""
    else:
        if p >= 0.70:
            lvl = "HIGH"
        elif p >= 0.58:
            lvl = "MID"
        else:
            lvl = "LOW"
        ml_line = f"🧠 ML: <b>{p*100:.1f}%</b>  <i>{lvl}</i>\n"

    return (
        f"{exch_icon} <b>СИГНАЛ: {escape(str(signal['symbol']))}</b> {quality}  {auto_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji_dir}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Вход:  <code>{signal['entry_price']:.6g}</code>  <i>{entry_label}</i>\n"
        f"🎯 TP1:   <code>{signal['tp1']:.6g}</code>  <i>(+{signal.get('tp1_pct',2):.1f}%)</i>\n"
        f"🎯 TP2:   <code>{signal['tp2']:.6g}</code>  <i>(+{signal.get('tp2_pct',4):.1f}%)</i>\n"
        f"🛑 SL:    <code>{signal['sl']:.6g}</code>   <i>(-{signal.get('sl_pct',2):.1f}%)</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Причины:\n{reasons}\n"
        f"{news_block}"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 24ч: <b>{change:+.1f}%</b>  Funding: <b>{funding*100:.4f}%</b>\n"
        f"{ml_line}"
        f"⚡ Сила: {_bar(s)} <b>{s}%</b>\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}"
    )


def _signal_keyboard(symbol: str, direction: str) -> InlineKeyboardMarkup:
    if is_auto_take():
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🤖 Взято автоматически", callback_data="noop"),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Взять сделку", callback_data=f"take:{symbol}:{direction}"),
        InlineKeyboardButton("❌ Пропустить",   callback_data=f"skip:{symbol}:{direction}"),
    ]])


async def send_signal(signal: dict):
    try:
        bot  = get_bot()
        msg  = format_signal_message(signal)
        kb   = _signal_keyboard(signal["symbol"], signal["direction"])
        sent = await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        return sent.message_id
    except Exception as e:
        print(f"[Bot] Ошибка отправки: {e}")
        return None


async def send_result(text: str, reply_to_msg_id: int = None):
    try:
        bot = get_bot()
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_msg_id,
        )
    except Exception as e:
        print(f"[Bot] Ошибка отправки результата: {e}")


# ── Карточка позиции ──────────────────────────────────────────────────────────

def _format_position_card(t: dict) -> tuple[str, InlineKeyboardMarkup]:
    lg        = t["direction"] == "LONG"
    exchange  = t.get("exchange", "binance")
    exch_icon = "🟦" if exchange == "binance" else ("🟨" if exchange == "bybit" else ("🟪" if exchange == "okx" else "⬜"))
    emoji_dir = "🟢" if lg else "🔴"
    entry     = t["entry_price"]
    tp1_hit   = t.get("status") == "TP1_HIT"
    dur       = _trade_duration(t.get("taken_at", ""))
    auto      = "🤖" if t.get("auto_taken") else "👤"

    tp1_line = (f"✅ TP1: <code>{t['tp1']:.6g}</code>  <i>(достигнут)</i>"
                if tp1_hit else f"🎯 TP1: <code>{t['tp1']:.6g}</code>")

    text = (
        f"{exch_icon} {emoji_dir} <b>{t['symbol']}</b> {t['direction']} {auto}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Вход: <code>{entry:.6g}</code>\n"
        f"{tp1_line}\n"
        f"🎯 TP2: <code>{t['tp2']:.6g}</code>\n"
        f"🛑 SL:  <code>{t['sl']:.6g}</code>\n"
        + (f"🔒 SL перемещён в безубыток\n" if tp1_hit else "")
        + (f"🕐 В сделке: {dur}\n" if dur else "")
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Изменить вход",
                             callback_data=f"editentry:{t['id']}:{entry}"),
        InlineKeyboardButton("✕ Закрыть сейчас",
                             callback_data=f"closepos:{t['id']}:{t['symbol']}:{t['direction']}"),
    ]])
    return text, kb


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    data   = query.data
    parts  = data.split(":")
    action = parts[0]

    # ── Взять / Пропустить ────────────────────────────────────────────────────
    if action == "take":
        if len(parts) < 3:
            return
        symbol, direction = parts[1], parts[2]
        msg_id = query.message.message_id
        signal = get_signal_by_msg_id(msg_id) or {
            "symbol": symbol, "direction": direction,
            "entry_price": 0, "tp1": 0, "tp2": 0, "sl": 0,
            "telegram_msg_id": msg_id,
        }
        # Не даём открыть позицию по символу, если уже есть открытая (без хеджа)
        if trade_exists_symbol(symbol):
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"⚠️ Сделка {symbol} уже открыта")
            return
        trade_id = save_taken_trade(signal, auto_taken=False)
        print(f"[Bot] ✅ Взята сделка {symbol} {direction} (trade_id={trade_id})")
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ ВЗЯТА — отслеживается", callback_data="noop")
            ]])
        )
        await query.message.reply_text(
            f"✅ <b>Сделка взята: {symbol} {direction}</b>",
            parse_mode=ParseMode.HTML, reply_markup=_main_kb(),
        )
        _export_and_push()

    elif action == "skip":
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Пропущено", callback_data="noop")
            ]])
        )

    elif action == "noop":
        pass

    # ── Редактирование входа ──────────────────────────────────────────────────
    elif action == "editentry":
        if len(parts) < 3:
            return
        trade_id  = int(parts[1])
        cur_entry = parts[2]
        context.user_data["edit_trade_id"] = trade_id
        await query.message.reply_text(
            f"✏️ <b>Введи новую цену входа</b>\n"
            f"Текущая: <code>{cur_entry}</code>\n\n"
            f"Отправь число, например: <code>0.2188</code>\n"
            f"Для отмены: /cancel",
            parse_mode=ParseMode.HTML,
        )
        return WAITING_ENTRY

    # ── Закрыть позицию ───────────────────────────────────────────────────────
    elif action == "closepos":
        if len(parts) < 4:
            return
        trade_id  = int(parts[1])
        symbol    = parts[2]
        direction = parts[3]
        trades    = get_open_trades()
        trade     = next((t for t in trades if t["id"] == trade_id), None)
        if not trade:
            await query.message.reply_text("⚠️ Позиция не найдена")
            return
        entry       = trade["entry_price"]
        close_price = entry
        try:
            from data.binance_client import get_cached_price
            ws_p = get_cached_price(symbol)
            if ws_p:
                close_price = ws_p
        except Exception:
            pass
        pnl = ((close_price - entry) / entry * 100
               if direction == "LONG"
               else (entry - close_price) / entry * 100)
        close_trade(trade_id, "MANUAL", close_price, round(pnl, 2))
        pnl_str   = f"+{pnl:.2f}%" if pnl >= 0 else f"{pnl:.2f}%"
        pnl_emoji = "✅" if pnl >= 0 else "❌"
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✋ <b>Позиция закрыта: {symbol} {direction}</b>\n"
            f"📍 Вход:    <code>{entry:.6g}</code>\n"
            f"📊 Закрыто: <code>{close_price:.6g}</code>\n"
            f"{pnl_emoji} P&L: <b>{pnl_str}</b>",
            parse_mode=ParseMode.HTML, reply_markup=_main_kb(),
        )
        _export_and_push()


# ── ConversationHandler ───────────────────────────────────────────────────────

async def receive_new_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trade_id = context.user_data.get("edit_trade_id")
    if not trade_id:
        return ConversationHandler.END
    text = update.message.text.strip().replace(",", ".")
    try:
        new_entry = float(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат. Введи число, например: <code>0.2188</code>",
            parse_mode=ParseMode.HTML,
        )
        return WAITING_ENTRY
    trades = get_open_trades()
    trade  = next((t for t in trades if t["id"] == trade_id), None)
    if not trade:
        await update.message.reply_text("⚠️ Позиция не найдена")
        return ConversationHandler.END
    old_entry = trade["entry_price"]
    update_trade_entry(trade_id, new_entry)
    await update.message.reply_text(
        f"✅ <b>Вход обновлён: {trade['symbol']}</b>\n"
        f"Было:  <code>{old_entry:.6g}</code>\n"
        f"Стало: <code>{new_entry:.6g}</code>",
        parse_mode=ParseMode.HTML, reply_markup=_main_kb(),
    )
    context.user_data.pop("edit_trade_id", None)
    _export_and_push()
    return ConversationHandler.END


async def cancel_entry_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("edit_trade_id", None)
    await update.message.reply_text("❌ Отменено", reply_markup=_main_kb())
    return ConversationHandler.END


# ── Команды ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = get_active_signals()
    auto   = is_auto_take()
    mode   = "🤖 AUTO-TAKE ВКЛ" if auto else "👤 Ручной режим"
    await update.message.reply_text(
        f"🤖 <b>Futures Signal Bot v4</b>\n\n"
        f"📡 Топ-{config.TOP_FUTURES_COUNT} USDT фьючерсов\n"
        f"⏱ Скан каждые {config.SCAN_INTERVAL}с\n"
        f"🎯 Мин. сила: {config.MIN_SIGNAL_STRENGTH}%\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Режим: <b>{mode}</b>\n"
        f"📡 Активных сигналов: <b>{len(active)}</b>\n\n"
        f"Кнопка <b>🤖 Авто-вход</b> — включить/выключить автовход\n"
        f"Кнопка <b>📦 ML Данные</b> — статистика датасета\n",
        parse_mode=ParseMode.HTML,
        reply_markup=_main_kb(),
    )


async def cmd_autotake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда или кнопка — переключает авто-вход."""
    new_state = toggle_auto_take()
    status    = "🟢 ВКЛЮЧЁН" if new_state else "🔴 ВЫКЛЮЧЕН"
    icon      = "🤖" if new_state else "👤"

    if new_state:
        desc = (
            "Все новые сигналы будут автоматически браться как сделки.\n\n"
            "📦 <b>ML режим:</b> каждый сигнал пишется в БД с полными "
            "данными индикаторов и исходом (TP1/TP2/SL).\n"
            "Используй /mlstats чтобы видеть накопленный датасет."
        )
    else:
        desc = "Теперь нужно нажимать <b>✅ Взять сделку</b> вручную."

    print(f"[Bot] 🔄 AUTO_TAKE → {status}")

    await update.message.reply_text(
        f"{icon} <b>Авто-вход {status}</b>\n\n{desc}",
        parse_mode=ParseMode.HTML,
        reply_markup=_main_kb(),   # клавиатура обновится с новым статусом
    )


async def cmd_mlstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика ML датасета."""
    s    = get_ml_dataset_stats()
    collect = is_collect_ml_data()
    auto = is_auto_take()
    mode_line = "🟢 Сбор ML-данных: ВКЛ" if collect else "🔴 Сбор ML-данных: ВЫКЛ"
    auto_line = "🤖 Авто-вход: ВКЛ" if auto else "👤 Авто-вход: ВЫКЛ (paper-сигналы всё равно размечаются)"

    lines = [
        f"📦 <b>ML Датасет</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{mode_line}\n"
        f"{auto_line}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 Всего записей:    <b>{s['total']}</b>\n"
        f"✅ Размечено:        <b>{s['labeled']}</b>\n"
        f"⌛ EXPIRED:          <b>{s.get('expired', 0)}</b>\n"
        f"🧱 NOT_FILLED:       <b>{s.get('not_filled', 0)}</b>\n"
        f"🧼 Clean (без EXPIRED): <b>{s.get('labeled_clean', 0)}</b>\n"
        f"🧼 Clean2 (без EXPIRED+NOT_FILLED): <b>{s.get('labeled_clean2', 0)}</b>\n"
        f"⏳ Без исхода:       <b>{s['unlabeled']}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏆 Winrate:          <b>{s['winrate']}%</b>\n"
        f"🏆 Winrate (clean):  <b>{s.get('winrate_clean', 0)}%</b>\n"
        f"🏆 Winrate (clean2): <b>{s.get('winrate_clean2', 0)}%</b>\n"
        f"✅ TP сделок:        <b>{s['wins']}</b>\n"
        f"❌ SL сделок:        <b>{s['losses']}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📅 За 24ч: размечено <b>{s.get('labeled_24h', 0)}</b> | "
        f"EXPIRED <b>{s.get('expired_rate_24h', 0)}%</b> | "
        f"NOT_FILLED <b>{s.get('not_filled_rate_24h', 0)}%</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<b>Средние значения фич:</b>\n"
        f"  RSI:         <b>{s['avg_rsi']}</b>\n"
        f"  Vol ratio:   <b>{s['avg_vol_ratio']}x</b>\n"
        f"  Fear&Greed:  <b>{s['avg_fg']}</b>\n"
        f"  MTF score:   <b>{s['avg_mtf_score']}</b>\n"
        f"  CVD score:   <b>{s['avg_cvd_score']}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<b>По исходам:</b>"
    ]
    for r in s["breakdown"][:12]:
        emoji = "✅" if r["target"] in ("TP1","TP2","SL_AFTER_TP1") else (
                "❌" if r["target"] == "SL" else (
                "⌛" if r["target"] == "EXPIRED" else (
                "🧱" if r["target"] == "NOT_FILLED" else "⏰")))
        lines.append(
            f"  {emoji} {r['direction']} → {r['target']}  "
            f"×{r['count']}  avg {r['avg_pnl']:+.2f}%  "
            f"~{int(r['avg_dur_min'])}м"
        )

    if s["labeled"] < 50:
        lines.append(
            f"\n⚠️ <i>Нужно минимум 200 сделок для обучения.\n"
            f"Включи авто-вход и дай боту поработать несколько дней.</i>"
        )
    elif not s["ready_for_ml"]:
        pct = round(s["labeled"] / 200 * 100)
        lines.append(f"\n📈 <i>Прогресс: {s['labeled']}/200  [{pct}%]</i>")
    else:
        lines.append(f"\n🚀 <b>Датасет готов для обучения!</b>\n"
                     f"<i>Используй /exportml для выгрузки в JSON</i>")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=_main_kb(),
    )


async def cmd_exportml(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспортирует ML датасет в JSON файл."""
    await update.message.reply_text("⏳ Экспортирую датасет...", reply_markup=_main_kb())
    count = export_ml_dataset("docs/ml_dataset.json")
    clean = export_ml_dataset_clean("docs/ml_dataset_clean.json")
    clean2 = export_ml_dataset_clean2("docs/ml_dataset_clean2.json")
    if count == 0:
        collect = is_collect_ml_data()
        await update.message.reply_text(
            ("⚠️ Нет размеченных данных.\n\n"
             + ("🔴 Сбор ML-данных выключен. Включи сбор и подожди исходов сигналов/сделок."
                if not collect
                else "Подожди, пока сигналы/сделки получат исход (TP/SL/EXPIRED/TIMEOUT).")),
            reply_markup=_main_kb(),
        )
        return
    try:
        from github_push import push_data_json
        import subprocess, shutil
        git = shutil.which("git") or "git"
        subprocess.run([git, "add", "docs/ml_dataset.json", "docs/ml_dataset_clean.json", "docs/ml_dataset_clean2.json"], capture_output=True)
        subprocess.run([git, "commit", "-m", f"ml dataset {count} rows (clean {clean}, clean2 {clean2})"], capture_output=True)
        subprocess.run([git, "push"], capture_output=True)
        await update.message.reply_text(
            f"✅ <b>ML датасет экспортирован</b>\n"
            f"📊 Строк: <b>{count}</b>\n"
            f"📁 Файл: <code>docs/ml_dataset.json</code>\n"
            f"🧼 Clean: <b>{clean}</b> строк → <code>docs/ml_dataset_clean.json</code>\n"
            f"🧼 Clean2: <b>{clean2}</b> строк → <code>docs/ml_dataset_clean2.json</code>\n"
            f"🌐 Доступен на GitHub Pages",
            parse_mode=ParseMode.HTML, reply_markup=_main_kb(),
        )
    except Exception as e:
        await update.message.reply_text(
            f"✅ Экспортировано {count} строк в docs/ml_dataset.json\n"
            f"🧼 Clean: {clean} строк в docs/ml_dataset_clean.json\n"
            f"🧼 Clean2: {clean2} строк в docs/ml_dataset_clean2.json\n"
            f"⚠️ Git push ошибка: {e}",
            reply_markup=_main_kb(),
        )


async def cmd_mlquality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Короткий отчёт качества датасета/сбора (контроль мусора)."""
    collect = is_collect_ml_data()
    rep = get_ml_quality_report(hours=24, goal_clean2=200)
    lines = [
        "🧪 <b>ML Quality (24h)</b>\n━━━━━━━━━━━━━━━",
        ("🟢 Сбор ML-данных: ВКЛ" if collect else "🔴 Сбор ML-данных: ВЫКЛ"),
        "━━━━━━━━━━━━━━━",
        f"📦 Clean2 всего: <b>{rep['clean2_total']}</b> / {rep['goal_clean2']}  "
        f"(осталось <b>{rep['left_to_goal']}</b>)",
        "━━━━━━━━━━━━━━━",
        f"🧾 Размечено за 24ч: <b>{rep['labeled']}</b>",
        f"🧼 Clean2 за 24ч:    <b>{rep['clean2']}</b>",
        f"🏆 Winrate clean2:   <b>{rep['winrate_clean2']}%</b>",
        "━━━━━━━━━━━━━━━",
        f"🗑️ EXPIRED rate:     <b>{rep['expired_rate']}%</b>",
        f"🧱 NOT_FILLED rate:  <b>{rep['not_filled_rate']}%</b>",
        "━━━━━━━━━━━━━━━",
        f"⭐ High-conf (жёстко): <b>{rep['high_conf_n']}</b> · wr <b>{rep['high_conf_wr']}%</b>",
        f"⭐ High-conf (мягче):  <b>{rep['mid_conf_n']}</b> · wr <b>{rep['mid_conf_wr']}%</b>",
        f"⚡ Быстрые исходы ≤2м: <b>{rep.get('fast_rate', 0)}%</b>  "
        f"(n={rep.get('fast_n', 0)} | TP1={rep.get('fast_tp1', 0)} "
        f"TP2={rep.get('fast_tp2', 0)} SL={rep.get('fast_sl', 0)})",
        "\n<i>Цель: уменьшать EXPIRED/NOT_FILLED и растить Clean2.</i>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=_main_kb())

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    signals = get_active_signals()
    if not signals:
        await update.message.reply_text("📭 Нет активных сигналов", reply_markup=_main_kb())
        return
    for s in signals[:5]:
        kb = _signal_keyboard(s["symbol"], s["direction"])
        await update.message.reply_text(
            format_signal_message(s),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades = get_open_trades()
    if not trades:
        await update.message.reply_text("📭 Нет открытых позиций", reply_markup=_main_kb())
        return
    auto_cnt   = sum(1 for t in trades if t.get("auto_taken"))
    manual_cnt = len(trades) - auto_cnt
    await update.message.reply_text(
        f"📊 <b>Открытых позиций: {len(trades)}</b>\n"
        f"🤖 Авто: {auto_cnt}  👤 Ручных: {manual_cnt}",
        parse_mode=ParseMode.HTML, reply_markup=_main_kb(),
    )
    for t in trades:
        text, kb = _format_position_card(t)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades    = get_all_trades()
    if not trades:
        await update.message.reply_text("📭 Нет сделок", reply_markup=_main_kb())
        return
    closed    = [t for t in trades if t["status"] not in ("OPEN", "TP1_HIT")]
    wins      = [t for t in closed if t["status"] in ("TP1","TP2","SL_AFTER_TP1")]
    losses    = [t for t in closed if t["status"] == "SL"]
    open_     = [t for t in trades if t["status"] in ("OPEN","TP1_HIT")]
    total_pnl = sum(t.get("pnl_pct", 0) or 0 for t in closed)
    winrate   = round(len(wins) / len(closed) * 100, 1) if closed else 0
    avg_win   = round(sum(t.get("pnl_pct",0) for t in wins) / len(wins), 2) if wins else 0
    avg_loss  = round(sum(t.get("pnl_pct",0) for t in losses) / len(losses), 2) if losses else 0
    auto_wins = sum(1 for t in wins if t.get("auto_taken"))
    man_wins  = len(wins) - auto_wins
    await update.message.reply_text(
        f"📊 <b>P&L Статистика</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 Всего сделок: <b>{len(trades)}</b>\n"
        f"✅ Профит: <b>{len(wins)}</b>  (avg +{avg_win:.2f}%)  🤖{auto_wins} 👤{man_wins}\n"
        f"❌ Стоп:   <b>{len(losses)}</b>  (avg {avg_loss:.2f}%)\n"
        f"🔓 Открытых: <b>{len(open_)}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏆 Winrate:       <b>{winrate}%</b>\n"
        f"💰 Суммарный P&L: <b>{total_pnl:+.2f}%</b>\n",
        parse_mode=ParseMode.HTML, reply_markup=_main_kb(),
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status IN ('TP1','TP2') THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN status = 'SL' THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END) as active
        FROM signals
    """)
    row = c.fetchone()
    conn.close()
    total, wins, losses, active = row
    closed  = (wins or 0) + (losses or 0)
    winrate = round(wins / closed * 100, 1) if closed > 0 else 0
    await update.message.reply_text(
        f"📊 <b>Статистика сигналов</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Всего:       <b>{total or 0}</b>\n"
        f"✅ Профит:   <b>{wins or 0}</b>\n"
        f"❌ Стоп:     <b>{losses or 0}</b>\n"
        f"📡 Активных: <b>{active or 0}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏆 Winrate: <b>{winrate}%</b>",
        parse_mode=ParseMode.HTML, reply_markup=_main_kb(),
    )


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_filter_stats(hours=24)
    lines = [
        f"🔍 <b>Фильтрация за 24ч</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 Оценено: <b>{stats['total']}</b>\n"
        f"✅ Прошло:  <b>{stats['passed']}</b>\n"
        f"🚫 Отсеяно: <b>{stats['blocked']}</b>\n"
        f"📈 Пропуск: <b>{stats['pass_rate']}%</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<b>Топ причин блокировки:</b>"
    ]
    for i, b in enumerate(stats["top_blocks"][:8], 1):
        lines.append(f"{i}. {b['reason']} — <b>{b['count']}×</b>")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=_main_kb(),
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_signal_history()
    await update.message.reply_text(
        "🗑 <b>История сигналов очищена</b>",
        parse_mode=ParseMode.HTML, reply_markup=_main_kb(),
    )


# ── Обработчик кнопок ReplyKeyboard ──────────────────────────────────────────

async def handle_keyboard_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if text == "📡 Сигналы":
        await cmd_signals(update, context)
    elif text == "📊 Позиции":
        await cmd_positions(update, context)
    elif text == "💰 P&L":
        await cmd_pnl(update, context)
    elif text == "📈 Статистика":
        await cmd_stats(update, context)
    elif text == "🔍 Фильтры":
        await cmd_filters(update, context)
    elif text == "📦 ML Данные":
        await cmd_mlstats(update, context)
    elif text == "📤 Экспорт ML":
        await cmd_exportml(update, context)
    elif "Авто-вход" in text:
        # Кнопка авто-входа — и ВКЛ и ВЫКЛ вариант
        await cmd_autotake(update, context)


# ── Build app ─────────────────────────────────────────────────────────────────

def build_app():
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    entry_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(handle_callback, pattern=r"^editentry:"),
        ],
        states={
            WAITING_ENTRY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_entry),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_entry_edit)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("signals",   cmd_signals))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("pnl",       cmd_pnl))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("filters",   cmd_filters))
    app.add_handler(CommandHandler("autotake",  cmd_autotake))
    app.add_handler(CommandHandler("mlstats",   cmd_mlstats))
    app.add_handler(CommandHandler("mlquality", cmd_mlquality))
    app.add_handler(CommandHandler("exportml",   cmd_exportml))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("cancel",    cancel_entry_edit))
    app.add_handler(entry_conv)
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_keyboard_buttons
    ))
    return app