"""Blocked rule-BUY digest — stock_bot.main._evaluate_blocked_rule_buys_alert
(added 2026-08-27). Stock-bot analog of the crypto bot's _evaluate_blocked_buy_alert:
closes the recurring "why didn't the bot buy X" question, which until now was only
answerable from console print() output (gone with the terminal).
"""
import inspect
from unittest.mock import MagicMock

import stock_bot.main as sm


def _state():
    return {"seen": {}}


def test_alerts_when_a_rule_buy_is_first_blocked():
    st, n = _state(), MagicMock()
    sm._evaluate_blocked_rule_buys_alert({"AMD": "REGIME_SKIP (market NEUTRAL)"}, st, n)
    assert n.ops_alert.call_count == 1
    title, body = n.ops_alert.call_args[0]
    assert "1 rule BUY" in title
    assert "AMD" in body and "REGIME_SKIP" in body
    assert st["seen"] == {"AMD": "REGIME_SKIP (market NEUTRAL)"}


def test_no_re_alert_while_the_blocked_set_is_unchanged():
    st, n = _state(), MagicMock()
    blocked = {"AMD": "REGIME_SKIP", "PLTR": "REGIME_SKIP"}
    for _ in range(4):
        sm._evaluate_blocked_rule_buys_alert(dict(blocked), st, n)
    assert n.ops_alert.call_count == 1


def test_re_alerts_when_a_symbol_is_added():
    st, n = _state(), MagicMock()
    sm._evaluate_blocked_rule_buys_alert({"AMD": "REGIME_SKIP"}, st, n)
    sm._evaluate_blocked_rule_buys_alert({"AMD": "REGIME_SKIP", "T": "SIZE_SKIP"}, st, n)
    assert n.ops_alert.call_count == 2


def test_re_alerts_when_a_symbols_gate_changes():
    st, n = _state(), MagicMock()
    sm._evaluate_blocked_rule_buys_alert({"AMD": "REGIME_SKIP"}, st, n)
    sm._evaluate_blocked_rule_buys_alert({"AMD": "CORRELATION (0.81 with RY)"}, st, n)
    assert n.ops_alert.call_count == 2
    assert "CORRELATION" in n.ops_alert.call_args[0][1]


def test_clears_with_a_distinct_all_clear_message():
    st, n = _state(), MagicMock()
    sm._evaluate_blocked_rule_buys_alert({"AMD": "REGIME_SKIP"}, st, n)
    sm._evaluate_blocked_rule_buys_alert({}, st, n)
    assert n.ops_alert.call_count == 2
    assert "no longer blocked" in n.ops_alert.call_args[0][0].lower()
    assert st["seen"] == {}


def test_nothing_blocked_and_nothing_changed_is_silent():
    st, n = _state(), MagicMock()
    sm._evaluate_blocked_rule_buys_alert({}, st, n)
    sm._evaluate_blocked_rule_buys_alert({}, st, n)
    assert n.ops_alert.call_count == 0


def test_wired_into_run_loop():
    src = inspect.getsource(sm.run)
    assert "_blocked_rule_buys: dict" in src           # collected per cycle
    assert "_blocked_rule_buys[symbol] =" in src       # populated at gate sites
    assert "_evaluate_blocked_rule_buys_alert(" in src  # evaluated at cycle end
    # populated inside a rule-BUY guard, not unconditionally
    assert "if _rule_buy:\n" in src
