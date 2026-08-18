"""
Tests for TSX-specific price corruption detection in stock_bot/data/price_feed.py.

Strategy: mock yf.download to return a known close price, mock
yf.Ticker(...).fast_info to return controlled previous_close / last_price
values, then assert fetch_candles() accept/rejects and the counter behaves.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from stock_bot.data import price_feed
from stock_bot.data.price_feed import fetch_candles, get_tsx_warnings, reset_price_cache


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_df(close: float) -> pd.DataFrame:
    """Minimal single-row DataFrame that mimics yf.download output."""
    idx = pd.DatetimeIndex(["2026-06-28"])
    return pd.DataFrame(
        {
            "Open":   [close * 0.99],
            "High":   [close * 1.01],
            "Low":    [close * 0.98],
            "Close":  [close],
            "Volume": [500_000.0],
        },
        index=idx,
    )


def _mock_fast_info(prev_close: float, last_price: float | None = None) -> MagicMock:
    fi = MagicMock()
    fi.previous_close = prev_close
    fi.previousClose  = prev_close
    fi.last_price     = last_price
    fi.lastPrice      = last_price
    return fi


# ── fixture ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_state():
    """Isolate module-level counters and duplicate-price cache between tests."""
    reset_price_cache()
    price_feed._tsx_corruption_warnings = 0
    yield
    reset_price_cache()
    price_feed._tsx_corruption_warnings = 0


# ── tests ─────────────────────────────────────────────────────────────────────

def test_tsx_mismatch_rejected(caplog):
    """Candle close $20, last_price $30 (50% gap) → None returned, WARNING logged, counter +1."""
    close = 20.0
    last  = 30.0  # 50% deviation — well above the 5% threshold

    with patch("yfinance.download", return_value=_make_df(close)), \
         patch("yfinance.Ticker") as mock_ticker, \
         patch("time.sleep"):
        mock_ticker.return_value.fast_info = _mock_fast_info(
            prev_close = close * 0.99,   # passes the existing 20% previous_close check
            last_price = last,
        )
        with caplog.at_level(logging.WARNING, logger="stock_bot.data.price_feed"):
            result = fetch_candles("SHOP.TO", lookback_days=5)

    assert result is None, "Expected rejection on >5% TSX mismatch"
    assert any("TSX price mismatch" in r.message for r in caplog.records), \
        "Expected WARNING with 'TSX price mismatch'"
    assert get_tsx_warnings() == 1, "Counter should be 1 after one rejection"


def test_tsx_within_threshold_passes(caplog):
    """Candle close $20, last_price $20.50 (2.5% gap) → candles returned, no warning."""
    close = 20.0
    last  = 20.50  # 2.5% — under the 5% threshold

    with patch("yfinance.download", return_value=_make_df(close)), \
         patch("yfinance.Ticker") as mock_ticker, \
         patch("time.sleep"):
        mock_ticker.return_value.fast_info = _mock_fast_info(
            prev_close = close * 0.99,
            last_price = last,
        )
        with caplog.at_level(logging.WARNING, logger="stock_bot.data.price_feed"):
            result = fetch_candles("RY.TO", lookback_days=5)

    assert result is not None, "Expected candles to be returned when deviation is within 5%"
    assert get_tsx_warnings() == 0
    assert not any("TSX price mismatch" in r.message for r in caplog.records)


def test_non_tsx_skips_last_price_check(caplog):
    """US symbol AAPL: even a 54% last_price deviation must not trigger the TSX check."""
    close = 195.0
    last  = 300.0  # 54% deviation — would fail the TSX check if applied

    with patch("yfinance.download", return_value=_make_df(close)), \
         patch("yfinance.Ticker") as mock_ticker, \
         patch("time.sleep"):
        mock_ticker.return_value.fast_info = _mock_fast_info(
            prev_close = close * 0.99,   # passes existing 20% check
            last_price = last,
        )
        with caplog.at_level(logging.WARNING, logger="stock_bot.data.price_feed"):
            result = fetch_candles("AAPL", lookback_days=5)

    assert result is not None, "Non-.TO symbols must not be rejected by the TSX check"
    assert get_tsx_warnings() == 0
    assert not any("TSX price mismatch" in r.message for r in caplog.records)


def test_counter_accumulates_across_rejections(caplog):
    """Counter increments independently for each rejected .TO symbol in one run."""
    close = 20.0
    last  = 50.0  # 150% gap

    with patch("yfinance.download", return_value=_make_df(close)), \
         patch("yfinance.Ticker") as mock_ticker, \
         patch("time.sleep"):
        mock_ticker.return_value.fast_info = _mock_fast_info(
            prev_close = close * 0.99,
            last_price = last,
        )
        with caplog.at_level(logging.WARNING, logger="stock_bot.data.price_feed"):
            fetch_candles("AC.TO", lookback_days=5)
            reset_price_cache()          # avoid duplicate-price rejection on the second call
            fetch_candles("SHOP.TO", lookback_days=5)

    assert get_tsx_warnings() == 2, "Counter should accumulate across separate calls"
    warnings = [r.message for r in caplog.records if "TSX price mismatch" in r.message]
    assert len(warnings) == 2


def test_existing_20pct_check_still_fires(caplog):
    """The original previous_close / 20% check must still reject when it should."""
    close = 20.0
    # prev_close is 30% above close — triggers the pre-existing check, not the TSX one
    prev  = close * 1.30

    with patch("yfinance.download", return_value=_make_df(close)), \
         patch("yfinance.Ticker") as mock_ticker, \
         patch("time.sleep"):
        mock_ticker.return_value.fast_info = _mock_fast_info(
            prev_close = prev,
            last_price = close,   # last_price matches — TSX check would pass
        )
        with caplog.at_level(logging.WARNING, logger="stock_bot.data.price_feed"):
            result = fetch_candles("SHOP.TO", lookback_days=5)

    assert result is None, "Existing 20% previous_close check must still reject"
    # This rejection comes from the old check, not the new TSX one
    assert get_tsx_warnings() == 0, "Counter must NOT increment for the old check"
