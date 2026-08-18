"""
Unit tests for the candle watchdog circuit breaker (_check_candle_watchdog).

Upgraded 2026-08-07: was alert-only before (fired once, reset its own timer
to avoid spam, never changed trading behavior). Now blocks new BUYs for as
long as the feed stays stale — ss['candle_feed_stale'] is the breaker's
persistent state (that flag is what prevents re-alerting while continuously
stale now, replacing the old timer-reset trick), and the function's return
value is what bot/main.py's BUY gate checks each tick.
"""
from unittest.mock import MagicMock

from bot.main import _check_candle_watchdog


def _alerter():
    return MagicMock()


def _ss(last_candle_time: float, stale: bool = False) -> dict:
    return {"last_candle_time": last_candle_time, "candle_feed_stale": stale}


# ── Happy path: fresh candle ──────────────────────────────────────────────

def test_watchdog_silent_and_unblocked_when_fresh():
    a = _alerter()
    ss = _ss(1_000.0)
    now = 1_000.0 + 59 * 60  # 59 min — under the 120-min threshold
    blocked = _check_candle_watchdog(ss, 60, now, a)
    a.error.assert_not_called()
    assert blocked is False
    assert ss["candle_feed_stale"] is False


def test_watchdog_silent_exactly_at_boundary():
    a = _alerter()
    ss = _ss(1_000.0)
    now = 1_000.0 + 120 * 60  # exactly 120 min — not yet stale (need > threshold)
    blocked = _check_candle_watchdog(ss, 60, now, a)
    a.error.assert_not_called()
    assert blocked is False


# ── Stale feed: alerts once and blocks ──────────────────────────────────────

def test_watchdog_fires_and_blocks_past_threshold():
    a = _alerter()
    ss = _ss(1_000.0)
    now = 1_000.0 + 120 * 60 + 1  # 1 second past threshold
    blocked = _check_candle_watchdog(ss, 60, now, a)
    a.error.assert_called_once()
    msg = a.error.call_args[0][0]
    assert "60min" in msg
    assert "Candle watchdog" in msg
    assert "BUYs blocked" in msg
    assert blocked is True
    assert ss["candle_feed_stale"] is True
    # last_candle_time itself is never touched by the watchdog — only the
    # real candle-fetch path in bot/main.py's tick loop advances it.
    assert ss["last_candle_time"] == 1_000.0


def test_watchdog_fires_with_4h_candles():
    a = _alerter()
    ss = _ss(5_000.0)
    now = 5_000.0 + 240 * 60 * 2 + 30  # 30s past 8h threshold
    blocked = _check_candle_watchdog(ss, 240, now, a)
    a.error.assert_called_once()
    assert "240min" in a.error.call_args[0][0]
    assert blocked is True


def test_watchdog_does_not_re_alert_while_continuously_stale():
    """The flag (not a reset timer) is what throttles now — staying stale
    across many ticks at the same or later 'now' must alert only once."""
    a = _alerter()
    ss = _ss(1_000.0)
    stale_now = 1_000.0 + 120 * 60 + 1

    blocked1 = _check_candle_watchdog(ss, 60, stale_now, a)
    assert a.error.call_count == 1
    assert blocked1 is True

    # Still stale, later tick, same underlying last_candle_time (no fresh
    # candle arrived) — must stay blocked without a second alert.
    blocked2 = _check_candle_watchdog(ss, 60, stale_now + 30, a)
    assert a.error.call_count == 1
    assert blocked2 is True


# ── Recovery: fresh candle arrives while flagged stale ──────────────────────

def test_watchdog_alerts_and_unblocks_on_recovery():
    a = _alerter()
    ss = _ss(1_000.0)
    stale_now = 1_000.0 + 120 * 60 + 1
    _check_candle_watchdog(ss, 60, stale_now, a)
    assert ss["candle_feed_stale"] is True
    assert a.error.call_count == 1

    # bot/main.py's real candle-fetch path advances last_candle_time when a
    # genuinely new candle arrives — simulate that, then re-check.
    ss["last_candle_time"] = stale_now
    blocked = _check_candle_watchdog(ss, 60, stale_now + 60, a)  # 1 min later — fresh

    assert blocked is False
    assert ss["candle_feed_stale"] is False
    assert a.error.call_count == 2
    recovery_msg = a.error.call_args[0][0]
    assert "recovered" in recovery_msg
    assert "re-enabled" in recovery_msg


def test_watchdog_recovery_does_not_re_fire():
    """Once recovered, staying fresh across more ticks must not alert again."""
    a = _alerter()
    ss = _ss(1_000.0)
    stale_now = 1_000.0 + 120 * 60 + 1
    _check_candle_watchdog(ss, 60, stale_now, a)     # goes stale — alert 1
    ss["last_candle_time"] = stale_now
    _check_candle_watchdog(ss, 60, stale_now + 60, a)  # recovers — alert 2
    assert a.error.call_count == 2

    blocked = _check_candle_watchdog(ss, 60, stale_now + 120, a)  # still fresh
    assert blocked is False
    assert a.error.call_count == 2  # no third alert


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
