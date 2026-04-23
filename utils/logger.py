"""
utils/logger.py — цветные логи для CashTrack Bot
Положи этот файл в папку utils/ (создай её если нет)
"""

from colorama import init, Fore, Back, Style
from datetime import datetime, timezone

init(autoreset=True)  # Windows требует явной инициализации

# ── Цвета по категориям ───────────────────────────────────────────────────────
G  = Fore.GREEN
Y  = Fore.YELLOW
R  = Fore.RED
C  = Fore.CYAN
M  = Fore.MAGENTA
W  = Fore.WHITE
DIM = Style.DIM
BR  = Style.BRIGHT
RST = Style.RESET_ALL


def _ts() -> str:
    """Текущее время UTC для префикса."""
    return Fore.WHITE + Style.DIM + datetime.now(timezone.utc).strftime("%H:%M:%S") + " " + Style.RESET_ALL


# ── Основные функции ──────────────────────────────────────────────────────────

def info(tag: str, msg: str):
    """Синий — общая информация, запуск компонентов."""
    print(f"{_ts()}{C}{BR}[{tag}]{RST} {msg}")


def ok(tag: str, msg: str):
    """Зелёный — успех, сигнал найден, TP."""
    print(f"{_ts()}{G}{BR}[{tag}]{RST} {G}{msg}{RST}")


def warn(tag: str, msg: str):
    """Жёлтый — предупреждение, близко к SL, funding."""
    print(f"{_ts()}{Y}{BR}[{tag}]{RST} {Y}{msg}{RST}")


def err(tag: str, msg: str):
    """Красный — ошибка, SL, стоп."""
    print(f"{_ts()}{R}{BR}[{tag}]{RST} {R}{msg}{RST}")


def dim(tag: str, msg: str):
    """Серый — пустые сканы, монеты без сигнала."""
    print(f"{_ts()}{DIM}[{tag}] {msg}{RST}")


def ml(tag: str, msg: str):
    """Фиолетовый — ML события, обучение."""
    print(f"{_ts()}{M}{BR}[{tag}]{RST} {M}{msg}{RST}")


def trade_result(symbol: str, direction: str, status: str, pnl: float, price: float):
    """Специальный формат для закрытия сделок."""
    dir_color = G if direction == "LONG" else R
    dir_arrow = "▲" if direction == "LONG" else "▼"

    if status in ("TP1", "TP2"):
        s_color = G
        s_icon  = "✅"
    elif status == "SL":
        s_color = R
        s_icon  = "❌"
    elif status == "SL_AFTER_TP1":
        s_color = Y
        s_icon  = "🔒"
    elif status == "TIMEOUT":
        s_color = Y
        s_icon  = "⏰"
    else:
        s_color = W
        s_icon  = "➖"

    pnl_color = G if pnl >= 0 else R
    pnl_str   = f"+{pnl:.2f}%" if pnl >= 0 else f"{pnl:.2f}%"

    print(
        f"{_ts()}"
        f"{s_icon} {BR}{W}{symbol:<14}{RST}"
        f" {dir_color}{dir_arrow} {direction}{RST}"
        f" → {s_color}{BR}{status}{RST}"
        f"  {pnl_color}{BR}{pnl_str}{RST}"
        f"  @ {W}{price:.6g}{RST}"
    )


def signal_found(idx: int, total: int, exch: str, symbol: str,
                 price: float, change: float, vol_s: str,
                 direction: str, strength: int,
                 tp_pct: float, sl_pct: float,
                 mtf: float, cvd: float, vol_score: float):
    """Яркий зелёный/красный для найденного сигнала."""
    dir_color = G if direction == "LONG" else R
    dir_arrow = "▲ LONG" if direction == "LONG" else "▼ SHORT"
    chg_color = G if change >= 0 else R
    str_color = G if strength >= 70 else Y if strength >= 50 else R

    exch_colors = {"BN": Fore.YELLOW, "BY": Fore.MAGENTA, "OK": Fore.BLUE}
    ec = exch_colors.get(exch, W)

    print(
        f"{_ts()}"
        f"{G}{BR}✅ {idx:02d}/{total}{RST}"
        f" {ec}[{exch}]{RST}"
        f" {BR}{W}{symbol:<14}{RST}"
        f" ${price:<10.4f}"
        f" {chg_color}{change:+.1f}%{RST}"
        f" vol={W}{vol_s}{RST}"
        f"  {dir_color}{BR}{dir_arrow}{RST}"
        f"  сила={str_color}{BR}{strength}%{RST}"
        f"  TP={G}{tp_pct:.1f}%{RST} SL={R}{sl_pct:.1f}%{RST}"
        f"  [{C}MTF={mtf:.2f} CVD={cvd:.2f} VOL={vol_score:.2f}{RST}]"
    )


def scan_empty(idx: int, total: int, exch: str, symbol: str,
               price: float, change: float, vol_s: str):
    """Серый для монет без сигнала."""
    chg_color = G if change >= 0 else R
    exch_colors = {"BN": Fore.YELLOW, "BY": Fore.MAGENTA, "OK": Fore.BLUE}
    ec = exch_colors.get(exch, W)

    print(
        f"{_ts()}"
        f"{DIM}── {idx:02d}/{total}{RST}"
        f" {DIM}{ec}[{exch}]{RST}"
        f" {DIM}{symbol:<14}{RST}"
        f" {DIM}${price:<10.4f}{RST}"
        f" {chg_color}{DIM}{change:+.1f}%{RST}"
        f" {DIM}vol={vol_s}{RST}"
    )


def scan_header(bn: int, by: int, okx: int, total: int,
                session: str, btc_trend: str, btc_chg: float, blocked_str: str):
    """Заголовок скана."""
    trend_color = G if btc_trend == "bull" else R if btc_trend == "bear" else Y
    chg_color   = G if btc_chg >= 0 else R
    print(
        f"\n{_ts()}"
        f"{C}{BR}[Scanner]{RST}"
        f" ▶ Скан "
        f"{Y}{bn}{RST} BN + {M}{by}{RST} BY + {C}{okx}{RST} OKX"
        f" = {BR}{W}{total}{RST} монет"
        f"  {session}"
        f"  BTC: {trend_color}{btc_trend}{RST}"
        f" ({chg_color}{btc_chg:+.1f}%{RST})"
        f"  {blocked_str}"
    )


def positions_summary(count: int, symbols_str: str):
    """Сводка открытых позиций."""
    color = G if count < 20 else Y if count < 40 else R
    print(
        f"{_ts()}"
        f"{C}[Tracker]{RST}"
        f" 📊 Открытых: {color}{BR}{count}{RST}"
        f"  {DIM}{symbols_str[:120]}{'...' if len(symbols_str) > 120 else ''}{RST}"
    )


def separator(title: str = ""):
    """Разделитель секций."""
    line = "─" * 60
    if title:
        pad = (60 - len(title) - 2) // 2
        print(f"{DIM}{'─'*pad} {W}{title}{RST} {DIM}{'─'*pad}{RST}")
    else:
        print(f"{DIM}{line}{RST}")


def startup_banner(mode: str, scan_interval: int, fg_value: int, fg_label: str):
    """Баннер при запуске."""
    fg_color = G if fg_value >= 60 else R if fg_value <= 25 else Y
    mode_color = G if "AUTO" in mode else Y

    print(f"\n{C}{'═'*55}{RST}")
    print(f"{C}  ██████╗ █████╗ ███████╗██╗  ██╗{RST}")
    print(f"{C}  ██╔════╝██╔══██╗██╔════╝██║  ██║{RST}")
    print(f"{C}  ██║     ███████║███████╗███████║{RST}")
    print(f"{C}  ██║     ██╔══██║╚════██║██╔══██║{RST}")
    print(f"{C}  ╚██████╗██║  ██║███████║██║  ██║{RST}")
    print(f"{C}  ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝{RST}")
    print(f"{C}{'─'*55}{RST}")
    print(f"  {W}Режим:{RST}    {mode_color}{BR}{mode}{RST}")
    print(f"  {W}Интервал:{RST} {Y}{scan_interval}с{RST}")
    print(f"  {W}F&G:{RST}      {fg_color}{BR}{fg_value} — {fg_label}{RST}")
    print(f"{C}{'═'*55}{RST}\n")