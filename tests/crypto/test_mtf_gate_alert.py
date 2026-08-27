"""MTF gate fail-open alert (added 2026-08-27).

The multi-timeframe (1D) BEARISH veto fails open — allows the BUY — when the
daily-candle fetch fails and no cached closes exist. Fail-open on missing data
is the deliberate, documented design for every gate in this bot, but a risk
gate silently bypassed on a live-money BUY should not be invisible. `run()` now
fires an `alerter.error()` in exactly that no-cache branch.

`run()` is the ~1700-line tick loop and needs a full live exchange/strategy
stack to exercise behaviorally, so this is a source-inspection guard — same
idiom as the VIX/macro/correlation/auth-health guards elsewhere in the suite.
"""
import inspect
import re

import bot.main as bot_main


def _mtf_gate_block() -> str:
    """The MTF-gate section of run()'s source, from its banner comment to the
    start of the next gate (2d — external signal gate)."""
    src = inspect.getsource(bot_main.run)
    start = src.index("2c. MTF gate")
    end = src.index("2d. External signal gate", start)
    return src[start:end]


def test_mtf_gate_alerts_when_it_fails_open_with_no_cache():
    block = _mtf_gate_block()
    # An alert must be raised for the bypass, and it must be recognisably about
    # the MTF gate being skipped (not some unrelated alert in the same block).
    assert "alerter.error(" in block, "MTF fail-open path no longer alerts"
    assert "MTF GATE BYPASSED" in block


def test_mtf_gate_bypass_alert_is_guarded_by_the_no_cache_condition():
    block = _mtf_gate_block()
    # The alert lives inside the `if not _mtf_has_cache:` branch — using the
    # cached closes (gate still runs, just on slightly older data) must NOT
    # alert.
    m = re.search(r"if not _mtf_has_cache:(.+?)\n(?:            [^ ]|            #)", block, re.S)
    assert m is not None, "expected an `if not _mtf_has_cache:` guard around the alert"
    assert "alerter.error(" in m.group(1), "bypass alert must be inside the no-cache guard"

    # And the guard is only reached from the fetch-failure `except` handler.
    assert "_mtf_has_cache = bool(ss['mtf_1d_closes'])" in block
