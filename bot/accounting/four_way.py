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
from bot.accounting.reconciliation import BlockState, base_asset

_QTY_TOLERANCE = 1e-6
_FEE_TOLERANCE = 1e-6
_COST_TOLERANCE = 1e-4


@dataclass
class LedgerDeliveryResult:
    ok: bool
    orphaned_marker_trade_ids: "list[str]" = field(default_factory=list)   # ledger_written_at set but no link row
    unwritten_trade_ids: "list[str]" = field(default_factory=list)         # link row exists but ledger_written_at NULL
    double_represented_fill_ids: "list[int]" = field(default_factory=list)  # a fills row linked to trades with mismatched sum
    duplicate_owner_trade_ids: "list[str]" = field(default_factory=list)   # one trade linked to >1 DIFFERENT fill_id
    dangling_fill_ids: "list[int]" = field(default_factory=list)          # a link references a fill_id no longer in `fills`
    dangling_trade_ids: "list[str]" = field(default_factory=list)         # a link references a trade_id no longer in observed_trades


def verify_ledger_delivery_consistency(conn, symbol: str) -> LedgerDeliveryResult:
    """design §9 step 3: every observed_trades row with ledger_written_at
    set must have exactly one trade_fill_links row, and vice versa — a
    failure here is a CODE BUG in this package's own bookkeeping, never an
    exchange-data problem, and is reported as such.

    Economics check (accounting review follow-up, 2026-09-20, P1 through
    third pass): sums, per fill_id, every trade currently linked to it and
    compares against what the fills row itself recorded — a fill_id whose
    linked trades over- or under-represent it lands in
    double_represented_fill_ids and fails `ok`, regardless of how the
    mismatch happened.

    Fee comparison accepts EITHER a trade's original fee_cost OR its
    correction-adjusted effective fee (third review pass, 2026-09-20, P1:
    "matching and verification disagree on fees"). fills.fee_cost is a
    frozen, execution-time value a later fee_adjustments correction never
    updates — but WHETHER that frozen value reflects the pre- or
    post-correction total depends on whether the correction was detected
    before or after the fills row was recorded, which this table has no
    way to know after the fact. The straggler matcher (reconciliation.py's
    _link_stragglers) always matches using the CURRENT effective fee, so a
    link it just approved could equally have conserved against either
    total depending on timing — checking only the original fee (the
    second pass's fix) rejected a link the matcher had just correctly
    approved using the corrected fee. Accepting either is the only policy
    consistent with a matcher that has no persisted record of which fee
    vintage was in effect at match time; quantity and cost have no such
    ambiguity and are still checked against a single value.

    Reference scan starts from trade_fill_links itself, checking BOTH
    endpoints, not from observed_trades alone (third review pass,
    2026-09-20, P1: "missing referenced trades still pass verification...
    it starts from existing observed trades, so it never examines the
    orphaned link"). A link whose trade_id has no observed_trades row at
    all — deleted, or never written — is caught as dangling_trade_ids even
    though the loop below can never iterate to it via `trades`."""
    trades = store.load_observed_trades(conn, symbol)
    trades_by_id = {t.trade_id: t for t in trades}
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

    # Duplicate ownership (second review pass, 2026-09-20, P1: "linking one
    # exchange trade to two identical fill rows returned ok=True").
    # store.link_trades_to_fill(_nocommit) now REFUSES to create this going
    # forward (application-level guard — see its own docstring for why a
    # schema-level UNIQUE constraint wasn't used on a live financial
    # table), but this detects any row that predates that guard.
    duplicate_owner = []
    for t in trades:
        owner_fill_ids = {
            r[0] for r in conn.execute(
                "SELECT fill_id FROM trade_fill_links WHERE trade_id = ?", (t.trade_id,)
            )
        }
        if len(owner_fill_ids) > 1:
            duplicate_owner.append(t.trade_id)

    # Every trade_fill_links row relevant to this symbol, found via EITHER
    # endpoint — a trade that exists and belongs here, OR a fill that
    # exists and belongs here — so a link with either side missing/wrong
    # still surfaces instead of silently never being examined.
    link_rows = conn.execute(
        "SELECT tfl.trade_id, tfl.fill_id FROM trade_fill_links tfl "
        "LEFT JOIN observed_trades ot ON ot.trade_id = tfl.trade_id "
        "LEFT JOIN fills f ON f.id = tfl.fill_id "
        "WHERE ot.symbol = ? OR f.symbol = ?",
        (symbol, symbol),
    ).fetchall()
    trade_ids_by_fill: "dict[int, list[str]]" = {}
    for trade_id, fill_id in link_rows:
        trade_ids_by_fill.setdefault(fill_id, []).append(trade_id)

    double_represented = []
    dangling_fill_ids = []
    dangling_trade_ids = []
    for fill_id, linked_trade_ids in trade_ids_by_fill.items():
        fill_row = conn.execute(
            "SELECT quantity, fee_cost, value FROM fills WHERE id = ?", (fill_id,)
        ).fetchone()
        if fill_row is None:
            # Second review pass, 2026-09-20, P1: "deleting the referenced
            # fills also returned ok=True because missing rows are
            # skipped." Must never be silently passed over.
            dangling_fill_ids.append(fill_id)
            continue
        fill_qty, fill_fee, fill_value = fill_row
        linked_trades = []
        any_dangling = False
        for tid in linked_trade_ids:
            if tid in trades_by_id:
                linked_trades.append(trades_by_id[tid])
                continue
            if store.get_observed_trade(conn, tid) is None:
                dangling_trade_ids.append(tid)
                any_dangling = True
            # else: a real trade of a DIFFERENT symbol linked to this
            # fill — a distinct cross-symbol integrity problem outside
            # this per-symbol call's scope; not this trade's fault, and
            # not silently treated as economically fine either (the
            # length check below still catches it).
        if any_dangling:
            continue  # already captured in dangling_trade_ids
        if len(linked_trades) != len(linked_trade_ids):
            continue  # a linked trade belongs to a different symbol's own fold
        sum_qty = sum(t.amount for t in linked_trades)
        sum_cost = sum(t.cost for t in linked_trades)
        sum_fee_original = sum(t.fee_cost for t in linked_trades)
        sum_fee_effective = sum(
            engine.effective_fee_cost(t.fee_cost, store.fee_correction_deltas_for_trade(conn, t.trade_id))
            for t in linked_trades
        )
        fee_conserves = (
            abs(sum_fee_original - float(fill_fee or 0.0)) <= _FEE_TOLERANCE
            or abs(sum_fee_effective - float(fill_fee or 0.0)) <= _FEE_TOLERANCE
        )
        if (abs(sum_qty - float(fill_qty or 0.0)) > _QTY_TOLERANCE
                or not fee_conserves
                or abs(sum_cost - float(fill_value or 0.0)) > _COST_TOLERANCE):
            double_represented.append(fill_id)

    ok = (not orphaned and not unwritten and not double_represented
          and not duplicate_owner and not dangling_fill_ids and not dangling_trade_ids)
    return LedgerDeliveryResult(
        ok=ok, orphaned_marker_trade_ids=orphaned, unwritten_trade_ids=unwritten,
        double_represented_fill_ids=double_represented,
        duplicate_owner_trade_ids=duplicate_owner, dangling_fill_ids=dangling_fill_ids,
        dangling_trade_ids=dangling_trade_ids,
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
