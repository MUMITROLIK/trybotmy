"""
Smart Entry System - Rule-Based (без ML)

Пока ML модель плохая (CV AUC 0.54, винрейт 45%) - используем умные правила.
Это даст ЛУЧШИЙ результат чем плохая ML модель!

Логика:
1. Многоступенчатая фильтрация
2. Оценка уверенности (0-100%)
3. Контекст рынка
4. Адаптивные пороги
"""

from datetime import datetime, timezone
from database.db import get_open_trades, get_conn


def should_enter_trade(signal: dict, market_context: dict) -> dict:
    """
    Умный анализ перед входом - бот 'думает'.
    
    Returns:
        {
            "enter": True/False,
            "reason": "...",
            "confidence": 0-100,
            "risk_level": "low/medium/high"
        }
    """
    
    # Шаг 1: Базовая сила сигнала
    strength = signal.get("strength", 0)
    if strength < 65:
        return {
            "enter": False,
            "reason": f"Слабый сигнал ({strength}%)",
            "confidence": 0,
            "risk_level": "high"
        }
    
    # Шаг 2: Категория сигнала (только что добавили!)
    category = signal.get("signal_category", "WEAK")
    if category == "WEAK":
        return {
            "enter": False,
            "reason": "Категория WEAK - пропускаем",
            "confidence": 0,
            "risk_level": "high"
        }
    
    # Шаг 3: Тяжёлые индикаторы (должны подтвердить)
    heavy_confirmed = signal.get("heavy_confirmed", 0)
    if heavy_confirmed < 1:
        return {
            "enter": False,
            "reason": "Нет подтверждения от тяжёлых индикаторов",
            "confidence": 0,
            "risk_level": "high"
        }
    
    # Шаг 4: BTC тренд (КРИТИЧНО!)
    btc_trend = signal.get("btc_trend_strength", "NEUTRAL")
    direction = signal["direction"]
    
    # Блокируем против сильного тренда BTC
    if direction == "LONG" and btc_trend in ["BEAR", "STRONG_BEAR"]:
        return {
            "enter": False,
            "reason": f"LONG против BTC {btc_trend} - слишком рискованно",
            "confidence": 0,
            "risk_level": "high"
        }
    
    if direction == "SHORT" and btc_trend in ["BULL", "STRONG_BULL"]:
        return {
            "enter": False,
            "reason": f"SHORT против BTC {btc_trend} - слишком рискованно",
            "confidence": 0,
            "risk_level": "high"
        }
    
    # Шаг 5: Сессия (азиатская = низкая ликвидность)
    session = market_context.get("session", "")
    if session == "asian":
        return {
            "enter": False,
            "reason": "Азиатская сессия - низкая ликвидность",
            "confidence": 0,
            "risk_level": "medium"
        }
    
    # Шаг 6: Проверка перегрева рынка
    if is_market_overheated(signal):
        return {
            "enter": False,
            "reason": "Рынок перегрет - жду откат",
            "confidence": 0,
            "risk_level": "high"
        }
    
    # Шаг 7: Проверка коррелированных позиций
    open_trades = get_open_trades()
    if has_too_many_positions(signal["symbol"], open_trades):
        return {
            "enter": False,
            "reason": "Слишком много открытых позиций",
            "confidence": 0,
            "risk_level": "medium"
        }
    
    # Шаг 8: Проверка времени (низкая ликвидность)
    if is_low_liquidity_time():
        return {
            "enter": False,
            "reason": "Низкая ликвидность (02:00-06:00 UTC)",
            "confidence": 0,
            "risk_level": "medium"
        }
    
    # Шаг 9: Вычисляем уверенность (0-100%)
    confidence = calculate_confidence(signal, market_context)
    
    # Шаг 10: Определяем уровень риска
    risk_level = get_risk_level(confidence, category, btc_trend, direction)
    
    # Шаг 11: Финальное решение
    if confidence >= 70:
        return {
            "enter": True,
            "reason": f"Высокая уверенность ({confidence}%) - {category} сигнал",
            "confidence": confidence,
            "risk_level": risk_level
        }
    elif confidence >= 60:
        return {
            "enter": True,
            "reason": f"Средняя уверенность ({confidence}%) - осторожно",
            "confidence": confidence,
            "risk_level": risk_level
        }
    else:
        return {
            "enter": False,
            "reason": f"Низкая уверенность ({confidence}%) - пропускаем",
            "confidence": confidence,
            "risk_level": risk_level
        }


def calculate_confidence(signal: dict, market_context: dict) -> int:
    """
    Вычисляет уверенность бота в сделке (0-100%).
    Это и есть "мышление" бота - взвешивает все факторы.
    """
    confidence = 0
    
    # 1. Категория сигнала (25% веса)
    category = signal.get("signal_category", "WEAK")
    if category == "PREMIUM":
        confidence += 25
    elif category == "STANDARD":
        confidence += 18
    elif category == "RISKY":
        confidence += 10
    
    # 2. Сила сигнала (20% веса)
    strength = signal.get("strength", 0)
    if strength >= 80:
        confidence += 20
    elif strength >= 75:
        confidence += 17
    elif strength >= 70:
        confidence += 14
    elif strength >= 65:
        confidence += 10
    
    # 3. Совпадение с BTC трендом (20% веса)
    btc_trend = signal.get("btc_trend_strength", "NEUTRAL")
    direction = signal["direction"]
    
    if direction == "LONG":
        if btc_trend == "STRONG_BULL":
            confidence += 20
        elif btc_trend == "BULL":
            confidence += 15
        elif btc_trend == "NEUTRAL":
            confidence += 8
    elif direction == "SHORT":
        if btc_trend == "STRONG_BEAR":
            confidence += 20
        elif btc_trend == "BEAR":
            confidence += 15
        elif btc_trend == "NEUTRAL":
            confidence += 8
    
    # 4. Тяжёлые индикаторы (15% веса)
    heavy = signal.get("heavy_confirmed", 0)
    if heavy >= 3:
        confidence += 15
    elif heavy >= 2:
        confidence += 12
    elif heavy >= 1:
        confidence += 8
    
    # 5. Сессия (10% веса)
    session = market_context.get("session", "")
    if session == "european":
        confidence += 10
    elif session == "american":
        confidence += 7
    elif session == "asian":
        confidence += 3
    
    # 6. Fear & Greed (10% веса)
    fg = market_context.get("fear_greed", 50)
    if direction == "LONG":
        if fg < 25:  # Extreme Fear - хорошо для покупки
            confidence += 10
        elif fg < 40:  # Fear
            confidence += 7
        elif fg > 75:  # Extreme Greed - плохо для покупки
            confidence -= 5
    elif direction == "SHORT":
        if fg > 75:  # Extreme Greed - хорошо для продажи
            confidence += 10
        elif fg > 60:  # Greed
            confidence += 7
        elif fg < 25:  # Extreme Fear - плохо для продажи
            confidence -= 5
    
    return max(0, min(100, confidence))


def get_risk_level(confidence: int, category: str, btc_trend: str, direction: str) -> str:
    """Определяет уровень риска сделки."""
    
    # Высокий риск
    if confidence < 60:
        return "high"
    
    if category == "RISKY":
        return "high"
    
    # Против тренда BTC = высокий риск
    if direction == "LONG" and btc_trend in ["BEAR", "STRONG_BEAR"]:
        return "high"
    if direction == "SHORT" and btc_trend in ["BULL", "STRONG_BULL"]:
        return "high"
    
    # Средний риск
    if confidence < 70:
        return "medium"
    
    if category == "STANDARD":
        return "medium"
    
    # Низкий риск
    return "low"


def is_market_overheated(signal: dict) -> bool:
    """
    Проверяет не перегрет ли рынок (слишком быстрое движение).
    Перегрев = высокий риск разворота.
    """
    try:
        from data.binance_client import get_klines
        
        symbol = signal["symbol"]
        df = get_klines(symbol, "15m", limit=20)
        
        if df is None or df.empty or len(df) < 4:
            return False
        
        # Изменение за последние 3 свечи (45 минут)
        change_3candles = (df["close"].iloc[-1] - df["close"].iloc[-4]) / df["close"].iloc[-4] * 100
        
        # Если цена изменилась >5% за 45 минут - перегрев
        if abs(change_3candles) > 5:
            return True
        
        # Проверяем ATR - если волатильность экстремальная
        if "ATR_14" in df.columns:
            atr_pct = df["ATR_14"].iloc[-1] / df["close"].iloc[-1] * 100
            if atr_pct > 4:  # ATR >4% = экстремальная волатильность
                return True
        
        return False
    except Exception as e:
        # В случае ошибки (нет данных, биржа недоступна) - не блокируем сигнал
        return False


def has_too_many_positions(symbol: str, open_trades: list) -> bool:
    """
    Проверяет не слишком ли много открытых позиций.
    Диверсификация + управление рисками.
    """
    # Максимум 15 открытых позиций одновременно (можно настроить)
    MAX_POSITIONS = 15
    if len(open_trades) >= MAX_POSITIONS:
        return True
    
    # Не открываем вторую позицию по тому же символу
    for trade in open_trades:
        if trade["symbol"] == symbol:
            return True
    
    return False


def is_low_liquidity_time() -> bool:
    """
    Проверяет не низкая ли сейчас ликвидность.
    Низкая ликвидность = широкие спреды, проскальзывание.
    """
    hour = datetime.now(timezone.utc).hour
    
    # Азиатская ночь (02:00-06:00 UTC) - самая низкая ликвидность
    if 2 <= hour <= 6:
        return True
    
    return False


def get_position_size_multiplier(confidence: int, risk_level: str) -> float:
    """
    Возвращает множитель размера позиции на основе уверенности.
    Высокая уверенность = больше риска, низкая = меньше.
    """
    if risk_level == "high":
        return 0.5  # 50% от базового размера
    
    if confidence >= 80:
        return 1.5  # 150% от базового размера
    elif confidence >= 70:
        return 1.2  # 120%
    elif confidence >= 60:
        return 1.0  # 100%
    else:
        return 0.7  # 70%


# Для интеграции в main.py:
"""
from analysis.smart_entry import should_enter_trade

# В обработке сигнала:
if signal:
    decision = should_enter_trade(signal, market_context)
    
    if decision["enter"]:
        print(f"[Smart Entry] ✅ Входим: {decision['reason']}")
        print(f"[Smart Entry]    Уверенность: {decision['confidence']}%")
        print(f"[Smart Entry]    Риск: {decision['risk_level']}")
        
        if is_auto_take():
            trade_id = save_taken_trade(signal, auto_taken=True)
    else:
        print(f"[Smart Entry] ❌ Пропускаем: {decision['reason']}")
"""
