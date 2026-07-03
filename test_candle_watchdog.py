"""
Unit tests for the candle watchdog helper (_check_candle_watchdog).

The watchdog fires alerter.error() when no new candle has arrived for
2 × candle_minutes. It resets the timer after firing to avoid spam.
"""
from unittest.mock import MagicMock

from bot.main import _check_candle_watchdog


def _alerter():
    return MagicMock()


# ── Happy path: fresh candle ──────────────────────────────────────────────

def test_watchdog_silent_when_fresh():
    a = _alerter()
    last = 1_000.0
    minutes = 60
    now = last + 59 * 60  # 59 min — under the 120-min threshold
    result = _check_candle_watchdog(last, minutes, now, a)
    a.error.assert_not_called()
    assert result == last  # timer unchanged


def test_watchdog_silent_exactly_at_boundary():
    a = _alerter()
    last = 1_000.0
    minutes = 60
    now = last + 120 * 60  # exactly 120 min — not yet stale (need > threshold)
    result = _check_candle_watchdog(last, minutes, now, a)
    a.error.assert_not_called()
    assert result == last


# ── Stale feed: alert fires ───────────────────────────────────────────────

def test_watchdog_fires_past_threshold():
    a = _alerter()
    last = 1_000.0
    minutes = 60
    now = last + 120 * 60 + 1  # 1 second past threshold
    result = _check_candle_watchdog(last, minutes, now, a)
    a.error.assert_called_once()
    msg = a.error.call_args[0][0]
    assert "60min" in msg
    assert "Candle watchdog" in msg
    assert result == now  # timer reset to prevent spam


def test_watchdog_fires_with_4h_candles():
    a = _alerter()
    last = 5_000.0
    minutes = 240
    now = last + 240 * 60 * 2 + 30  # 30s past 8h threshold
    result = _check_candle_watchdog(last, minutes, now, a)
    a.error.assert_called_once()
    assert "240min" in a.error.call_args[0][0]
    assert result == now


def test_watchdog_does_not_fire_twice_after_reset():
    """After firing and resetting the timer, a second call with same 'now' stays silent."""
    a = _alerter()
    last = 1_000.0
    minutes = 60
    stale_now = last + 120 * 60 + 1

    # First call — fires and resets
    new_last = _check_candle_watchdog(last, minutes, stale_now, a)
    assert a.error.call_count == 1

    # Second call with same now — timer was reset, not stale yet
    new_last2 = _check_candle_watchdog(new_last, minutes, stale_now, a)
    assert a.error.call_count == 1  # no new alert
    assert new_last2 == new_last


if __name__ == "__main__":
    import sys
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed+failed} passed")
    sys.exit(failed)
