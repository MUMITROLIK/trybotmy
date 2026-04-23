import asyncio
import re
import json
import aiohttp
import feedparser
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import config
from database.db import save_news, get_recent_news

_vader = SentimentIntensityAnalyzer()

# Расширенный список монет — включая полные названия для RSS
KNOWN_COINS = [
    # Тикеры
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "MATIC",
    "LINK", "UNI", "ATOM", "LTC", "TRX", "NEAR", "FTM", "APT", "ARB", "OP",
    "INJ", "SUI", "SEI", "TIA", "PEPE", "WIF", "BONK", "JUP", "BLUR", "IMX",
    "SAND", "MANA", "AXS", "ENS", "LDO", "AAVE", "CRV", "GMX", "DYDX",
    "HYPE", "ZEC", "XMR", "DASH", "ETC", "BCH", "FIL", "RENDER", "FETCH",
    "TAO", "SUI", "SEI", "STRK", "ARB", "OP", "BLUR",
    # Полные названия (для RSS где пишут "Bitcoin" а не "BTC")
    "BITCOIN", "ETHEREUM", "SOLANA", "RIPPLE", "CARDANO", "DOGECOIN",
    "AVALANCHE", "POLKADOT", "POLYGON", "CHAINLINK", "UNISWAP", "COSMOS",
    "LITECOIN", "TRON", "BINANCE", "SHIBA", "PEPE", "ARBITRUM", "OPTIMISM",
]

# Маппинг полных названий → тикеры
COIN_NAMES = {
    "BITCOIN": "BTC",
    "ETHEREUM": "ETH",
    "SOLANA": "SOL",
    "RIPPLE": "XRP",
    "CARDANO": "ADA",
    "DOGECOIN": "DOGE",
    "AVALANCHE": "AVAX",
    "POLKADOT": "DOT",
    "POLYGON": "MATIC",
    "CHAINLINK": "LINK",
    "UNISWAP": "UNI",
    "COSMOS": "ATOM",
    "LITECOIN": "LTC",
    "TRON": "TRX",
    "BINANCE": "BNB",
    "SHIBA": "SHIB",
    "ARBITRUM": "ARB",
    "OPTIMISM": "OP",
    "FETCH": "FET",
    "RENDER": "RNDR",
}

BULLISH_WORDS = [
    "bull", "bullish", "surge", "rally", "breakout", "adoption", "launch",
    "partnership", "listing", "upgrade", "etf", "approval", "buy", "long",
    "moon", "pump", "growth", "all-time high", "ath", "inflow", "positive",
    "gains", "recovery", "bounce", "institutional", "record", "high", "rises",
    "jumped", "soared", "exploded", "accumulated", "bullrun",
]

BEARISH_WORDS = [
    "bear", "bearish", "crash", "dump", "hack", "exploit", "ban", "lawsuit",
    "sec", "regulation", "fine", "sell", "short", "drop", "decline", "fall",
    "liquidation", "outflow", "negative", "loss", "vulnerability", "fraud",
    "scam", "delist", "bankrupt", "plunge", "tumble", "slump", "warning",
    "arrested", "seized", "investigation", "collapse", "panic",
]


def extract_coins(text: str) -> list:
    """Извлекает тикеры монет из текста."""
    text_upper = text.upper()
    found = set()
    for coin in KNOWN_COINS:
        if re.search(r'\b' + coin + r'\b', text_upper):
            # Конвертируем полное название в тикер если нужно
            found.add(COIN_NAMES.get(coin, coin))
    return list(found)


def calc_sentiment(title: str, body: str = "") -> float:
    """Считает сентимент текста от -1.0 до +1.0."""
    full = (title + " " + body).lower()
    vader_score = _vader.polarity_scores(full)["compound"]
    bull = sum(1 for w in BULLISH_WORDS if w in full)
    bear = sum(1 for w in BEARISH_WORDS if w in full)
    raw = vader_score + (bull - bear) * 0.1
    return max(-1.0, min(1.0, round(raw, 3)))


async def fetch_rss(session: aiohttp.ClientSession, url: str) -> list:
    """Загружает RSS фид и парсит новости."""
    items = []
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
        feed   = feedparser.parse(text)
        source = feed.feed.get("title", url)
        for entry in feed.entries[:10]:
            title   = entry.get("title", "")
            link    = entry.get("link", "")
            summary = entry.get("summary", "")
            full_text = title + " " + summary
            coins = extract_coins(full_text)
            sentiment = calc_sentiment(title, summary)
            items.append({
                "title":     title,
                "url":       link,
                "source":    source,
                "sentiment": sentiment,
                "coins":     coins,
            })
    except Exception as e:
        print(f"[News] RSS ошибка {url}: {e}")
    return items


async def fetch_cryptopanic(session: aiohttp.ClientSession) -> list:
    """
    Загружает новости с CryptoPanic API.
    Использует поле 'instruments' (новый API v2) вместо 'currencies'.
    Голосования bullish/bearish учитываются в сентименте.
    """
    key = config.CRYPTOPANIC_API_KEY
    if not key or key in ("your_cryptopanic_key", ""):
        return []

    items = []
    # Правильный URL для Developer API v2
    url = (
        f"https://cryptopanic.com/api/developer/v2/posts/"
        f"?auth_token={key}&public=true&kind=news&filter=hot&regions=en"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 403:
                print(f"[News] CryptoPanic: план не поддерживает этот запрос (403)")
                return []
            if resp.status == 429:
                print(f"[News] CryptoPanic: rate limit (429)")
                return []
            data = await resp.json()

        results = data.get("results", [])
        print(f"[News] CryptoPanic получено: {len(results)} постов")

        for post in results[:20]:
            title = post.get("title", "")
            link  = post.get("url", "") or post.get("original_url", "")

            # Монеты: новый API использует 'instruments' вместо 'currencies'
            instruments = post.get("instruments", []) or post.get("currencies", [])
            coins = [c["code"] for c in instruments if c.get("code")]

            # Если монеты не пришли из API — извлекаем из заголовка
            if not coins:
                coins = extract_coins(title)

            # Сентимент: TextBlob + голосования пользователей
            votes    = post.get("votes", {})
            pos      = votes.get("positive", 0) or votes.get("liked", 0) or 0
            neg      = votes.get("negative", 0) or votes.get("disliked", 0) or 0
            total    = pos + neg
            vote_s   = (pos - neg) / total if total > 0 else 0
            text_s   = calc_sentiment(title)
            sent     = round((text_s + vote_s) / 2, 3)

            items.append({
                "title":     title,
                "url":       link,
                "source":    "CryptoPanic",
                "sentiment": sent,
                "coins":     coins,
            })

    except Exception as e:
        print(f"[News] CryptoPanic ошибка: {e}")

    return items


async def fetch_binance_announcements(session: aiohttp.ClientSession) -> list:
    """
    Загружает анонсы с Binance.
    Пробует несколько эндпоинтов — Binance часто меняет API.
    """
    items = []

    # Новый эндпоинт Binance
    urls_to_try = [
        "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=10",
        "https://www.binance.com/en/support/announcement/new-cryptocurrency-listing",
    ]

    for url in urls_to_try:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.content_type and "json" in resp.content_type:
                    data = await resp.json()
                    articles = (data.get("data", {}) or {}).get("articles", [])
                    if articles:
                        for article in articles:
                            title = article.get("title", "")
                            code  = article.get("code", "")
                            link  = f"https://www.binance.com/en/support/announcement/{code}"
                            sent  = calc_sentiment(title)
                            if any(w in title.lower() for w in ["will list", "listing", "adds"]):
                                sent = min(1.0, sent + 0.5)
                            if any(w in title.lower() for w in ["delist", "remove", "will remove"]):
                                sent = max(-1.0, sent - 0.5)
                            items.append({
                                "title":     title,
                                "url":       link,
                                "source":    "Binance",
                                "sentiment": sent,
                                "coins":     extract_coins(title),
                            })
                        break  # Успех — выходим из цикла
        except Exception as e:
            print(f"[News] Binance ошибка ({url[:50]}...): {e}")
            continue

    return items


async def fetch_all_news() -> list:
    """Загружает все новости из всех источников."""
    all_items = []
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
        tasks = [
            *[fetch_rss(session, url) for url in config.RSS_FEEDS],
            fetch_cryptopanic(session),
            fetch_binance_announcements(session),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, list):
            all_items.extend(result)
        elif isinstance(result, Exception):
            print(f"[News] Ошибка задачи: {result}")

    for item in all_items:
        save_news(
            item["title"], item["url"], item["source"],
            item["sentiment"], item["coins"]
        )

    # Статистика
    cp_count = sum(1 for i in all_items if i["source"] == "CryptoPanic")
    bn_count = sum(1 for i in all_items if i["source"] == "Binance")
    rss_count = len(all_items) - cp_count - bn_count
    print(f"[News] Загружено {len(all_items)} новостей "
          f"(RSS={rss_count}, CryptoPanic={cp_count}, Binance={bn_count})")
    return all_items


def get_coin_sentiment(coin_base: str, minutes: int = 60) -> dict:
    """Возвращает агрегированный сентимент по монете за последние N минут."""
    news = get_recent_news(coin_base, minutes)
    if not news:
        return {"score": 0.0, "count": 0, "top_news": None}
    scores = [n["sentiment"] for n in news]
    top    = max(news, key=lambda x: abs(x["sentiment"]))
    return {
        "score":    round(sum(scores) / len(scores), 3),
        "count":    len(news),
        "top_news": top["title"],
    }