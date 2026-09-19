"""
Unit tests for the live-tick price-feed staleness gate (_update_price_feed_staleness).

2026-09-18 review finding (P1-7): a run of ticker-fetch failures reused
ss['last_price'] (a stale value) and let the tick loop continue exactly as
if a fresh price had been read — including evaluating a brand-new BUY
signal against data that may no longer reflect the market. This mirrors
_check_candle_watchdog's edge-trigger pattern for that independent signal.
"""
from unittest.mock import MagicMock

from bot.main import _update_price_feed_staleness


def _alerter():
    return MagicMock()


def _ss(err_count: int = 0, stale: bool = False) -> dict:
    return {"err_count": err_count, "price_feed_stale": stale}


def test_successful_fetch_stays_unblocked():
    a = _alerter()
    ss = _ss(err_count=0)
    result = _update_price_feed_staleness(ss, True, "BTC/CAD", a)
    assert result is False
    assert ss["price_feed_stale"] is False
    a.error.assert_not_called()
    a.message.assert_not_called()


def test_failures_below_threshold_do_not_block():
    a = _alerter()
    ss = _ss(err_count=4)
    result = _update_price_feed_staleness(ss, False, "BTC/CAD", a, threshold=5)
    assert result is False
    assert ss["price_feed_stale"] is False
    a.error.assert_not_called()


def test_failures_at_threshold_blocks_and_alerts_once():
    a = _alerter()
    ss = _ss(err_count=5)
    result = _update_price_feed_staleness(ss, False, "BTC/CAD", a, threshold=5)
    assert result is True
    assert ss["price_feed_stale"] is True
    a.error.assert_called_once()
    msg = a.error.call_args[0][0]
    assert "BTC/CAD" in msg
    assert "stale" in msg.lower()


def test_does_not_re_alert_while_continuously_failing():
    a = _alerter()
    ss = _ss(err_count=5)
    _update_price_feed_staleness(ss, False, "BTC/CAD", a, threshold=5)
    assert a.error.call_count == 1

    ss["err_count"] = 6
    _update_price_feed_staleness(ss, False, "BTC/CAD", a, threshold=5)
    assert a.error.call_count == 1   # edge-triggered, not every tick


def test_recovery_clears_flag_and_alerts():
    a = _alerter()
    ss = _ss(err_count=5)
    _update_price_feed_staleness(ss, False, "BTC/CAD", a, threshold=5)
    assert ss["price_feed_stale"] is True

    ss["err_count"] = 0
    result = _update_price_feed_staleness(ss, True, "BTC/CAD", a, threshold=5)

    assert result is False
    assert ss["price_feed_stale"] is False
    a.message.assert_called_once()
    assert "recovered" in a.message.call_args[0][0].lower() or "resumed" in a.message.call_args[0][0].lower()


def test_recovery_does_not_re_fire():
    a = _alerter()
    ss = _ss(err_count=5)
    _update_price_feed_staleness(ss, False, "BTC/CAD", a, threshold=5)
    ss["err_count"] = 0
    _update_price_feed_staleness(ss, True, "BTC/CAD", a, threshold=5)
    assert a.message.call_count == 1

    _update_price_feed_staleness(ss, True, "BTC/CAD", a, threshold=5)
    assert a.message.call_count == 1   # no second recovery alert
