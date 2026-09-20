"""
Four-way joint verification (design §9, implementation item 7):
    1. Exchange data (reconciliation's balance-identity result, §2)
    2. Executor state (LiveExecutor's own position/cash — caller-supplied)
    3. PositionManager / CapitalPool (caller-supplied)
    4. SQLite (observed_trades fold + the ledger-delivery invariant)

Every check is reported SEPARATELY and by name (design §9 step 5) — this
module never collapses anything into a single boolean before the final
`FourWayReport.ready` property, and even that property's docstring says
plainly what it does and doesn't prove. Read-only: makes no exchange calls
itself (the caller passes in an already-run `BlockState` from
reconciliation.run_cycle so this can be re-derived cheaply as often as the
health digest wants without doubling API traffic) and never mutates any
trading state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from bot.accounting import engine, store
from bot.accounting.reconciliation import BlockState, _effective_fee_trades, base_asset

_QTY_TOLERANCE = 1e-6
_FEE_TOLERANCE = 1e-6
_COST_TOLERANCE = 1e-4


@dataclass
class LedgerDeliveryResult:
    ok: bool
    orphaned_marker_trade_ids: "list[str]" = field(default_factory=list)   # ledger_written_at set but no link row
    unwritten_trade_ids: "list[str]" = field(default_factory=list)         # link row exists but ledger_written_at NULL
    double_represented_fill_ids: "list[int]" = field(default_factory=list)  # a fills row linked to >1 trade with mismatched sum


def verify_ledger_delivery_consistency(conn, symbol: str) -> LedgerDeliveryResult:
    """design §9 step 3: every observed_trades row with ledger_written_at
    set must have exactly one trade_fill_links row, and vice versa — a
    failure here is a CODE BUG in this package's own bookkeeping, never an
    exchange-data problem, and is reported as such.

    Economics check (accounting review follow-up, 2026-09-20, P1): the
    link/marker existence check above returned ok=True for a real
    reproduced misallocation — two quantity-1 trades both linked to a
    single quantity-1 fill (live_observe's old order_id-only match, since
    fixed — see live_observe.py). Existence alone can't catch that: both
    trades genuinely have a link row and a marker, so nothing above flags
    it. This additionally sums, per fill_id, every trade currently linked
    to it (using the CURRENT correction-adjusted fee, same
    _effective_fee_trades transformation diff_position_against_fold already
    applies below) and compares against what the fills row itself recorded
    — a fill_id whose linked trades over- or under-represent it lands in
    double_represented_fill_ids and fails `ok`, regardless of how the
    mismatch happened."""
    trades = store.load_observed_trades(conn, symbol)
    orphaned, unwritten = [], []
    for t in trades:
        has_link = store.is_ledger_represented(conn, t.trade_id)
        written = conn.execute(
            "SELECT ledger_written_at FROM observed_trades WHERE trade_id = ?", (t.trade_id,)
        ).fetchone()
        is_written = bool(written and written[0])
        if is_written and not has_link:
            orphaned.append(t.trade_id)
        elif has_link and not is_written:
            unwritten.append(t.trade_id)

    double_represented = []
    trade_ids = [t.trade_id for t in trades]
    fill_ids = sorted(store.linked_fill_ids_for_trades(conn, trade_ids)) if trade_ids else []
    trades_by_id = {t.trade_id: t for t in trades}
    for fill_id in fill_ids:
        fill_row = conn.execute(
            "SELECT quantity, fee_cost, value FROM fills WHERE id = ?", (fill_id,)
        ).fetchone()
        if fill_row is None:
            continue  # a link pointing at a nonexistent fills row is a different failure mode, not this check's job
        fill_qty, fill_fee, fill_value = fill_row
        linked_trade_ids = store.trade_ids_for_fill(conn, fill_id)
        linked_trades = [trades_by_id[tid] for tid in linked_trade_ids if tid in trades_by_id]
        if len(linked_trades) != len(linked_trade_ids):
            continue  # a trade linked here belongs to a different symbol's fold — out of scope for this call
        effective = _effective_fee_trades(conn, linked_trades)
        sum_qty = sum(t.amount for t in effective)
        sum_fee = sum(t.fee_cost for t in effective)
        sum_cost = sum(t.cost for t in effective)
        if (abs(sum_qty - float(fill_qty or 0.0)) > _QTY_TOLERANCE
                or abs(sum_fee - float(fill_fee or 0.0)) > _FEE_TOLERANCE
                or abs(sum_cost - float(fill_value or 0.0)) > _COST_TOLERANCE):
            double_represented.append(fill_id)

    ok = not orphaned and not unwritten and not double_represented
    return LedgerDeliveryResult(
        ok=ok, orphaned_marker_trade_ids=orphaned, unwritten_trade_ids=unwritten,
        double_represented_fill_ids=double_represented,
    )


@dataclass
class PositionRebuildDiff:
    ok: bool
    fold_qty: float = 0.0
    fold_avg_cost: float = 0.0
    fold_realized_pnl: float = 0.0
    live_qty: float = 0.0
    live_avg_cost: float = 0.0
    live_realized_pnl: float = 0.0
    reason: str = ""


def diff_position_against_fold(
    conn, symbol: str, *, live_qty: float, live_avg_cost: float, live_realized_pnl: float,
    qty_tolerance: float = 1e-8, cash_tolerance: float = 0.01,
) -> PositionRebuildDiff:
    """design §9 step 4: recompute the observed_trades fold independently
    of whatever PositionManager/LiveExecutor currently hold and diff. A
    difference means the in-memory state was not actually rebuilt from the
    ledger at last startup (a wiring bug), not an exchange-reconciliation
    problem — reported distinctly from the balance-identity checks above.

    Honest scope note: until item 8's migration has run for a symbol,
    observed_trades may only contain a SUBSET of that symbol's real
    history (whatever's been observed live since this package was
    deployed) — a diff here before migration is EXPECTED and not itself
    evidence of a bug. Callers should treat this check as meaningful only
    once a symbol has a migration-backed complete observed_trades history
    (tracked externally, e.g. via a migration report, not by this
    function, which has no way to know that on its own).

    Money-readiness review 2026-09-19: "do not let an empty or
    pre-migration ledger appear healthy merely because there are no link
    errors." A FLAT symbol (live_qty ~= 0) with no observed_trades is
    genuinely fine — nothing is at risk, there is nothing to verify. A
    symbol currently HOLDING a real position with zero observed-trade
    history is the opposite of healthy: it means this accounting layer
    has no ledger evidence at all for money that is actually at risk right
    now (the exact pre-migration gap), and must not report ok=True."""
    trades = store.load_observed_trades(conn, symbol)
    if not trades:
        if abs(live_qty) > qty_tolerance:
            return PositionRebuildDiff(
                ok=False, live_qty=live_qty, live_avg_cost=live_avg_cost,
                live_realized_pnl=live_realized_pnl,
                reason=(
                    f"holding a live position (qty={live_qty}) with ZERO observed_trades "
                    f"history for this symbol — unverifiable pre-migration gap, not treated as healthy"
                ),
            )
        return PositionRebuildDiff(ok=True, live_qty=live_qty, live_avg_cost=live_avg_cost,
                                    live_realized_pnl=live_realized_pnl,
                                    reason="flat, no observed_trades for this symbol yet — nothing at risk to verify")
    corrected = []
    for t in trades:
        deltas = store.fee_correction_deltas_for_trade(conn, t.trade_id)
        effective_fee = engine.effective_fee_cost(t.fee_cost, deltas)
        corrected.append(engine.ObservedTrade(
            trade_id=t.trade_id, order_id=t.order_id, symbol=t.symbol, side=t.side,
            price=t.price, amount=t.amount, cost=t.cost, fee_cost=effective_fee,
            fee_currency=t.fee_currency, exchange_timestamp=t.exchange_timestamp, source=t.source,
        ))
    try:
        fold = engine.recover_position(corrected)
    except ValueError as exc:
        return PositionRebuildDiff(ok=False, live_qty=live_qty, live_avg_cost=live_avg_cost,
                                    live_realized_pnl=live_realized_pnl, reason=str(exc))
    qty_ok = abs(fold.final_qty - live_qty) <= qty_tolerance
    cost_ok = fold.final_qty <= qty_tolerance or abs(fold.avg_cost - live_avg_cost) <= cash_tolerance
    ok = qty_ok and cost_ok
    reason = "" if ok else (
        f"qty diff={fold.final_qty - live_qty:.10f}" if not qty_ok
        else f"avg_cost diff={fold.avg_cost - live_avg_cost:.6f}"
    )
    return PositionRebuildDiff(
        ok=ok, fold_qty=fold.final_qty, fold_avg_cost=fold.avg_cost, fold_realized_pnl=fold.realized_pnl,
        live_qty=live_qty, live_avg_cost=live_avg_cost, live_realized_pnl=live_realized_pnl, reason=reason,
    )


@dataclass
class FourWayReport:
    block_state: BlockState
    ledger_delivery: "dict[str, LedgerDeliveryResult]" = field(default_factory=dict)
    position_diff: "dict[str, PositionRebuildDiff]" = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """PASS only with zero blocked flags (exchange-data checks) and
        zero diffs (SQLite-internal checks) — design §9 step 5. This is
        NOT a profitability or resumption verdict — the 2026-09-12
        review-deadline decision's fresh out-of-sample walk-forward
        requirement is a completely separate, additional gate, unaffected
        by this property."""
        if self.block_state.account_cash_blocked or self.block_state.coverage_blocked:
            return False
        if any(self.block_state.symbol_blocked.values()):
            return False
        if any(not r.ok for r in self.ledger_delivery.values()):
            return False
        if any(not d.ok for d in self.position_diff.values()):
            return False
        return True

    def explain(self) -> str:
        if self.ready:
            return "four-way verification PASS: zero blocked flags, zero ledger/delivery or position-fold diffs"
        failing = []
        state_explain = self.block_state.explain()
        if state_explain != "ok":
            failing.append(f"exchange-data: {state_explain}")
        for sym, r in self.ledger_delivery.items():
            if not r.ok:
                failing.append(
                    f"ledger/delivery [{sym}]: orphaned={r.orphaned_marker_trade_ids} "
                    f"unwritten={r.unwritten_trade_ids}"
                )
        for sym, d in self.position_diff.items():
            if not d.ok:
                failing.append(f"position-fold [{sym}]: {d.reason}")
        return "not ready — failing: " + "; ".join(failing)


def run_four_way_verification(
    conn, block_state: BlockState, *, symbols: "list[str]",
    live_positions: "dict[str, dict]",
) -> FourWayReport:
    """live_positions: {symbol: {"qty": float, "avg_cost": float, "realized_pnl": float}}
    — the caller (bot/main.py) supplies this from the ALREADY-RUNNING
    LiveExecutor/PositionManager for each symbol; this function makes no
    assumption about how it was obtained and calls nothing in bot/execution
    or bot/portfolio directly, keeping this package's only coupling to the
    live bot at the call site, not inside the accounting logic itself."""
    report = FourWayReport(block_state=block_state)
    for sym in symbols:
        report.ledger_delivery[sym] = verify_ledger_delivery_consistency(conn, sym)
        pos = live_positions.get(sym, {"qty": 0.0, "avg_cost": 0.0, "realized_pnl": 0.0})
        report.position_diff[sym] = diff_position_against_fold(
            conn, sym, live_qty=pos.get("qty", 0.0), live_avg_cost=pos.get("avg_cost", 0.0),
            live_realized_pnl=pos.get("realized_pnl", 0.0),
        )
    return report
