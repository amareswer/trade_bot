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


def adx(
    highs:  list[float],
    lows:   list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    """
    Average Directional Index using Wilder's smoothing.

    Measures trend *strength* (not direction): > 25 = trending, < 20 = ranging.
    Requires at least 2 * period + 1 data points.
    Returns None when data is insufficient.
    """
    n = len(closes)
    if n < 2 * period + 1 or len(highs) != n or len(lows) != n:
        return None

    tr_list  = []
    pdm_list = []
    ndm_list = []

    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        ph, pl   = highs[i - 1], lows[i - 1]

        tr   = max(h - l, abs(h - pc), abs(l - pc))
        up   = h - ph
        down = pl - l
        pdm  = up   if up > down and up > 0   else 0.0
        ndm  = down if down > up and down > 0 else 0.0

        tr_list.append(tr)
        pdm_list.append(pdm)
        ndm_list.append(ndm)

    # Seed Wilder's smoothing using first `period` values
    atr  = sum(tr_list[:period])
    apdm = sum(pdm_list[:period])
    andm = sum(ndm_list[:period])

    dx_list: list[float] = []
    for i in range(period, len(tr_list)):
        atr  = atr  - atr  / period + tr_list[i]
        apdm = apdm - apdm / period + pdm_list[i]
        andm = andm - andm / period + ndm_list[i]

        pdi   = 100.0 * apdm / atr if atr > 0 else 0.0
        ndi   = 100.0 * andm / atr if atr > 0 else 0.0
        denom = pdi + ndi
        dx_list.append(100.0 * abs(pdi - ndi) / denom if denom > 0 else 0.0)

    if len(dx_list) < period:
        return None

    # Seed ADX from first `period` DX values, then Wilder's smooth the rest
    adx_val = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period

    return adx_val


def atr(
    highs:  list[float],
    lows:   list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    """
    Average True Range via Wilder's smoothing (same method as ADX).
    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    Requires at least 2*period data points.
    """
    n = len(closes)
    if n < 2 * period or len(highs) != n or len(lows) != n:
        return None

    tr_list: list[float] = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        tr_list.append(tr)

    if len(tr_list) < period:
        return None

    # Seed: simple average of first `period` TR values
    atr_val = sum(tr_list[:period]) / period
    # Wilder's smooth for the rest
    for tr in tr_list[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period

    return atr_val


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


def macd(
    prices: list[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[float, float, float] | None:
    """
    MACD — Moving Average Convergence Divergence.
    Returns (macd_line, signal_line, histogram) or None on insufficient data.
    Histogram rising across consecutive candles = momentum accelerating.
    """
    if len(prices) < slow_period + signal_period:
        return None
    macd_values: list[float] = []
    for i in range(slow_period - 1, len(prices)):
        window = prices[: i + 1]
        fast = ema(window, fast_period)
        slow = ema(window, slow_period)
        if fast is None or slow is None:
            continue
        macd_values.append(fast - slow)
    if len(macd_values) < signal_period:
        return None
    signal_line = ema(macd_values, signal_period)
    if signal_line is None:
        return None
    macd_line = macd_values[-1]
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram
