"""SL/TP exit-failure alert — bot.main run() (added 2026-08-27).

The native-stop deadlock (2026-08-27) rejected ~200 urgent SL/TP SELL orders over
8 minutes with ZERO Telegram alert — the SL/TP block only handled the FILLED
case, no else. On a VPS that incident would have been completely invisible.
run() now edge-escalates a failed exit (1st, 3rd, 10th, then every 20th).

Source-inspection guard — run() is the ~1700-line tick loop and needs a full
live stack to exercise behaviorally.
"""
import inspect

import bot.main as bot_main


def _sl_tp_block() -> str:
    src = inspect.getsource(bot_main.run)
    start = src.index("if _ic_order and _ic_order.status == OrderStatus.FILLED:")
    end = src.index("SL/TP SELL halted", start)
    return src[start:end]


def test_failed_sl_tp_exit_has_an_else_branch_that_alerts():
    block = _sl_tp_block()
    assert "else:" in block, "the FILLED-only branch still has no failure handler"
    assert "exit_fail_count" in block
    assert "SL/TP EXIT FAILED" in block
    assert "alerter.error(" in block


def test_exit_fail_alert_is_edge_escalated_not_every_tick():
    block = _sl_tp_block()
    # Not an unconditional alert on every failed tick.
    assert "_n in (1, 3, 10)" in block or "_n % 20" in block
    # Counter resets on a successful exit.
    assert "ss['exit_fail_count'] = 0" in block


def test_exit_fail_count_initialised_in_symbol_state():
    src = inspect.getsource(bot_main.run)
    assert src.count("'exit_fail_count': 0") >= 2   # both symbol_state init blocks
