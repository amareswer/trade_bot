"""2026-09-26: a four-way verification failure (BTC deposit / SOL staking-dust
position-fold diff) was logged every cycle as "account cash unreconciled
(four-way verification: ... exchange-data: account cash unreconciled ())"
although the cash check itself had PASSED — the inline code flagged cash
first, then asked the report (which references the same state) to explain
itself. A reviewer read that as a real cash discrepancy."""
import inspect

import bot.main as bot_main
from bot.accounting.four_way import FourWayReport, PositionRebuildDiff
from bot.accounting.reconciliation import BlockState as AccountingBlockState


def _clean_state() -> AccountingBlockState:
    st = AccountingBlockState(reconciled=True)
    st.symbol_blocked = {"SOL/CAD": False}
    return st


def _fold_failure(state) -> FourWayReport:
    return FourWayReport(
        block_state=state,
        position_diff={"SOL/CAD": PositionRebuildDiff(ok=False, reason="qty diff=-0.0000035775")},
    )


def test_four_way_failure_is_labelled_as_four_way_not_cash():
    st = _clean_state()
    bot_main._apply_four_way_result(st, _fold_failure(st))

    assert st.account_cash_blocked is False, "the cash check passed — must not be relabelled"
    assert st.four_way_blocked is True
    text = st.explain()
    assert "account cash unreconciled" not in text
    assert "()" not in text, "no self-referential empty reason"
    assert "four-way verification failed" in text and "qty diff=-0.0000035775" in text


def test_four_way_failure_still_blocks_every_buy():
    st = _clean_state()
    bot_main._apply_four_way_result(st, _fold_failure(st))
    assert st.blocked_for_buy("SOL/CAD") and st.blocked_for_buy("BTC/CAD")


def test_four_way_pass_changes_nothing():
    st = _clean_state()
    report = FourWayReport(block_state=st)
    assert report.ready
    bot_main._apply_four_way_result(st, report)
    assert not st.four_way_blocked and st.explain() == "ok"
    assert not st.blocked_for_buy("SOL/CAD")


def test_run_uses_the_helper_not_the_old_inline_fold_in():
    src = inspect.getsource(bot_main.run)
    assert "_apply_four_way_result(_accounting_state, _fw_report)" in src
    assert 'f"four-way verification: {_fw_report.explain()}"' not in src
