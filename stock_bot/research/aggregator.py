"""
Research aggregator — bundles all sources into one ResearchReport per symbol.

fetch_research() runs news and earnings concurrently (ThreadPoolExecutor).
Sentiment is derived from news headlines after they are fetched — no extra
network call. Fear & Greed and market_trends_score are fetched ONCE per scan
cycle by the caller and passed in to avoid redundant network hits.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime

from stock_bot.research.news_fetcher      import fetch_news,       NewsItem
from stock_bot.research.sentiment_scraper import score_headlines,  SentimentData
from stock_bot.research.earnings          import fetch_earnings,   EarningsInfo
from stock_bot.research.fear_greed        import fetch_fear_greed, FearGreedData

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Company name lookup — used to build richer Reddit search queries
# ---------------------------------------------------------------------------

COMPANY_NAMES: dict[str, str] = {
    "SHOP.TO": "Shopify",
    "RY.TO":   "Royal Bank of Canada",
    "AC.TO":   "Air Canada",
    "AAPL":    "Apple",
    "NVDA":    "Nvidia",
}


@dataclass
class ResearchReport:
    symbol:     str
    timestamp:  datetime
    news:       list[NewsItem]
    sentiment:           SentimentData
    market_trends_score: int           # 0-100, fetched once per cycle
    earnings:            EarningsInfo
    fear_greed: FearGreedData


def fetch_research(
    symbol:              str,
    company_name:        str | None          = None,
    fear_greed_data:     FearGreedData | None = None,
    market_trends_score: int                  = 0,
) -> ResearchReport:
    """
    Fetch all research for one symbol.

    news + earnings run concurrently (2 threads). Sentiment is scored from the
    fetched headlines — no extra network call.
    fear_greed_data and market_trends_score should be pre-fetched once per
    scan cycle by the caller and passed in.

    Never raises — every source failure is caught and returns a safe default.
    """
    if company_name is None:
        company_name = COMPANY_NAMES.get(symbol, symbol.split(".")[0])

    if fear_greed_data is None:
        fear_greed_data = fetch_fear_greed()

    # ── Concurrent fetch of the two per-symbol sources ────────────────────
    news     = []
    earnings = EarningsInfo(earnings_note="No data")

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_news     = ex.submit(fetch_news,     symbol, company_name)
        f_earnings = ex.submit(fetch_earnings, symbol)

        for src, future in [
            ("news",     f_news),
            ("earnings", f_earnings),
        ]:
            try:
                result = future.result(timeout=15)
                if src == "news":
                    news = result
                else:
                    earnings = result
            except FutureTimeout:
                logger.warning("Research fetch timed out for %s (%s)", symbol, src)
            except Exception as exc:
                logger.warning("Research fetch failed for %s (%s): %s", symbol, src, exc)

    # Sentiment derived from news already fetched — no extra network call
    sentiment = score_headlines(news)

    return ResearchReport(
        symbol               = symbol,
        timestamp            = datetime.now(),
        news                 = news,
        sentiment            = sentiment,
        market_trends_score  = market_trends_score,
        earnings             = earnings,
        fear_greed           = fear_greed_data,
    )
