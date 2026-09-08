"""Raw-BUY-signal Telegram heads-up — bot.main._evaluate_buy_signal_alert.

Fires ONE alerter.message() the moment the strategy's raw signal first turns BUY
for a symbol (before any gate / execution). Edge-triggered per symbol; resets when
the raw signal is no longer BUY so the next fresh BUY episode re-alerts. The
existing fill alert / blocked-BUY alert report the outcome.
"""
from unittest.mock import MagicMock

import bot.main as bot_main


def _ss():
    return {"last_buy_signal_alerted": False}


def test_alerts_once_when_the_raw_signal_turns_buy():
    ss, alerter = _ss(), MagicMock()
    bot_main._evaluate_buy_signal_alert(ss, "SOL/CAD", True, 142.5, alerter)
    assert alerter.message.call_count == 1
    msg = alerter.message.call_args[0][0]
    assert "SOL/CAD" in msg and "142.5" in msg
    assert ss["last_buy_signal_alerted"] is True


def test_does_not_re_alert_while_signal_stays_buy():
    ss, alerter = _ss(), MagicMock()
    for _ in range(5):
        bot_main._evaluate_buy_signal_alert(ss, "BTC/CAD", True, 110_000.0, alerter)
    assert alerter.message.call_count == 1


def test_resets_when_signal_no_longer_buy_then_re_alerts():
    ss, alerter = _ss(), MagicMock()
    bot_main._evaluate_buy_signal_alert(ss, "BTC/CAD", True, 110_000.0, alerter)
    bot_main._evaluate_buy_signal_alert(ss, "BTC/CAD", False, 109_000.0, alerter)
    assert ss["last_buy_signal_alerted"] is False
    bot_main._evaluate_buy_signal_alert(ss, "BTC/CAD", True, 111_000.0, alerter)
    assert alerter.message.call_count == 2


def test_no_alert_when_signal_never_buy():
    ss, alerter = _ss(), MagicMock()
    bot_main._evaluate_buy_signal_alert(ss, "BTC/CAD", False, 110_000.0, alerter)
    assert alerter.message.call_count == 0
    assert ss["last_buy_signal_alerted"] is False


def test_missing_price_omits_the_price_clause():
    ss, alerter = _ss(), MagicMock()
    bot_main._evaluate_buy_signal_alert(ss, "SOL/CAD", True, 0.0, alerter)
    assert alerter.message.call_count == 1
    assert "near $" not in alerter.message.call_args[0][0]


def test_wired_into_run_before_the_blocked_buy_alert():
    import inspect
    src = inspect.getsource(bot_main.run)
    assert "_evaluate_buy_signal_alert(" in src
    # the heads-up fires at raw-signal time, ahead of the gate-outcome alert
    assert src.index("_evaluate_buy_signal_alert(") < src.index("_evaluate_blocked_buy_alert(")
