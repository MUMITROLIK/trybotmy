"""
candle_cache.py — кэш свечей через WebSocket

Держит актуальные свечи для топ-50 монет в памяти.
Обновляется в реальном времени через Binance WebSocket.
Сканер читает из кэша — мгновенно.

Автообновление списка монет каждый час — новые листинги попадают в кэш.
"""

import asyncio
import threading
import time
import pandas as pd
import ccxt
import ccxt.pro as ccxtpro
from datetime import datetime
from typing import List, Dict


class CandleCache:
    def __init__(self, symbols: List[str], timeframe: str = '5m', limit: int = 100, auto_update_symbols: bool = True):
        self.symbols = symbols
        self.timeframe = timeframe
        self.limit = limit
        self.cache: Dict[str, pd.DataFrame] = {}
        self.ready = False
        self._loop = None
        self._thread = None
        self.running = False
        self.auto_update_symbols = auto_update_symbols
        self._last_symbol_update = time.time()

    def start(self):
        """Запускает кэш — сначала REST, потом WebSocket"""
        print(f"[CandleCache] Инициализация {len(self.symbols)} монет...", flush=True)
        
        # Шаг 1: Загружаем начальные данные через REST параллельно
        self._load_initial_candles()
        
        # Шаг 2: Запускаем WebSocket для обновлений
        self.running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True
        )
        self._thread.start()
        
        self.ready = True
        print(f"[CandleCache] Готов! {len(self.cache)} монет в кэше", flush=True)

    def _run_loop(self):
        """Запускает event loop в отдельном потоке"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._ws_feed())

    def _load_initial_candles(self):
        """Загружает начальные свечи через REST параллельно"""
        from concurrent.futures import ThreadPoolExecutor
        
        exchange = ccxt.binanceusdm()
        
        def fetch_one(symbol):
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, self.timeframe, limit=self.limit)
                df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
                df['time'] = pd.to_datetime(df['time'], unit='ms')
                self.cache[symbol] = df
                print(f"  [CandleCache] ✓ {symbol}", flush=True)
            except Exception as e:
                print(f"  [CandleCache] ✗ {symbol}: {e}", flush=True)
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(fetch_one, self.symbols)
        
        print(f"[CandleCache] REST загрузка завершена: {len(self.cache)}/{len(self.symbols)}", flush=True)

    async def _ws_feed(self):
        """WebSocket — обновляет последнюю свечу в реальном времени + автообновление списка монет"""
        exchange = ccxtpro.binanceusdm()
        
        print("[CandleCache] WebSocket запущен", flush=True)
        
        while self.running:
            try:
                # ── Автообновление списка монет каждый час ───────────────────
                if self.auto_update_symbols:
                    now = time.time()
                    if now - self._last_symbol_update > 3600:  # 1 час
                        await self._update_symbols()
                        self._last_symbol_update = now
                
                # ── Обновление свечей через WebSocket ────────────────────────
                for symbol in list(self.symbols):  # list() чтобы избежать изменения во время итерации
                    if symbol not in self.cache:
                        continue
                    
                    try:
                        ohlcv = await exchange.watch_ohlcv(symbol, self.timeframe)
                        
                        if not ohlcv:
                            continue
                        
                        df = self.cache[symbol]
                        
                        # Берём последнюю свечу из WebSocket
                        candle = ohlcv[-1]
                        candle_time = pd.to_datetime(candle[0], unit='ms')
                        last_time = df['time'].iloc[-1]
                        
                        if candle_time > last_time:
                            # Новая свеча — добавляем
                            new_row = pd.DataFrame([[
                                candle_time, candle[1], candle[2], 
                                candle[3], candle[4], candle[5]
                            ]], columns=['time', 'open', 'high', 'low', 'close', 'volume'])
                            
                            self.cache[symbol] = pd.concat(
                                [df.iloc[1:], new_row], ignore_index=True
                            )
                        else:
                            # Обновляем текущую свечу (она ещё не закрылась)
                            df.iloc[-1] = [
                                candle_time, candle[1], candle[2],
                                candle[3], candle[4], candle[5]
                            ]
                    except Exception as e:
                        # Ошибка для конкретного символа — пропускаем
                        continue
                
                await asyncio.sleep(0.1)  # Небольшая задержка между итерациями
            
            except Exception as e:
                print(f"[CandleCache] WS ошибка: {e}, переподключаюсь...", flush=True)
                await asyncio.sleep(3)
        
        await exchange.close()

    async def _update_symbols(self):
        """Обновляет список монет — добавляет новые листинги"""
        try:
            print("[CandleCache] Обновление списка монет...", flush=True)
            
            # Получаем актуальный топ монет
            from data.binance_client import get_top_futures_async
            new_symbols = await get_top_futures_async(len(self.symbols))
            
            # Находим новые монеты
            added = set(new_symbols) - set(self.symbols)
            removed = set(self.symbols) - set(new_symbols)
            
            if added:
                print(f"[CandleCache] Новые монеты: {', '.join(added)}", flush=True)
                # Загружаем свечи для новых монет
                exchange = ccxt.binanceusdm()
                for symbol in added:
                    try:
                        ohlcv = exchange.fetch_ohlcv(symbol, self.timeframe, limit=self.limit)
                        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
                        df['time'] = pd.to_datetime(df['time'], unit='ms')
                        self.cache[symbol] = df
                        print(f"  [CandleCache] ✓ {symbol} добавлен", flush=True)
                    except Exception as e:
                        print(f"  [CandleCache] ✗ {symbol}: {e}", flush=True)
            
            if removed:
                print(f"[CandleCache] Удалены монеты: {', '.join(removed)}", flush=True)
                for symbol in removed:
                    self.cache.pop(symbol, None)
            
            # Обновляем список
            self.symbols = new_symbols
            
            if added or removed:
                print(f"[CandleCache] Обновлено: {len(self.cache)} монет в кэше", flush=True)
            else:
                print(f"[CandleCache] Список не изменился", flush=True)
                
        except Exception as e:
            print(f"[CandleCache] Ошибка обновления списка: {e}", flush=True)

    def get(self, symbol: str) -> pd.DataFrame:
        """Возвращает свечи из кэша — мгновенно"""
        return self.cache.get(symbol, pd.DataFrame()).copy()

    def stop(self):
        self.running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        print("[CandleCache] Остановлен", flush=True)


# Глобальный экземпляр
_candle_cache = None


def init_candle_cache(symbols: List[str]) -> CandleCache:
    """Инициализирует глобальный кэш"""
    global _candle_cache
    _candle_cache = CandleCache(symbols, timeframe='5m', limit=100)
    _candle_cache.start()
    return _candle_cache


def get_cached_candles(symbol: str) -> pd.DataFrame:
    """Получить свечи из кэша"""
    global _candle_cache
    if _candle_cache is None or not _candle_cache.ready:
        return pd.DataFrame()
    return _candle_cache.get(symbol)


def stop_candle_cache():
    """Остановить кэш"""
    global _candle_cache
    if _candle_cache:
        _candle_cache.stop()
