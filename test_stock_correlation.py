"""
Unit tests for stock_bot/risk/correlation.py — the stock bot's correlation
gate. pearson()/pct_returns()/CORRELATION_THRESHOLD themselves are already
covered by test_correlation.py (bot/risk/correlation.py, reused unchanged
here) — these tests only cover fetch_correlation_from_closes(), the
stock-specific no-network wrapper.
"""
import pytest

from stock_bot.risk.correlation import CORRELATION_THRESHOLD, fetch_correlation_from_closes


def test_perfectly_correlated_closes_returns_near_one():
    closes_a = [100.0 + i * 2 for i in range(35)]          # upward trend
    closes_b = [10.0 + i * 0.2 for i in range(35)]          # same trend, different scale
    corr = fetch_correlation_from_closes(closes_a, closes_b)
    assert corr is not None
    assert corr == pytest.approx(1.0, abs=1e-6)
    assert corr > CORRELATION_THRESHOLD


def test_uncorrelated_closes_below_threshold():
    import math
    closes_a = [100.0 + i for i in range(35)]                       # monotone trend
    closes_b = [50.0 + 5 * math.sin(i * math.pi / 3) for i in range(35)]  # oscillating
    corr = fetch_correlation_from_closes(closes_a, closes_b)
    assert corr is not None
    assert corr < CORRELATION_THRESHOLD


def test_empty_closes_returns_none():
    assert fetch_correlation_from_closes([], [1.0, 2.0, 3.0]) is None
    assert fetch_correlation_from_closes([1.0, 2.0, 3.0], []) is None


def test_too_few_points_returns_none():
    closes_a = [100.0, 101.0, 102.0]   # only 2 returns — below the n>=5 floor
    closes_b = [50.0, 51.0, 49.0]
    assert fetch_correlation_from_closes(closes_a, closes_b) is None


def test_no_network_call_required():
    # Purely a documentation-style assertion: the function signature takes
    # plain float lists, not a data source — confirms it can't reach out.
    import inspect
    sig = inspect.signature(fetch_correlation_from_closes)
    assert list(sig.parameters) == ["closes_a", "closes_b", "days"]
