"""
Returns-based correlation between two exchange symbols.

Used by the correlation gate in main.py to block BUY orders on symbols
that are highly correlated with an already-open position — preventing
simultaneous exposure to assets that move together during drawdowns.

All math is pure (no NumPy). fetch_correlation() is the only function
that touches the exchange; the rest are unit-testable without mocking.
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

# Threshold above which two symbols are considered too correlated to hold together.
CORRELATION_THRESHOLD: float = 0.70

# Number of daily closes to use. 30 trading days ≈ 6 calendar weeks.
_CORR_DAYS: int = 30


def pearson(a: list[float], b: list[float]) -> float | None:
    """
    Pearson correlation coefficient of two equal-length sequences.
    Returns None when n < 5 or either series has zero variance.
    Result is clamped to [-1, 1] to absorb floating-point drift.
    """
    n = len(a)
    if n != len(b) or n < 5:
        return None

    mean_a = sum(a) / n
    mean_b = sum(b) / n

    num   = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)

    denom = math.sqrt(var_a * var_b)
    if denom == 0.0:
        return None

    return max(-1.0, min(1.0, num / denom))


def pct_returns(closes: list[float]) -> list[float]:
    """Close-to-close percentage returns. Skips any zero-priced close."""
    return [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]


def fetch_correlation(exchange, sym_a: str, sym_b: str, days: int = _CORR_DAYS) -> float | None:
    """
    Fetch `days` daily closes for sym_a and sym_b from `exchange`, align them
    by timestamp, then return the Pearson correlation of their daily returns.

    Returns None on any error — callers treat None as "no data, allow the BUY."

    Both symbols must be valid on the same exchange (e.g. Kraken BTC/CAD and XRP/CAD).
    """
    limit = days + 1   # need N+1 closes to compute N returns

    try:
        raw_a = exchange.fetch_ohlcv(sym_a, timeframe="1d", limit=limit)
        raw_b = exchange.fetch_ohlcv(sym_b, timeframe="1d", limit=limit)
    except Exception as exc:
        logger.warning("correlation: fetch failed (%s vs %s): %s", sym_a, sym_b, exc)
        return None

    if len(raw_a) < 6 or len(raw_b) < 6:
        logger.warning(
            "correlation: insufficient daily candles (%s=%d, %s=%d)",
            sym_a, len(raw_a), sym_b, len(raw_b),
        )
        return None

    # Align by timestamp — exchange clocks may return slightly different sets
    closes_a = {int(row[0]): float(row[4]) for row in raw_a}
    closes_b = {int(row[0]): float(row[4]) for row in raw_b}

    common_ts = sorted(set(closes_a) & set(closes_b))
    if len(common_ts) < 6:
        logger.warning(
            "correlation: only %d common timestamps between %s and %s",
            len(common_ts), sym_a, sym_b,
        )
        return None

    aligned_a = [closes_a[t] for t in common_ts]
    aligned_b = [closes_b[t] for t in common_ts]

    ret_a = pct_returns(aligned_a)
    ret_b = pct_returns(aligned_b)

    n = min(len(ret_a), len(ret_b))
    if n < 5:
        return None

    corr = pearson(ret_a[-n:], ret_b[-n:])
    logger.info(
        "correlation(%s, %s, %d days) = %s",
        sym_a, sym_b, n,
        f"{corr:.3f}" if corr is not None else "None",
    )
    return corr
