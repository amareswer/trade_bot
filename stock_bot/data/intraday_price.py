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
    #
    # A >20% deviation is ambiguous on its own: it's the shape of BOTH a
    # corrupted read (stale cache, currency mixup, decimal-point error) and
    # a genuine crash — and this function backs the SL/TP watcher, so
    # silently discarding the second case disables stop-loss protection
    # exactly when it matters most (2026-09 finding). day_high/day_low come
    # from a separately-fetched price-history series inside fast_info (not
    # the same live-quote field as last_price), so checking the deviant
    # price against them is a genuine independent cross-check, not just
    # re-reading the same suspect number: a real crash's last_price falls
    # inside the day's own low (that feed moved too); a corrupted read
    # usually doesn't land anywhere near the day's actual traded range.
    # Only fetched when the cheap previous_close check above already fired
    # — day_high/day_low trigger their own lazy network call, so this cost
    # is paid only in the rare deviation case, not on every price check.
    try:
        prev_close = (
            getattr(fi, "previous_close", None) or
            getattr(fi, "previousClose",  None)
        )
        if prev_close and prev_close > 0:
            deviation = abs(price - prev_close) / prev_close
            if deviation > 0.20:
                if _price_within_day_range(fi, price):
                    logger.warning(
                        "%s — live price $%.2f deviates %.1f%% from previous "
                        "close $%.2f but matches today's own trading range — "
                        "treating as a genuine move, not corruption",
                        symbol, price, deviation * 100, prev_close,
                    )
                else:
                    logger.warning(
                        "%s — live price $%.2f deviates %.1f%% from "
                        "previous close $%.2f — returning None",
                        symbol, price, deviation * 100, prev_close,
                    )
                    return None
    except Exception:
        pass  # never block on guard failure

    return price


def _price_within_day_range(fi, price: float) -> bool:
    """Independent corroboration for a price that deviates >20% from
    previous close: does it fall inside today's own day_high/day_low (from
    fast_info's separately-fetched price-history series)? A 2% tolerance
    covers after-hours movement past the last regular-session extreme.
    Returns False (fail toward the conservative old behavior) if day_high/
    day_low aren't available or the lookup itself fails — this is a bonus
    corroboration, not a replacement guard."""
    try:
        day_high = fi.day_high
        day_low  = fi.day_low
        if not day_high or not day_low or day_high <= 0 or day_low <= 0:
            return False
        return (day_low * 0.98) <= price <= (day_high * 1.02)
    except Exception:
        return False
