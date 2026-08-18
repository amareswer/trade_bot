"""
TWS connection monitor (stock_bot/alerts/tws_monitor.py) — pure state
machine, no I/O. Alert-once semantics per outage, blip tolerance below the
threshold, recovery notice only after a fired alert.
"""
from stock_bot.alerts.tws_monitor import TwsConnectionMonitor


def test_connected_never_alerts():
    mon = TwsConnectionMonitor(alert_after_s=600)
    for t in range(0, 3600, 60):
        assert mon.update(True, float(t)) is None


def test_short_blip_stays_quiet():
    mon = TwsConnectionMonitor(alert_after_s=600)
    assert mon.update(False, 0.0) is None      # outage starts
    assert mon.update(False, 300.0) is None    # under threshold
    assert mon.update(True, 400.0) is None     # recovered before alert — no noise


def test_down_fires_once_at_threshold():
    mon = TwsConnectionMonitor(alert_after_s=600)
    assert mon.update(False, 0.0) is None
    assert mon.update(False, 599.0) is None
    assert mon.update(False, 600.0) == "down"
    assert mon.update(False, 660.0) is None    # no repeat while still down
    assert mon.update(False, 7200.0) is None


def test_recovered_fires_once_after_down():
    mon = TwsConnectionMonitor(alert_after_s=600)
    mon.update(False, 0.0)
    assert mon.update(False, 600.0) == "down"
    assert mon.update(True, 700.0) == "recovered"
    assert mon.update(True, 760.0) is None     # no repeat


def test_next_outage_alerts_again():
    mon = TwsConnectionMonitor(alert_after_s=600)
    mon.update(False, 0.0)
    assert mon.update(False, 600.0) == "down"
    assert mon.update(True, 700.0) == "recovered"
    # second outage gets its own fresh alert
    assert mon.update(False, 1000.0) is None
    assert mon.update(False, 1600.0) == "down"


def test_down_for_tracks_outage_duration():
    mon = TwsConnectionMonitor(alert_after_s=600)
    assert mon.down_for(0.0) is None
    mon.update(False, 100.0)
    assert mon.down_for(400.0) == 300.0
    mon.update(True, 500.0)
    assert mon.down_for(600.0) is None
