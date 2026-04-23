import asyncio
import json
import os
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import uvicorn
import time

import httpx

app = FastAPI()

# ── Директория с файлами ──────────────────────────────────────────
DOCS_DIR = Path(__file__).parent / "docs"
DATA_FILE = DOCS_DIR / "data.json"

# ── WebSocket manager ──────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"Client connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        print(f"Client disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Отправляет сообщение всем подключённым клиентам"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"Send error: {e}")
                disconnected.append(connection)
        
        # Удаляем неживых клиентов
        for conn in disconnected:
            self.disconnect(conn)

manager = ConnectionManager()

# ── Simple in-memory cache for Binance API ───────────────────────────────────
_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL_SEC = 5.0
_CACHE_STALE_TTL_SEC = 60.0

async def _get_json_cached(key: str, url: str, timeout: float = 6.0):
    now = time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL_SEC:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        _cache[key] = (now, data)
        return data
    except Exception as e:
        # If upstream is flaky/rate-limited, return last good value for a while.
        if hit and (now - hit[0]) < _CACHE_STALE_TTL_SEC:
            return hit[1]
        # Re-raise with helpful context for UI debugging
        if isinstance(e, httpx.HTTPStatusError):
            st = e.response.status_code
            txt = (e.response.text or "")[:200]
            raise RuntimeError(f"HTTP {st} for {url} | {txt}") from e
        if isinstance(e, httpx.RequestError):
            raise RuntimeError(f"Request error for {url}: {e}") from e
        raise

# ── Routes ─────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """Раздаём index.html"""
    html_file = DOCS_DIR / "index.html"
    if html_file.exists():
        return FileResponse(
            html_file,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )
    return {"error": "index.html not found"}

@app.get("/data.json")
async def get_data():
    """API для получения текущих данных (backup для старого кода)"""
    if DATA_FILE.exists():
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return JSONResponse(
                content=json.load(f),
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                },
            )
    return JSONResponse(content={})

@app.post("/api/update-data")
async def update_data(data: dict):
    """
    POST endpoint для обновления данных от Python бота.
    Сохраняет в data.json и broadcast'ит на WebSocket.
    """
    try:
        # Сохраняем в файл
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"Data updated at {datetime.now().isoformat()}")
        
        # Broadcast на все подключённые WebSocket клиенты
        await manager.broadcast({
            "type": "data_update",
            "data": data,
            "timestamp": datetime.now().isoformat()
        })
        
        return {"status": "ok"}
    except Exception as e:
        print(f"Update error: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/klines")
async def api_klines(
    symbol: str = Query(..., min_length=3, max_length=20),
    interval: str = Query("1m"),
    limit: int = Query(100, ge=1, le=1000),
):
    """
    Proxy for Binance Futures klines.
    Using server-side request avoids browser CORS/network quirks.
    """
    sym = symbol.upper()
    # Basic allowlist for interval
    allowed = {"1m","3m","5m","15m","30m","1h","2h","4h","6h","8h","12h","1d"}
    if interval not in allowed:
        raise HTTPException(status_code=400, detail="bad interval")
    fut_url  = f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={interval}&limit={limit}"
    spot_url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}"
    try:
        key = f"klines:fapi:{sym}:{interval}:{limit}"
        return await _get_json_cached(key, fut_url)
    except Exception as e_fut:
        # Fallback to Binance Spot if futures endpoint is blocked/unstable.
        try:
            key = f"klines:spot:{sym}:{interval}:{limit}"
            return await _get_json_cached(key, spot_url)
        except Exception as e_spot:
            raise HTTPException(status_code=502, detail=f"binance klines error: fut={e_fut} spot={e_spot}")


@app.get("/api/ticker")
async def api_ticker(
    symbol: str | None = Query(None, min_length=3, max_length=20),
    symbols: str | None = Query(None, description="Comma-separated symbols, e.g. BTCUSDT,ETHUSDT"),
):
    """
    Proxy for Binance Futures ticker/price.
    Returns either a single object or a map for multiple symbols.
    """
    if not symbol and not symbols:
        raise HTTPException(status_code=400, detail="symbol or symbols required")

    # Normalize symbols list
    syms: list[str]
    if symbols:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        syms = [symbol.strip().upper()]  # type: ignore[union-attr]

    # Bulk tickers (fast): 1 upstream request per exchange.
    async def _bn_all_prices() -> dict[str, float]:
        url = "https://fapi.binance.com/fapi/v1/ticker/price"
        key = "ticker:bn:all"
        data = await _get_json_cached(key, url, timeout=6.0)
        out: dict[str, float] = {}
        if isinstance(data, list):
            for it in data:
                try:
                    s = str(it.get("symbol", "")).upper()
                    p = float(it.get("price", 0) or 0)
                    if s and p > 0:
                        out[s] = p
                except Exception:
                    continue
        return out

    async def _by_all_prices() -> dict[str, float]:
        url = "https://api.bybit.com/v5/market/tickers?category=linear"
        key = "ticker:by:all"
        data = await _get_json_cached(key, url, timeout=6.0)
        out: dict[str, float] = {}
        try:
            rows = (((data or {}).get("result") or {}).get("list") or [])
            for t in rows:
                s = str(t.get("symbol", "")).upper()
                if not s:
                    continue
                try:
                    p = float(t.get("lastPrice") or t.get("markPrice") or t.get("indexPrice") or 0)
                except Exception:
                    p = 0.0
                if p > 0:
                    out[s] = p
        except Exception:
            pass
        return out

    async def _get_prices_for(syms: list[str]) -> dict[str, float]:
        bn_task = asyncio.create_task(_bn_all_prices())
        by_task = asyncio.create_task(_by_all_prices())
        bn_map, by_map = await asyncio.gather(bn_task, by_task)
        out: dict[str, float] = {}
        for s in syms:
            p = bn_map.get(s) or by_map.get(s) or 0.0
            out[s] = float(p or 0.0)
        return out

    try:
        if len(syms) == 1:
            prices = await _get_prices_for([syms[0]])
            sym0 = syms[0]
            return {"symbol": sym0, "price": prices.get(sym0, 0.0)}

        out = await _get_prices_for(syms)
        return {"prices": out}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"binance error: {e}")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint для realtime обновлений"""
    await manager.connect(websocket)
    
    try:
        # Сразу отправляем текущие данные новому клиенту
        if DATA_FILE.exists():
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                current_data = json.load(f)
            await websocket.send_json({
                "type": "initial_data",
                "data": current_data,
                "timestamp": datetime.now().isoformat()
            })
        
        # Держим соединение живым: ping/pong и таймаут на receive.
        # Некоторые прокси/браузеры рвут "молчаливые" WS.
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=35)
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # Server-initiated ping (клиент может просто игнорировать)
                try:
                    await websocket.send_json({"type": "ping", "ts": datetime.now().isoformat()})
                except Exception:
                    raise
    
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        # Часто бывает если вкладка/браузер закрывает WS без close frame.
        print(f"WebSocket closed: {e}")
        manager.disconnect(websocket)

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "clients": len(manager.active_connections),
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/ml-stats")
async def ml_stats():
    """ML датасет статистика для UI"""
    try:
        from database.db import get_ml_dataset_stats, get_conn
        s = get_ml_dataset_stats()

        # Считаем только чистые строки (с market_regime — новые данные после фикса)
        conn = get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*) FROM ml_features
            WHERE labeled = 1
            AND market_regime IS NOT NULL
            AND COALESCE(target,'') NOT IN ('EXPIRED','NOT_FILLED')
        """)
        clean_new = c.fetchone()[0] or 0

        c.execute("""
            SELECT SUM(CASE WHEN target IN ('TP1','TP2','SL_AFTER_TP1') THEN 1 ELSE 0 END),
                   SUM(CASE WHEN target = 'SL' THEN 1 ELSE 0 END)
            FROM ml_features
            WHERE labeled = 1
            AND market_regime IS NOT NULL
            AND COALESCE(target,'') NOT IN ('EXPIRED','NOT_FILLED')
        """)
        row = c.fetchone()
        wins_clean = row[0] or 0
        losses_clean = row[1] or 0
        wr_clean = round(wins_clean / (wins_clean + losses_clean) * 100, 1) \
                   if (wins_clean + losses_clean) > 0 else 0
        conn.close()

        # Прогресс до переобучения
        try:
            from ml_auto_trainer import _last_trained_count, RETRAIN_EVERY_N_TRADES
            new_since_last = max(0, clean_new - _last_trained_count)
            until_retrain = max(0, RETRAIN_EVERY_N_TRADES - new_since_last)
        except Exception:
            new_since_last = 0
            until_retrain = 0

        goal = 600  # цель для удаления старых данных
        return {
            "total": s["total"],
            "labeled": s["labeled"],
            "clean2": s["labeled_clean2"],
            "clean_new": clean_new,
            "winrate_clean2": s["winrate_clean2"],
            "winrate_clean_new": wr_clean,
            "goal": goal,
            "new_since_last": new_since_last,
            "until_retrain": until_retrain,
            "progress_pct": round(clean_new / goal * 100, 1)
        }
    except Exception as e:
        return {"error": str(e)}

# ── Static files (docs/) ───────────────────────────────────────────
# Важно: монтируем ПОСЛЕ API/WebSocket routes, чтобы не перехватывать /data.json и /api/*
app.mount("/", StaticFiles(directory=str(DOCS_DIR), html=True), name="docs")

if __name__ == "__main__":
    # Проверяем что папка docs существует
    if not DOCS_DIR.exists():
        print(f"ERROR: {DOCS_DIR} не найдена!")
        print(f"Создайте папку и положите туда index.html и data.json")
        exit(1)
    
    # Без эмодзи: Windows консоль часто в cp1251/cp866 и падает на Unicode.
    print(f"Serving files from: {DOCS_DIR}")
    print(f"Data file: {DATA_FILE}")
    print("Starting server at http://localhost:8000")
    print("WebSocket at ws://localhost:8000/ws")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
