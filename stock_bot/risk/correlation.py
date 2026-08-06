"""
Returns-based correlation gate for the stock bot's rule-trading BUY path.

Reuses the crypto bot's pure Pearson/returns math (bot/risk/correlation.py)
unchanged — identical statistics, no duplication. The stock-specific piece
is fetch_correlation_from_closes(), which works entirely off candle data the
scan cycle already fetched via yfinance (stock_bot/data/price_feed.py) — no
extra network call per BUY, unlike the crypto gate which makes a fresh
exchange.fetch_ohlcv() call for each open-position pair. Same fail-open
philosophy: any missing/short history returns None and the caller allows
the BUY rather than blocking on absent data.
"""
from __future__ import annotations

from bot.risk.correlation import CORRELATION_THRESHOLD, pct_returns, pearson

__all__ = ["CORRELATION_THRESHOLD", "fetch_correlation_from_closes"]

_CORR_DAYS = 30


def fetch_correlation_from_closes(
    closes_a: list[float], closes_b: list[float], days: int = _CORR_DAYS,
) -> float | None:
    """
    Pearson correlation of two symbols' daily-return series, computed from
    already-fetched candle closes — no network call. Returns None when
    there isn't enough overlapping history (callers treat None as "no
    data, allow the BUY", matching the crypto gate's fail-open behavior).
    """
    if not closes_a or not closes_b:
        return None
    ret_a = pct_returns(closes_a[-(days + 1):])
    ret_b = pct_returns(closes_b[-(days + 1):])
    n = min(len(ret_a), len(ret_b))
    if n < 5:
        return None
    return pearson(ret_a[-n:], ret_b[-n:])
