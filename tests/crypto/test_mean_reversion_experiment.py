"""
Unit tests for mean_reversion_experiment.py's standalone backtest engine.

Hermetic — synthetic data only, no network. The entry/exit decision logic is
extracted into pure helpers (`_entry_check`, `_exit_reason`) tested with exact
hand-picked numbers; integration tests drive the full
`run_mean_reversion_backtest` loop over crafted candle series (the crafted
series and their expected entry indices were verified against the real
`bot.indicators` math, not guessed).

Run: python -m pytest tests/crypto/test_mean_reversion_experiment.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.data.historical_feed import Candle
from mean_reversion_experiment import (
    ADX_RANGING_MAX,
    COOLDOWN_BARS,
    STOP_PCT,
    TIME_STOP_BARS,
    WARMUP,
    _entry_check,
    _exit_reason,
    _fmt_pf,
    _pf_stats,
    _verdict,
    run_mean_reversion_backtest,
)

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _c(o: float, h: float, l: float, cl: float, i: int) -> Candle:
    return Candle(timestamp=_T0 + timedelta(hours=i), open=o, high=h, low=l, close=cl, volume=1.0)


# ---------------------------------------------------------------------------
# _exit_reason — pure arithmetic, exact
# ---------------------------------------------------------------------------

def test_exit_reason_stop_takes_precedence_over_target():
    stop_px = 100.0 * (1 - STOP_PCT)
    assert _exit_reason(low_i=stop_px - 0.01, close_i=105.0, entry_px=100.0,
                        bars_held=3, middle=100.0) == "stop"


def test_exit_reason_stop_boundary_is_inclusive():
    assert _exit_reason(low_i=100.0 * (1 - STOP_PCT), close_i=97.0, entry_px=100.0,
                        bars_held=1, middle=None) == "stop"


def test_exit_reason_target_when_close_reaches_middle_band():
    assert _exit_reason(low_i=99.0, close_i=100.5, entry_px=100.0,
                        bars_held=2, middle=100.0) == "target"


def test_exit_reason_time_stop_at_the_bar_limit_only():
    assert _exit_reason(low_i=98.0, close_i=98.5, entry_px=100.0,
                        bars_held=TIME_STOP_BARS, middle=100.0) == "time"
    assert _exit_reason(low_i=98.0, close_i=98.5, entry_px=100.0,
                        bars_held=TIME_STOP_BARS - 1, middle=100.0) is None


def test_exit_reason_none_when_middle_unknown_and_no_stop():
    assert _exit_reason(low_i=98.0, close_i=99.9, entry_px=100.0,
                        bars_held=1, middle=None) is None


# ---------------------------------------------------------------------------
# _entry_check — the three gates
# ---------------------------------------------------------------------------

def _ranging_dip() -> tuple[list, list, list]:
    """Tight chop (ADX well below 20) then a 6-bar decline into an oversold
    close below the lower band. Verified: rsi < 35, adx < 20, close < band."""
    c = [100.0 + (0.5 if k % 2 == 0 else -0.5) for k in range(54)]
    c += [98.0, 96.0, 94.0, 92.0, 90.0, 86.0]
    h = [x + 0.6 for x in c]
    l = [x - 0.6 for x in c]
    return c, h, l


def test_entry_check_passes_on_oversold_dip_in_a_range():
    ok, adx_val = _entry_check(*_ranging_dip())
    assert ok is True
    assert adx_val is not None and adx_val < ADX_RANGING_MAX


def test_entry_check_rejects_when_close_stays_above_lower_band():
    c, h, l = _ranging_dip()
    c[-1] = 99.0                                   # mild pullback, inside the band
    h[-1], l[-1] = 99.6, 98.4
    assert _entry_check(c, h, l)[0] is False


def test_entry_check_rejects_when_not_oversold():
    # rising into a shallow last-bar dip: close pierces the band but RSI ~46
    c = [80.0 + 0.7 * k for k in range(30)] + \
        [100.0 + (0.4 if k % 2 else -0.4) for k in range(15)] + [96.0]
    h = [x + 0.4 for x in c]
    l = [x - 0.4 for x in c]
    assert _entry_check(c, h, l)[0] is False       # RSI gate blocks it


def test_entry_check_rejects_when_adx_says_trending():
    # a rally (ADX -> ~80) that reverses into an oversold decline: close below
    # the band AND RSI < 35, but ADX is still far above the ranging cap.
    c = [60.0 + k for k in range(40)] + [99.0, 96.0, 92.0, 87.0, 81.0, 74.0]
    h = [x + 0.5 for x in c]
    l = [x - 0.5 for x in c]
    ok, adx_val = _entry_check(c, h, l)
    assert ok is False
    assert adx_val is not None and adx_val >= ADX_RANGING_MAX


def test_entry_check_none_on_short_history():
    c = [100.0] * 10
    assert _entry_check(c, c, c) == (False, None)


# ---------------------------------------------------------------------------
# _pf_stats / _fmt_pf
# ---------------------------------------------------------------------------

def test_pf_stats_empty():
    assert _pf_stats([], 1000.0) == {
        "trades": 0, "wins": 0, "win_rate": 0.0, "pf": 0.0, "ret_pct": 0.0
    }


def test_pf_stats_mixed_inf_and_fmt():
    s = _pf_stats([10.0, -5.0, 20.0, -5.0], 1000.0)
    assert (s["trades"], s["wins"], s["win_rate"]) == (4, 2, 50.0)
    assert s["pf"] == pytest.approx(3.0)
    assert s["ret_pct"] == pytest.approx(2.0)
    assert _pf_stats([1.0, 2.0], 1000.0)["pf"] == float("inf")
    assert _fmt_pf(float("inf")) == "inf"
    assert _fmt_pf(1.239) == "1.24"


# ---------------------------------------------------------------------------
# run_mean_reversion_backtest — integration
# (base(): chop + decline [98,95,92,90]; verified entry is idx 52 @ close 92,
#  position open at bar 53 — continuations are appended from bar 54.)
# ---------------------------------------------------------------------------

def _base() -> list[Candle]:
    b = [_c(p, p + 0.6, p - 0.6, p, i)
         for i, p in ((k, 100.0 + (0.5 if k % 2 == 0 else -0.5)) for k in range(50))]
    for i, p in enumerate([98.0, 95.0, 92.0, 90.0], start=50):
        b.append(_c(p + 1, p + 1.1, p - 0.5, p, i))
    return b

_ENTRY_IDX = 52
_ENTRY_PX = 92.0


def test_backtest_guards_empty_and_short():
    assert run_mean_reversion_backtest([]).trades == []
    assert run_mean_reversion_backtest(
        [_c(100, 100.5, 99.5, 100, i) for i in range(WARMUP)]
    ).trades == []


def test_backtest_profitable_target_round_trip():
    bars = _base()
    for i, p in enumerate([93.0, 97.0, 102.0, 105.0], start=54):
        bars.append(_c(p - 1, p + 0.5, p - 1.2, p, i))
    res = run_mean_reversion_backtest(bars, fee_pct=0.0)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert (t.entry_idx, t.entry_px) == (_ENTRY_IDX, pytest.approx(_ENTRY_PX))
    assert t.reason == "target"
    assert t.pnl > 0
    assert t.entry_adx < ADX_RANGING_MAX           # ranging-regime gate held


def test_backtest_stop_exit_at_the_stop_level_is_a_loss():
    bars = _base()
    bars.append(_c(90, 90, 84.0, 85.0, 54))        # craters through the stop
    for i, p in enumerate([101, 103, 105], start=55):
        bars.append(_c(p, p + 0.5, p - 0.5, p, i))
    res = run_mean_reversion_backtest(bars, fee_pct=0.0)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.reason == "stop"
    assert t.exit_px == pytest.approx(_ENTRY_PX * (1 - STOP_PCT))
    assert t.pnl == pytest.approx(1000.0 * (1 - STOP_PCT) - 1000.0)


def test_backtest_time_stop_fires_exactly_at_the_bar_limit():
    bars = _base()
    hold = _ENTRY_PX * (1 - STOP_PCT) + 1.0        # above the stop, below the ~99 mean
    for k in range(1, TIME_STOP_BARS + 4):
        bars.append(_c(hold, hold + 0.3, hold - 0.3, hold, 53 + k))
    res = run_mean_reversion_backtest(bars, fee_pct=0.0)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.reason == "time"
    assert t.exit_idx - t.entry_idx == TIME_STOP_BARS


def test_backtest_open_position_marked_to_market_at_end():
    bars = _base()
    for k in range(1, 3):
        bars.append(_c(89, 89.3, 88.7, 89, 53 + k))
    res = run_mean_reversion_backtest(bars, fee_pct=0.0)
    assert len(res.trades) == 1
    assert res.trades[0].reason == "eod"
    assert res.trades[0].exit_idx == len(bars) - 1


def test_backtest_fee_costs_both_legs():
    bars = _base()
    for i, p in enumerate([93.0, 97.0, 102.0, 105.0], start=54):
        bars.append(_c(p - 1, p + 0.5, p - 1.2, p, i))
    free = run_mean_reversion_backtest(bars, fee_pct=0.0).trades[0].pnl
    fee = run_mean_reversion_backtest(bars, fee_pct=0.008).trades[0].pnl
    assert fee < free


def test_backtest_cooldown_blocks_immediate_re_entry():
    bars = _base()
    bars.append(_c(90, 102, 89, 101, 54))          # target exit on bar 54
    bars.append(_c(101, 101, 70, 72, 55))          # re-qualifying plunge, 1 bar later
    bars.append(_c(72, 90, 71, 80, 56))
    res = run_mean_reversion_backtest(bars, fee_pct=0.0)
    assert len(res.trades) == 1                     # the bar-55 re-entry was suppressed
    assert res.trades[0].exit_idx == 54
    assert COOLDOWN_BARS >= 1


# ---------------------------------------------------------------------------
# _verdict
# ---------------------------------------------------------------------------

def test_verdict_pass_fail_marginal_nosample():
    ok = [{"window": w, "trades": 15, "pf": 1.5} for w in (5000, 3000, 1000)]
    assert _verdict(ok).startswith("PASS")

    bad = [
        {"window": 5000, "trades": 15, "pf": 1.5},
        {"window": 3000, "trades": 15, "pf": 0.9},
        {"window": 1000, "trades": 15, "pf": 1.4},
    ]
    assert _verdict(bad).startswith("FAILED") and "3000c" in _verdict(bad)

    marginal = [
        {"window": 5000, "trades": 15, "pf": 1.5},
        {"window": 3000, "trades": 15, "pf": 1.3},
        {"window": 1000, "trades": 6, "pf": 5.0},
    ]
    assert _verdict(marginal).startswith("MARGINAL")

    assert _verdict([{"window": w, "trades": 3, "pf": 9.9}
                     for w in (5000, 3000, 1000)]).startswith("FAILED")
