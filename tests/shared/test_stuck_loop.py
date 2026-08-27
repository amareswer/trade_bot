"""
StuckLoopDetector — bot/alerts/stuck_loop.py (added 2026-08-27).

Generic "the same operation keeps failing" watchdog, error-string-agnostic.
Prompted by the native-stop deadlock (execute() rejected ~200 SL/TP exits over
8 min): the fix special-cased that one path; this catches the next class.
"""
from bot.alerts.stuck_loop import StuckLoopDetector


class _Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def _det(**kw):
    alerts = []
    clock = _Clock()
    d = StuckLoopDetector(alerts.append, time_fn=clock,
                          **{"threshold": 5, "re_alert_every": 20, **kw})
    return d, alerts, clock


def test_no_alert_below_threshold():
    d, alerts, _ = _det()
    for _ in range(4):
        d.record("place:BTC", ok=False)
    assert alerts == []


def test_alert_fires_exactly_at_threshold():
    d, alerts, _ = _det()
    for _ in range(5):
        d.record("place:BTC", ok=False)
    assert len(alerts) == 1
    assert "place:BTC" in alerts[0] and "5 times" in alerts[0]


def test_success_resets_the_streak():
    d, alerts, _ = _det()
    for _ in range(4):
        d.record("place:BTC", ok=False)
    d.record("place:BTC", ok=True)
    for _ in range(4):
        d.record("place:BTC", ok=False)
    assert alerts == []                 # never reached 5 in a row
    assert d.snapshot() == {"place:BTC": 4}


def test_re_alerts_on_escalation_cadence_not_every_failure():
    d, alerts, _ = _det()          # threshold 5, re_alert_every 20
    for _ in range(45):
        d.record("x", ok=False)
    # fires at 5, then 25, then 45
    assert len(alerts) == 3


def test_detail_is_included_and_last_wins():
    d, alerts, _ = _det()
    d.record("k", ok=False, detail="first")
    for _ in range(4):
        d.record("k", ok=False, detail="Insufficient funds")
    assert "Insufficient funds" in alerts[0]


def test_keys_are_independent():
    d, alerts, _ = _det()
    for _ in range(5):
        d.record("place:BTC", ok=False)
    for _ in range(4):
        d.record("place:SOL", ok=False)
    assert len(alerts) == 1                      # only BTC hit threshold
    assert d.failing_keys() == {"place:BTC": 5}
    assert d.snapshot() == {"place:BTC": 5, "place:SOL": 4}


def test_stale_keys_are_pruned_after_ttl():
    d, alerts, clock = _det(ttl_s=100.0)
    for _ in range(3):
        d.record("k", ok=False)
    clock.t = 101.0
    assert d.snapshot() == {}                    # pruned
    # and a fresh failure after pruning starts from 1, not 4
    d.record("k", ok=False)
    assert d.snapshot() == {"k": 1}


def test_alerter_exception_never_propagates():
    def boom(_): raise RuntimeError("telegram down")
    clock = _Clock()
    d = StuckLoopDetector(boom, threshold=2, time_fn=clock)
    d.record("k", ok=False)
    d.record("k", ok=False)                      # would fire — must swallow


def test_failing_keys_excludes_below_threshold():
    d, _, _ = _det()
    for _ in range(3):
        d.record("k", ok=False)
    assert d.failing_keys() == {}
    assert d.snapshot() == {"k": 3}


def test_bad_config_rejected():
    import pytest
    with pytest.raises(ValueError):
        StuckLoopDetector(lambda _: None, threshold=0)
    with pytest.raises(ValueError):
        StuckLoopDetector(lambda _: None, re_alert_every=0)
