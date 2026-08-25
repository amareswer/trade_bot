"""
Tests for stock_bot.main._update_ai_health() — added 2026-08-25.

Closes the stock-bot analog of the 2026-08-15 Kraken-auth-outage gap
(bot/main.py's _update_auth_health(), tests/crypto/test_drift_escalation.py):
the AI provider (nvidia_nim) has degraded three separate times on this
project and every one was only ever caught by manually testing the API by
hand — _ai_failed_n/_ai_nvidia_n/_ai_fallback_n were already computed every
scan cycle but only ever printed to the console, never alerted. Mirrors the
crypto test structure one-for-one; the one deliberate difference is that
this health flag is NOT wired into either heartbeat's healthy_fn (AI here is
advisory-only — rule-based trading is unaffected by an AI outage), so there
is no heartbeat-flip test here, only the ops_alert edge-triggering.
"""
import inspect
from unittest.mock import MagicMock

import stock_bot.main as main_mod
from stock_bot.main import _update_ai_health


def test_ai_health_below_threshold_no_alert():
    """Failures under the threshold just count up — no alert yet."""
    notifier = MagicMock()
    ai_health = {"ok": True}
    n = 0
    for _ in range(2):
        n = _update_ai_health(ai_health, False, n, 3, notifier, detail="3/3 failed")
    notifier.ops_alert.assert_not_called()
    assert ai_health["ok"] is True
    assert n == 2


def test_ai_health_trips_at_threshold():
    """At the 3rd consecutive fully-failed cycle: alerts once and flips
    ok -> False."""
    notifier = MagicMock()
    ai_health = {"ok": True}
    n = 0
    for _ in range(3):
        n = _update_ai_health(ai_health, False, n, 3, notifier, detail="5/5 failed")
    notifier.ops_alert.assert_called_once()
    title, message = notifier.ops_alert.call_args[0]
    assert title == "AI provider degraded"
    assert "3 consecutive scan cycles" in message
    assert "5/5 failed" in message
    assert "Rule-based BUY/SELL signals are unaffected" in message
    assert ai_health["ok"] is False
    assert n == 0  # counter resets after evaluating the threshold


def test_ai_health_stays_failing_without_realert():
    """Once tripped, further failure batches keep ok=False but must NOT
    re-alert every threshold hit — edge-triggered, not per-cycle spam."""
    notifier = MagicMock()
    ai_health = {"ok": True}
    n = 0
    for _ in range(3):
        n = _update_ai_health(ai_health, False, n, 3, notifier)
    for _ in range(9):  # three more threshold batches
        n = _update_ai_health(ai_health, False, n, 3, notifier)
    notifier.ops_alert.assert_called_once()
    assert ai_health["ok"] is False


def test_ai_health_recovers_and_alerts_once():
    """A single success after tripping flips ok -> True, fires exactly one
    recovery alert, and resets the failure counter."""
    notifier = MagicMock()
    ai_health = {"ok": True}
    n = 0
    for _ in range(3):
        n = _update_ai_health(ai_health, False, n, 3, notifier)
    assert ai_health["ok"] is False

    n = _update_ai_health(ai_health, True, n, 3, notifier)

    assert ai_health["ok"] is True
    assert n == 0
    assert notifier.ops_alert.call_count == 2  # one trip alert + one recovery alert
    recovery_title = notifier.ops_alert.call_args[0][0]
    assert "RECOVERED" in recovery_title


def test_ai_health_success_while_already_healthy_never_alerts():
    """The common case — every attempted cycle has at least one successful
    AI call — must never touch the notifier at all."""
    notifier = MagicMock()
    ai_health = {"ok": True}
    n = 0
    for _ in range(50):
        n = _update_ai_health(ai_health, True, n, 3, notifier)
    notifier.ops_alert.assert_not_called()
    assert ai_health["ok"] is True


def test_ai_health_detail_omitted_when_blank():
    """detail is optional — an empty string must not leave a stray '()' in
    the alert message."""
    notifier = MagicMock()
    ai_health = {"ok": True}
    n = 0
    for _ in range(3):
        n = _update_ai_health(ai_health, False, n, 3, notifier)
    message = notifier.ops_alert.call_args[0][1]
    assert "()" not in message


# ── Wiring guard — run() needs a live yfinance/IBKR/screener/dashboard
# stack to execute directly, same constraint as the VIX/macro/correlation
# gate wiring tests, so this is source inspection rather than behavioral.

def test_run_evaluates_ai_health_only_on_attempted_cycles():
    source = inspect.getsource(main_mod.run)
    assert "_ai_attempted_n = _ai_nvidia_n + _ai_fallback_n + _ai_failed_n" in source
    assert "if _ai_attempted_n > 0:" in source
    assert "_update_ai_health(" in source


def test_run_does_not_wire_ai_health_into_heartbeat():
    """Deliberate — AI is advisory-only (rule-based trading is unaffected
    by an AI outage), so a degraded AI provider must not flip either
    heartbeat's healthy_fn (that would misreport 'the bot is down')."""
    source = inspect.getsource(main_mod.run)
    for line in source.splitlines():
        if "healthy_fn=" in line:
            assert "_ai_health" not in line
