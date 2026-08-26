"""
Wiring guard for the RULES-decision log line added 2026-08-26.

Before this fix, run()'s per-symbol rule-signal summary (BUY/SELL/HOLD +
RSI/ADX/trend/regime — "why did/didn't the bot buy symbol X today") was a
print() only, never written to logs/stock_bot.log — unanswerable from the
log file, only from whichever terminal happened to have the scrollback.
run() is a large, live-stack-dependent function (same constraint as the
VIX/macro/correlation/AI-health wiring tests), so this is source inspection
rather than a behavioral test.
"""
import inspect

import stock_bot.main as main_mod


def test_run_logs_rule_signal_decision_per_symbol():
    source = inspect.getsource(main_mod.run)
    assert '"RULES [%s]: %s%s"' in source, (
        'Expected a logger.info("RULES [%s]: ...", symbol, ...) call mirroring '
        "the console-only print() of the per-symbol rule-signal decision — "
        "without it, 'why didn't it buy X' is unanswerable from the log file."
    )


def test_rules_log_line_includes_symbol_name():
    """The console print() doesn't embed the symbol (it relies on a header
    line printed just before it, in visual proximity) — the log version
    must, since log lines get read out of that guaranteed order."""
    source = inspect.getsource(main_mod.run)
    idx = source.index('"RULES [%s]: %s%s"')
    call_line = source[idx - 40:idx + 80]
    assert "symbol" in call_line
