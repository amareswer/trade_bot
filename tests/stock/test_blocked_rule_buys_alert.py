"""Blocked rule-BUY digest — stock_bot.main._evaluate_blocked_rule_buys_alert.

Added 2026-08-27 (stock-bot analog of the crypto blocked-BUY alert — the recurring
"why didn't the bot buy X" question). Debounced same day after BNS/GM flapped the
MAX_EXPOSURE gate BUY↔HOLD every other cycle and each toggle re-alerted.
"""
import inspect
from unittest.mock import MagicMock

import stock_bot.main as sm

_N = sm._BLOCKED_BUY_ABSENT_CYCLES_TO_CLEAR


def _run(seq, state=None):
    """Feed a sequence of {sym: gate} dicts; return the MagicMock notifier."""
    state = state if state is not None else {}
    n = MagicMock()
    for current in seq:
        sm._evaluate_blocked_rule_buys_alert(dict(current), state, n)
    return n, state


def test_alerts_when_a_symbol_is_first_blocked():
    n, _ = _run([{"AMD": "REGIME_SKIP"}])
    assert n.ops_alert.call_count == 1
    title, body = n.ops_alert.call_args[0]
    assert "1 rule BUY" in title and "AMD" in body and "REGIME_SKIP" in body


def test_no_re_alert_while_the_set_is_unchanged():
    n, _ = _run([{"AMD": "REGIME_SKIP", "PLTR": "REGIME_SKIP"}] * 5)
    assert n.ops_alert.call_count == 1


def test_alerts_when_a_new_symbol_is_added():
    n, _ = _run([{"AMD": "REGIME_SKIP"}, {"AMD": "REGIME_SKIP", "T": "SIZE_SKIP"}])
    assert n.ops_alert.call_count == 2
    assert "T" in n.ops_alert.call_args[0][1]


def test_alerts_when_a_symbols_gate_changes():
    n, _ = _run([{"AMD": "REGIME_SKIP"}, {"AMD": "CORRELATION (0.81 with RY)"}])
    assert n.ops_alert.call_count == 2
    assert "CORRELATION" in n.ops_alert.call_args[0][1]


def test_a_symbol_dropping_out_does_not_alert():
    n, _ = _run([{"BNS": "MAX_EXPOSURE", "GM": "MAX_EXPOSURE"}, {"GM": "MAX_EXPOSURE"}])
    assert n.ops_alert.call_count == 1   # only the initial block


def test_flapping_within_the_absent_window_does_not_re_alert():
    # BNS in → out → in, all within _N cycles: one alert total.
    seq = [
        {"BNS": "MAX_EXPOSURE", "GM": "MAX_EXPOSURE"},
        {"GM": "MAX_EXPOSURE"},
        {"BNS": "MAX_EXPOSURE", "GM": "MAX_EXPOSURE"},
        {"GM": "MAX_EXPOSURE"},
        {"BNS": "MAX_EXPOSURE", "GM": "MAX_EXPOSURE"},
    ]
    n, _ = _run(seq)
    assert n.ops_alert.call_count == 1


def test_reappearance_after_full_absent_window_re_alerts():
    seq = (
        [{"BNS": "MAX_EXPOSURE"}]
        + [{"GM": "MAX_EXPOSURE"}] * _N       # BNS absent long enough to be forgotten
        + [{"BNS": "MAX_EXPOSURE", "GM": "MAX_EXPOSURE"}]
    )
    n, _ = _run(seq)
    # 1 (BNS first) + 1 (GM first appears) + 1 (BNS returns after aging out) = 3
    assert n.ops_alert.call_count == 3
    assert "BNS" in n.ops_alert.call_args[0][1]


def test_all_clear_fires_once_when_the_set_empties():
    n, _ = _run([{"AMD": "REGIME_SKIP"}, {}, {}, {}])
    assert n.ops_alert.call_count == 2
    assert "no longer blocked" in n.ops_alert.call_args[0][0].lower()


def test_silent_when_nothing_blocked_and_nothing_changed():
    n, _ = _run([{}, {}, {}])
    assert n.ops_alert.call_count == 0


def test_wired_into_run_loop():
    src = inspect.getsource(sm.run)
    assert "_blocked_rule_buys: dict" in src
    assert "_blocked_rule_buys[symbol] =" in src
    assert "_evaluate_blocked_rule_buys_alert(" in src
    assert "if _rule_buy:\n" in src
