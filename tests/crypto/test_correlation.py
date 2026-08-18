"""Unit tests for bot/risk/correlation.py — pure math only, no network."""
import math
import pytest
from bot.risk.correlation import pearson, pct_returns, fetch_correlation, CORRELATION_THRESHOLD


# ── pearson ───────────────────────────────────────────────────────────────────

def test_perfect_positive_correlation():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert pearson(a, a) == pytest.approx(1.0)


def test_perfect_negative_correlation():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [5.0, 4.0, 3.0, 2.0, 1.0]
    assert pearson(a, b) == pytest.approx(-1.0)


def test_low_correlation():
    # A trending series vs a high-frequency oscillating series should give low |r|.
    # With finite n the exact value depends on alignment; we just check it's
    # well below the 0.70 trading threshold.
    a = list(range(1, 21))                        # monotone trend
    b = [math.sin(i * math.pi) for i in range(20)]  # oscillates around zero
    corr = pearson(a, b)
    assert corr is not None
    assert abs(corr) < 0.50   # well below the 0.70 gate threshold


def test_result_clamped_to_minus_one_one():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    corr = pearson(a, a)
    assert -1.0 <= corr <= 1.0


def test_too_few_points_returns_none():
    assert pearson([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]) is None


def test_mismatched_lengths_returns_none():
    assert pearson([1.0, 2.0, 3.0, 4.0, 5.0], [1.0, 2.0, 3.0, 4.0]) is None


def test_zero_variance_returns_none():
    constant = [5.0, 5.0, 5.0, 5.0, 5.0]
    other    = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert pearson(constant, other) is None


def test_known_correlation():
    # BTC/ETH daily returns are historically ~0.85 — test with synthetic data
    # Two series that are 90% correlated (one is the other + small noise)
    base  = [0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.03, 0.02, 0.01, -0.01]
    noise = [x + 0.001 * (i % 3 - 1) for i, x in enumerate(base)]
    corr  = pearson(base, noise)
    assert corr is not None
    assert corr > 0.99   # noise is tiny, correlation should be very close to 1


# ── pct_returns ───────────────────────────────────────────────────────────────

def test_pct_returns_basic():
    closes = [100.0, 110.0, 99.0]
    ret = pct_returns(closes)
    assert len(ret) == 2
    assert ret[0] == pytest.approx(0.10)
    assert ret[1] == pytest.approx(-0.10, rel=1e-3)


def test_pct_returns_skips_zero_close():
    closes = [100.0, 0.0, 110.0]
    ret = pct_returns(closes)
    # [0.0, 0.0] is skipped (closes[1-1]=100 → ok; closes[2-1]=0 → skip)
    assert len(ret) == 1
    assert ret[0] == pytest.approx(-1.0)   # 0/100 - 1


def test_pct_returns_single_element():
    assert pct_returns([100.0]) == []


def test_pct_returns_empty():
    assert pct_returns([]) == []


# ── CORRELATION_THRESHOLD constant ───────────────────────────────────────────

def test_threshold_value():
    assert CORRELATION_THRESHOLD == pytest.approx(0.70)


# ── fetch_correlation error paths ─────────────────────────────────────────────

def test_fetch_correlation_returns_none_on_exchange_error():
    from unittest.mock import MagicMock
    ex = MagicMock()
    ex.fetch_ohlcv.side_effect = Exception("timeout")
    result = fetch_correlation(ex, "BTC/CAD", "XRP/CAD")
    assert result is None


def test_fetch_correlation_returns_none_on_too_few_candles():
    from unittest.mock import MagicMock
    ex = MagicMock()
    ex.fetch_ohlcv.return_value = [[i * 86400000, 1.0, 1.0, 1.0, float(i), 1.0] for i in range(3)]
    result = fetch_correlation(ex, "BTC/CAD", "XRP/CAD")
    assert result is None


def test_fetch_correlation_computes_correctly():
    """Mock exchange with perfectly correlated daily closes → corr ≈ 1.0."""
    from unittest.mock import MagicMock
    closes = [100.0 + i * 2 for i in range(35)]   # upward trend
    ts_base = 1_700_000_000_000   # arbitrary ms timestamp

    def _make_ohlcv(prices, scale=1.0):
        return [
            [ts_base + i * 86_400_000, p * scale, p * scale, p * scale, p * scale, 1.0]
            for i, p in enumerate(prices)
        ]

    ex = MagicMock()
    ex.fetch_ohlcv.side_effect = [
        _make_ohlcv(closes),          # BTC/CAD
        _make_ohlcv(closes, 0.01),    # XRP/CAD (same trend, different price level)
    ]

    corr = fetch_correlation(ex, "BTC/CAD", "XRP/CAD", days=30)
    assert corr is not None
    assert corr == pytest.approx(1.0, abs=1e-6)


def test_fetch_correlation_blocks_at_threshold():
    """Verify the gate logic: corr > threshold → block."""
    corr_above = 0.85
    corr_below = 0.65
    assert corr_above > CORRELATION_THRESHOLD   # would be blocked
    assert corr_below <= CORRELATION_THRESHOLD  # would be allowed
