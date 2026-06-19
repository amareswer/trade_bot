"""
Google Trends market-wide interest score via pytrends — no API key required.

fetch_market_trends() is called ONCE per scan cycle (like fear_greed) and
returns a single 0-100 integer representing average search interest across
3 market-level keywords. One API call per cycle instead of one per symbol.
"""
from __future__ import annotations

import logging
import warnings

import pandas as pd
warnings.filterwarnings("ignore", category=FutureWarning, module="pytrends")
pd.set_option("future.no_silent_downcasting", True)

logger = logging.getLogger(__name__)

_KEYWORDS = ["stock market", "buy stocks", "stock crash"]


def fetch_market_trends() -> int | None:
    """
    Fetch Google Trends interest for 3 market-level keywords in one call.
    Returns the average score as an integer 0-100, or None on 429 / any error.
    """
    try:
        from pytrends.request import TrendReq

        pytrends = TrendReq(hl="en-US", tz=0)
        pytrends.build_payload(_KEYWORDS, timeframe="now 7-d")
        df = pytrends.interest_over_time()

        if df.empty:
            logger.debug("Google Trends: no data returned for market keywords")
            return None

        cols = [k for k in _KEYWORDS if k in df.columns]
        if not cols:
            return None

        score = int(df[cols].mean().mean())
        logger.debug("Google Trends market score=%d (keywords: %s)", score, cols)
        return score

    except Exception as exc:
        if "429" in str(exc) or "Too Many Requests" in str(exc):
            logger.debug("Google Trends rate-limited — skipping this cycle")
        else:
            logger.warning("Google Trends fetch failed: %s", exc)
        return None
