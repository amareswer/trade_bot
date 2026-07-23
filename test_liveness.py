"""
Hermetic tests for LivenessTracker (bot/alerts/liveness.py).

2026-07-22 incident: the stock bot's swing-book worker thread hung silently
for 5+ hours (no exception raised, nothing logged) while both bots'
heartbeats kept reporting healthy — they only ever checked process-alive
(crypto) or IBKR-socket-alive (stock), never whether the actual work loop
was still making progress. LivenessTracker closes that gap: touch() marks
progress, is_alive(max_stale_s) answers "has the loop touched recently."
Injectable time_fn keeps this fully hermetic — no real sleeps.
"""
from bot.alerts.liveness import LivenessTracker


def test_fresh_tracker_is_alive():
    t = LivenessTracker(time_fn=lambda: 0.0)
    assert t.is_alive(max_stale_s=10) is True


def test_touch_resets_the_clock():
    clock = [0.0]
    t = LivenessTracker(time_fn=lambda: clock[0])
    clock[0] = 100.0
    t.touch()
    assert t.is_alive(max_stale_s=10) is True


def test_stale_tracker_reports_not_alive():
    clock = [0.0]
    t = LivenessTracker(time_fn=lambda: clock[0])
    clock[0] = 700.0   # 700s elapsed since construction, threshold 600
    assert t.is_alive(max_stale_s=600) is False


def test_is_alive_boundary_is_strict_less_than():
    clock = [0.0]
    t = LivenessTracker(time_fn=lambda: clock[0])
    clock[0] = 600.0
    assert t.is_alive(max_stale_s=600) is False   # exactly at threshold = stale


def test_seconds_since_touch_reports_elapsed():
    clock = [0.0]
    t = LivenessTracker(time_fn=lambda: clock[0])
    clock[0] = 42.0
    assert t.seconds_since_touch() == 42.0


def test_multiple_touches_track_the_latest():
    clock = [0.0]
    t = LivenessTracker(time_fn=lambda: clock[0])
    clock[0] = 50.0
    t.touch()
    clock[0] = 55.0
    t.touch()
    assert t.seconds_since_touch() == 0.0


def test_a_hang_between_touches_is_detected():
    # Simulates exactly the 2026-07-22 incident: the loop touches normally,
    # then something hangs indefinitely (no exception, no further touch) —
    # is_alive() must flip to False once the stale threshold passes, even
    # though nothing ever raised or logged an error.
    clock = [0.0]
    t = LivenessTracker(time_fn=lambda: clock[0])
    for tick in range(1, 6):
        clock[0] = float(tick) * 30   # normal ticks, 30s apart
        t.touch()
    assert t.is_alive(max_stale_s=600) is True
    clock[0] += 3600   # the loop hangs for an hour — no more touches
    assert t.is_alive(max_stale_s=600) is False


if __name__ == "__main__":
    import sys
    failures = 0
    for t in [
        test_fresh_tracker_is_alive,
        test_touch_resets_the_clock,
        test_stale_tracker_reports_not_alive,
        test_is_alive_boundary_is_strict_less_than,
        test_seconds_since_touch_reports_elapsed,
        test_multiple_touches_track_the_latest,
        test_a_hang_between_touches_is_detected,
    ]:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
