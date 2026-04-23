import json
import os
from datetime import datetime, timezone
from database.db import get_active_signals, get_conn, get_open_trades, is_auto_take
from data.fear_greed import get_cached as get_fg
from github_push import get_publish_status
import requests
import config


def send_to_websocket(data_dict: dict):
    """Отправляет обновления на WebSocket/API сервер (если запущен)."""
    try:
        requests.post(
            f"{getattr(config, 'WEB_SERVER_URL', 'http://localhost:8000')}/api/update-data",
            json=data_dict,
            timeout=3,
        )
    except Exception:
        pass  # Server not running / no network / etc.


def export_to_json(path: str = "docs/data.json"):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    signals = get_active_signals()
    signals_out = [{
        "id": s["id"], "symbol": s["symbol"], "direction": s["direction"],
        "exchange": s.get("exchange", "binance"),
        "entry": s["entry_price"], "tp1": s["tp1"], "tp2": s["tp2"], "sl": s["sl"],
        "strength": s["strength"], "reasons": s["reasons"],
        "news": s.get("news_title", ""), "created_at": s["created_at"], "status": s["status"],
    } for s in signals]

    def _dedupe_by_symbol_dir(trades: list) -> list:
        # Keep latest by id for each symbol+direction
        m = {}
        for t in trades or []:
            key = f"{t.get('symbol','')}_{t.get('direction','')}"
            cur = m.get(key)
            if not cur or (t.get("id", 0) or 0) > (cur.get("id", 0) or 0):
                m[key] = t
        return list(m.values())

    # Открытые позиции + лимитки (pending)
    open_trades = get_open_trades(include_pending=True)
    active_trades = [t for t in open_trades if t.get("status") in ("OPEN", "TP1_HIT")]
    pending_trades = _dedupe_by_symbol_dir([t for t in open_trades if t.get("status") == "PENDING"])

    trades_open_out = []
    for t in open_trades:
        # исходные уровни (если есть в БД) — нужны фронту для стабильного прогресс-бара
        orig_entry = t.get("orig_entry_price") if isinstance(t, dict) else None
        orig_tp1   = t.get("orig_tp1") if isinstance(t, dict) else None
        orig_tp2   = t.get("orig_tp2") if isinstance(t, dict) else None
        orig_sl    = t.get("orig_sl") if isinstance(t, dict) else None
        trades_open_out.append({
            "id": t["id"],
            "symbol": t["symbol"],
            "direction": t["direction"],
            "exchange": t.get("exchange", "binance"),
            "entry": t["entry_price"],
            "tp1": t["tp1"],
            "tp2": t["tp2"],
            "sl":  t["sl"],
            "entry_orig": orig_entry if orig_entry is not None else t["entry_price"],
            "tp1_orig":   orig_tp1 if orig_tp1 is not None else t["tp1"],
            "tp2_orig":   orig_tp2 if orig_tp2 is not None else t["tp2"],
            "sl_orig":    orig_sl if orig_sl is not None else t["sl"],
            "strength": t.get("strength", 0),
            "taken_at": t["taken_at"],
            "status": t["status"],
            "reasons": t.get("reasons", []),
        })

    # ── Trade stats + история закрытий (БЕЗ LIMIT=100) ───────────────────────
    # Важно: database.db.get_all_trades() возвращает LIMIT 100, из‑за этого
    # счётчики wins/losses и "последняя сделка" могут отставать при большом количестве сделок.
    conn = get_conn()
    c = conn.cursor()

    # История закрытых сделок (последние 200 по времени закрытия)
    c.execute("""
        SELECT symbol, direction, entry_price, tp1, tp2, sl,
               strength, status, pnl_pct, close_price, taken_at, closed_at
        FROM taken_trades
        WHERE status NOT IN ('OPEN', 'TP1_HIT', 'PENDING')
        ORDER BY datetime(COALESCE(closed_at, taken_at)) DESC
        LIMIT 200
    """)
    trades_history_out = [{
        "symbol":      r[0],
        "direction":   r[1],
        "entry":       r[2],
        "tp1":         r[3],
        "tp2":         r[4],
        "sl":          r[5],
        "strength":    r[6] or 0,
        "status":      r[7],
        "pnl_pct":     r[8] or 0,
        "close_price": r[9],
        "taken_at":    r[10],
        "closed_at":   r[11],
    } for r in c.fetchall()]

    # Агрегаты по всем закрытым сделкам
    c.execute("""
        SELECT status, pnl_pct
        FROM taken_trades
        WHERE status NOT IN ('OPEN', 'TP1_HIT', 'PENDING', 'LIMIT_TIMEOUT')
    """)
    closed_rows = c.fetchall()
    conn.close()

    def _is_win_row(st: str, pnl: float) -> bool:
        return st in ("TP1", "TP2", "SL_AFTER_TP1") or (st in ("MANUAL", "TIMEOUT") and (pnl or 0) >= 0)

    def _is_loss_row(st: str, pnl: float) -> bool:
        return st == "SL" or (st in ("MANUAL", "TIMEOUT") and (pnl or 0) < 0)

    t_wins_cnt = sum(1 for st, pnl in closed_rows if _is_win_row(st, pnl))
    t_losses_cnt = sum(1 for st, pnl in closed_rows if _is_loss_row(st, pnl))
    all_closed_cnt = len(closed_rows)
    t_pnl = sum((pnl or 0) for _, pnl in closed_rows)
    t_wr = round(t_wins_cnt / all_closed_cnt * 100, 1) if all_closed_cnt else 0

    # Статистика сигналов
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
    c.execute("""
        SELECT symbol, direction, entry_price, tp1, tp2, sl,
               strength, status, created_at, reasons
        FROM signals WHERE status != 'ACTIVE'
        ORDER BY created_at DESC LIMIT 20
    """)
    history = [{
        "symbol": r[0], "direction": r[1], "entry": r[2],
        "tp1": r[3], "tp2": r[4], "sl": r[5], "strength": r[6],
        "status": r[7], "created_at": r[8],
        "reasons": json.loads(r[9]) if r[9] else [],
    } for r in c.fetchall()]
    conn.close()

    total, wins, losses, active = row
    wins    = wins   or 0
    losses  = losses or 0
    closed  = wins + losses
    winrate = round(wins / closed * 100, 1) if closed > 0 else 0

    fg = get_fg()

    # ── ML мета-информация (название модели, AUC, дата обучения) ─────────────
    ml_info = {"model_name": "—", "roc_auc": None, "rows": 0, "trained_at": None}
    try:
        import json as _json
        from pathlib import Path as _Path
        _meta_path = _Path("ml/model_meta.json")
        if _meta_path.exists():
            _meta = _json.loads(_meta_path.read_text(encoding="utf-8"))
            ml_info = {
                "model_name": _meta.get("model_name", "LogisticRegression"),
                "roc_auc":    _meta.get("roc_auc"),
                "rows":       _meta.get("rows", 0),
                "trained_at": _meta.get("trained_at"),
            }
    except Exception:
        pass

    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "publish_status": get_publish_status(),
        "settings": {
            "auto_take": bool(is_auto_take()),
            "limit_order_ttl_minutes": getattr(__import__("config"), "LIMIT_ORDER_TTL_MINUTES", 60),
        },
        "stats": {
            "total": total or 0, "wins": wins, "losses": losses,
            "active": active or 0, "winrate": winrate,
        },
        "trade_stats": {
            "open":      len(active_trades),
            "pending":   len(pending_trades),
            "wins":      t_wins_cnt,
            "losses":    t_losses_cnt,
            "total_pnl": round(t_pnl, 2),
            "winrate":   t_wr,
        },
        "fear_greed": {
            "value": fg.get("value", 50),
            "label": fg.get("label", "Neutral"),
            "emoji": fg.get("emoji", "😐"),
        },
        "ml_info": ml_info,
        "signals":        signals_out,
        "history":        history,
        "trades_open":    trades_open_out,
        "trades_history": trades_history_out,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    send_to_websocket(data)
    return data