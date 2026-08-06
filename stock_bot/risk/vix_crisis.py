"""
VIX-based crisis mode — disables new BUYs market-wide when the CBOE
Volatility Index crosses a crisis threshold (default 35, matching the
spec's "Crisis mode: VIX >35. Disable aggressive trading"). Closes
punch-list item #8 from the trading-spec gap review (see CLAUDE.md).

VIX data comes from Yahoo Finance's ^VIX ticker, fetched once per scan
cycle in stock_bot/main.py using the same fetch_with_retry pattern already
used for the SPY regime filter. This module holds only the pure threshold
check — no network code — so it's fully unit-testable without mocking yfinance.
"""
from __future__ import annotations


def is_vix_crisis(vix_value: float | None, threshold: float) -> bool:
    """
    Returns True if vix_value is at or above threshold.

    vix_value=None (fetch failed or feature disabled upstream) fails open —
    same philosophy as every other gate in this codebase: missing data
    allows the trade rather than blocking it. threshold <= 0 also disables
    the feature (mirrors _is_earnings_blackout's own guard).
    """
    if threshold <= 0 or vix_value is None:
        return False
    return vix_value >= threshold
