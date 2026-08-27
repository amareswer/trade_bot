"""Blocked-BUY Telegram alert — bot.main._evaluate_blocked_buy_alert (added 2026-08-27).

Closes the 2026-08-18 gap: the bot sat flat through a $90k→$108k BTC rally while a
real BUY signal fired and was correctly vetoed by the MTF daily-trend gate — only
recoverable after the fact from logs/live_signals.csv, nothing pushed it. Now an
edge-triggered Telegram alert fires when the strategy signals BUY but a gate holds it.
"""
from unittest.mock import MagicMock

import bot.main as bot_main


def _ss():
    return {"last_buy_block_alert": ""}


def test_alerts_once_when_a_buy_is_blocked():
    ss, alerter = _ss(), MagicMock()
    bot_main._evaluate_blocked_buy_alert(ss, "SOL/CAD", True, "mtf_trend", alerter)
    assert alerter.error.call_count == 1
    msg = alerter.error.call_args[0][0]
    assert "SOL/CAD" in msg and "mtf_trend" in msg and "BEARISH" in msg
    assert ss["last_buy_block_alert"] == "mtf_trend"


def test_does_not_re_alert_while_same_gate_blocks():
    ss, alerter = _ss(), MagicMock()
    for _ in range(5):
        bot_main._evaluate_blocked_buy_alert(ss, "SOL/CAD", True, "state_machine", alerter)
    assert alerter.error.call_count == 1


def test_re_alerts_when_the_blocking_gate_changes():
    ss, alerter = _ss(), MagicMock()
    bot_main._evaluate_blocked_buy_alert(ss, "BTC/CAD", True, "mtf_trend", alerter)
    bot_main._evaluate_blocked_buy_alert(ss, "BTC/CAD", True, "capital_pool", alerter)
    assert alerter.error.call_count == 2
    assert "capital_pool" in alerter.error.call_args[0][0]


def test_clears_when_raw_signal_no_longer_buy():
    ss, alerter = _ss(), MagicMock()
    bot_main._evaluate_blocked_buy_alert(ss, "BTC/CAD", True, "regime", alerter)
    bot_main._evaluate_blocked_buy_alert(ss, "BTC/CAD", False, "", alerter)
    assert ss["last_buy_block_alert"] == ""
    # a fresh block after clearing re-alerts
    bot_main._evaluate_blocked_buy_alert(ss, "BTC/CAD", True, "regime", alerter)
    assert alerter.error.call_count == 2


def test_no_alert_when_buy_is_approved():
    ss, alerter = _ss(), MagicMock()
    bot_main._evaluate_blocked_buy_alert(ss, "BTC/CAD", True, "", alerter)
    assert alerter.error.call_count == 0
    assert ss["last_buy_block_alert"] == ""


def test_unknown_gate_still_alerts_with_raw_name():
    ss, alerter = _ss(), MagicMock()
    bot_main._evaluate_blocked_buy_alert(ss, "BTC/CAD", True, "some_new_gate", alerter)
    assert alerter.error.call_count == 1
    assert "some_new_gate" in alerter.error.call_args[0][0]


def test_wired_into_run_after_the_live_signals_csv_write():
    import inspect
    src = inspect.getsource(bot_main.run)
    assert "_evaluate_blocked_buy_alert(" in src
    # fires from the candle-close branch, after the CSV write
    assert src.index("live_signals.csv") < src.index("_evaluate_blocked_buy_alert(")
