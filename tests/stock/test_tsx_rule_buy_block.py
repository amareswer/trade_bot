"""
Wiring guard: `.TO` (TSX) symbols must never reach an automated BUY order.

CIRO rule DMR 3200 A.1.(b)(i) prohibits IBKR Canada clients from placing
orders on Canadian exchanges via any automated system. `.TO` names stay
watch-list / top-mover scannable (advisory + AI training) but are not
auto-buyable. This guard was implicit in RULE_WHITELIST (which had no `.TO`
members) until the whitelist stopped gating BUYs on 2026-08-23; a live
AC.TO rule BUY on 2026-09-02 hit IBKR's 'Inactive' rejection and fired a
false "Order rejected" ops alert, proving the implicit guard was gone.

run() is a large live-stack-dependent function (same constraint as the
VIX / macro / correlation / RULES-log wiring tests), so this is source
inspection.
"""
import inspect

import stock_bot.main as main_mod


def _run_src() -> str:
    return inspect.getsource(main_mod.run)


def test_run_has_a_tsx_dot_to_buy_guard():
    src = _run_src()
    assert 'symbol.upper().endswith(".TO")' in src
    assert "TSX_BLOCKED" in src


def test_tsx_guard_clears_act_buy_and_records_the_block():
    src = _run_src()
    i = src.index("TSX_BLOCKED")
    window = src[i - 400:i + 400]
    assert "_act_buy = False" in window, "guard must clear _act_buy so the BUY is not acted on"
    assert '_blocked_rule_buys[symbol] = "TSX_BLOCKED"' in window, (
        "guard must record the block so the end-of-cycle digest reports it"
    )


def test_tsx_guard_runs_before_the_buy_execution_block():
    src = _run_src()
    guard_at = src.index('if _act_buy and symbol.upper().endswith(".TO"):')
    exec_at  = src.index("if executor is not None and (_act_buy or _act_sell):")
    assert guard_at < exec_at, "the .TO guard must run before _act_buy is consumed"


def test_tsx_guard_does_not_touch_sell_path():
    # The guard is BUY-only — a manual/legacy .TO position must still be
    # exit-manageable; it only prevents the bot OPENING one.
    src = _run_src()
    i = src.index("TSX_BLOCKED")
    window = src[i - 400:i + 400]
    assert "_act_sell" not in window
