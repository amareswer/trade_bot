"""
Pure execution-accounting algorithms — exchange-agnostic, SQLite-agnostic
beyond the store.py helpers it's given.

Ported and adapted from execution_accounting_reference_model.py (repo root),
which proved this reasoning against a synthetic exchange across four review
passes (PASS-3 review .. R4). The adaptations here are:

  - ObservedTrade (store.py) instead of SynTrade — ISO-8601 UTC timestamp
    strings instead of epoch-ms ints (matching the rest of this codebase's
    convention — bot/data/trade_log.py, bot/execution/live_executor.py all
    use ISO strings). Internally this module converts to epoch-ms only
    where strict numeric ordering is required (causal_order, watermark
    math) via _ts_ms(), then converts back.
  - check_balance_consistency takes an explicit `tolerance` (design §5:
    derived from real exchange market precision, never a flat epsilon) —
    the caller (kraken_adapter.py, or a test) supplies it.
  - Ledger writes are NOT a second fills-table writer. verify_ledger_
    delivery_consistency and recover_position both read through
    store.trade_fill_links (a trade_id -> existing fills.id mapping) rather
    than assuming a fills row is keyed `exec_key=trade_id` — see store.py's
    module docstring for why.
  - Legacy-row matching (match_legacy_fill) uses the exact-conservation
    subset search from the reference model's migrate_legacy_row, adapted
    to the real `fills` row shape (dict from store.unlinked_fills).

Nothing in this module makes a network call or touches logs/HALT.
"""
from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol

from bot.accounting.store import ObservedTrade


# ============================================================================
# Timestamp helpers
# ============================================================================

def _ts_ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# Public alias — reconciliation.py and tests use this directly rather than
# reaching into the underscore-prefixed internal name.
ts_ms = _ts_ms


def iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================
# Exchange adapter protocol — kraken_adapter.py implements this for real;
# tests implement a fake one directly, no ccxt/network involved.
# ============================================================================

@dataclass
class LedgerMovement:
    """A deposit or withdrawal, for the §7.2 external-movement check."""
    entry_id: str
    type: str              # "deposit" | "withdrawal"
    asset: str
    amount: float           # signed
    timestamp: str          # ISO


@dataclass
class TradePage:
    trades: "list[ObservedTrade]"
    reported_total: int
    next_offset: "Optional[int]"


class ExchangeAdapter(Protocol):
    def fetch_my_trades_page(
        self, symbol: Optional[str], *, since: Optional[str], offset: int, limit: int,
    ) -> TradePage: ...

    def fetch_balance_total(self, asset: str) -> float: ...

    def fetch_deposits(self, asset: str, *, since: Optional[str]) -> "list[LedgerMovement]": ...

    def fetch_withdrawals(self, asset: str, *, since: Optional[str]) -> "list[LedgerMovement]": ...


# ============================================================================
# 1. History coverage — proven by exchange-reported count, never inferred
#    from a balance match (design §1, review R2 finding 1).
# ============================================================================

@dataclass
class CoverageResult:
    complete: bool
    trades: "list[ObservedTrade]"
    reported_total: int
    fetched_count: int
    reason: str = ""


def retrieve_with_coverage_proof(
    exchange: ExchangeAdapter, symbol: Optional[str], *, since: Optional[str] = None,
    page_size: int = 50, page_limit_override: Optional[int] = None,
) -> CoverageResult:
    """Pages fetch_my_trades_page until the accumulated record count equals
    the FIRST page's reported total. If a later page reports a DIFFERENT
    total, the window was not stable across this retrieval attempt (new
    trades arrived mid-pagination) — completeness is refused, not silently
    accepted."""
    all_trades: "list[ObservedTrade]" = []
    offset = 0
    first_total: Optional[int] = None
    while True:
        limit = page_size
        if page_limit_override is not None:
            remaining = page_limit_override - len(all_trades)
            if remaining <= 0:
                return CoverageResult(False, all_trades, first_total or 0, len(all_trades),
                                       "retrieval attempt truncated by page_limit_override")
            limit = min(limit, remaining)
        page = exchange.fetch_my_trades_page(symbol, since=since, offset=offset, limit=limit)
        if first_total is None:
            first_total = page.reported_total
        elif page.reported_total != first_total:
            return CoverageResult(False, all_trades, first_total, len(all_trades),
                                   f"reported total drifted mid-pagination "
                                   f"({first_total} -> {page.reported_total}) — window unstable")
        all_trades.extend(page.trades)
        if page.next_offset is None:
            break
        offset = page.next_offset
    complete = len(all_trades) == (first_total or 0)
    reason = "" if complete else f"fetched {len(all_trades)} of reported {first_total}"
    return CoverageResult(complete, all_trades, first_total or 0, len(all_trades), reason)


def compute_safe_watermark(
    coverage: CoverageResult, *, now_ms: int, previous_watermark_ms: Optional[int],
    safety_margin_ms: int,
) -> Optional[int]:
    """The only function allowed to advance a persisted retrieval cursor.
    Never exceeds now_ms - safety_margin_ms, monotonically, regardless of
    what coverage claims — a trade hidden at read time gets every
    subsequent read within the margin to still be picked up. If coverage
    was not page-exhausted, the watermark does not advance at all this
    cycle. See execution_accounting_reference_model.py's identical function
    for the full residual-limitation discussion (a trade hidden LONGER than
    the margin can still be missed — inherent to any finite-margin REST
    polling protocol, not something this function can fix)."""
    if not coverage.complete:
        return previous_watermark_ms
    candidate = now_ms - safety_margin_ms
    if previous_watermark_ms is not None:
        candidate = max(candidate, previous_watermark_ms)
    return candidate


def is_watermark_confirmed(window_until_ms: int, *, now_ms: int, safety_margin_ms: int) -> bool:
    return now_ms - window_until_ms >= safety_margin_ms


@dataclass
class AuditResult:
    status: str   # "clean" | "violated" | "inconclusive"
    newly_discovered_trade_ids: "list[str]"
    reason: str = ""

    @property
    def violated(self) -> bool:
        return self.status == "violated"

    def to_readiness_flag(self) -> Optional[bool]:
        if self.status == "violated":
            return False
        if self.status == "clean":
            return True
        return None


def audit_historical_window(
    exchange: ExchangeAdapter, known_trade_ids: set[str], symbol: Optional[str], *,
    audit_since: Optional[str], audit_until_ms: int,
) -> AuditResult:
    """Re-queries an already-watermarked window directly against the
    exchange and checks whether it now reports any trade `known_trade_ids`
    doesn't already have — the genuine independent confirmation
    is_watermark_confirmed cannot provide on its own (that function only
    checks elapsed time against an assumed bound)."""
    try:
        coverage = retrieve_with_coverage_proof(exchange, symbol, since=audit_since)
    except Exception as exc:
        return AuditResult("inconclusive", [], reason=f"audit retrieval raised: {exc}")
    newly_discovered = [
        t.trade_id for t in coverage.trades
        if t.trade_id not in known_trade_ids and _ts_ms(t.exchange_timestamp) <= audit_until_ms
    ]
    if newly_discovered:
        reason = "" if coverage.complete else f"retrieval was also incomplete: {coverage.reason}"
        return AuditResult("violated", newly_discovered, reason=reason)
    if not coverage.complete:
        return AuditResult("inconclusive", [], reason=f"audit retrieval itself was incomplete: {coverage.reason}")
    return AuditResult("clean", [])


# ============================================================================
# 2. Balance consistency — an accounting identity over a COVERAGE-approved
#    set, using a real market-precision tolerance (design §5), never used
#    to infer coverage.
# ============================================================================

@dataclass
class BalanceCheckResult:
    consistent: bool
    expected_balance: float
    actual_balance: float
    residual: float


def check_balance_consistency(
    prior_balance: float, trades: "list[ObservedTrade]", deposits: "list[LedgerMovement]",
    withdrawals: "list[LedgerMovement]", fresh_balance: float, *, side_asset_is_quote: bool,
    quote: str, tolerance: float, fee_correction_deltas: "list[float]" = (),
) -> BalanceCheckResult:
    """side_asset_is_quote: whether the balance being checked is the quote
    currency (cash) that trades move (base-asset checks instead sum signed
    trade `amount`). tolerance must be derived from real market precision
    by the caller (design §5) — this function applies no implicit rounding
    allowance of its own beyond the passed tolerance."""
    delta = 0.0
    for t in trades:
        if side_asset_is_quote:
            trade_fee = t.fee_cost if t.fee_currency == quote else 0.0
            delta += (-t.cost - trade_fee) if t.side == "buy" else (t.cost - trade_fee)
        else:
            delta += t.amount if t.side == "buy" else -t.amount
    for d in deposits:
        delta += d.amount
    for w in withdrawals:
        delta += w.amount  # already signed negative by the adapter
    if side_asset_is_quote:
        for fd in fee_correction_deltas:
            delta -= fd  # a fee INCREASE (positive fd) reduces quote cash further
    expected = prior_balance + delta
    residual = fresh_balance - expected
    return BalanceCheckResult(abs(residual) <= tolerance, expected, fresh_balance, residual)


# ============================================================================
# 3. Causal ordering + position fold — same-timestamp ties resolved by the
#    one real invariant this spot bot has (running position never negative),
#    never by an opaque trade-id sort (design §4, review R2 finding 3).
# ============================================================================

def causal_order(trades: "list[ObservedTrade]") -> "Optional[list[ObservedTrade]]":
    by_symbol: "dict[str, list[ObservedTrade]]" = {}
    for t in trades:
        by_symbol.setdefault(t.symbol, []).append(t)

    resolved_by_symbol: "dict[str, list[ObservedTrade]]" = {}
    for symbol, group in by_symbol.items():
        group = sorted(group, key=lambda t: _ts_ms(t.exchange_timestamp))
        i = 0
        resolved: "list[ObservedTrade]" = []
        running_qty = 0.0
        while i < len(group):
            j = i
            while (j + 1 < len(group)
                   and _ts_ms(group[j + 1].exchange_timestamp) == _ts_ms(group[i].exchange_timestamp)):
                j += 1
            tied = group[i:j + 1]
            if len(tied) == 1:
                t = tied[0]
                running_qty += t.amount if t.side == "buy" else -t.amount
                if running_qty < -1e-9:
                    return None
                resolved.append(t)
            else:
                found = None
                for perm in itertools.permutations(tied):
                    q = running_qty
                    ok = True
                    for t in perm:
                        q += t.amount if t.side == "buy" else -t.amount
                        if q < -1e-9:
                            ok = False
                            break
                    if ok:
                        if found is not None and found != perm:
                            return None
                        found = perm
                if found is None:
                    return None
                resolved.extend(found)
                running_qty += sum((t.amount if t.side == "buy" else -t.amount) for t in tied)
            i = j + 1
        resolved_by_symbol[symbol] = resolved

    rank: "dict[str, int]" = {
        t.trade_id: i for group in resolved_by_symbol.values() for i, t in enumerate(group)
    }
    flat = [t for group in resolved_by_symbol.values() for t in group]
    flat.sort(key=lambda t: (_ts_ms(t.exchange_timestamp), t.symbol, rank[t.trade_id]))
    return flat


@dataclass
class FoldResult:
    final_qty: float
    avg_cost: float
    realized_pnl: float
    per_trade_pnl: "dict[str, float]"


def fold_position(trades_in_order: "list[ObservedTrade]") -> FoldResult:
    """Minimal FIFO-average-cost position fold. trades_in_order's fee_cost
    must already be the CURRENT (correction-aware) value — see
    effective_fee_cost() below and load_observed_trades_with_corrections."""
    qty = 0.0
    avg_cost = 0.0
    realized = 0.0
    per_trade: "dict[str, float]" = {}
    for t in trades_in_order:
        if t.side == "buy":
            new_qty = qty + t.amount
            avg_cost = ((avg_cost * qty) + t.cost + t.fee_cost) / new_qty if new_qty > 0 else 0.0
            qty = new_qty
        else:
            proceeds = t.cost - t.fee_cost
            cost_of_sold = avg_cost * t.amount
            pnl = proceeds - cost_of_sold
            realized += pnl
            per_trade[t.trade_id] = pnl
            qty -= t.amount
    return FoldResult(qty, avg_cost, realized, per_trade)


# ============================================================================
# Fee corrections — layered on the EXISTING fee_adjustments table
# (bot/data/trade_log.py), never a new fee table (design §4).
# ============================================================================

def fee_correction_adjustment_id(trade_id: str, revision: int) -> str:
    return f"{trade_id}:fee_correction:{revision}"


def next_fee_correction_revision(existing_adjustment_ids: "list[str]", trade_id: str) -> int:
    prefix = f"{trade_id}:fee_correction:"
    revisions = [
        int(a[len(prefix):]) for a in existing_adjustment_ids
        if a.startswith(prefix) and a[len(prefix):].isdigit()
    ]
    return (max(revisions) + 1) if revisions else 1


def effective_fee_cost(original_fee_cost: float, correction_deltas: "list[float]") -> float:
    """original_fee_cost (observed_trades.fee_cost, frozen forever) plus
    every fee_adjustments delta recorded for this trade_id, in insertion
    order — the CURRENT fee a reconstruction should use. Mirrors the
    reference model's load_observed_trades correction-aware read, but
    layered on the real fee_adjustments table's signed deltas rather than a
    replace-the-latest-value scheme (fee_adjustments.delta_fee is already
    additive by construction — bot/data/trade_log.py's docstring)."""
    return original_fee_cost + sum(correction_deltas)


# ============================================================================
# Recovery — reconstructing state purely from what's persisted.
# ============================================================================

def recover_position(trades: "list[ObservedTrade]") -> FoldResult:
    """trades must already have correction-aware fee_cost values applied
    (the caller — reconciliation.py's load helper — does this by joining
    against fee_adjustments before calling here). Raises ValueError if the
    trades cannot be causally ordered (a genuine data-integrity problem,
    never silently guessed past)."""
    ordered = causal_order(trades)
    if ordered is None:
        symbols = {t.symbol for t in trades}
        raise ValueError(
            f"observed trades for {symbols} cannot be causally ordered — "
            f"data integrity problem, not a recovery bug"
        )
    return fold_position(ordered)


# ============================================================================
# Legacy / straggler matching — exact conservation, blocks on ambiguity
# (design §3.3, review R2 finding 2). Used by BOTH migration.py (one-time
# historical backfill) and reconciliation.py (an ordinary fills row a
# straggler trade wasn't synchronously linked to at fill time, e.g. a
# broker-triggered native-stop fill or a fetch_my_trades visibility delay).
# ============================================================================

@dataclass
class UnlinkedFill:
    """Adapts a real `fills` row (dict, store.unlinked_fills) into the
    shape the conservation matcher needs.

    cost and order_id (accounting review follow-up, 2026-09-20, P1) are
    NOT optional decoration — without them the matcher below only conserves
    quantity+fee, which a real reproduction showed accepts a completely
    wrong candidate (same quantity and fee, unrelated price/order) as an
    unambiguous match. cost defaults to 0.0 and order_id to None for the
    rare legacy row where neither is available (true pre-order-tracking
    history) — see match_legacy_fill's docstring for exactly how those
    defaults degrade the check rather than silently skipping it."""
    fill_id: int
    symbol: str
    side: str                  # "buy" / "sell" (lowercased from fills.side)
    quantity: float
    fee_cost: float
    window_start_ms: int
    window_end_ms: int
    cost: float = 0.0
    order_id: "str | None" = None

    @classmethod
    def from_fills_row(cls, row: dict, *, window_s: float = 300.0) -> "UnlinkedFill":
        ts_ms = _ts_ms(row["timestamp"])
        return cls(
            fill_id=row["id"], symbol=row["symbol"], side=(row["side"] or "").lower(),
            quantity=float(row["quantity"] or 0.0), fee_cost=float(row["fee_cost"] or 0.0),
            window_start_ms=ts_ms - int(window_s * 1000), window_end_ms=ts_ms + int(window_s * 1000),
            cost=float(row["value"] or 0.0), order_id=(row.get("order_id") or None),
        )


@dataclass
class MatchResult:
    fill_id: int
    matched_trade_ids: "list[str]"
    blocked: bool
    reason: str = ""
    candidate_trade_ids: "list[str]" = field(default_factory=list)


def match_legacy_fill(
    row: UnlinkedFill, candidate_trades: "list[ObservedTrade]", *,
    tolerance: float = 1e-6, cost_tolerance: float = 1e-4,
    already_linked: "frozenset[str] | set[str]" = frozenset(),
) -> MatchResult:
    """Finds a SUBSET of candidate_trades (same symbol/side, within the
    row's time window) whose summed quantity, fee, AND cost (notional,
    quantity*price) EXACTLY matches the fills row's own totals (within
    tolerance) — never a nearest-timestamp proximity guess (the weakness of
    reconcile_ledger.py's existing matcher, which checks only
    timestamp+side+symbol proximity with no amount check at all). No match,
    or more than one disjoint subset matching equally well, BLOCKS for
    manual resolution rather than guessing — item 8's explicit requirement.

    cost conservation was added after an accounting review follow-up
    (2026-09-20, P1) reproduced quantity+fee-only matching silently
    accepting a WRONG candidate: a quantity-1 BUY at $100 matched a sole
    candidate priced at $900 (same quantity, same zero fee, different
    order) with blocked=False. Cost uses a looser cost_tolerance than
    quantity/fee (float products of two already-rounded numbers compound
    more rounding noise) but is still tight enough to catch a real
    misattribution, which is off by orders of magnitude, not cents.

    When row.order_id is known (fills.order_id populated), the candidate
    pool is narrowed to EXACTLY that order_id, unconditionally — a real,
    exact disambiguator when available. This must never fall back to the
    unfiltered pool when the narrowed one comes up empty (a second review
    pass's own P1 finding, 2026-09-20: the first version of this fix did
    exactly that — `if order_pool: pool = order_pool` — so a known order_id
    with no matching candidate silently matched against a completely
    UNRELATED order instead, which is worse than not narrowing at all).
    A legacy row with no order_id (the common case for true pre-order-
    tracking history) still requires full quantity+fee+cost conservation
    with no narrowing; this is a strict ADDITION to the existing check,
    never a replacement for it, so it only prevents matches the old check
    would have wrongly allowed, never accepts anything the old check
    would have refused."""
    pool = [
        t for t in candidate_trades
        if t.symbol == row.symbol and t.side == row.side and t.trade_id not in already_linked
        and row.window_start_ms <= _ts_ms(t.exchange_timestamp) <= row.window_end_ms
    ]
    if row.order_id:
        pool = [t for t in pool if t.order_id == row.order_id]
    pool_ids = [t.trade_id for t in pool]
    if not pool:
        return MatchResult(row.fill_id, [], True, "no candidate trades in window", pool_ids)
    n = len(pool)
    if n > 20:
        return MatchResult(row.fill_id, [], True,
                            f"candidate pool too large ({n}) for the bounded matcher — manual resolution required",
                            pool_ids)
    matches: "list[tuple[str, ...]]" = []
    for r in range(1, n + 1):
        for combo in itertools.combinations(pool, r):
            qty = sum(t.amount for t in combo)
            fee = sum(t.fee_cost for t in combo)
            cost = sum(t.cost for t in combo)
            if (abs(qty - row.quantity) < tolerance and abs(fee - row.fee_cost) < tolerance
                    and abs(cost - row.cost) < cost_tolerance):
                matches.append(tuple(sorted(t.trade_id for t in combo)))
    unique_matches = set(matches)
    if len(unique_matches) == 0:
        return MatchResult(row.fill_id, [], True, "no subset conserves quantity/fee/cost exactly", pool_ids)
    if len(unique_matches) > 1:
        return MatchResult(row.fill_id, [], True,
                            f"{len(unique_matches)} disjoint subsets all conserve totals — ambiguous",
                            pool_ids)
    return MatchResult(row.fill_id, list(next(iter(unique_matches))), False, candidate_trade_ids=pool_ids)


# ============================================================================
# Readiness — reported as separate properties, never merged into one
# boolean (design §9 step 5 / reference-model review R2's "bounded next
# step").
# ============================================================================

@dataclass
class ReadinessReport:
    coverage_complete: bool
    watermark_confirmed: bool
    balance_consistent: bool
    ledger_delivery_ok: bool
    coverage_reason: str = ""
    balance_residual: Optional[float] = None
    historical_audit_clean: Optional[bool] = None

    @property
    def ready(self) -> bool:
        if self.historical_audit_clean is False:
            return False
        return (self.coverage_complete and self.watermark_confirmed
                and self.balance_consistent and self.ledger_delivery_ok)

    def explain(self) -> str:
        if self.historical_audit_clean is False:
            return ("not ready — audit found trade(s) the persisted watermark had "
                    "already passed: the bounded-delay assumption was violated for "
                    "this window; reconciliation is a human decision, not automatic")
        if self.ready:
            if self.historical_audit_clean is True:
                return "ready: coverage, balance, and ledger/delivery all consistent, and an audit re-check found nothing new"
            return ("ready (conditional on the assumed visibility bound — coverage "
                    "complete, watermark aged past the safety margin, balance and "
                    "ledger/delivery consistent, but NOT independently audited)")
        failing = []
        if not self.coverage_complete:
            failing.append(f"coverage ({self.coverage_reason})")
        if self.coverage_complete and not self.watermark_confirmed:
            failing.append("watermark (page-exhausted but not yet aged past the safety margin)")
        if not self.balance_consistent:
            failing.append(f"balance (residual={self.balance_residual})")
        if not self.ledger_delivery_ok:
            failing.append("ledger/delivery")
        return "not ready — failing: " + ", ".join(failing)


def assess_readiness(coverage: CoverageResult, balance: "Optional[BalanceCheckResult]",
                      ledger_delivery_ok: bool, *, watermark_confirmed: bool,
                      historical_audit_clean: Optional[bool] = None) -> ReadinessReport:
    balance_ok = bool(balance and balance.consistent)
    residual = balance.residual if balance else None
    return ReadinessReport(
        coverage_complete=coverage.complete,
        watermark_confirmed=watermark_confirmed,
        balance_consistent=balance_ok,
        ledger_delivery_ok=ledger_delivery_ok,
        coverage_reason=coverage.reason,
        historical_audit_clean=historical_audit_clean,
        balance_residual=residual,
    )


def new_checkpoint_id() -> str:
    return str(uuid.uuid4())
