"""
Fetches the latest intraday price for a symbol using yfinance.
Used for paper trade execution and stop-loss checks — NOT for indicator math.
Never raises — returns None on any failure.
"""
from __future__ import annotations
import logging
import yfinance as yf

logger = logging.getLogger(__name__)


def get_live_price(symbol: str) -> float | None:
    """Return the latest trade price for symbol, or None on failure."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = info.last_price
        if price and 0 < price < 500_000:
            return float(price)
        return None
    except Exception as e:
        logger.warning("get_live_price failed for %s: %s", symbol, e)
        return None
