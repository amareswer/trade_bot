"""
Technical indicator calculations for the stock bot — pure, stateless functions.

Copied from bot/indicators/indicators.py and extended with MACD.
All functions return None (or a tuple of Nones) when data is insufficient.
No side-effects, no state, no I/O.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Core building blocks
# ---------------------------------------------------------------------------

def sma(prices: list[float], period: int) -> float | None:
    """Simple Moving Average of the last `period` values."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def ema(prices: list[float], period: int) -> float | None:
    """
    Exponential Moving Average.  k = 2 / (period + 1).
    Seeded from the SMA of the first `period` values, then EMA smoothing applied.
    Returns None when fewer than `period` prices are available.
    """
    if len(prices) < period:
        return None
    k = 2.0 / (period + 1)
    val = sum(prices[:period]) / period  # SMA seed
    for p in prices[period:]:
        val = p * k + val * (1.0 - k)
    return val


def _ema_series(prices: list[float], period: int) -> list[float]:
    """
    Return the full EMA series (one value per input price, starting once
    we have `period` values). Length = max(0, len(prices) - period + 1).
    Used internally by macd().
    """
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    val = sum(prices[:period]) / period
    series = [val]
    for p in prices[period:]:
        val = p * k + val * (1.0 - k)
        series.append(val)
    return series  # series[i] corresponds to prices[period - 1 + i]


# ---------------------------------------------------------------------------
# Oscillators
# ---------------------------------------------------------------------------

def rsi(prices: list[float], period: int = 14) -> float | None:
    """
    Relative Strength Index using Wilder's smoothing method.
    Requires at least period + 1 data points.
    Returns a value in [0, 100], or None on insufficient data.
    """
    if len(prices) < period + 1:
        return None

    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [max(c, 0.0) for c in changes]
    losses = [abs(min(c, 0.0)) for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def macd(
    prices:        list[float],
    fast_period:   int = 12,
    slow_period:   int = 26,
    signal_period: int = 9,
) -> tuple[float, float, float] | None:
    """
    Moving Average Convergence/Divergence.

    Returns (macd_line, signal_line, histogram) or None when data is insufficient.

    macd_line = EMA(fast) - EMA(slow)
    signal    = EMA(macd_line, signal_period)
    histogram = macd_line - signal
    """
    fast_s = _ema_series(prices, fast_period)
    slow_s = _ema_series(prices, slow_period)

    # Align: fast_s[i] ↔ prices[fast_period-1+i]
    #        slow_s[i] ↔ prices[slow_period-1+i]
    # Trim fast_s so both series cover the same price range.
    offset = slow_period - fast_period
    if len(fast_s) <= offset or not slow_s:
        return None

    aligned_fast = fast_s[offset:]  # now same length as slow_s
    macd_series  = [f - s for f, s in zip(aligned_fast, slow_s)]

    if len(macd_series) < signal_period:
        return None

    signal_val = ema(macd_series, signal_period)
    if signal_val is None:
        return None

    macd_val  = macd_series[-1]
    histogram = macd_val - signal_val
    return macd_val, signal_val, histogram


# ---------------------------------------------------------------------------
# Trend / regime
# ---------------------------------------------------------------------------

def adx(
    highs:  list[float],
    lows:   list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    """
    Average Directional Index using Wilder's smoothing.
    Measures trend strength (not direction): >25 = trending, <20 = ranging.
    Requires at least 2 * period + 1 data points.
    """
    n = len(closes)
    if n < 2 * period + 1 or len(highs) != n or len(lows) != n:
        return None

    tr_list, pdm_list, ndm_list = [], [], []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        ph, pl   = highs[i - 1], lows[i - 1]
        tr   = max(h - l, abs(h - pc), abs(l - pc))
        up   = h - ph
        down = pl - l
        tr_list.append(tr)
        pdm_list.append(up   if up > down and up > 0   else 0.0)
        ndm_list.append(down if down > up and down > 0 else 0.0)

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
    Average True Range using Wilder's smoothing.
    Returns ATR of the most recent candle, or None if fewer than period+1 candles.
    """
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return None

    tr_list: list[float] = []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))

    atr_val = sum(tr_list[:period]) / period
    for tr in tr_list[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def trend(
    prices:               list[float],
    fast_period:          int = 9,
    slow_period:          int = 21,
    prev_trend:           str | None = None,
    confirmation_candles: int = 1,
) -> str:
    """
    Trend direction via EMA crossover.
    Returns "BULLISH", "BEARISH", or "NEUTRAL".

    confirmation_candles=1 (default): uses prev_trend for one-candle confirmation.
      Returns NEUTRAL on the first candle of a new crossover (suppresses whipsaws).
    confirmation_candles=2: computes confirmation internally — requires both the
      current and prior candle to show fast EMA > slow EMA (BULLISH) or < (BEARISH).
      Does not require external state tracking via prev_trend.
    """
    fast = ema(prices, fast_period)
    slow = ema(prices, slow_period)
    if fast is None or slow is None:
        return "NEUTRAL"
    band = slow * 0.0001
    if fast > slow + band:
        current = "BULLISH"
    elif fast < slow - band:
        current = "BEARISH"
    else:
        return "NEUTRAL"

    if confirmation_candles >= 2:
        # Check prior candle direction by recomputing EMA on prices[:-1]
        fast_prev = ema(prices[:-1], fast_period)
        slow_prev = ema(prices[:-1], slow_period)
        if fast_prev is None or slow_prev is None:
            return "NEUTRAL"
        band_prev = slow_prev * 0.0001
        if current == "BULLISH" and not (fast_prev > slow_prev + band_prev):
            return "NEUTRAL"
        if current == "BEARISH" and not (fast_prev < slow_prev - band_prev):
            return "NEUTRAL"
        return current

    # Original prev_trend confirmation path (confirmation_candles=1)
    opposite = "BEARISH" if current == "BULLISH" else "BULLISH"
    if prev_trend is None or prev_trend == opposite:
        return "NEUTRAL"
    return current
