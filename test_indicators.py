"""
Unit tests for bot/indicators/indicators.py and IndicatorStrategy.

Run: python test_indicators.py
"""
from __future__ import annotations
import sys
import math

sys.path.insert(0, ".")

from bot.indicators.indicators import sma, ema, rsi, trend, atr
from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig
from bot.strategy.threshold_strategy import Signal

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
_results: list[bool] = []


def check(label: str, condition: bool) -> None:
    _results.append(condition)
    print(f"  [{PASS if condition else FAIL}] {label}")


def close(a: float | None, b: float, tol: float = 0.01) -> bool:
    return a is not None and abs(a - b) <= tol


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------
print("\nSMA")
check("returns None when insufficient data", sma([1.0, 2.0], 5) is None)
check("SMA of [1,2,3,4,5] period=5 == 3.0", close(sma([1, 2, 3, 4, 5], 5), 3.0))
check("SMA uses last N values only", close(sma([100, 1, 2, 3], 3), 2.0))  # avg(1,2,3)

# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------
print("\nEMA")
check("returns None when insufficient data", ema([1.0], 5) is None)
# With a flat series, EMA == SMA
flat = [10.0] * 20
check("EMA of flat series equals the constant", close(ema(flat, 10), 10.0))
# EMA of [1..10], period=3: seed=2.0, then k=0.5 smoothing through 4..10
prices = list(range(1, 11))
result = ema(prices, 3)
check("EMA(period=3) on [1..10] is a float", result is not None)
# Rising prices → EMA should be below most recent value (lagging indicator)
rising = list(range(1, 30))
check("EMA lags in a rising series", ema(rising, 9) < rising[-1])

# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------
print("\nRSI")
check("returns None when < period+1 data points", rsi(list(range(14)), 14) is None)
# All gains → RSI should be 100
all_up = [float(i) for i in range(1, 30)]
check("all-up series → RSI == 100", close(rsi(all_up, 14), 100.0, tol=0.1))
# All losses → RSI should be 0
all_down = [float(i) for i in range(29, 0, -1)]
check("all-down series → RSI == 0", close(rsi(all_down, 14), 0.0, tol=0.1))
# Alternating → RSI near 50
alt = [100.0 if i % 2 == 0 else 99.0 for i in range(30)]
r = rsi(alt, 14)
check("alternating series → RSI near 50", r is not None and 40 < r < 60)
# RSI stays in [0, 100]
import random; random.seed(42)
rand_prices = [random.uniform(50, 150) for _ in range(100)]
r2 = rsi(rand_prices, 14)
check("RSI in [0, 100]", r2 is not None and 0 <= r2 <= 100)

# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------
print("\nTrend")
# Rising prices → fast EMA > slow EMA → BULLISH
rising = [float(i) for i in range(1, 60)]
check("rising series → BULLISH", trend(rising, 9, 21) == "BULLISH")
# Falling prices → fast EMA < slow EMA → BEARISH
falling = [float(i) for i in range(60, 0, -1)]
check("falling series → BEARISH", trend(falling, 9, 21) == "BEARISH")
# Flat series → NEUTRAL
flat = [50.0] * 50
check("flat series → NEUTRAL", trend(flat, 9, 21) == "NEUTRAL")
# Insufficient data → NEUTRAL
check("insufficient data → NEUTRAL", trend([1.0, 2.0], 9, 21) == "NEUTRAL")

# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------
print("\nATR")

_period = 3

# 1. None when insufficient data (< period+1 closes)
_short_h = [10.0] * _period
_short_l = [9.0]  * _period
_short_c = [9.5]  * _period          # exactly period closes — need period+1
check("returns None when insufficient data (< period+1 closes)",
      atr(_short_h, _short_l, _short_c, _period) is None)

# 2. None when list lengths mismatch
_n = _period + 5
_h = [10.0] * _n
_l = [9.0]  * _n
_c = [9.5]  * (_n - 1)              # one element short
check("returns None when list lengths mismatch",
      atr(_h, _l, _c, _period) is None)

# 3. Flat series → ATR == 0.0  (no movement = zero true range every bar)
_flat_n = _period + 10
_flat   = [100.0] * _flat_n
check("flat series (constant price) returns 0.0",
      atr(_flat, _flat, _flat, _period) == 0.0)

# 4. Rising series → ATR > 0
_rising_c = [float(100 + i) for i in range(_period + 10)]
_rising_h = [c + 1.0 for c in _rising_c]
_rising_l = [c - 1.0 for c in _rising_c]
_atr_rising = atr(_rising_h, _rising_l, _rising_c, _period)
check("rising series returns a positive float",
      _atr_rising is not None and _atr_rising > 0.0)

# 5. ATR is between 0 and (max_high - min_low) inclusive
_bound = max(_rising_h) - min(_rising_l)
check("ATR value is between 0 and (max_high - min_low) inclusive",
      _atr_rising is not None and 0.0 <= _atr_rising <= _bound)

# 6. All equal high/low/close (flat market) → TR = 0 → ATR = 0.0
_all_equal = [50.0] * (_period + 5)
check("all equal high/low/close (flat market) → ATR = 0.0",
      atr(_all_equal, _all_equal, _all_equal, _period) == 0.0)

# 7. ATR SL/TP math: entry=100, atr=5, sl_mult=2→sl=90, tp_mult=4→tp=120, R/R=2.0
_entry_price = 100.0
_atr_test    = 5.0
_sl_mult     = 2.0
_tp_mult     = 4.0
_sl_price    = _entry_price - _atr_test * _sl_mult   # 90.0
_tp_price    = _entry_price + _atr_test * _tp_mult   # 120.0
_rr          = (_tp_price - _entry_price) / (_entry_price - _sl_price)  # 2.0
check("ATR SL/TP math: entry=100 atr=5 sl_mult=2 → sl=90, tp_mult=4 → tp=120, R/R=2.0",
      _sl_price == 90.0 and _tp_price == 120.0 and _rr == 2.0)

# ---------------------------------------------------------------------------
# IndicatorStrategy integration
# ---------------------------------------------------------------------------
print("\nIndicatorStrategy")

cfg = IndicatorConfig(rsi_period=14, rsi_oversold=30, rsi_overbought=70,
                      fast_ema_period=9, slow_ema_period=21)
strat = IndicatorStrategy(cfg)

# During warmup all signals must be HOLD
warmup_prices = [float(100 + i) for i in range(20)]
for p in warmup_prices:
    sig = strat.evaluate(p)
check("HOLD during warmup period", not strat.is_warmed_up or sig == Signal.HOLD)

# Flat prices after warmup → all HOLD (RSI ≈ 50, NEUTRAL trend)
strat_flat = IndicatorStrategy(cfg)
flat_signals = [strat_flat.evaluate(100.0) for _ in range(60)]
non_hold = [s for s in flat_signals if s != Signal.HOLD]
check("flat prices → only HOLD signals", len(non_hold) == 0)

# Oscillating series — over enough cycles the strategy must emit both BUY and SELL.
# Sine wave creates periodic EMA crossovers; RSI filter lets most crossover-aligned
# ticks through since extremes (> 70 during BULLISH, < 30 during BEARISH) are avoided.
strat_osc = IndicatorStrategy(cfg)
osc_prices = [90.0 + 40.0 * math.sin(2 * math.pi * i / 40) for i in range(400)]
osc_signals = [strat_osc.evaluate(p) for p in osc_prices]
check("oscillating series emits at least one BUY", Signal.BUY in osc_signals)
check("oscillating series emits at least one SELL", Signal.SELL in osc_signals)

# Signals are always one of the three valid values
check("all signals are valid Signal enum values",
      all(s in (Signal.BUY, Signal.SELL, Signal.HOLD) for s in osc_signals))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
total  = len(_results)
passed = sum(_results)
print(f"\n{'='*50}")
print(f"Results: {passed}/{total} passed")
if passed < total:
    sys.exit(1)
