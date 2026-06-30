"""
Fetches the latest intraday price for a symbol using yfinance.
Used for paper trade execution and stop-loss checks — NOT for indicator math.
Never raises — returns None on any failure.
"""
from __future__ import annotations
import logging
import yfinance as yf

from stock_bot.data.yf_client import fetch_with_retry

logger = logging.getLogger(__name__)


def get_live_price(symbol: str) -> float | None:
    """Return the latest trade price for symbol, or None on failure."""
    # fast_info is lazily evaluated in yfinance — the YFRateLimitError surfaces
    # at fi.last_price access, not at fi itself.  Access both inside the lambda
    # so fetch_with_retry catches and retries the RL exception correctly.
    def _fetch() -> tuple:
        fi = yf.Ticker(symbol).fast_info
        return fi, fi.last_price

    result = fetch_with_retry(_fetch, label=f"{symbol}:live_price")
    if result is None:
        return None

    fi, raw_price = result

    try:
        if not raw_price or not (0 < raw_price < 500_000):
            return None
        price = float(raw_price)
    except Exception as e:
        logger.warning("get_live_price failed for %s: %s", symbol, e)
        return None

    # Previous-close corruption guard — same 20% threshold as fetch_candles().
    # fi is already fetched above; previous_close is a free attribute read.
    try:
        prev_close = (
            getattr(fi, "previous_close", None) or
            getattr(fi, "previousClose",  None)
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
