"""
Unit tests for stock_momentum_experiment.py.

Hermetic — synthetic price matrices only, no network. Pure helpers are
tested with exact numbers; a few integration tests drive `run_momentum`
and the benchmark builders over engineered price paths.

Run: python -m pytest tests/stock/test_stock_momentum_experiment.py -v
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from stock_momentum_experiment import (
    LOOKBACK_DAYS,
    REBAL_DAYS,
    SKIP_DAYS,
    TOP_N,
    WARMUP,
    _buy_and_hold,
    _cagr,
    _commission,
    _equal_weight_hold,
    _max_drawdown,
    _momentum_score,
    _select_top,
    _sharpe,
    _verdict,
    run_momentum,
)


# ---------------------------------------------------------------------------
# _momentum_score
# ---------------------------------------------------------------------------

def test_momentum_score_none_without_enough_history():
    assert _momentum_score([100.0] * (LOOKBACK_DAYS + SKIP_DAYS)) is None


def test_momentum_score_measures_the_lookback_window_ending_skip_days_ago():
    # flat for a long time, then a ramp only in the final SKIP_DAYS -> the
    # skip-month means that ramp is NOT counted; score ~ 0.
    n = LOOKBACK_DAYS + SKIP_DAYS + 10
    closes = [100.0] * (n - SKIP_DAYS) + [100.0 + 3 * k for k in range(SKIP_DAYS)]
    assert _momentum_score(closes) == pytest.approx(0.0, abs=1e-9)


def test_momentum_score_positive_when_the_measured_window_rose():
    n = LOOKBACK_DAYS + SKIP_DAYS + 10
    # rises 50% over the whole series; the window ending SKIP_DAYS ago still
    # captures most of that gain
    closes = [100.0 * (1 + 0.5 * i / (n - 1)) for i in range(n)]
    sc = _momentum_score(closes)
    assert sc is not None and sc > 0.2


# ---------------------------------------------------------------------------
# _select_top
# ---------------------------------------------------------------------------

def test_select_top_picks_highest_scores_ties_broken_by_name():
    scores = {"C": 0.3, "A": 0.3, "B": 0.9, "D": -0.1}
    assert _select_top(scores, 2) == ["B", "A"]     # B highest, then A before C on the tie
    assert _select_top(scores, 10) == ["B", "A", "C", "D"]
    assert _select_top({}, 5) == []


# ---------------------------------------------------------------------------
# _sharpe / _max_drawdown / _cagr / _commission
# ---------------------------------------------------------------------------

def test_sharpe_zero_on_degenerate_input():
    assert _sharpe([]) == 0.0
    assert _sharpe([0.01]) == 0.0
    assert _sharpe([0.01, 0.01, 0.01]) == 0.0       # zero volatility


def test_sharpe_sign_and_scale():
    up = _sharpe([0.001] * 50 + [-0.0005] * 50)
    assert up > 0
    down = _sharpe([-0.001] * 50 + [0.0005] * 50)
    assert down < 0


def test_max_drawdown_peak_to_trough():
    assert _max_drawdown([100, 120, 90, 110]) == pytest.approx((120 - 90) / 120)
    assert _max_drawdown([100, 101, 102]) == 0.0
    assert _max_drawdown([]) == 0.0


def test_cagr_roundtrips_a_known_double():
    # doubles over exactly one year of trading days
    eq = [100.0, 200.0]
    assert _cagr(eq, days=252) == pytest.approx(1.0, abs=1e-6)


def test_commission_is_per_share_with_a_floor():
    assert _commission(10) == pytest.approx(1.0)         # 10*0.005=0.05 -> floor $1
    assert _commission(1000) == pytest.approx(5.0)       # 1000*0.005


# ---------------------------------------------------------------------------
# _verdict
# ---------------------------------------------------------------------------

def test_verdict_pass_and_each_fail_reason():
    spy = {"sharpe": 0.8, "mdd": 0.20}
    ew  = {"sharpe": 0.9, "mdd": 0.25}

    assert _verdict({"sharpe": 1.2, "mdd": 0.18}, spy, ew).startswith("PASS")
    assert "SPY" in _verdict({"sharpe": 0.7, "mdd": 0.18}, spy, ew)          # loses to SPY
    assert "equal-weight" in _verdict({"sharpe": 0.85, "mdd": 0.18}, spy, ew)  # beats SPY, not EW
    assert "maxDD" in _verdict({"sharpe": 1.2, "mdd": 0.30}, spy, ew)        # DD too deep


# ---------------------------------------------------------------------------
# run_momentum + benchmarks — integration on synthetic matrices
# ---------------------------------------------------------------------------

_D0 = date(2020, 1, 1)


def _dates(n: int) -> list:
    return [_D0 + timedelta(days=i) for i in range(n)]


def _ramp(n: int, start: float, cagr_like: float) -> list[float]:
    """A smooth compounding path."""
    return [start * (1.0 + cagr_like) ** (i / 252) for i in range(n)]


def test_run_momentum_rotates_into_the_strongest_names():
    n = WARMUP + REBAL_DAYS * 6
    # 12 names: 3 strong uptrends, the rest flat-ish
    matrix = {}
    for k in range(12):
        matrix[f"S{k:02d}"] = _ramp(n, 100.0, 0.8 if k < 3 else 0.0)
    spy = _ramp(n, 300.0, 0.1)
    res = run_momentum(matrix, _dates(n), spy, use_regime=False,
                       start_i=WARMUP, end_i=n)
    assert res.rebalances >= 5
    assert res.equity[-1] > res.equity[0]          # captured the uptrends
    assert res.total_cost > 0                       # paid to rebalance


def test_run_momentum_regime_filter_goes_to_cash_below_the_spy_ma():
    n = WARMUP + REBAL_DAYS * 5
    matrix = {f"S{k:02d}": _ramp(n, 100.0, 0.3) for k in range(12)}
    # SPY well below any trailing 200d average for the whole window
    spy = [500.0 - 0.5 * i for i in range(n)]
    res = run_momentum(matrix, _dates(n), spy, use_regime=True,
                       start_i=WARMUP, end_i=n)
    # never invested -> equity is flat at the starting capital (no positions,
    # no cost beyond nothing traded)
    assert res.equity[-1] == pytest.approx(res.equity[0], rel=1e-6)
    assert res.total_cost == pytest.approx(0.0)


def test_buy_and_hold_tracks_the_single_series():
    closes = _ramp(300, 100.0, 0.5)
    eq = _buy_and_hold(closes, start_i=10, end_i=300)
    assert len(eq) == 290
    assert eq[-1] > eq[0]
    # roughly the series' own return over the window (minus slippage/commission)
    series_ret = closes[299] / closes[10] - 1
    eq_ret = eq[-1] / eq[0] - 1
    assert eq_ret == pytest.approx(series_ret, rel=0.02)


def test_equal_weight_hold_spreads_capital_across_all_names():
    n = 200
    matrix = {f"S{k}": _ramp(n, 50.0 + 10 * k, 0.2) for k in range(8)}
    eq = _equal_weight_hold(matrix, start_i=5, end_i=n)
    assert len(eq) == n - 5
    assert eq[-1] > eq[0]
