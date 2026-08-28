"""
Unit tests for stock_mean_reversion_experiment.py.

Hermetic — synthetic data only, no network. Decision logic is in the pure
helpers `_signal` (3 gates, long + short) and `_exit_reason` (stop/target/
time precedence for both sides), tested with exact hand-picked numbers;
integration tests drive `run_backtest`. Synthetic slices were verified
against the real `bot.indicators` math.

Run: python -m pytest tests/stock/test_stock_mean_reversion_experiment.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stock_bot.data.price_feed import Candle
from stock_mean_reversion_experiment import (
    ADX_RANGING_MAX,
    COOLDOWN_BARS,
    MIN_TRADES_FULL_WINDOW,
    STOP_PCT,
    TIME_STOP_BARS,
    WARMUP,
    _exit_reason,
    _fmt_pf,
    _pf_stats,
    _signal,
    _verdict,
    run_backtest,
)

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _c(o: float, h: float, l: float, cl: float, i: int) -> Candle:
    return Candle(timestamp=_T0 + timedelta(days=i), open=o, high=h, low=l, close=cl, volume=1e6)


def _ranging(n: int = 54) -> list[float]:
    return [100.0 + (0.5 if k % 2 == 0 else -0.5) for k in range(n)]


def _long_slice():
    c = _ranging() + [98.0, 96.0, 94.0, 92.0, 90.0, 86.0]   # oversold dip
    h = [x + 0.6 for x in c]
    l = [x - 0.6 for x in c]
    return c, h, l


def _short_slice():
    c = _ranging() + [102.0, 104.0, 106.0, 108.0, 110.0, 114.0]   # overbought spike
    h = [x + 0.6 for x in c]
    l = [x - 0.6 for x in c]
    return c, h, l


# ---------------------------------------------------------------------------
# _signal
# ---------------------------------------------------------------------------

def test_signal_long_on_oversold_dip_in_a_range():
    side, adx_val = _signal(*_long_slice(), allow_short=True)
    assert side == "LONG"
    assert adx_val is not None and adx_val < ADX_RANGING_MAX


def test_signal_short_on_overbought_spike_when_shorts_allowed():
    side, _ = _signal(*_short_slice(), allow_short=True)
    assert side == "SHORT"


def test_signal_no_short_when_shorts_disabled():
    assert _signal(*_short_slice(), allow_short=False) == (None, None)


def test_signal_none_when_close_inside_the_bands():
    c, h, l = _long_slice()
    c[-1], h[-1], l[-1] = 99.0, 99.6, 98.4          # mild pullback, inside band
    assert _signal(c, h, l, allow_short=True)[0] is None


def test_signal_none_when_rsi_not_extreme():
    # rising into a shallow last-bar dip: pierces the band but RSI ~46
    c = [80.0 + 0.7 * k for k in range(30)] + \
        [100.0 + (0.4 if k % 2 else -0.4) for k in range(15)] + [96.0]
    h = [x + 0.4 for x in c]
    l = [x - 0.4 for x in c]
    assert _signal(c, h, l, allow_short=True)[0] is None


def test_signal_none_when_adx_trending():
    # a rally that reverses into an oversold decline: below the band AND
    # RSI < 35, but ADX is far above the ranging cap.
    c = [60.0 + k for k in range(40)] + [99.0, 96.0, 92.0, 87.0, 81.0, 74.0]
    h = [x + 0.5 for x in c]
    l = [x - 0.5 for x in c]
    side, adx_val = _signal(c, h, l, allow_short=True)
    assert side is None
    assert adx_val is not None and adx_val >= ADX_RANGING_MAX


# ---------------------------------------------------------------------------
# _exit_reason
# ---------------------------------------------------------------------------

def test_exit_long_stop_beats_target():
    stop = 100.0 * (1 - STOP_PCT)
    assert _exit_reason("LONG", high_i=110, low_i=stop - 0.01, close_i=105,
                        entry_px=100.0, bars_held=2, middle=100.0) == "stop"


def test_exit_long_target_and_time():
    assert _exit_reason("LONG", 101, 99, 100.5, 100.0, 2, 100.0) == "target"
    assert _exit_reason("LONG", 99, 98, 98.5, 100.0, TIME_STOP_BARS, 100.0) == "time"
    assert _exit_reason("LONG", 99, 98, 98.5, 100.0, TIME_STOP_BARS - 1, 100.0) is None


def test_exit_short_stop_on_high_beats_target():
    stop = 100.0 * (1 + STOP_PCT)
    assert _exit_reason("SHORT", high_i=stop + 0.01, low_i=90, close_i=95,
                        entry_px=100.0, bars_held=2, middle=100.0) == "stop"


def test_exit_short_target_when_close_falls_to_middle():
    assert _exit_reason("SHORT", 101, 98, 99.5, 100.0, 2, 100.0) == "target"
    # close still above the middle band, stop not hit -> hold
    assert _exit_reason("SHORT", 103, 99, 101.0, 100.0, 2, 100.0) is None


# ---------------------------------------------------------------------------
# _pf_stats / _fmt_pf / _verdict
# ---------------------------------------------------------------------------

def test_pf_stats_and_fmt():
    assert _pf_stats([]) == {"trades": 0, "wins": 0, "win_rate": 0.0, "pf": 0.0, "net": 0.0}
    s = _pf_stats([10.0, -5.0, 20.0, -5.0])
    assert s["pf"] == pytest.approx(3.0) and s["net"] == pytest.approx(20.0)
    assert _pf_stats([1.0, 2.0])["pf"] == float("inf")
    assert _fmt_pf(float("inf")) == "inf" and _fmt_pf(1.239) == "1.24"


def test_verdict_gate():
    def W(window, trades, pf):
        return {"window": window, "trades": trades, "pf": pf, "win_rate": 50.0, "net": 1.0}

    ok = [W(0, 15, 1.5), W(750, 10, 1.4), W(500, 8, 2.0), W(250, 4, 3.0)]
    assert _verdict(ok, full_sl_rate=0.4).startswith("PASS")

    assert _verdict([W(0, 6, 3.0)], full_sl_rate=0.4).startswith("FAILED")            # < 10 full
    assert _verdict(ok, full_sl_rate=0.8).startswith("FAILED")                        # SL-exit too high
    bad_pf = [W(0, 15, 1.5), W(750, 12, 0.9), W(500, 8, 2.0), W(250, 1, 5.0)]
    assert _verdict(bad_pf, full_sl_rate=0.4).startswith("FAILED") and "750d" in _verdict(bad_pf, 0.4)
    # a low-sample window with weak PF does NOT fail the verdict
    lows = [W(0, 15, 1.5), W(750, 10, 1.3), W(500, 2, 0.2), W(250, 1, 0.1)]
    assert _verdict(lows, full_sl_rate=0.4).startswith("PASS")


# ---------------------------------------------------------------------------
# run_backtest — integration
# base(): chop + decline [98,95,92,90]; verified LONG entry at idx 52 @ 92,
# open at bar 53, continuations appended from bar 54.
# ---------------------------------------------------------------------------

def _base() -> list[Candle]:
    b = [_c(p, p + 0.6, p - 0.6, p, i)
         for i, p in ((k, 100.0 + (0.5 if k % 2 == 0 else -0.5)) for k in range(50))]
    for i, p in enumerate([98.0, 95.0, 92.0, 90.0], start=50):
        b.append(_c(p + 1, p + 1.1, p - 0.5, p, i))
    return b

_ENTRY_IDX = 52


def test_backtest_guards():
    assert run_backtest("X", [], allow_short=False).trades == []
    assert run_backtest("X", [_c(100, 101, 99, 100, i) for i in range(WARMUP)],
                        allow_short=False).trades == []


def test_backtest_long_target_round_trip_is_profitable_net_of_costs():
    bars = _base()
    for i, p in enumerate([93.0, 97.0, 102.0, 105.0], start=54):
        bars.append(_c(p - 1, p + 0.5, p - 1.2, p, i))
    res = run_backtest("AMD", bars, allow_short=False)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.side == "LONG" and t.entry_idx == _ENTRY_IDX and t.reason == "target"
    assert t.shares == int(1000 / 92.0)
    assert t.pnl > 0                       # still profitable after IBKR commission + slippage


def test_backtest_long_stop_exit_is_a_loss():
    bars = _base()
    bars.append(_c(90, 90, 84.0, 85.0, 54))
    for i, p in enumerate([101, 103, 105], start=55):
        bars.append(_c(p, p + 0.5, p - 0.5, p, i))
    res = run_backtest("AMD", bars, allow_short=False)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.reason == "stop"
    assert t.exit_px == pytest.approx(92.0 * (1 - STOP_PCT) * (1 - 15 / 10_000))
    assert t.pnl < 0


def test_backtest_costs_reduce_pnl_vs_a_zero_cost_run():
    bars = _base()
    for i, p in enumerate([93.0, 97.0, 102.0, 105.0], start=54):
        bars.append(_c(p - 1, p + 0.5, p - 1.2, p, i))
    with_costs = run_backtest("AMD", bars, allow_short=False, slippage_bps=15).trades[0].pnl
    no_slip = run_backtest("AMD", bars, allow_short=False, slippage_bps=0).trades[0].pnl
    assert with_costs < no_slip           # slippage costs both legs


def test_backtest_time_stop_at_the_bar_limit():
    bars = _base()
    hold = 92.0 * (1 - STOP_PCT) + 1.0
    for k in range(1, TIME_STOP_BARS + 4):
        bars.append(_c(hold, hold + 0.3, hold - 0.3, hold, 53 + k))
    res = run_backtest("AMD", bars, allow_short=False)
    assert len(res.trades) == 1
    assert res.trades[0].reason == "time"
    assert res.trades[0].exit_idx - res.trades[0].entry_idx == TIME_STOP_BARS


def test_backtest_short_leg_only_fires_when_allowed():
    bars = [_c(p, p + 0.6, p - 0.6, p, i)
            for i, p in ((k, 100.0 + (0.5 if k % 2 == 0 else -0.5)) for k in range(50))]
    for i, p in enumerate([102.0, 105.0, 108.0, 112.0], start=50):
        bars.append(_c(p - 1, p + 0.5, p - 1.2, p, i))
    for i, p in enumerate([108.0, 103.0, 99.0], start=54):   # falls back to the mean
        bars.append(_c(p + 1, p + 1.2, p - 0.5, p, i))

    assert run_backtest("AMD", bars, allow_short=False).trades == []
    res = run_backtest("AMD", bars, allow_short=True)
    assert len(res.trades) == 1
    assert res.trades[0].side == "SHORT"


def test_backtest_skips_a_symbol_priced_above_the_notional():
    b = [_c(p, p + 5, p - 5, p, i)
         for i, p in ((k, 3000.0 + (20 if k % 2 == 0 else -20)) for k in range(50))]
    for i, p in enumerate([2900.0, 2800.0, 2700.0, 2500.0], start=50):
        b.append(_c(p + 20, p + 25, p - 20, p, i))
    res = run_backtest("BRK", b, allow_short=False)
    assert res.trades == []
    assert res.skipped_unaffordable >= 1   # int(1000 / 2500) == 0


def test_backtest_cooldown_blocks_next_bar_re_entry():
    bars = _base()
    bars.append(_c(90, 102, 89, 101, 54))          # target exit on bar 54
    bars.append(_c(101, 101, 70, 72, 55))          # re-qualifying plunge, 1 bar later
    bars.append(_c(72, 90, 71, 80, 56))
    res = run_backtest("AMD", bars, allow_short=False)
    assert len(res.trades) == 1
    assert res.trades[0].exit_idx == 54
    assert COOLDOWN_BARS >= 1
