"""
Unit tests for bot/indicators/indicators.py and IndicatorStrategy.

Run: python -m pytest test_indicators.py -v
"""
from __future__ import annotations
import math
import random
import sys

sys.path.insert(0, ".")

from bot.indicators.indicators import sma, ema, rsi, trend, atr
from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig
from bot.strategy.threshold_strategy import Signal


def _close(a: float | None, b: float, tol: float = 0.01) -> bool:
    return a is not None and abs(a - b) <= tol


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------

def test_sma_returns_none_when_insufficient_data():
    assert sma([1.0, 2.0], 5) is None


def test_sma_basic():
    assert _close(sma([1, 2, 3, 4, 5], 5), 3.0)


def test_sma_uses_last_n_values():
    assert _close(sma([100, 1, 2, 3], 3), 2.0)


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

def test_ema_returns_none_when_insufficient_data():
    assert ema([1.0], 5) is None


def test_ema_flat_series_equals_constant():
    flat = [10.0] * 20
    assert _close(ema(flat, 10), 10.0)


def test_ema_returns_float_on_valid_input():
    prices = list(range(1, 11))
    assert ema(prices, 3) is not None


def test_ema_lags_in_rising_series():
    rising = list(range(1, 30))
    assert ema(rising, 9) < rising[-1]


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

def test_rsi_returns_none_when_insufficient_data():
    assert rsi(list(range(14)), 14) is None


def test_rsi_all_up_equals_100():
    all_up = [float(i) for i in range(1, 30)]
    assert _close(rsi(all_up, 14), 100.0, tol=0.1)


def test_rsi_all_down_equals_0():
    all_down = [float(i) for i in range(29, 0, -1)]
    assert _close(rsi(all_down, 14), 0.0, tol=0.1)


def test_rsi_alternating_near_50():
    alt = [100.0 if i % 2 == 0 else 99.0 for i in range(30)]
    r = rsi(alt, 14)
    assert r is not None and 40 < r < 60


def test_rsi_stays_in_bounds():
    random.seed(42)
    rand_prices = [random.uniform(50, 150) for _ in range(100)]
    r = rsi(rand_prices, 14)
    assert r is not None and 0 <= r <= 100


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------

def test_trend_rising_is_bullish():
    rising = [float(i) for i in range(1, 60)]
    assert trend(rising, 9, 21) == "BULLISH"


def test_trend_falling_is_bearish():
    falling = [float(i) for i in range(60, 0, -1)]
    assert trend(falling, 9, 21) == "BEARISH"


def test_trend_flat_is_neutral():
    flat = [50.0] * 50
    assert trend(flat, 9, 21) == "NEUTRAL"


def test_trend_insufficient_data_is_neutral():
    assert trend([1.0, 2.0], 9, 21) == "NEUTRAL"


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

_ATR_PERIOD = 3


def test_atr_returns_none_when_insufficient_data():
    h = [10.0] * _ATR_PERIOD
    l = [9.0] * _ATR_PERIOD
    c = [9.5] * _ATR_PERIOD
    assert atr(h, l, c, _ATR_PERIOD) is None


def test_atr_returns_none_on_length_mismatch():
    n = _ATR_PERIOD + 5
    h = [10.0] * n
    l = [9.0] * n
    c = [9.5] * (n - 1)
    assert atr(h, l, c, _ATR_PERIOD) is None


def test_atr_flat_series_returns_zero():
    flat_n = _ATR_PERIOD + 10
    flat = [100.0] * flat_n
    assert atr(flat, flat, flat, _ATR_PERIOD) == 0.0


def test_atr_rising_series_positive():
    rising_c = [float(100 + i) for i in range(_ATR_PERIOD + 10)]
    rising_h = [c + 1.0 for c in rising_c]
    rising_l = [c - 1.0 for c in rising_c]
    result = atr(rising_h, rising_l, rising_c, _ATR_PERIOD)
    assert result is not None and result > 0.0


def test_atr_within_price_bounds():
    rising_c = [float(100 + i) for i in range(_ATR_PERIOD + 10)]
    rising_h = [c + 1.0 for c in rising_c]
    rising_l = [c - 1.0 for c in rising_c]
    result = atr(rising_h, rising_l, rising_c, _ATR_PERIOD)
    bound = max(rising_h) - min(rising_l)
    assert result is not None and 0.0 <= result <= bound


def test_atr_all_equal_returns_zero():
    all_equal = [50.0] * (_ATR_PERIOD + 5)
    assert atr(all_equal, all_equal, all_equal, _ATR_PERIOD) == 0.0


def test_atr_sl_tp_math():
    entry = 100.0
    atr_val = 5.0
    sl_mult = 2.0
    tp_mult = 4.0
    sl_price = entry - atr_val * sl_mult   # 90.0
    tp_price = entry + atr_val * tp_mult   # 120.0
    rr = (tp_price - entry) / (entry - sl_price)
    assert sl_price == 90.0 and tp_price == 120.0 and rr == 2.0


# ---------------------------------------------------------------------------
# IndicatorStrategy integration
# ---------------------------------------------------------------------------

def _make_cfg() -> IndicatorConfig:
    return IndicatorConfig(
        rsi_period=14, rsi_oversold=30, rsi_overbought=70,
        fast_ema_period=9, slow_ema_period=21,
    )


def test_indicator_strategy_hold_during_warmup():
    cfg = _make_cfg()
    strat = IndicatorStrategy(cfg)
    warmup_prices = [float(100 + i) for i in range(20)]
    sig = Signal.HOLD
    for p in warmup_prices:
        sig = strat.evaluate(p)
    assert not strat.is_warmed_up or sig == Signal.HOLD


def test_indicator_strategy_flat_prices_all_hold():
    cfg = _make_cfg()
    strat = IndicatorStrategy(cfg)
    signals = [strat.evaluate(100.0) for _ in range(60)]
    non_hold = [s for s in signals if s != Signal.HOLD]
    assert len(non_hold) == 0


def test_indicator_strategy_oscillating_emits_buy():
    cfg = _make_cfg()
    strat = IndicatorStrategy(cfg)
    prices = [90.0 + 40.0 * math.sin(2 * math.pi * i / 40) for i in range(400)]
    signals = [strat.evaluate(p) for p in prices]
    assert Signal.BUY in signals


def test_indicator_strategy_oscillating_emits_sell():
    cfg = _make_cfg()
    strat = IndicatorStrategy(cfg)
    prices = [90.0 + 40.0 * math.sin(2 * math.pi * i / 40) for i in range(400)]
    signals = [strat.evaluate(p) for p in prices]
    assert Signal.SELL in signals


def test_indicator_strategy_all_signals_valid():
    cfg = _make_cfg()
    strat = IndicatorStrategy(cfg)
    prices = [90.0 + 40.0 * math.sin(2 * math.pi * i / 40) for i in range(400)]
    signals = [strat.evaluate(p) for p in prices]
    assert all(s in (Signal.BUY, Signal.SELL, Signal.HOLD) for s in signals)
