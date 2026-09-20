"""
Wiring guard: the execution-accounting reconciliation gate must actually
sit between the risk gate's approval and order execution in bot.main.run(),
must never fire on a SELL/exit, and must be gated behind BOTH
cfg.accounting.enabled and cfg.accounting.block_buys_on_unreconciled.

run() is a large live-stack-dependent function (same constraint as the
TSX / VIX / macro / correlation wiring tests in the stock bot), so the
fixed-roster path is verified by source inspection here; the dynamic-buy
path (_execute_ranked_dynamic_buys) is a standalone function and gets a
full behavioral test in test_dynamic_live_integration.py instead
(test_ranked_execution_blocks_on_unreconciled_accounting_state /
test_ranked_execution_accounting_disabled_is_a_no_op) — added the same
pass, 2026-09-20 money-readiness review, after this guard's own source
inspection surfaced that the dynamic path never consulted the accounting
block state at all.
"""
import inspect

import bot.main as main_mod


def _run_src() -> str:
    return inspect.getsource(main_mod.run)


def test_run_has_an_accounting_buy_gate():
    src = _run_src()
    assert "cfg.accounting.enabled and cfg.accounting.block_buys_on_unreconciled" in src
    assert "_accounting_state.blocked_for_buy(" in src
    assert "BlockReason.ACCOUNTING" in src


def test_accounting_gate_runs_after_risk_approval_and_before_execution():
    src = _run_src()
    risk_at  = src.index("approval     = risk.evaluate(")
    gate_at  = src.index("cfg.accounting.enabled and cfg.accounting.block_buys_on_unreconciled")
    exec_at  = src.index("elif approval:")
    assert risk_at < gate_at < exec_at, (
        "the accounting gate must only be consulted after risk.evaluate() "
        "has already approved, and before the approval is acted on"
    )


def test_accounting_gate_only_fires_on_buy_never_on_sell():
    src = _run_src()
    i = src.index("cfg.accounting.enabled and cfg.accounting.block_buys_on_unreconciled")
    window = src[i - 300:i + 400]
    assert "final_signal == Signal.BUY" in window
    assert "Signal.SELL" not in window


def test_accounting_gate_rejects_by_replacing_approval_not_by_raising():
    src = _run_src()
    i = src.index("BlockReason.ACCOUNTING")
    window = src[i - 300:i + 50]
    assert "approval = ApprovalResult(" in window
    assert "approved=False" in window


def test_dynamic_ranked_buy_path_also_consults_the_accounting_gate():
    """Companion to the fixed-roster checks above: the dynamic-buy queue
    is executed by a separate function (_execute_ranked_dynamic_buys),
    which must be handed the same accounting_state/flags at its one call
    site in run() — not just accounting_enabled/conn/adapter (those alone
    only wire fee recording on a fill, not the BUY-block itself; see that
    function's own 2026-09-20 docstring addendum)."""
    src = _run_src()
    call_at = src.index("_execute_ranked_dynamic_buys(")
    window = src[call_at:call_at + 900]
    assert "accounting_state=_accounting_state" in window
    assert "accounting_block_buys_on_unreconciled=cfg.accounting.block_buys_on_unreconciled" in window
    assert "accounting_max_age_ms=_accounting_max_age_ms" in window

    fn_src = inspect.getsource(main_mod._execute_ranked_dynamic_buys)
    assert "accounting_state.blocked_for_buy(" in fn_src
