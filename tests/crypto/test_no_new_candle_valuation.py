"""Source guard for the 2026-09-15 kill-switch/between-candle finding.

`run()` is the ~1700-line tick loop and needs a full live-exchange/strategy
stack to exercise behaviorally — same idiom as the MTF/VIX/macro/auth-health
guards elsewhere in the suite (see tests/crypto/test_mtf_gate_alert.py).

The finding: risk.evaluate() (and with it the peak/kill-switch update) is
only reached when a new candle has closed — the "no new candle" branch
`continue`s straight past it. On a 4h timeframe that's most ticks, so a
drawdown-and-recovery entirely between two candle closes escaped the kill
switch even after the 2026-09-14 fix made its trip check run on every
evaluate() call, because evaluate() itself just wasn't being called.
risk.mark_valuation() must run on that path too, using the live tick price
already fetched into ss['last_price'] before the "no new candle" check.
"""
import inspect

import bot.main as bot_main


def _no_new_candle_block() -> str:
    src   = inspect.getsource(bot_main.run)
    start = src.index("if candle is None:")
    end   = src.index("continue  # no new candle for this symbol", start)
    return src[start:end]


def test_no_new_candle_path_updates_risk_valuation():
    block = _no_new_candle_block()
    assert "risk.mark_valuation(" in block, (
        "the no-new-candle branch must feed the risk manager a fresh "
        "valuation every tick, not just on candle-close ticks — otherwise a "
        "drawdown-and-recovery entirely between two candle closes still "
        "never trips the kill switch"
    )


def test_no_new_candle_valuation_uses_the_whole_account_not_one_slot():
    block = _no_new_candle_block()
    # Must match the same account-wide valuation evaluate() itself is fed
    # elsewhere in run() (_account_value(), not a single symbol's slot value)
    # — otherwise a multi-symbol account's kill switch would trip on one
    # slot's isolated drawdown instead of the whole account's.
    assert "risk.mark_valuation(_account_value())" in block
