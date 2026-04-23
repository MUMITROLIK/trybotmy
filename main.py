import asyncio
import sys
from datetime import datetime, timezone

import config
from utils import logger
from database.db import (init_db, signal_exists_recent, save_signal,
                         is_auto_take, is_collect_ml_data, save_ml_features,
                         update_telegram_msg_id, trade_exists, save_taken_trade,
                         get_filter_stats, migrate_db, trade_exists_symbol,
                         symbol_in_cooldown, get_open_trades, get_signal_log)
from data.binance_client import get_top_futures_async, get_full_data, ws_price_stream
from data.bybit_client import (get_top_futures_async as bybit_get_symbols,
                                get_full_data as bybit_get_full_data)
from data.okx_client import (get_top_futures_async as okx_get_symbols,
                              get_full_data as okx_get_full_data)
from data.news_parser import fetch_all_news
from data.fear_greed import get_fear_greed
from data.market_context import get_btc_context, get_cached_context
from analysis.signal_generator import generate_signal
from tracker.signal_tracker import run_tracker, set_notify_callback
from bot.telegram_bot import build_app, send_signal, send_result
from export_data import export_to_json
from github_push import push_data_json
from ml_auto_trainer import run_auto_trainer, set_notify_callback as ml_set_notify

_dead_symbols: set[str] = set()  # делистированные символы — пропускаем в скане


def startup_check():
    """
    Проверка БД при старте — колонки, статистика, позиции.
    Вызывается один раз после init_db() и migrate_db().
    """
    from database.db import get_conn
    
    logger.info("Startup", "Проверка БД...")
    conn = get_conn()
    c = conn.cursor()
    
    # ── 1. Проверка колонок ml_features ──────────────────────────────────────
    REQUIRED_COLS = [
        "trade_id", "signal_id", "symbol", "direction", "exchange", "session",
        "fear_greed", "btc_trend_4h", "btc_trend_1h", "btc_change_4h", "funding_rate",
        "rsi", "macd_hist", "bb_position", "ema20_50_ratio", "volume_ratio", "atr_pct",
        "cvd_norm", "oi_change_pct", "ob_imbalance", "btc_correlation",
        "score_rsi", "score_macd", "score_bb", "score_ema", "score_volume",
        "score_orderbook", "score_news", "score_mtf", "score_oi", "score_cvd",
        "score_patterns", "score_btc_corr", "score_levels",
        "strength", "heavy_confirmed", "entry_type",
        "hour_of_day", "day_of_week", "market_regime", "atr_percentile", "btc_drawdown_pct",
        "target", "pnl_pct", "duration_min", "labeled"
    ]
    
    c.execute("PRAGMA table_info(ml_features)")
    existing_cols = {row[1] for row in c.fetchall()}
    missing_cols = [col for col in REQUIRED_COLS if col not in existing_cols]
    
    if missing_cols:
        logger.err("Startup", f"Отсутствуют колонки в ml_features: {missing_cols}")
    else:
        logger.ok("Startup", f"Все {len(REQUIRED_COLS)} колонок ml_features на месте")
    
    # ── 2. Статистика датасета ────────────────────────────────────────────────
    c.execute("SELECT COUNT(*) FROM ml_features")
    total = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM ml_features WHERE labeled = 1")
    labeled = c.fetchone()[0]
    
    c.execute("""
        SELECT COUNT(*) FROM ml_features
        WHERE labeled = 1
          AND COALESCE(target, '') NOT IN ('EXPIRED', 'NOT_FILLED')
    """)
    clean2 = c.fetchone()[0]
    
    c.execute("""
        SELECT COUNT(*) FROM ml_features
        WHERE labeled = 1
          AND COALESCE(target, '') NOT IN ('EXPIRED', 'NOT_FILLED')
          AND target IN ('TP1', 'TP2', 'SL_AFTER_TP1')
    """)
    wins = c.fetchone()[0]
    
    winrate = round(wins / clean2 * 100, 1) if clean2 > 0 else 0
    
    logger.info("Startup", f"ML датасет: {total} строк | "
          f"размечено: {labeled} | clean2: {clean2} | winrate: {winrate}%")
    
    # ── 3. Проверка открытых позиций ─────────────────────────────────────────
    c.execute("""
        SELECT id, symbol, direction, entry_price, tp1, tp2, sl
        FROM taken_trades
        WHERE status IN ('OPEN', 'TP1_HIT', 'PENDING')
    """)
    positions = c.fetchall()
    
    null_fields = []
    for p in positions:
        pid, sym, direction, entry, tp1, tp2, sl = p
        if entry is None: null_fields.append(f"{sym} entry_price")
        if tp1 is None: null_fields.append(f"{sym} tp1")
        if tp2 is None: null_fields.append(f"{sym} tp2")
        if sl is None: null_fields.append(f"{sym} sl")
    
    if null_fields:
        logger.warn("Startup", f"Позиции с NULL полями: {null_fields}")
    else:
        logger.ok("Startup", f"Открытые позиции: {len(positions)} — все поля заполнены")
    
    # ── 4. Последняя размеченная строка ──────────────────────────────────────
    c.execute("""
        SELECT symbol, direction, target, score_levels, pnl_pct, duration_min
        FROM ml_features
        WHERE labeled = 1
        ORDER BY id DESC
        LIMIT 1
    """)
    last = c.fetchone()
    
    if last:
        sym, direction, target, score_lvl, pnl, dur = last
        score_lvl_str = f"{score_lvl:.2f}" if score_lvl is not None else "NULL"
        pnl_str = f"{pnl:.2f}%" if pnl is not None else "NULL"
        dur_str = f"{dur}м" if dur is not None else "NULL"
        logger.info("Startup", f"Последняя разметка: {sym} {direction} → {target} | "
              f"score_levels={score_lvl_str} | pnl={pnl_str} | dur={dur_str}")
    else:
        logger.warn("Startup", "Нет размеченных строк в ml_features")
    
    conn.close()
    logger.ok("Startup", "Проверка завершена\n")


async def ws_loop(symbols: list):
    while True:
        try:
            await ws_price_stream(symbols, lambda s, p: None)
        except asyncio.CancelledError:
            raise  # CancelledError is graceful shutdown
        except Exception as e:
            logger.err("WS", f"Обрыв, перезапуск через 5с: {e}")
            await asyncio.sleep(5)


async def scan_loop(app):
    print(f"[Scanner] Запущен, интервал {config.SCAN_INTERVAL}с")
    while True:
        try:
            await run_scan()
        except Exception as e:
            print(f"[Scanner] Ошибка: {e}")
        await asyncio.sleep(config.SCAN_INTERVAL)


async def run_scan():
    def _dedupe_keep_order(items: list[str]) -> list[str]:
        # dict preserves insertion order in py3.7+
        return list(dict.fromkeys(items))

    def _max_coins_for_budget(req_per_min_limit: int) -> int:
        rpm_budget = max(0, int(req_per_min_limit) - int(getattr(config, "REQ_PER_MIN_RESERVE", 50)))
        req_per_coin = max(1, int(getattr(config, "SCAN_REQ_PER_COIN_ESTIMATE", 8)))
        interval_s = max(1, int(getattr(config, "SCAN_INTERVAL", 60)))
        # coins_max = floor(rpm_budget * interval_s / (req_per_coin * 60))
        return max(0, (rpm_budget * interval_s) // (req_per_coin * 60))

    # Binance — основные монеты
    bn_limit = _max_coins_for_budget(getattr(config, "BINANCE_REQ_PER_MIN_LIMIT", 1100))
    bn_wanted = int(getattr(config, "TOP_FUTURES_COUNT", 100))
    bn_take = min(bn_wanted, bn_limit) if bn_limit > 0 else bn_wanted
    binance_symbols = _dedupe_keep_order(await get_top_futures_async(bn_take))
    binance_set     = set(binance_symbols)

    # Bybit — только уникальные монеты которых нет на Binance
    by_limit = _max_coins_for_budget(getattr(config, "BYBIT_REQ_PER_MIN_LIMIT", 500))
    by_wanted = int(getattr(config, "BYBIT_TOP_COUNT", 100))
    bybit_count = min(by_wanted, by_limit) if by_limit > 0 else by_wanted
    bybit_symbols = []
    if bybit_count > 0:
        try:
            print(f"[Bybit] Запрос списка монет: wanted={by_wanted}, "
                  f"limit_by_budget={by_limit}, take={bybit_count}, "
                  f"exclude_binance={len(binance_set)}, "
                  f"min_turnover={getattr(config, 'BYBIT_MIN_VOLUME_USDT', 0)}")
            bybit_symbols = await bybit_get_symbols(bybit_count, exclude=binance_set)
        except Exception as e:
            print(f"[Bybit] Ошибка загрузки монет: {e}")
    else:
        print(f"[Bybit] Пропуск: BYBIT_TOP_COUNT={by_wanted}, "
              f"limit_by_budget={by_limit} → take={bybit_count}")
    bybit_symbols = _dedupe_keep_order([s for s in bybit_symbols if s not in binance_set])
    if bybit_count > 0 and not bybit_symbols:
        print("[Bybit] ⚠️ Список монет пуст. Возможные причины: "
              "слишком высокий BYBIT_MIN_VOLUME_USDT, "
              "ошибка Bybit API, "
              "или почти все монеты пересекаются с Binance.")

    # OKX — только уникальные монеты которых нет на Binance+Bybit
    okx_symbols = []
    okx_set = binance_set | set(bybit_symbols)
    if getattr(config, "OKX_TOP_COUNT", 0) > 0:
        try:
            okx_symbols = await okx_get_symbols(config.OKX_TOP_COUNT, exclude=okx_set)
        except Exception as e:
            print(f"[OKX] Ошибка загрузки монет: {e}")
    okx_symbols = [s for s in okx_symbols if s not in okx_set]

    all_symbols = [(s, "binance") for s in binance_symbols] + \
                  [(s, "bybit")   for s in bybit_symbols]   + \
                  [(s, "okx")     for s in okx_symbols]

    ctx = get_cached_context()

    session_label = {
        "asian":    "🌏 Азия",
        "european": "🌍 Европа",
        "american": "🌎 Америка",
    }.get(ctx.get("session", ""), "")

    btc_info    = f"BTC 4h: {ctx.get('trend_4h','?')} ({ctx.get('change_4h_pct',0):+.1f}%)"
    blocked     = []
    if ctx.get("block_long"):  blocked.append("❌LONG")
    if ctx.get("block_short"): blocked.append("❌SHORT")
    blocked_str = " ".join(blocked) or "✅ все направления"

    _bn_budget = max(0, getattr(config, "BINANCE_REQ_PER_MIN_LIMIT", 1100) - getattr(config, "REQ_PER_MIN_RESERVE", 50))
    _by_budget = max(0, getattr(config, "BYBIT_REQ_PER_MIN_LIMIT", 500) - getattr(config, "REQ_PER_MIN_RESERVE", 50))
    
    logger.scan_header(
        bn=len(binance_symbols), by=len(bybit_symbols), okx=len(okx_symbols),
        total=len(all_symbols), session=session_label,
        btc_trend=ctx.get('trend_4h', '?'),
        btc_chg=ctx.get('change_4h_pct', 0),
        blocked_str=blocked_str
    )
    logger.info("Scanner", f"Лимиты: Binance≤{_bn_budget} req/min, Bybit≤{_by_budget} req/min "
          f"(оценка {getattr(config, 'SCAN_REQ_PER_COIN_ESTIMATE', 8)} req/coin, interval={config.SCAN_INTERVAL}s)")
    if is_auto_take():
        logger.info("Scanner", "AUTO_TAKE включён — все сигналы берутся автоматически")

    signals_sent = 0
    # Лимит сигналов по направлению за один скан
    signals_by_dir = {"LONG": 0, "SHORT": 0}

    # ── Параллельный скан батчами ─────────────────────────────────────────────
    # Binance: 1200 req/min, Bybit: 600 req/min, OKX: 20 req/sec (72000 req/min max)
    # На монету ~8 запросов. Батч 5 монет = 40 запросов.
    # Пауза между батчами: Binance 0.5с, Bybit 1.0с, OKX 2.0с — безопасно для всех.
    BATCH_SIZE       = int(getattr(config, "SCAN_BATCH_SIZE", 5))
    BATCH_PAUSE_BN   = float(getattr(config, "SCAN_BATCH_PAUSE_BN", 0.5))
    BATCH_PAUSE_BY   = float(getattr(config, "SCAN_BATCH_PAUSE_BY", 1.0))
    BATCH_PAUSE_OKX  = float(getattr(config, "OKX_BATCH_PAUSE", 2.0))

    # Семафоры — ограничиваем параллельные запросы к каждой бирже
    sem_binance = asyncio.Semaphore(BATCH_SIZE)
    sem_bybit   = asyncio.Semaphore(BATCH_SIZE)

    async def scan_one(idx: int, symbol: str, exchange: str) -> dict | None:
        """Сканирует одну монету, возвращает результат или None."""
        if symbol in _dead_symbols:
            return None

        sem = sem_binance if exchange == "binance" else (sem_bybit if exchange == "bybit" else sem_bybit)
        async with sem:
            try:
                # ── Используем кэш для Binance (быстро) ──────────────────────
                if exchange == "binance":
                    from core.candle_cache import get_cached_candles
                    c15 = await asyncio.to_thread(get_cached_candles, symbol)
                    
                    # Если нет в кэше — fallback на REST
                    if c15.empty:
                        data = await get_full_data(symbol)
                    else:
                        # Получаем остальные данные параллельно
                        from data.binance_client import (get_candles_async, get_orderbook_async,
                                                          get_price_info_async, get_open_interest)
                        c5, c1h, c4h, ob, pi, oi, btc_1h = await asyncio.gather(
                            get_candles_async(symbol, "5m", 50),
                            get_candles_async(symbol, "1h", 100),
                            get_candles_async(symbol, "4h", 50),
                            get_orderbook_async(symbol),
                            get_price_info_async(symbol),
                            get_open_interest(symbol),
                            get_candles_async("BTCUSDT", "1h", 50),
                        )
                        data = {
                            "symbol": symbol,
                            "candles_5m": c5,
                            "candles_15m": c15,
                            "candles_1h": c1h,
                            "candles_4h": c4h,
                            "orderbook": ob,
                            "price_info": pi,
                            "open_interest": oi,
                            "btc_candles_1h": btc_1h,
                        }
                elif exchange == "bybit":
                    data = await bybit_get_full_data(symbol)
                elif exchange == "okx":
                    data = await okx_get_full_data(symbol)
                else:
                    data = await get_full_data(symbol)
                
                signal = generate_signal(data)
                pi     = data.get("price_info", {})
                return {
                    "idx": idx, "symbol": symbol, "exchange": exchange,
                    "signal": signal, "pi": pi,
                }

            except Exception as e:
                err_str = str(e)

                # Если 400 на Binance — пробуем Bybit
                if exchange == "binance" and "400" in err_str:
                    try:
                        data = await bybit_get_full_data(symbol)
                        signal = generate_signal(data)
                        pi = data.get("price_info", {})
                        logger.info("Scanner", f"{symbol} fallback Binance→Bybit ✅")
                        return {
                            "idx": idx, "symbol": symbol, "exchange": "bybit",
                            "signal": signal, "pi": pi,
                        }
                    except Exception:
                        # Нет и на Bybit — пробуем OKX
                        try:
                            data = await okx_get_full_data(symbol)
                            signal = generate_signal(data)
                            pi = data.get("price_info", {})
                            logger.info("Scanner", f"{symbol} fallback Binance→OKX ✅")
                            return {
                                "idx": idx, "symbol": symbol, "exchange": "okx",
                                "signal": signal, "pi": pi,
                            }
                        except Exception:
                            # Нет нигде — блокируем до перезапуска
                            _dead_symbols.add(symbol)
                            logger.warn("Scanner", f"{symbol} делистирован на всех биржах — пропускаем")
                            return None

                # Остальные ошибки — просто логируем
                logger.err("Scanner", f"{exchange.upper()} {symbol}: {e}")
                return None

    # Разбиваем на батчи — отдельно Binance, Bybit и OKX
    bn_symbols = [(i, s, e) for i, (s, e) in enumerate(all_symbols, 1)
                  if e == "binance"]
    by_symbols = [(i, s, e) for i, (s, e) in enumerate(all_symbols, 1)
                  if e == "bybit"]
    okx_symbol_list = [(i, s, e) for i, (s, e) in enumerate(all_symbols, 1)
                       if e == "okx"]

    async def run_batches(symbol_list: list, pause: float) -> list:
        """Запускает батчи и возвращает все результаты."""
        results = []
        for batch_start in range(0, len(symbol_list), BATCH_SIZE):
            batch = symbol_list[batch_start:batch_start + BATCH_SIZE]
            batch_results = await asyncio.gather(
                *[scan_one(idx, sym, exch) for idx, sym, exch in batch]
            )
            results.extend([r for r in batch_results if r is not None])
            if batch_start + BATCH_SIZE < len(symbol_list):
                await asyncio.sleep(pause)
        return results

    # Запускаем Binance, Bybit и OKX параллельно друг с другом
    bn_task = asyncio.create_task(run_batches(bn_symbols, BATCH_PAUSE_BN))
    by_task = asyncio.create_task(run_batches(by_symbols, BATCH_PAUSE_BY))
    okx_task = asyncio.create_task(run_batches(okx_symbol_list, BATCH_PAUSE_OKX))
    batch_results = await asyncio.gather(bn_task, by_task, okx_task, return_exceptions=True)
    
    # Фильтруем ошибки — if a batch failed, treat as empty result
    bn_results = batch_results[0] if not isinstance(batch_results[0], Exception) else []
    by_results = batch_results[1] if not isinstance(batch_results[1], Exception) else []
    okx_results = batch_results[2] if not isinstance(batch_results[2], Exception) else []
    
    # Логируем ошибки батчей
    for exchange, idx in [("Binance", 0), ("Bybit", 1), ("OKX", 2)]:
        if isinstance(batch_results[idx], Exception):
            logger.err("Scanner", f"{exchange} batch failed: {batch_results[idx]}")
            if exchange == "Binance":
                bn_results = []
            elif exchange == "Bybit":
                by_results = []
            else:
                okx_results = []

    # Сортируем по индексу чтобы логи шли по порядку
    all_results = sorted(bn_results + by_results + okx_results, key=lambda r: r["idx"])

    # ── Обрабатываем результаты ───────────────────────────────────────────────
    # Проверяем лимит открытых позиций перед обработкой
    # MAX_OPEN_POSITIONS = 0 означает без лимита (режим сбора ML данных)
    _open_trades = get_open_trades(include_pending=False)  # только OPEN/TP1_HIT
    _positions_limit_reached = False
    if config.MAX_OPEN_POSITIONS > 0 and len(_open_trades) >= config.MAX_OPEN_POSITIONS:
        logger.err("Scanner", f"ЛИМИТ ПОЗИЦИЙ: {len(_open_trades)} из {config.MAX_OPEN_POSITIONS} — "
              f"новые сигналы не берутся пока не закроются старые")
        _positions_limit_reached = True
    elif config.MAX_OPEN_POSITIONS == 0 and len(_open_trades) >= 50:
        logger.err("Scanner", f"КРИТИЧЕСКИЙ ЛИМИТ: {len(_open_trades)} позиций — "
              f"достигнут максимум 50 (режим сбора ML)")
        _positions_limit_reached = True
    
    for res in all_results:
        symbol   = res["symbol"]
        exchange = res["exchange"]
        signal   = res["signal"]
        pi       = res["pi"]
        idx      = res["idx"]
        exch_tag = "[BY]" if exchange == "bybit" else ("[OK]" if exchange == "okx" else "[BN]")

        price  = pi.get("price", 0)
        change = pi.get("change_24h_pct", 0)
        vol    = pi.get("volume_24h", 0)
        vol_s  = f"{vol/1_000_000:.1f}M" if vol >= 1_000_000 else f"{vol/1_000:.0f}K"

        if signal:
            direction = signal["direction"]

            # Проверяем лимит направления за скан
            # 0 = без лимита (режим сбора ML данных)
            max_dir = (config.MAX_LONG_PER_SCAN
                       if direction == "LONG"
                       else config.MAX_SHORT_PER_SCAN)
            
            if max_dir > 0 and signals_by_dir[direction] >= max_dir:
                logger.info("Scanner", f"⏭ {symbol} {direction} — лимит {max_dir} за скан")
                continue

            logger.signal_found(
                idx=idx, total=len(all_symbols), exch=exch_tag.strip("[]"),
                symbol=symbol, price=price, change=change, vol_s=vol_s,
                direction=direction, strength=signal['strength'],
                tp_pct=signal['tp1_pct'], sl_pct=signal['sl_pct'],
                mtf=signal['scores'].get('mtf', 0),
                cvd=signal['scores'].get('cvd', 0),
                vol_score=signal['scores'].get('volume', 0)
            )

            # Не создаём новый сигнал, если по символу уже есть открытая позиция
            # Cooldown: не входим если был стоп по этой монете за последний час
            _cooldown_min = int(getattr(config, "SL_COOLDOWN_MINUTES", 60))
            if symbol_in_cooldown(signal["symbol"], _cooldown_min):
                logger.warn("Scanner", f"{symbol} в кулдауне {_cooldown_min}м после SL — пропуск")
                continue

            if (not signal_exists_recent(signal["symbol"], direction, 30)
                    and not trade_exists_symbol(signal["symbol"])):

                sig_id = await asyncio.to_thread(save_signal, signal)
                signal["id"] = sig_id
                signal["signal_id"] = sig_id
                msg_id = await send_signal(signal)
                if msg_id:
                    await asyncio.to_thread(update_telegram_msg_id, sig_id, msg_id)
                    signal["telegram_msg_id"] = msg_id

                if is_collect_ml_data() and not is_auto_take():
                    ml_id = await asyncio.to_thread(save_ml_features, signal, None)
                    if ml_id:
                        logger.ml("ML", f"saved signal features (ml_id={ml_id})")

                if is_auto_take():
                    try:
                        if trade_exists_symbol(signal["symbol"]):
                            logger.info("Scanner", f"AUTO_TAKE пропущен: {symbol} уже есть открытая позиция")
                        elif _positions_limit_reached:
                            logger.info("Scanner", f"AUTO_TAKE пропущен: достигнут лимит {config.MAX_OPEN_POSITIONS} позиций")
                        else:
                            trade_signal = dict(signal)
                            trade_signal["exchange"] = exchange  # ✅ Фикс: сохраняем биржу
                            if trade_signal.get("entry_type") == "limit":
                                trade_id = await asyncio.to_thread(save_taken_trade, trade_signal, True, "PENDING", None)
                                ml_id = await asyncio.to_thread(save_ml_features, trade_signal, trade_id)
                                logger.info("Scanner", f"AUTO_TAKE: {symbol} {direction} PENDING(limit) "
                                      f"(trade_id={trade_id}, ml_id={ml_id})")
                            else:
                                p_ = float(trade_signal.get("current_price") or 0)
                                dir_ = trade_signal["direction"]
                                instant = (
                                    p_ > 0 and (
                                        (dir_ == "LONG"  and (p_ >= trade_signal["tp2"] or p_ <= trade_signal["sl"])) or
                                        (dir_ == "SHORT" and (p_ <= trade_signal["tp2"] or p_ >= trade_signal["sl"]))
                                    )
                                )
                                if instant:
                                    logger.info("Scanner", f"AUTO_TAKE пропущен: {symbol} {dir_} уже за TP/SL (p={p_})")
                                else:
                                    trade_id = await asyncio.to_thread(save_taken_trade, trade_signal, True, "OPEN", None)
                                    ml_id = await asyncio.to_thread(save_ml_features, trade_signal, trade_id)
                                    logger.info("Scanner", f"AUTO_TAKE: {symbol} {direction} OPEN(market) "
                                          f"(trade_id={trade_id}, ml_id={ml_id})")
                    except Exception as _at_err:
                        logger.err("Scanner", f"AUTO_TAKE ошибка {symbol}: {_at_err}")

                signals_by_dir[direction] += 1
                signals_sent += 1

        else:
            logger.scan_empty(
                idx=idx, total=len(all_symbols), exch=exch_tag.strip("[]"),
                symbol=symbol, price=price, change=change, vol_s=vol_s
            )

    logger.info("Scanner", f"◀ Готово. Сигналов: {signals_sent} "
          f"(LONG={signals_by_dir['LONG']}, SHORT={signals_by_dir['SHORT']})")

    # ── Детальная статистика фильтрации за этот скан ─────────────────────────
    # Получаем статистику за последние ~2 минуты (один скан)
    scan_stats = get_filter_stats(hours=0.033)  # 2 минуты = 0.033 часа
    logger.info("Scanner", f"📊 Этот скан: {scan_stats['passed']} сигналов / "
          f"{scan_stats['total']} монет ({scan_stats['pass_rate']}%)")
    
    # Топ причин блокировки за этот скан
    if scan_stats["top_blocks"]:
        logger.info("Scanner", "🚫 Причины отсева:")
        for block in scan_stats["top_blocks"][:5]:  # топ-5
            reason = block["reason"]
            count = block["count"]
            # Сокращаем длинные причины
            short_reason = reason.replace("Сила ", "").replace(" (не взят)", "")
            logger.info("Scanner", f"  • {short_reason:22} — {count} монет")
    
    # Если есть прошедшие сигналы — показать их
    if scan_stats["passed"] > 0:
        log = get_signal_log(hours=0.033)
        passed_signals = [r for r in log if r["passed"] == 1]
        if passed_signals:
            for sig in passed_signals[:10]:  # максимум 10
                logger.ok("Scanner", f"{sig['symbol']} {sig['direction']} "
                      f"сила={sig['strength']}%")

    # Статистика фильтрации за 1ч (для общей картины)
    stats = get_filter_stats(hours=1)
    logger.info("Scanner", f"📈 За 1ч: {stats['passed']} прошло / {stats['total']} оценено "
          f"({stats['pass_rate']}%)")

    # ML прогресс — сколько новых сделок накоплено до следующего переобучения
    try:
        from database.db import get_conn as _gc
        from ml_auto_trainer import RETRAIN_EVERY_N_TRADES, _last_trained_count
        _conn = _gc()
        _cur = _conn.cursor()
        _cur.execute("""
            SELECT COUNT(*) FROM ml_features
            WHERE labeled = 1
              AND COALESCE(target,'') NOT IN ('EXPIRED','NOT_FILLED')
        """)
        _total_clean = _cur.fetchone()[0] or 0
        _conn.close()
        _new = _total_clean - _last_trained_count
        _need = max(0, RETRAIN_EVERY_N_TRADES - _new)
        logger.ml("MLAuto", f"Размечено: {_total_clean} строк | "
              f"+{_new} новых | до переобучения: {_need} сделок")
    except Exception:
        pass

    export_to_json()
    push_data_json()


async def news_loop():
    logger.info("News", f"Запущен, интервал {config.NEWS_FETCH_INTERVAL}с")
    while True:
        try:
            await fetch_all_news()
        except Exception as e:
            logger.err("News", f"Ошибка: {e}")
        await asyncio.sleep(config.NEWS_FETCH_INTERVAL)


async def export_loop():
    """
    Периодический экспорт/обновление сайта.
    Нужен, чтобы UI не "застывал", даже если нет новых сигналов/событий,
    и чтобы изменения по позициям гарантированно доезжали.
    """
    while True:
        try:
            export_to_json()
            push_data_json()
        except Exception as e:
            logger.err("Export", f"Ошибка: {e}")
        await asyncio.sleep(60)


async def log_cleaner_loop():
    """
    Очищает консоль каждые 10 минут чтобы не накапливать логи в памяти.
    Перед очисткой выводит разделитель с временем — видно что бот живой.
    """
    import os as _os
    await asyncio.sleep(600)  # первый раз через 10 минут после старта
    while True:
        try:
            now = datetime.now(timezone.utc).strftime("%H:%M UTC")
            _os.system("cls" if _os.name == "nt" else "clear")
            logger.separator(f"🤖 CashTrack Bot — работает | {now}")
            print(f"  Логи очищены (каждые 10 минут)\n")
        except Exception:
            pass
        await asyncio.sleep(600)


async def context_loop():
    while True:
        try:
            ctx     = await get_btc_context()
            session = ctx.get("session", "")
            trend   = ctx.get("trend_4h", "neutral")
            change  = ctx.get("change_4h_pct", 0)
            logger.info("Context", f"BTC {trend} {change:+.1f}% | {session} сессия"
                  + (f" | ⚠ {ctx['reason']}" if ctx.get("reason") else ""))
        except Exception as e:
            logger.err("Context", f"Ошибка: {e}")
        await asyncio.sleep(300)


async def fg_loop():
    while True:
        try:
            await get_fear_greed()
        except Exception as e:
            logger.err("FearGreed", f"Ошибка: {e}")
        await asyncio.sleep(3600)


async def main():
    if not config.TELEGRAM_BOT_TOKEN:
        logger.err("Main", "TELEGRAM_BOT_TOKEN не задан"); sys.exit(1)
    if not config.TELEGRAM_CHAT_ID:
        logger.err("Main", "TELEGRAM_CHAT_ID не задан"); sys.exit(1)

    init_db()
    migrate_db()
    startup_check()  # ← Проверка БД
    set_notify_callback(send_result)
    ml_set_notify(send_result)  # ML авто-тренер тоже шлёт уведомления в Telegram

    logger.info("Main", "Загружаем фьючерсы...")
    symbols = await get_top_futures_async(config.TOP_FUTURES_COUNT)
    logger.info("Main", f"{len(symbols)} монет: {', '.join(symbols[:8])}...")

    # ── Инициализируем кэш свечей ─────────────────────────────────────────
    logger.info("Main", "🚀 Инициализация кэша свечей...")
    from core.candle_cache import init_candle_cache
    await asyncio.to_thread(init_candle_cache, symbols)
    logger.ok("Main", "✅ Кэш свечей готов!")

    mode = "🤖 AUTO_TAKE" if is_auto_take() else "👤 Ручной режим"
    logger.info("Main", f"Режим торговли: {mode}")
    logger.info("Main", f"Лимит сигналов за скан: "
          f"LONG≤{config.MAX_LONG_PER_SCAN}, SHORT≤{config.MAX_SHORT_PER_SCAN}")

    logger.info("Main", "Загружаем контекст, новости, Fear & Greed...")
    await asyncio.gather(fetch_all_news(), get_fear_greed(), get_btc_context())

    # Баннер при запуске
    from data.fear_greed import get_cached as get_fg_cached
    fg = get_fg_cached()
    fg_val = fg.get("value", 50)
    fg_label = fg.get("label", "")
    logger.startup_banner(mode, config.SCAN_INTERVAL, fg_val, fg_label)

    app = build_app()
    logger.ok("Main", "Бот запущен!\n")

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        results = await asyncio.gather(
            ws_loop(symbols),
            scan_loop(app),
            news_loop(),
            context_loop(),
            fg_loop(),
            export_loop(),
            log_cleaner_loop(),
            run_tracker(5),
            run_auto_trainer(),
            return_exceptions=True,  # ← One task failure won't kill all others
        )
        # Log any exceptions from tasks
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.err("Main", f"Task {i} failed: {result}")
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warn("Main", "Остановлен")