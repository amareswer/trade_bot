"""
Unit tests for grid_stress_test.py's pure helper functions: crash-period
date parsing, the buy-and-hold P&L calc, and PASS/MARGINAL/FAILED
classification.

Hermetic — no network, no real crash-period data fetched. The actual stress
run against Binance is a separate, manual step (`python grid_stress_test.py`).

Run: python -m pytest tests/crypto/test_grid_stress_test.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.data.historical_feed import Candle
from grid_stress_test import (
    CRASH_PERIODS,
    _MARGINAL_LOSS_PCT,
    _WIDE_CFG,
    _period_to_ms,
    buy_and_hold_pnl,
    classify_verdict,
)
from grid_dca_experiment import GRID_CONFIGS

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _candles(closes: list[float]) -> list[Candle]:
    return [
        Candle(timestamp=_T0 + timedelta(hours=i), open=c, high=c, low=c, close=c, volume=1.0)
        for i, c in enumerate(closes)
    ]


# ---------------------------------------------------------------------------
# Config integrity — the stress test must use the config that actually
# passed, not a hand-retyped copy that could silently drift from it.
# ---------------------------------------------------------------------------

def test_wide_config_pulled_from_grid_dca_experiment_unchanged():
    assert _WIDE_CFG is next(c for c in GRID_CONFIGS if c.name == "wide_35pct")
    assert _WIDE_CFG.range_pct == 0.35
    assert _WIDE_CFG.grid_levels == 14
    assert _WIDE_CFG.floor_buffer_pct == 0.05


def test_crash_periods_are_two_distinct_non_overlapping_windows():
    assert len(CRASH_PERIODS) == 2
    labels = [p[0] for p in CRASH_PERIODS]
    assert "2022 Crash" in labels
    assert "COVID Crash" in labels
    # non-overlapping: COVID ends before the 2022 crash period starts
    covid_end   = next(p[2] for p in CRASH_PERIODS if p[0] == "COVID Crash")
    crash_start = next(p[1] for p in CRASH_PERIODS if p[0] == "2022 Crash")
    assert covid_end < crash_start


# ---------------------------------------------------------------------------
# Crash-period date -> ms conversion (the "data loading" plumbing)
# ---------------------------------------------------------------------------

def test_period_to_ms_matches_known_utc_timestamps():
    since_ms, until_ms = _period_to_ms("2021-11-01", "2022-12-31")
    since_dt = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc)
    until_dt = datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc)
    assert since_dt == datetime(2021, 11, 1, tzinfo=timezone.utc)
    assert until_dt == datetime(2022, 12, 31, tzinfo=timezone.utc)


def test_period_to_ms_since_before_until():
    since_ms, until_ms = _period_to_ms("2020-01-01", "2020-12-31")
    assert since_ms < until_ms


def test_period_to_ms_for_both_configured_crash_periods():
    """Every period actually configured for the stress run must parse
    without error and produce since < until."""
    for _label, start, end in CRASH_PERIODS:
        since_ms, until_ms = _period_to_ms(start, end)
        assert since_ms < until_ms


# ---------------------------------------------------------------------------
# Buy-and-hold P&L — same two-leg fee convention as the grid engine
# ---------------------------------------------------------------------------

def test_buy_and_hold_pnl_zero_fee_matches_simple_return():
    candles = _candles([100.0, 150.0])   # +50% price move
    pnl = buy_and_hold_pnl(candles, capital=1000.0, fee_pct=0.0)
    # qty = 1000/100 = 10; proceeds = 10*150 = 1500; pnl = 1500 - 1000 = 500
    assert pnl == pytest.approx(500.0, rel=1e-9)


def test_buy_and_hold_pnl_applies_fee_on_both_legs():
    candles = _candles([100.0, 100.0])   # flat price — pure fee drag
    pnl = buy_and_hold_pnl(candles, capital=1000.0, fee_pct=0.01)
    # cost = 1000*1.01 = 1010; qty = 1000/100 = 10; proceeds = 10*100*0.99 = 990
    # pnl = 990 - 1010 = -20
    assert pnl == pytest.approx(-20.0, rel=1e-9)


def test_buy_and_hold_pnl_crash_scenario_is_a_large_loss():
    candles = _candles([69000.0, 15500.0])   # the actual 2022 crash magnitude
    pnl = buy_and_hold_pnl(candles, capital=1000.0, fee_pct=0.008)
    assert pnl < 0
    assert pnl == pytest.approx(-((69000 - 15500) / 69000) * 1000.0, abs=15.0)


def test_buy_and_hold_pnl_empty_candles_returns_zero():
    assert buy_and_hold_pnl([], capital=1000.0, fee_pct=0.008) == 0.0


# ---------------------------------------------------------------------------
# Verdict classification
# ---------------------------------------------------------------------------

def test_classify_verdict_pf_above_one_is_pass_regardless_of_pnl_sign():
    assert classify_verdict(pf=1.5, total_pnl=100.0, capital=1000.0).startswith("PASS")
    assert classify_verdict(pf=1.0, total_pnl=0.0, capital=1000.0).startswith("PASS")


def test_classify_verdict_small_loss_is_marginal():
    # 10% loss — below the 20% MARGINAL/FAILED boundary
    v = classify_verdict(pf=0.8, total_pnl=-100.0, capital=1000.0)
    assert v.startswith("MARGINAL")


def test_classify_verdict_severe_loss_is_failed():
    # 50% loss — well past the boundary
    v = classify_verdict(pf=0.3, total_pnl=-500.0, capital=1000.0)
    assert v.startswith("FAILED")
    assert "severe" in v


def test_classify_verdict_boundary_at_exactly_marginal_threshold_is_failed():
    """loss_pct < _MARGINAL_LOSS_PCT is MARGINAL; == is FAILED (strict <,
    documented in classify_verdict's own docstring) — pin the boundary."""
    loss = _MARGINAL_LOSS_PCT * 1000.0
    v = classify_verdict(pf=0.5, total_pnl=-loss, capital=1000.0)
    assert v.startswith("FAILED")


def test_classify_verdict_just_under_boundary_is_marginal():
    loss = _MARGINAL_LOSS_PCT * 1000.0 - 0.01
    v = classify_verdict(pf=0.5, total_pnl=-loss, capital=1000.0)
    assert v.startswith("MARGINAL")
