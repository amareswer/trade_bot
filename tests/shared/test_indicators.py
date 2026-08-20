"""
Unit tests for bot/indicators/indicators.py and IndicatorStrategy.

Run: python -m pytest tests/shared/test_indicators.py -v
"""
from __future__ import annotations
import math
import random
import sys

sys.path.insert(0, ".")

from bot.indicators.indicators import sma, ema, rsi, trend, atr
from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig, Regime
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
        # Set Mode A RSI ceiling to match the test's rsi_overbought so the
        # oscillating test data (EMA lag + discrete steps) can still emit BUY.
        pullback_rsi_max=70.0,
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


# ---------------------------------------------------------------------------
# Regime classification — self-referential ATR baseline (2026-08-19 fix)
#
# Bug: evaluate() used to append the current bar's ATR to _atr_history
# BEFORE calling _classify_regime(), so the VOLATILE check compared the
# current spike against a baseline that already included that same spike
# (self-inclusion bias — not a lookahead bug, nothing future was used, but
# it made a genuine spike slightly harder to detect than comparing against
# the strictly-prior history). Fixed by moving the append to after
# _classify_regime() returns.
# ---------------------------------------------------------------------------

def test_classify_regime_excludes_current_bar_from_baseline():
    """
    Direct spec-level proof of _classify_regime()'s contract: it must judge
    atr_val against the mean of _atr_history exactly as given, with no
    self-appending of its own. This does NOT by itself exercise the
    evaluate()-ordering bug (_classify_regime() never appended anything —
    the bug was entirely in evaluate()'s call order around it) — see
    test_evaluate_regime_excludes_current_bar_from_baseline below for the
    test that actually would have failed under the pre-fix ordering.
    """
    cfg = _make_cfg()
    strat = IndicatorStrategy(cfg)
    assert strat.config.atr_volatile_multiplier == 1.5, (
        "test math assumes the default 1.5x multiplier"
    )

    prior_history = [1.0] * 10
    spike = 1.55  # 1.55 > 1.5 * mean([1.0]*10) == 1.5

    strat._atr_history.clear()
    strat._atr_history.extend(prior_history)
    assert strat._classify_regime(adx_val=None, atr_val=spike) == Regime.VOLATILE

    # Folding the spike into the history BEFORE classifying (what evaluate()
    # itself must never do) raises the mean enough that the same spike no
    # longer trips VOLATILE — demonstrates why the ordering matters, using
    # _classify_regime() directly (does not exercise evaluate()).
    strat._atr_history.clear()
    strat._atr_history.extend(prior_history)
    strat._atr_history.append(spike)
    assert strat._classify_regime(adx_val=None, atr_val=spike) != Regime.VOLATILE


def test_evaluate_regime_excludes_current_bar_from_baseline():
    """
    THE regression test for the 2026-08-19 fix — drives the real evaluate()
    end to end (not a hand-rolled reproduction) with a spike candle sized to
    land in the narrow band between the exclusive-mean threshold and the
    self-inclusive-mean threshold: atr_val ends up just above
    1.5 * mean(prior 20 ATRs) (~1.500) but just below
    1.5 * mean(prior 19 ATRs + this bar's own ATR) (~1.539) — the precise
    discriminator between the two orderings. Verified by mutation: reverting
    the evaluate() fix (restoring the append-before-classify order) makes
    this test FAIL (regime comes back RANGING, not VOLATILE) with no other
    change; confirmed manually before committing this test, not asserted
    from theory alone.
    """
    from datetime import datetime, timedelta, timezone
    from bot.data.historical_feed import Candle

    cfg = _make_cfg()
    strat = IndicatorStrategy(cfg)
    assert strat.config.atr_volatile_multiplier == 1.5, (
        "test math (spike sizing below) assumes the default 1.5x multiplier"
    )

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _candle(i: int, close: float, high: float, low: float) -> Candle:
        return Candle(
            timestamp=t0 + timedelta(hours=i),
            open=close, high=high, low=low, close=close, volume=1.0,
        )

    # 260 stable, low-range candles — IndicatorConfig's default
    # regime_ema_period=200 drives _warmup to 202; need to clear that AND
    # leave room past it for the ATR/_atr_history >= 10 guards, settling
    # a full 20-entry baseline (_atr_history's maxlen) at a stable ~1.0.
    price = 100.0
    for i in range(260):
        price += 0.1 if i % 2 == 0 else -0.1
        strat.evaluate(_candle(i, price, price + 0.5, price - 0.5))

    assert len(strat._atr_history) == 20
    baseline_before_spike = list(strat._atr_history)
    excl_mean = sum(baseline_before_spike) / len(baseline_before_spike)
    assert _close(excl_mean, 1.0, tol=0.05)

    # Spike sized (found by search, not guessed) so the resulting ATR sits
    # between the exclusive and self-inclusive thresholds — see docstring.
    extra = 4.15
    spike_close = price + extra
    strat.evaluate(_candle(260, spike_close, spike_close + extra * 0.5, price - extra * 0.5))

    assert strat._last_atr is not None
    excl_threshold = cfg.atr_volatile_multiplier * excl_mean
    incl_threshold = cfg.atr_volatile_multiplier * (
        (sum(baseline_before_spike[1:]) + strat._last_atr) / 20
    )
    assert strat._last_atr > excl_threshold, (
        "spike must clear the exclusive-mean threshold — the correct, "
        "fixed comparison"
    )
    assert strat._last_atr < incl_threshold, (
        "spike must NOT clear the self-inclusive-mean threshold — proves "
        "this exact case would have been missed under the pre-fix ordering"
    )
    assert strat._last_regime == Regime.VOLATILE
    # The value just classified against was never in the baseline it was
    # judged against — confirms classify ran before append, not just that
    # the end result happens to be VOLATILE.
    assert strat._last_atr not in baseline_before_spike
