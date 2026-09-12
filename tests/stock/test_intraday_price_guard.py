"""
Tests for the get_live_price() previous-close corruption guard
(stock_bot/data/intraday_price.py).

2026-09 finding: the guard rejected ANY price deviating >20% from previous
close, including a genuine sharp fall — which backs the SL/TP watcher, so a
real crash silently disabled stop-loss protection exactly when it mattered.
Fixed: a deviant price that falls within today's own day_high/day_low (a
separately-fetched field inside fast_info, not the same live-quote value as
last_price) is treated as a genuine move, not corruption.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import stock_bot.data.intraday_price as ip_mod
from stock_bot.data.intraday_price import get_live_price


def _fake_ticker(last_price, previous_close, day_high=None, day_low=None):
    fi = SimpleNamespace(
        last_price=last_price,
        previous_close=previous_close,
        day_high=day_high,
        day_low=day_low,
    )
    return SimpleNamespace(fast_info=fi)


def test_normal_price_within_20pct_passes():
    with patch.object(ip_mod.yf, "Ticker", return_value=_fake_ticker(101.0, 100.0)):
        assert get_live_price("KO") == 101.0


def test_deviant_price_with_no_day_range_data_still_rejected():
    """No day_high/day_low available — fall back to the old conservative
    behavior (reject), since there's nothing to corroborate against."""
    with patch.object(ip_mod.yf, "Ticker", return_value=_fake_ticker(70.0, 100.0)):
        assert get_live_price("KO") is None


def test_genuine_crash_confirmed_by_day_range_is_not_discarded():
    """A real 30% crash: last_price=70, previous_close=100, but today's own
    day_low is 69 — the price-history feed moved too, corroborating a real
    move. Must be returned, not silently discarded (the actual finding)."""
    with patch.object(
        ip_mod.yf, "Ticker",
        return_value=_fake_ticker(70.0, 100.0, day_high=99.5, day_low=69.0),
    ):
        assert get_live_price("KO") == 70.0


def test_corrupted_price_outside_day_range_still_rejected():
    """A deviant price that ALSO falls outside today's own trading range —
    the classic signature of a bad read (stale cache, currency mixup) — must
    still be rejected even with day_high/day_low present."""
    with patch.object(
        ip_mod.yf, "Ticker",
        return_value=_fake_ticker(70.0, 100.0, day_high=101.0, day_low=98.0),
    ):
        assert get_live_price("KO") is None


def test_day_range_lookup_failure_falls_back_to_conservative_reject():
    """If reading day_high/day_low itself raises, the guard must fail toward
    the old safe default (reject) rather than let the exception escape or
    silently accept an uncorroborated deviant price."""
    class _BoomFastInfo:
        last_price = 70.0
        previous_close = 100.0

        @property
        def day_high(self):
            raise RuntimeError("network blip")

        @property
        def day_low(self):
            raise RuntimeError("network blip")

    with patch.object(
        ip_mod.yf, "Ticker",
        return_value=SimpleNamespace(fast_info=_BoomFastInfo()),
    ):
        assert get_live_price("KO") is None
