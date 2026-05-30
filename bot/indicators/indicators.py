"""
Technical indicator calculations — pure, stateless functions.

All functions return None when there is insufficient price history.
No side-effects, no state, no exchange dependencies.
"""
from __future__ import annotations


def sma(prices: list[float], period: int) -> float | None:
    """Simple Moving Average of the last `period` values."""
    if len(prices) < period:
        return None
    window = prices[-period:]
    return sum(window) / period


def ema(prices: list[float], period: int) -> float | None:
    """
    Exponential Moving Average.  k = 2 / (period + 1).
    Seeded from the SMA of the first `period` values, then EMA smoothing applied.
    Returns None when fewer than `period` prices are available.
    """
    if len(prices) < period:
        return None
    k = 2.0 / (period + 1)
    result = sum(prices[:period]) / period  # SMA seed
    for price in prices[period:]:
        result = price * k + result * (1.0 - k)
    return result


def rsi(prices: list[float], period: int = 14) -> float | None:
    """
    Relative Strength Index using Wilder's smoothing method.
    Requires at least period + 1 data points to compute one price change.
    Returns a value in [0, 100], or None on insufficient data.
    """
    if len(prices) < period + 1:
        return None

    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [max(c, 0.0) for c in changes]
    losses = [abs(min(c, 0.0)) for c in changes]

    # Seed from first `period` changes
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing for any additional data points
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def trend(prices: list[float], fast_period: int = 9, slow_period: int = 21) -> str:
    """
    Trend direction via EMA crossover.

    Returns:
        "BULLISH"  — fast EMA is above slow EMA (by more than 0.01%)
        "BEARISH"  — fast EMA is below slow EMA (by more than 0.01%)
        "NEUTRAL"  — EMAs are within 0.01% of each other, or data insufficient
    """
    fast = ema(prices, fast_period)
    slow = ema(prices, slow_period)
    if fast is None or slow is None:
        return "NEUTRAL"
    band = slow * 0.0001  # 0.01% dead-band avoids noise-driven flips
    if fast > slow + band:
        return "BULLISH"
    if fast < slow - band:
        return "BEARISH"
    return "NEUTRAL"
