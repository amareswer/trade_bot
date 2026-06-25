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
        if not price or not (0 < price < 500_000):
            return None
        price = float(price)
    except Exception as e:
        logger.warning("get_live_price failed for %s: %s", symbol, e)
        return None

    # Previous-close corruption guard — same 20% threshold as fetch_candles().
    # info (fast_info) is already fetched; previous_close is a free attribute read.
    try:
        prev_close = (
            getattr(info, "previous_close", None) or
            getattr(info, "previousClose",  None)
        )
        if prev_close and prev_close > 0:
            deviation = abs(price - prev_close) / prev_close
            if deviation > 0.20:
                logger.warning(
                    "%s — live price $%.2f deviates %.1f%% from "
                    "previous close $%.2f — returning None",
                    symbol, price, deviation * 100, prev_close,
                )
                return None
    except Exception:
        pass  # never block on guard failure

    return price
