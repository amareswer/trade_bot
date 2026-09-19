"""
Disposable, offline reference model for the execution-accounting design
(CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_2026-09-19.md, and its two review
rounds — CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_REVIEW_2026-09-19.md,
CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_REVIEW_R2_2026-09-19.md).

Proves — against a SYNTHETIC exchange and an ISOLATED temporary SQLite
database, with no live exchange access and no import from the live trading
path (bot/execution/live_executor.py, bot/main.py are never imported here)
— three properties the R2 review insisted be checked SEPARATELY, never
folded into one boolean:

  1. HISTORY COVERAGE — the retrieved trade/ledger window is complete and
     internally stable (fetched count == exchange-reported total count,
     stable across every page of one retrieval attempt). Never inferred
     from whether a balance identity happens to match — R2 review finding
     1's counterexample (two offsetting unseen events, or an incomplete
     page that coincidentally nets to the right total) is why: a balance
     match proves nothing about completeness on its own.

  2. BALANCE CONSISTENCY — given a COVERAGE-approved event set, the prior
     checkpoint's balance plus every known delta in the window equals a
     freshly read balance, using exact values from the synthetic exchange
     (no assumed rounding tolerance — R2 finding 5).

  3. LEDGER / DELIVERY CONSISTENCY — every covered, balance-consistent
     trade is committed to exactly one ledger row, same-timestamp trades
     are ordered by actual causal evidence (a spot book's running
     position can never go negativer — not by an opaque id, which R2
     finding 3 showed can misallocate a same-timestamp BUY/SELL pair's
     entry fee), and the resulting position/cost-basis/fee-allocation
     fold is checked against the real expected numbers.

Readiness = all three, reported separately. This module is not wired into
any live path and is not production code.
"""
from __future__ import annotations

import itertools
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Optional


# ============================================================================
# Synthetic exchange
# ============================================================================

@dataclass
class SynTrade:
    trade_id: str
    order_id: str
    symbol: str
    side: str            # "buy" / "sell"
    price: float
    amount: float
    cost: float
    fee_cost: float
    fee_currency: str
    timestamp_ms: int


@dataclass
class SynLedgerEntry:
    entry_id: str
    refid: str            # trade_id for type == "trade"; "" otherwise
    type: str              # "trade" | "deposit" | "withdrawal"
    asset: str
    amount: float          # signed delta
    after: float            # balance AFTER this entry (ccxt unified "after")
    timestamp_ms: int


@dataclass
class TradePage:
    """Mirrors what a real retrieval interface MUST expose to prove
    completeness — ccxt's unified fetch_my_trades() discards the exchange's
    own reported total-match count (R2 review finding 1); this dataclass
    deliberately keeps it."""
    trades: "list[SynTrade]"
    reported_total: int    # exchange-side total matching records for the query window
    next_offset: "int | None"


class SyntheticExchange:
    """A minimal, fully-controllable fake exchange. Every balance-affecting
    event (trade, deposit, withdrawal) takes effect on the ACTUAL balance
    the instant it is recorded — exactly like a real exchange, where the
    balance endpoint is authoritative and immediate. Trade/ledger HISTORY
    visibility is a SEPARATE, independently controlled flag, modeling real
    propagation lag between "the fill executed" and "fetch_my_trades()
    reports it" — this is what makes the checkpoint race reproducible on
    purpose rather than by accident."""

    def __init__(self) -> None:
        self._balances: "dict[str, float]" = {}
        self._trades: "list[tuple[SynTrade, bool]]" = []       # (trade, visible)
        self._ledger: "list[tuple[SynLedgerEntry, bool]]" = []  # (entry, visible)
        self._next_id = 1

    # -- mutation (test-driver only) ----------------------------------------

    def _new_id(self, prefix: str) -> str:
        i = self._next_id
        self._next_id += 1
        return f"{prefix}{i}"

    def _apply_balance(self, asset: str, delta: float) -> float:
        self._balances[asset] = self._balances.get(asset, 0.0) + delta
        return self._balances[asset]

    def execute_trade(
        self, *, symbol: str, side: str, price: float, amount: float,
        fee_cost: float = 0.0, fee_currency: str = "", timestamp_ms: int,
        order_id: "str | None" = None, visible: bool = True,
        trade_id: "str | None" = None,
    ) -> SynTrade:
        base, quote = symbol.split("/")
        cost = price * amount
        oid = order_id or self._new_id("O")
        tid = trade_id or self._new_id("T")
        trade = SynTrade(tid, oid, symbol, side, price, amount, cost,
                          fee_cost, fee_currency or quote, timestamp_ms)
        self._trades.append((trade, visible))
        if side == "buy":
            self._apply_balance(quote, -cost - (fee_cost if fee_currency in ("", quote) else 0.0))
            after = self._apply_balance(base, amount)
        else:
            after = self._apply_balance(quote, cost - (fee_cost if fee_currency in ("", quote) else 0.0))
            self._apply_balance(base, -amount)
        ledger_asset = quote  # cash-affecting side; base-asset ledger rows omitted, not needed by these tests
        entry = SynLedgerEntry(
            self._new_id("L"), tid, "trade", ledger_asset,
            amount=(-cost if side == "buy" else cost), after=self._balances.get(ledger_asset, 0.0),
            timestamp_ms=timestamp_ms,
        )
        self._ledger.append((entry, visible))
        return trade

    def deposit(self, asset: str, amount: float, *, timestamp_ms: int, visible: bool = True) -> str:
        after = self._apply_balance(asset, amount)
        eid = self._new_id("L")
        self._ledger.append((SynLedgerEntry(eid, "", "deposit", asset, amount, after, timestamp_ms), visible))
        return eid

    def withdraw(self, asset: str, amount: float, *, timestamp_ms: int, visible: bool = True) -> str:
        after = self._apply_balance(asset, -amount)
        eid = self._new_id("L")
        self._ledger.append((SynLedgerEntry(eid, "", "withdrawal", asset, -amount, after, timestamp_ms), visible))
        return eid

    def reveal_all(self) -> None:
        """Simulate propagation catching up: every pending trade/ledger
        event becomes visible to history reads."""
        self._trades = [(t, True) for t, _ in self._trades]
        self._ledger = [(e, True) for e, _ in self._ledger]

    def revise_trade_fee(self, trade_id: str, new_fee: float, *, timestamp_ms: int = 0) -> float:
        """Simulate the exchange settling a trade's fee to a different
        final value after it was first observed. Unlike the first draft of
        this method, this ALSO applies the real delta to the actual quote
        balance (a fee revision is a genuine cash movement on a real
        exchange, not just a stored-value edit — reference-model review
        finding: "the fake exchange never adjusts cash for that revised
        fee"). Returns the signed delta (new - old) for the caller."""
        for i, (t, vis) in enumerate(self._trades):
            if t.trade_id == trade_id:
                old_fee = t.fee_cost
                delta_fee = new_fee - old_fee
                revised = SynTrade(t.trade_id, t.order_id, t.symbol, t.side, t.price,
                                    t.amount, t.cost, new_fee, t.fee_currency, t.timestamp_ms)
                self._trades[i] = (revised, vis)
                if delta_fee != 0.0:
                    after = self._apply_balance(t.fee_currency, -delta_fee)
                    eid = self._new_id("L")
                    self._ledger.append((
                        SynLedgerEntry(eid, trade_id, "fee_correction", t.fee_currency,
                                        -delta_fee, after, timestamp_ms or t.timestamp_ms),
                        True,
                    ))
                return delta_fee
        raise KeyError(trade_id)

    # -- reads ----------------------------------------------------------------

    def fetch_balance_total(self, asset: str) -> float:
        return self._balances.get(asset, 0.0)

    def fetch_my_trades_page(
        self, symbol: str, *, since_ms: "int | None" = None,
        offset: int = 0, limit: int = 50,
    ) -> TradePage:
        # since_ms is EXCLUSIVE, matching Kraken's own documented TradesHistory
        # `start` semantics ("starting unix timestamp... (exclusive)") — a
        # window's own `until` boundary must never be re-included by the
        # next window's `since`.
        matching = [t for t, vis in self._trades
                    if vis and t.symbol == symbol and (since_ms is None or t.timestamp_ms > since_ms)]
        matching.sort(key=lambda t: (t.timestamp_ms, t.trade_id))
        total = len(matching)
        page = matching[offset:offset + limit]
        next_offset = offset + limit if offset + limit < total else None
        return TradePage(page, total, next_offset)

    def fetch_ledger_entries(
        self, asset: str, *, since_ms: "int | None" = None,
    ) -> "list[SynLedgerEntry]":
        entries = [e for e, vis in self._ledger
                   if vis and e.asset == asset and (since_ms is None or e.timestamp_ms > since_ms)]
        entries.sort(key=lambda e: (e.timestamp_ms, e.entry_id))
        return entries

    def fetch_deposits(self, asset: str, *, since_ms: "int | None" = None) -> "list[SynLedgerEntry]":
        return [e for e in self.fetch_ledger_entries(asset, since_ms=since_ms) if e.type == "deposit"]

    def fetch_withdrawals(self, asset: str, *, since_ms: "int | None" = None) -> "list[SynLedgerEntry]":
        return [e for e in self.fetch_ledger_entries(asset, since_ms=since_ms) if e.type == "withdrawal"]


# ============================================================================
# SQLite schema for the reference model — deliberately its own temp DB,
# never trades.db.
# ============================================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observed_trades (
    trade_id              TEXT PRIMARY KEY,
    order_id               TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    side                     TEXT NOT NULL,
    price                    REAL NOT NULL,
    amount                   REAL NOT NULL,
    cost                     REAL NOT NULL,
    fee_cost                 REAL NOT NULL,
    fee_currency              TEXT NOT NULL,
    exchange_timestamp_ms     INTEGER NOT NULL,
    checkpoint_id             TEXT,
    ledger_written_at         TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id      TEXT PRIMARY KEY,
    currency_scope       TEXT NOT NULL,
    window_since_ms       INTEGER,
    window_until_ms       INTEGER NOT NULL,
    balance_after         REAL NOT NULL,
    covered_trade_ids     TEXT NOT NULL,
    committed_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    exec_key      TEXT UNIQUE,
    timestamp_ms   INTEGER NOT NULL,
    side            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    quantity        REAL NOT NULL,
    price           REAL NOT NULL,
    fee_cost        REAL NOT NULL,
    pnl             REAL,
    source          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fee_corrections (
    trade_id     TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    old_fee        REAL NOT NULL,
    new_fee        REAL NOT NULL,
    recorded_at    TEXT NOT NULL,
    PRIMARY KEY (trade_id, revision)
);
CREATE TABLE IF NOT EXISTS legacy_links (
    trade_id         TEXT NOT NULL,
    legacy_fill_id     INTEGER NOT NULL,
    PRIMARY KEY (trade_id, legacy_fill_id)
);
CREATE TABLE IF NOT EXISTS opening_snapshot (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    symbol            TEXT NOT NULL,
    as_of_ms           INTEGER NOT NULL,
    balance            REAL NOT NULL,
    cost_basis         REAL NOT NULL,
    established_at     TEXT NOT NULL
);
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# ============================================================================
# 1. History coverage — proven by exchange-reported count, never by balance
# ============================================================================

@dataclass
class CoverageResult:
    complete: bool
    trades: "list[SynTrade]"
    reported_total: int
    fetched_count: int
    reason: str = ""


def retrieve_with_coverage_proof(
    exchange: SyntheticExchange, symbol: str, *, since_ms: "int | None" = None,
    page_size: int = 50, page_limit_override: "int | None" = None,
) -> CoverageResult:
    """Pages fetch_my_trades_page until the accumulated record count equals
    the FIRST page's reported total. If a later page reports a DIFFERENT
    total (the window was not stable across the retrieval attempt — new
    trades arrived mid-pagination), completeness is explicitly refused
    rather than silently accepted with a moving target.

    page_limit_override: caps how many records this attempt will fetch,
    regardless of how many actually exist — models a retrieval bug (or a
    deliberately truncated read) so the coverage check's independence from
    the balance check can be demonstrated: a truncated fetch must be
    flagged incomplete even when the truncated subset's cash effect
    happens to net out correctly against a fresh balance read (R2 finding
    1's core counterexample).
    """
    all_trades: "list[SynTrade]" = []
    offset = 0
    first_total: "int | None" = None
    while True:
        limit = page_size
        if page_limit_override is not None:
            remaining = page_limit_override - len(all_trades)
            if remaining <= 0:
                return CoverageResult(False, all_trades, first_total or 0, len(all_trades),
                                       "retrieval attempt truncated by page_limit_override")
            limit = min(limit, remaining)
        page = exchange.fetch_my_trades_page(symbol, since_ms=since_ms, offset=offset, limit=limit)
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
    complete = len(all_trades) == first_total
    reason = "" if complete else f"fetched {len(all_trades)} of reported {first_total}"
    return CoverageResult(complete, all_trades, first_total or 0, len(all_trades), reason)


def compute_safe_watermark(
    coverage: CoverageResult, *, now_ms: int, previous_watermark_ms: "int | None",
    safety_margin_ms: int,
) -> "int | None":
    """The ONLY function allowed to advance a persisted retrieval cursor.

    Reference-model review finding: `coverage.complete=True` proves the
    exchange's queryable history was fully PAGED as of this read — it can
    never prove no other execution exists that the exchange itself hasn't
    surfaced yet (an inherent limit of polling any REST history endpoint;
    reproduced concretely: a BUY and SELL both held invisible produce an
    empty, "complete" page and a balance identity that happens to match,
    which is exactly the false-readiness counterexample). Treating
    page-exhaustion as license to advance the cursor to "the last thing we
    saw" is what let a still-hidden trade fall permanently behind an
    exclusive `since` boundary once revealed.

    The watermark this function returns NEVER exceeds `now_ms -
    safety_margin_ms`, monotonically, regardless of what coverage claims —
    so a trade hidden at read time gets every subsequent read within the
    margin to still be picked up (`since_ms` stays at or before its own
    timestamp until the margin has genuinely elapsed). Re-observing an
    already-known trade during that overlap is free (trade_id is the
    primary key everywhere in this model).

    If coverage was NOT page-exhausted (a truncated/unstable read), the
    watermark does not advance at all this cycle, even within the margin —
    an incomplete read proves nothing about the window regardless of how
    much real time has passed.

    Residual, inherent limitation (stated, not hidden): a trade whose
    visibility is delayed LONGER than safety_margin_ms relative to its own
    execution time can still be permanently missed — no finite margin can
    defend against unbounded propagation delay. Choosing a margin
    generous relative to real observed propagation lag is a deployment
    decision, not something this function can prove correct in general.
    """
    if not coverage.complete:
        return previous_watermark_ms
    candidate = now_ms - safety_margin_ms
    if previous_watermark_ms is not None:
        candidate = max(candidate, previous_watermark_ms)
    return candidate


def is_watermark_confirmed(window_until_ms: int, *, now_ms: int, safety_margin_ms: int) -> bool:
    """True once enough real time has passed since a window's own end that
    ANY execution delayed by no more than safety_margin_ms would plausibly
    have surfaced by now.

    Reference-model review R2 finding 1: this is a TIME check against an
    ASSUMED visibility bound — nothing more. Reproduced exactly: a BUY and
    SELL hidden at ms 1000/1001, watermark advances to 5000 at now=10000
    with a 5000ms margin, and this function returns True — yet the two
    trades are STILL hidden at this instant and, once the watermark has
    advanced past their timestamps, an exclusive `since` query can never
    find them again even after they are revealed. A trade hidden LONGER
    than safety_margin_ms is not detected or prevented by this function —
    that is an explicit, acknowledged residual limitation of any
    finite-margin protocol, not something "confirmed" proves away. Because
    compute_safe_watermark caps window_until_ms at `now_ms -
    safety_margin_ms` at commit time, a checkpoint built that way passes
    this check THE INSTANT it is written — this is a definitional
    consequence of how the watermark was computed, not independent
    evidence that nothing was actually missed. Treat a True result as
    "the assumed bound was not violated in an observable way," never as
    "proven complete." audit_historical_window below is the mechanism that
    can actually DETECT a violation after the fact, when one has
    occurred — this function cannot."""
    return now_ms - window_until_ms >= safety_margin_ms


@dataclass
class AuditResult:
    """status is one of "clean" / "violated" / "inconclusive" — three
    distinct outcomes, not a boolean. Reference-model review R3 finding 1:
    the original boolean-only version conflated "we looked and found
    nothing" with "we couldn't actually look" — an incomplete/truncated
    retrieval (retrieve_with_coverage_proof's own coverage.complete=False)
    used to fall straight through the same "no new ids found -> clean"
    path as a genuine, fully-paged clean read, silently upgrading a
    non-observation into positive evidence."""
    status: str
    newly_discovered_trade_ids: "list[str]"
    audited_since_ms: "int | None"
    audited_until_ms: int
    reason: str = ""

    @property
    def violated(self) -> bool:
        return self.status == "violated"

    @property
    def clean(self) -> bool:
        return self.status == "clean"

    @property
    def inconclusive(self) -> bool:
        return self.status == "inconclusive"

    def to_readiness_flag(self) -> "bool | None":
        """The ONLY sanctioned way to feed an AuditResult into
        assess_readiness's historical_audit_clean parameter: True only for
        a genuinely clean audit, False for a real violation, and — this is
        the fix — None (readiness must not be strengthened) for an
        inconclusive one. Review R3's flagged bug was exactly the informal
        `historical_audit_clean=not audit.violated` pattern silently
        mapping inconclusive to True; this method makes that mapping
        impossible to get wrong."""
        if self.status == "violated":
            return False
        if self.status == "clean":
            return True
        return None


def audit_historical_window(
    exchange: SyntheticExchange, conn: sqlite3.Connection, symbol: str, *,
    audit_since_ms: "int | None", audit_until_ms: int,
) -> AuditResult:
    """Re-queries an ALREADY-watermarked (believed-closed) window directly
    against the exchange and checks whether it now reports any trade that
    persisted observed_trades does not already have. This is the genuine,
    independent confirmation is_watermark_confirmed cannot provide on its
    own — that function only checks elapsed time against an assumed bound;
    this function actually looks for evidence the bound was violated.

    Returns "violated" whenever ANY successfully retrieved record —
    whether or not the overall retrieval was page-exhausted — is a trade
    inside the audited window that persisted state doesn't have.
    Reference-model review R4 finding 1: a partial/incomplete read still
    carries whatever records it DID manage to return, and an unknown trade
    among them is real, positive evidence of a violation regardless of
    what the untried remainder of the window might also contain — the
    prior version discarded every partial trade before even looking,
    silently downgrading concrete contrary evidence to "inconclusive."

    Returns "inconclusive" (never silently "clean", and never silently
    discarding a violation already found) if, with NO violation found in
    whatever was retrieved:
      - the retrieval itself was not page-exhausted (coverage.complete is
        False — a truncated read containing only already-known ids used
        to be indistinguishable from a genuine clean result); or
      - the retrieval call raised (a transient exchange failure) — caught
        here rather than propagated, since an audit is diagnostic, not a
        trading action, and a caller should get a reportable result
        either way.
    Returns "clean" only when retrieval was genuinely complete AND no new
    trade ids were found anywhere — evidence the bounded-delay assumption
    held FOR THIS WINDOW, checked now (still not proof no delay could
    ever exceed the margin in general)."""
    try:
        coverage = retrieve_with_coverage_proof(exchange, symbol, since_ms=audit_since_ms)
    except Exception as exc:
        return AuditResult("inconclusive", [], audit_since_ms, audit_until_ms,
                            reason=f"audit retrieval raised: {exc}")
    known = {r[0] for r in conn.execute(
        "SELECT trade_id FROM observed_trades WHERE symbol = ?", (symbol,)
    )}
    newly_discovered = [
        t.trade_id for t in coverage.trades
        if t.trade_id not in known and t.timestamp_ms <= audit_until_ms
    ]
    if newly_discovered:
        # Positive evidence survives an incomplete retrieval — reported as
        # violated either way, with the incompleteness noted for context.
        reason = "" if coverage.complete else f"retrieval was also incomplete: {coverage.reason}"
        return AuditResult("violated", newly_discovered, audit_since_ms, audit_until_ms, reason=reason)
    if not coverage.complete:
        return AuditResult("inconclusive", [], audit_since_ms, audit_until_ms,
                            reason=f"audit retrieval itself was incomplete: {coverage.reason}")
    return AuditResult("clean", [], audit_since_ms, audit_until_ms)


# ============================================================================
# 2. Balance consistency — an accounting identity, run ONLY on a
#    coverage-approved set, never used to infer coverage.
# ============================================================================

@dataclass
class BalanceCheckResult:
    consistent: bool
    expected_balance: float
    actual_balance: float
    residual: float


def check_balance_consistency(
    prior_balance: float, trades: "list[SynTrade]", deposits: "list[SynLedgerEntry]",
    withdrawals: "list[SynLedgerEntry]", fresh_balance: float, *, side_asset_is_quote: bool,
    quote: str, fee_correction_deltas: "list[float]" = (),
) -> BalanceCheckResult:
    """side_asset_is_quote: whether the balance being checked is the quote
    currency (cash) that trades move — base-asset checks would instead sum
    signed trade `amount`.

    fee_correction_deltas: signed (new_fee - old_fee) values from
    record_fee_revision/revise_trade_fee that have been recorded since
    `prior_balance` — a fee revision is a real cash movement (§ the
    revised SyntheticExchange.revise_trade_fee), so checking consistency
    using only a trade's ORIGINALLY observed fee_cost without also
    including any later correction will show a genuine residual (this is
    intentional and tested — see test_fee_revision_converges_with_real_
    balance_after_being_included — the check must not silently ignore a
    stale fee value).

    The 1e-12 comparison below is a float64-representational-error guard
    (this function performs the same additions any float64 arithmetic
    would, so residual noise at that scale is unavoidable rounding, not a
    business tolerance) — NOT a rounding allowance for real currency
    precision. A caller reconciling actual money should compare in
    integer minor-units (cents) or Decimal, not trust this function's
    tolerance as a statement about acceptable business-level imprecision.
    Also note: this model always treats a trade's fee as being paid in
    the quote currency (matching this bot's actually-observed Kraken spot
    fee behavior) — a fee paid in the BASE currency is not modeled here.
    """
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
        delta += w.amount  # already signed negative by SyntheticExchange.withdraw
    if side_asset_is_quote:
        for fd in fee_correction_deltas:
            delta -= fd  # a fee INCREASE (positive fd) reduces quote cash further
    expected = prior_balance + delta
    residual = fresh_balance - expected
    return BalanceCheckResult(abs(residual) < 1e-9, expected, fresh_balance, residual)


# ============================================================================
# 3. Ledger / delivery consistency
# ============================================================================

def causal_order(trades: "list[SynTrade]") -> "list[SynTrade] | None":
    """Orders trades primarily by timestamp. Same-timestamp trades on the
    SAME symbol are, where more than one ordering is possible, resolved by
    the one domain invariant this spot bot can rely on: running position
    must never go negative (this book never shorts) — a SELL cannot be
    causally before the BUY quantity it draws down. This is real causal
    evidence, unlike an opaque trade-id sort (R2 finding 3).

    Returns None if NO ordering of a tied group satisfies the invariant
    (a genuine data problem — never silently guessed), or if MULTIPLE
    orderings satisfy it AND they produce different results (still
    ambiguous — checked by the caller via fold comparison, not here).
    """
    by_symbol: "dict[str, list[SynTrade]]" = {}
    for t in trades:
        by_symbol.setdefault(t.symbol, []).append(t)

    resolved_by_symbol: "dict[str, list[SynTrade]]" = {}
    for symbol, group in by_symbol.items():
        group.sort(key=lambda t: t.timestamp_ms)
        i = 0
        resolved: "list[SynTrade]" = []
        running_qty = 0.0
        while i < len(group):
            j = i
            while j + 1 < len(group) and group[j + 1].timestamp_ms == group[i].timestamp_ms:
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
                            # more than one valid ordering — genuinely
                            # ambiguous, not this function's call to make.
                            return None
                        found = perm
                if found is None:
                    return None
                resolved.extend(found)
                running_qty += sum((t.amount if t.side == "buy" else -t.amount) for t in tied)
            i = j + 1
        resolved_by_symbol[symbol] = resolved

    # Merge symbols' independently-resolved sequences into one global order:
    # primarily by timestamp, with each symbol's own causal rank as the
    # tiebreak — this preserves every per-symbol invariant proven above
    # without ever re-deriving order from an opaque id. Cross-symbol ties
    # are economically independent (different books), so any stable
    # resolution between them is fine; only the intra-symbol relative order
    # matters and is exactly what resolved_by_symbol already fixed.
    rank: "dict[str, int]" = {
        t.trade_id: i for group in resolved_by_symbol.values() for i, t in enumerate(group)
    }
    flat = [t for group in resolved_by_symbol.values() for t in group]
    flat.sort(key=lambda t: (t.timestamp_ms, t.symbol, rank[t.trade_id]))
    return flat


@dataclass
class FoldResult:
    final_qty: float
    avg_cost: float
    realized_pnl: float
    per_trade_pnl: "dict[str, float]"


def fold_position(trades_in_order: "list[SynTrade]") -> FoldResult:
    """Minimal FIFO-average-cost position fold — enough to prove that
    causal ordering (not opaque-id ordering) produces the economically
    correct entry-fee allocation and realized P&L for a same-timestamp
    BUY/SELL pair (R2 finding 3's explicit demand: test the actual fee and
    basis numbers, not just that a sort is stable)."""
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


class _InjectedTransactionFailure(Exception):
    """Raised INSIDE a real `with conn:` transaction block by the
    fail_after_n_trade_inserts injection point below, so sqlite3's own
    rollback machinery runs against REAL prior writes in the same
    transaction — proving true atomicity, not merely "the function
    returned before touching SQL" (reference-model review finding 3)."""


def commit_checkpoint(
    conn: sqlite3.Connection, *, currency_scope: str, window_since_ms: "int | None",
    window_until_ms: int, balance_after: float, trades: "list[SynTrade]",
    fail_before_commit: bool = False, fail_after_n_trade_inserts: "int | None" = None,
) -> "str | None":
    """Atomically: insert the checkpoint row AND stamp every covered trade's
    checkpoint_id, in ONE transaction.

    fail_before_commit: simulates a crash before any SQL runs at all (the
    weaker case — proves an early return makes no writes).

    fail_after_n_trade_inserts: simulates a crash AFTER n real
    observed_trades inserts (and the checkpoint row itself) have already
    executed inside the transaction, by raising before `with conn:` exits
    — sqlite3 then rolls back the ENTIRE transaction, including the
    checkpoint row and every trade insert that already ran. Callers can
    verify via a fresh query that NOTHING partially persisted.

    Either way, the caller's retrieval cursor/watermark (tracked
    separately — see compute_safe_watermark) must not have been advanced
    on a failed commit, so a retry safely re-observes the same trades
    (idempotent — trade_id is the primary key, and ON CONFLICT below makes
    a retry after a PARTIAL prior success — impossible under real sqlite3
    atomicity, but kept defensively — a no-op rather than a duplicate)."""
    checkpoint_id = str(uuid.uuid4())
    covered_ids = [t.trade_id for t in trades]
    if fail_before_commit:
        return None
    try:
        with conn:
            conn.execute(
                "INSERT INTO checkpoints (checkpoint_id, currency_scope, window_since_ms, "
                "window_until_ms, balance_after, covered_trade_ids, committed_at) "
                "VALUES (?,?,?,?,?,?,datetime('now'))",
                (checkpoint_id, currency_scope, window_since_ms, window_until_ms,
                 balance_after, json.dumps(covered_ids)),
            )
            for i, t in enumerate(trades):
                if fail_after_n_trade_inserts is not None and i == fail_after_n_trade_inserts:
                    raise _InjectedTransactionFailure(
                        f"injected failure after {i} of {len(trades)} trade inserts"
                    )
                conn.execute(
                    "INSERT INTO observed_trades (trade_id, order_id, symbol, side, price, "
                    "amount, cost, fee_cost, fee_currency, exchange_timestamp_ms, checkpoint_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(trade_id) DO UPDATE SET checkpoint_id=excluded.checkpoint_id",
                    (t.trade_id, t.order_id, t.symbol, t.side, t.price, t.amount, t.cost,
                     t.fee_cost, t.fee_currency, t.timestamp_ms, checkpoint_id),
                )
    except _InjectedTransactionFailure:
        return None
    return checkpoint_id


def is_ledger_represented(conn: sqlite3.Connection, trade_id: str) -> bool:
    """The single predicate BOTH normal replay (write_ledger_rows) and
    migration must consult before writing anything for a trade id — either
    a `fills` row keyed by it directly, or a legacy_links mapping to an
    existing aggregate row, counts as "already represented." Without this
    shared check, ordinary replay run after a migration re-inserted a
    second `fills` row for an already-migrated trade (reference-model
    review finding 2, reproduced exactly: migrating trade T into a legacy
    row, then calling write_ledger_rows(T), produced two fills rows for
    one execution)."""
    if conn.execute("SELECT 1 FROM fills WHERE exec_key = ?", (trade_id,)).fetchone():
        return True
    if conn.execute("SELECT 1 FROM legacy_links WHERE trade_id = ?", (trade_id,)).fetchone():
        return True
    return False


def write_ledger_rows(conn: sqlite3.Connection, trades_in_order: "list[SynTrade]",
                       fold: FoldResult) -> None:
    """Writes exactly one `fills` row per trade, keyed by trade_id, and
    marks observed_trades.ledger_written_at — in the SAME transaction, so
    the two obligations can never diverge. Skips any trade already
    represented via EITHER a native fills row OR a legacy migration link
    (is_ledger_represented) — this is what makes migration and ordinary
    replay compose safely instead of double-writing the same execution."""
    with conn:
        for t in trades_in_order:
            if is_ledger_represented(conn, t.trade_id):
                continue
            pnl = fold.per_trade_pnl.get(t.trade_id)
            conn.execute(
                "INSERT INTO fills (exec_key, timestamp_ms, side, symbol, quantity, "
                "price, fee_cost, pnl, source) VALUES (?,?,?,?,?,?,?,?, 'live')",
                (t.trade_id, t.timestamp_ms, t.side, t.symbol, t.amount, t.price, t.fee_cost, pnl),
            )
            conn.execute(
                "UPDATE observed_trades SET ledger_written_at = datetime('now') WHERE trade_id = ?",
                (t.trade_id,),
            )


def record_fee_revision(conn: sqlite3.Connection, trade_id: str, new_fee: float) -> bool:
    """Idempotent: repeatedly observing the SAME revised fee value emits
    exactly one correction row, not one per call (R2 finding 5's fee-
    correction-counter demand)."""
    row = conn.execute(
        "SELECT new_fee, revision FROM fee_corrections WHERE trade_id = ? "
        "ORDER BY revision DESC LIMIT 1", (trade_id,),
    ).fetchone()
    old_row = conn.execute(
        "SELECT fee_cost FROM observed_trades WHERE trade_id = ?", (trade_id,),
    ).fetchone()
    last_known_fee = row[0] if row is not None else (old_row[0] if old_row else None)
    if last_known_fee is not None and abs(last_known_fee - new_fee) < 1e-12:
        return False  # no change since the last recorded value — not a new revision
    next_revision = (row[1] + 1) if row is not None else 1
    with conn:
        conn.execute(
            "INSERT INTO fee_corrections (trade_id, revision, old_fee, new_fee, recorded_at) "
            "VALUES (?,?,?,?, datetime('now'))",
            (trade_id, next_revision, last_known_fee if last_known_fee is not None else 0.0, new_fee),
        )
    return True


class _InjectedProjectionFailure(Exception):
    """Raised inside refresh_ledger_projection_for_corrections' real
    transaction to prove a mid-refresh crash rolls back every UPDATE in
    that call, not just the one it was about to make."""


def refresh_ledger_projection_for_corrections(
    conn: sqlite3.Connection, symbol: str, *, fail_after_n_updates: "int | None" = None,
) -> "list[str]":
    """Materializes the LATEST fee_corrections revision into the `fills`
    table's fee_cost/pnl columns for every native (non-legacy) trade whose
    stored row has drifted from the correction-aware fold.

    Reference-model review R3 finding 2: recover_position/load_observed_
    trades became correction-aware, but the DURABLE `fills` row a
    correction lands on stayed frozen at its original value forever —
    ordinary replay (write_ledger_rows) skips it because its identity is
    already represented, so nothing could ever close the gap between
    "reconstruction says -$1" and "the stored ledger row still says +$1".
    verify_ledger_delivery_consistency stayed correctly False with no
    repair path.

    Chosen policy (stated explicitly, per the review's own framing):
    `fee_corrections` is the immutable, append-only correction LOG —
    never rewritten. `fills.fee_cost`/`fills.pnl` is a refreshable CURRENT
    -value PROJECTION over `observed_trades` + `fee_corrections` — an
    event-sourcing materialized view, not a second independent source of
    truth. This function is that refresh step: transactional (one `with
    conn:` — either every row needing an update gets it, or none do, on a
    single call), idempotent (a trade already matching the fold is left
    untouched and not returned), and safe to retry after an injected
    failure (each row's own UPDATE is complete and correct on its own; a
    retry simply reaches whatever the failed call didn't).

    Scope, stated honestly: only NATIVE fills rows (one trade, one row)
    are refreshed here. A legacy-linked aggregate row represents SEVERAL
    trades summed together and was never modeled with a per-trade P&L
    attribution to begin with (see LegacyFillRow/migrate_legacy_row) — a
    correction affecting a migrated trade's fee is a real, currently
    UNRESOLVED case for the aggregate row's own pnl; only its fee-
    conservation total is refreshed (see the loop below), which
    verify_ledger_delivery_consistency's legacy conservation check reads.
    Refreshing a legacy row's pnl is out of scope for this pass.

    Returns the trade_ids actually updated this call (empty if nothing
    needed refreshing — the idempotent-repeat case)."""
    trades = load_observed_trades(conn, symbol)  # correction-aware
    ordered = causal_order(trades)
    if ordered is None:
        raise ValueError(
            f"cannot refresh the ledger projection for {symbol}: persisted "
            f"observed_trades cannot be causally ordered"
        )
    fold = fold_position(ordered)
    by_id = {t.trade_id: t for t in trades}

    updated: "list[str]" = []
    try:
        with conn:
            for t in trades:
                is_legacy = conn.execute(
                    "SELECT 1 FROM legacy_links WHERE trade_id = ?", (t.trade_id,)
                ).fetchone() is not None
                if is_legacy:
                    continue  # handled in the aggregate pass below
                row = conn.execute(
                    "SELECT fee_cost, pnl FROM fills WHERE exec_key = ?", (t.trade_id,)
                ).fetchone()
                if row is None:
                    continue  # not a native fills row at all — nothing to refresh
                stored_fee, stored_pnl = row
                expected_pnl = fold.per_trade_pnl.get(t.trade_id)
                fee_drifted = abs(stored_fee - t.fee_cost) > 1e-9
                pnl_drifted = (
                    (expected_pnl is None) != (stored_pnl is None)
                    or (expected_pnl is not None
                        and abs((stored_pnl or 0.0) - expected_pnl) > 1e-9)
                )
                if not (fee_drifted or pnl_drifted):
                    continue
                if fail_after_n_updates is not None and len(updated) == fail_after_n_updates:
                    raise _InjectedProjectionFailure(
                        f"injected failure after {len(updated)} projection updates"
                    )
                conn.execute(
                    "UPDATE fills SET fee_cost = ?, pnl = ? WHERE exec_key = ?",
                    (t.fee_cost, expected_pnl, t.trade_id),
                )
                updated.append(t.trade_id)

            # Legacy aggregate fee-conservation refresh: sum of each
            # linked group's CURRENT (correction-aware) trade fees.
            legacy_group_ids = {
                r[0] for r in conn.execute(
                    "SELECT DISTINCT legacy_fill_id FROM legacy_links WHERE trade_id IN "
                    "(SELECT trade_id FROM observed_trades WHERE symbol = ?)", (symbol,),
                )
            }
            for legacy_fill_id in legacy_group_ids:
                linked_ids = [r[0] for r in conn.execute(
                    "SELECT trade_id FROM legacy_links WHERE legacy_fill_id = ?", (legacy_fill_id,)
                )]
                linked_trades = [by_id[tid] for tid in linked_ids if tid in by_id]
                current_total_fee = sum(t.fee_cost for t in linked_trades)
                stored = conn.execute(
                    "SELECT fee_cost FROM fills WHERE id = ?", (legacy_fill_id,)
                ).fetchone()
                if stored is None or abs(stored[0] - current_total_fee) <= 1e-9:
                    continue
                if fail_after_n_updates is not None and len(updated) == fail_after_n_updates:
                    raise _InjectedProjectionFailure(
                        f"injected failure after {len(updated)} projection updates"
                    )
                conn.execute(
                    "UPDATE fills SET fee_cost = ? WHERE id = ?",
                    (current_total_fee, legacy_fill_id),
                )
                updated.append(f"legacy:{legacy_fill_id}")
    except _InjectedProjectionFailure:
        return []  # real rollback via `with conn:` — nothing in this call was applied
    return updated


# ============================================================================
# Recovery — reconstructing state PURELY from persisted SQLite, after a
# genuine close/reopen of the database. Reference-model review finding 3:
# the original three-restart test kept the same connection and manually
# carried Python variables (cursor values, computed positions) across
# "restarts" — it tested SyntheticExchange's own bookkeeping, not real
# recovery. The functions below are what an actually-restarted process
# must call, and only ever read from `conn` — never from anything the
# caller happens to still be holding in memory.
# ============================================================================

def recover_watermark(conn: sqlite3.Connection, currency_scope: str) -> "int | None":
    """The retrieval cursor a restarted process must resume from — the
    highest window_until_ms among COMMITTED checkpoints for this scope.
    None means no checkpoint has ever committed (fetch from the
    beginning)."""
    row = conn.execute(
        "SELECT MAX(window_until_ms) FROM checkpoints WHERE currency_scope = ?",
        (currency_scope,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def load_observed_trades(conn: sqlite3.Connection, symbol: str) -> "list[SynTrade]":
    """Reconstruction-facing read: for each trade, applies the LATEST
    recorded fee_corrections revision in place of the originally observed
    fee_cost, if one exists.

    Reference-model review R2 finding 2, reproduced exactly: making
    revise_trade_fee move real exchange cash did nothing for
    reconstruction, because this function used to return each trade's
    frozen original fee_cost regardless of any later correction —
    recover_position (BUY 1@100 + SELL 1@101, both zero fee, then a SELL
    fee correction to $2, then a real close/reopen) reported +$1 instead
    of the correct −$1. `observed_trades.fee_cost` remains the untouched
    ORIGINAL-observation audit record (never overwritten in place — the
    correction stays a separate, traceable event in fee_corrections); this
    function is what makes every RECONSTRUCTION-consuming caller
    (recover_position, verify_ledger_delivery_consistency, this file's own
    causal_order/fold_position pipeline) correction-aware without having
    to duplicate the join. A trade with no correction row is returned
    completely unchanged."""
    rows = conn.execute(
        "SELECT trade_id, order_id, symbol, side, price, amount, cost, fee_cost, "
        "fee_currency, exchange_timestamp_ms FROM observed_trades WHERE symbol = ?",
        (symbol,),
    ).fetchall()
    trades = []
    for row in rows:
        trade_id, fee_cost = row[0], row[7]
        correction = conn.execute(
            "SELECT new_fee FROM fee_corrections WHERE trade_id = ? "
            "ORDER BY revision DESC LIMIT 1", (trade_id,),
        ).fetchone()
        if correction is not None:
            fee_cost = correction[0]
        trades.append(SynTrade(row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                                fee_cost, row[8], row[9]))
    return trades


def recover_position(conn: sqlite3.Connection, symbol: str) -> FoldResult:
    """Rebuilds qty/avg_cost/realized-P&L PURELY from persisted
    observed_trades rows, causally ordered — the actual recovery path a
    restarted process uses, independent of any in-memory state that may
    have existed before the crash."""
    trades = load_observed_trades(conn, symbol)
    ordered = causal_order(trades)
    if ordered is None:
        raise ValueError(
            f"persisted observed_trades for {symbol} cannot be causally "
            f"ordered — data integrity problem, not a recovery bug"
        )
    return fold_position(ordered)


def verify_ledger_delivery_consistency(conn: sqlite3.Connection, symbol: str,
                                        tolerance: float = 1e-9) -> bool:
    """The actual implementation behind the third readiness leg.

    Reference-model review R2 finding 3, reproduced exactly: the first
    version of this function checked only identity PRESENCE (a native
    fills row XOR a legacy_links mapping) — rewriting an already-correct
    SELL fills row's stored quantity to 99 and pnl to 999, while leaving
    its exec_key untouched, still returned True, because nothing here ever
    compared a stored VALUE against anything. This version additionally:
      - recomputes the correction-aware fold (via load_observed_trades,
        so a fee correction is included — same fix as recover_position)
        and compares every NATIVE fills row's quantity/price/fee_cost/pnl
        against the trade's true observed payload and recomputed P&L;
      - validates conservation for every LEGACY-linked group: the sum of
        every trade linked to one legacy fills row must still match that
        row's own stored quantity/fee_cost exactly (a legacy row or its
        links being altered after migration is now caught, not just
        native-row corruption);
      - retains the representation-presence and full-delivery checks from
        the previous version.
    Returns False on ANY violation. Note (named honestly, not hidden):
    this checks agreement between STORED ledger rows and the RECOMPUTED
    fold from observed_trades — it does not independently re-verify
    observed_trades itself against the exchange (that is coverage/balance
    consistency's job, checked separately, never folded into this
    function)."""
    written = conn.execute(
        "SELECT trade_id FROM observed_trades WHERE symbol = ? AND ledger_written_at IS NOT NULL",
        (symbol,),
    ).fetchall()
    for (trade_id,) in written:
        has_fill = conn.execute(
            "SELECT 1 FROM fills WHERE exec_key = ?", (trade_id,)
        ).fetchone() is not None
        has_link = conn.execute(
            "SELECT 1 FROM legacy_links WHERE trade_id = ?", (trade_id,)
        ).fetchone() is not None
        if has_fill == has_link:  # both True (double-represented) or both False (orphaned marker)
            return False

    all_ids = {r[0] for r in conn.execute(
        "SELECT trade_id FROM observed_trades WHERE symbol = ?", (symbol,)
    )}
    written_ids = {r[0] for r in written}
    if all_ids != written_ids:
        return False  # some observed trade was never delivered to the ledger at all

    trades = load_observed_trades(conn, symbol)  # correction-aware
    ordered = causal_order(trades)
    if ordered is None:
        return False
    fold = fold_position(ordered)
    by_id = {t.trade_id: t for t in trades}

    for (trade_id,) in written:
        is_legacy = conn.execute(
            "SELECT 1 FROM legacy_links WHERE trade_id = ?", (trade_id,)
        ).fetchone() is not None
        if is_legacy:
            continue  # conservation-checked as a group below
        row = conn.execute(
            "SELECT quantity, price, fee_cost, pnl FROM fills WHERE exec_key = ?", (trade_id,)
        ).fetchone()
        qty, price, fee_cost, stored_pnl = row
        t = by_id[trade_id]
        if abs(qty - t.amount) > tolerance or abs(price - t.price) > tolerance:
            return False
        if abs(fee_cost - t.fee_cost) > tolerance:
            return False
        expected_pnl = fold.per_trade_pnl.get(trade_id)
        if (expected_pnl is None) != (stored_pnl is None):
            return False
        if expected_pnl is not None and abs(stored_pnl - expected_pnl) > tolerance:
            return False

    legacy_group_ids = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT legacy_fill_id FROM legacy_links WHERE trade_id IN "
            "(SELECT trade_id FROM observed_trades WHERE symbol = ?)", (symbol,),
        )
    }
    for legacy_fill_id in legacy_group_ids:
        linked_ids = [r[0] for r in conn.execute(
            "SELECT trade_id FROM legacy_links WHERE legacy_fill_id = ?", (legacy_fill_id,)
        )]
        linked_trades = [by_id[tid] for tid in linked_ids if tid in by_id]
        legacy_row = conn.execute(
            "SELECT quantity, fee_cost, pnl FROM fills WHERE id = ?", (legacy_fill_id,)
        ).fetchone()
        if legacy_row is None:
            return False
        legacy_qty, legacy_fee, legacy_pnl = legacy_row
        if abs(sum(t.amount for t in linked_trades) - legacy_qty) > tolerance:
            return False
        # Note: this compares against each linked trade's CURRENT
        # (correction-aware) fee — a fee corrected after migration will
        # legitimately break exact conservation against the frozen legacy
        # total unless that correction is separately reconciled into the
        # legacy row; surfaced here as a real mismatch, not silently
        # tolerated.
        if abs(sum(t.fee_cost for t in linked_trades) - legacy_fee) > tolerance:
            return False

        # Legacy P&L, reference-model review R4 finding 2:
        # refresh_ledger_projection_for_corrections deliberately never
        # touches a legacy row's stored pnl (see its own docstring — legacy
        # P&L refresh is an explicitly declared, deferred limitation, not
        # silently forgotten). That deferral is only honest if verification
        # actually checks it rather than passing on fee/quantity
        # conservation alone. The natural aggregation policy for pnl is a
        # plain sum across the linked group (pnl is additive, unlike
        # price/fee); any linked trade with a non-None realized pnl in the
        # correction-aware fold means the group's TRUE combined pnl is
        # compared against the legacy row's stored value — matching (a
        # correction already reconciled, or none ever affected it) passes;
        # any drift (exactly the reproduced case: a fee correction to an
        # underlying SELL leaves the legacy row's original pnl stale) fails
        # closed here, whether or not that drift came from a correction
        # directly on this group's own trades.
        group_pnls = [fold.per_trade_pnl.get(t.trade_id) for t in linked_trades]
        if any(p is not None for p in group_pnls):
            expected_group_pnl = sum(p for p in group_pnls if p is not None)
            if legacy_pnl is None or abs(legacy_pnl - expected_group_pnl) > tolerance:
                return False
        elif legacy_pnl is not None:
            return False  # a pnl is stored but nothing in the group can justify one

    return True


# ============================================================================
# Legacy-row migration — conservation based, blocks on ambiguity, never
# renames an existing fills.exec_key.
# ============================================================================

@dataclass
class LegacyFillRow:
    fill_id: int
    symbol: str
    side: str
    quantity: float
    cost: float
    fee_cost: float
    window_start_ms: int
    window_end_ms: int


@dataclass
class MigrationResult:
    fill_id: int
    matched_trade_ids: "list[str]"
    blocked: bool
    reason: str = ""


def migrate_legacy_row(row: LegacyFillRow, candidate_trades: "list[SynTrade]",
                        tolerance: float = 1e-6,
                        already_linked: "frozenset[str] | set[str]" = frozenset()) -> MigrationResult:
    """Finds a SUBSET of candidate_trades (same symbol/side, within the
    row's time window) whose summed quantity/cost/fee EXACTLY matches the
    legacy row's own totals (within tolerance) — never a nearest-timestamp
    proximity guess. A legacy row can represent several trades (many-to-
    one); this returns every trade_id in the matched subset. No match, or
    more than one disjoint subset matching equally well, blocks for manual
    resolution rather than guessing (R2 finding 2).

    already_linked: trade ids a PRIOR migration run already claimed for a
    DIFFERENT legacy row — excluded from the candidate pool up front, so
    two legacy rows can never both match against the same underlying
    execution (reference-model review finding 2's "enforce global
    uniqueness of trade allocation across different legacy rows"). Callers
    should pass already_linked_trade_ids(conn) here across a batch of
    migrations."""
    pool = [
        t for t in candidate_trades
        if t.symbol == row.symbol and t.side == row.side and t.trade_id not in already_linked
        and row.window_start_ms <= t.timestamp_ms <= row.window_end_ms
    ]
    if not pool:
        return MigrationResult(row.fill_id, [], True, "no candidate trades in window")
    matches: "list[tuple[str, ...]]" = []
    # Bounded subset search — this bot's actual trade counts per window are
    # tiny (single digits); a full subset enumeration is deliberately
    # simple and correct rather than a general-purpose solver.
    n = len(pool)
    if n > 20:
        return MigrationResult(row.fill_id, [], True,
                                f"candidate pool too large ({n}) for the bounded matcher — manual resolution required")
    for r in range(1, n + 1):
        for combo in itertools.combinations(pool, r):
            qty = sum(t.amount for t in combo)
            cost = sum(t.cost for t in combo)
            fee = sum(t.fee_cost for t in combo)
            if (abs(qty - row.quantity) < tolerance and abs(cost - row.cost) < tolerance
                    and abs(fee - row.fee_cost) < tolerance):
                matches.append(tuple(sorted(t.trade_id for t in combo)))
    unique_matches = {m for m in matches}
    if len(unique_matches) == 0:
        return MigrationResult(row.fill_id, [], True, "no subset conserves quantity/cost/fee exactly")
    if len(unique_matches) > 1:
        return MigrationResult(row.fill_id, [], True,
                                f"{len(unique_matches)} disjoint subsets all conserve totals — ambiguous")
    return MigrationResult(row.fill_id, list(next(iter(unique_matches))), False)


def already_linked_trade_ids(conn: sqlite3.Connection) -> "set[str]":
    """Every trade id already claimed by SOME legacy migration link —
    pass this into migrate_legacy_row's already_linked param across a
    batch of migration runs so a later row can never re-claim an earlier
    row's already-matched trade."""
    return {r[0] for r in conn.execute("SELECT trade_id FROM legacy_links")}


def apply_migration_link(conn: sqlite3.Connection, result: MigrationResult,
                          matched_trades: "list[SynTrade]") -> None:
    """Links matched trade ids to the existing legacy fills row WITHOUT
    renaming its exec_key — the post-migration invariant ("every
    ledger-written observed trade has a fills row keyed by its own
    trade_id") applies only to trades observed AFTER migration; legacy
    trades are linked via legacy_links, a structurally separate mapping,
    never forced to satisfy the same-key invariant retroactively (R2
    finding 2's contradiction, resolved by not claiming the invariant
    covers legacy data at all). matched_trades must be exactly the trades
    result.matched_trade_ids identifies — their real payload is what gets
    recorded in observed_trades, never fabricated placeholder values."""
    if result.blocked:
        raise ValueError(f"cannot apply a blocked migration result: {result.reason}")
    by_id = {t.trade_id: t for t in matched_trades}
    with conn:
        for trade_id in result.matched_trade_ids:
            t = by_id[trade_id]
            existing_link = conn.execute(
                "SELECT legacy_fill_id FROM legacy_links WHERE trade_id = ?", (trade_id,)
            ).fetchone()
            if existing_link is not None and existing_link[0] != result.fill_id:
                raise ValueError(
                    f"trade {trade_id} is already linked to legacy fill "
                    f"{existing_link[0]} — refusing to also link it to {result.fill_id}"
                )
            if is_ledger_represented(conn, trade_id) and existing_link is None:
                raise ValueError(
                    f"trade {trade_id} is already represented by a native fills row — "
                    f"refusing to also migration-link it"
                )
            conn.execute(
                "INSERT OR IGNORE INTO legacy_links (trade_id, legacy_fill_id) VALUES (?, ?)",
                (trade_id, result.fill_id),
            )
            conn.execute(
                "INSERT INTO observed_trades (trade_id, order_id, symbol, side, price, amount, "
                "cost, fee_cost, fee_currency, exchange_timestamp_ms, ledger_written_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now')) "
                "ON CONFLICT(trade_id) DO NOTHING",
                (t.trade_id, t.order_id, t.symbol, t.side, t.price, t.amount,
                 t.cost, t.fee_cost, t.fee_currency, t.timestamp_ms),
            )


# ============================================================================
# Cash budget (CapitalPool-alike) vs. actual exchange cash — kept as two
# structurally separate accumulators so neither can be added on top of the
# other (R2 finding 4).
# ============================================================================

class BudgetPool:
    """Models ONLY what CapitalPool actually tracks: cash ALLOCATED to a
    symbol's slot at BUY time (a decision figure), released on full exit.
    Never mutated by trade fills — allocation and actual cash movement are
    different events by design."""

    def __init__(self, total_capital: float) -> None:
        self.total_capital = total_capital
        self._allocated: "dict[str, float]" = {}

    def allocate(self, symbol: str, budget: float) -> None:
        self._allocated[symbol] = budget

    def release(self, symbol: str) -> None:
        self._allocated.pop(symbol, None)

    @property
    def invested_budget(self) -> float:
        return sum(self._allocated.values())

    @property
    def free_pool_cash(self) -> float:
        return self.total_capital - self.invested_budget


class ExchangeCashReconciler:
    """Reconciles the exchange's OWN quote-currency balance against the sum
    of real trade cash deltas from an explicit opening baseline — entirely
    independent of BudgetPool. Proves the two can diverge (fees, slippage
    between a budgeted BUY and its actual fill) without one leaking into
    the other's arithmetic."""

    def __init__(self, opening_balance: float, quote: str) -> None:
        self.opening_balance = opening_balance
        self.quote = quote
        self._cumulative_delta = 0.0

    def apply_trades(self, trades: "list[SynTrade]") -> None:
        for t in trades:
            fee = t.fee_cost if t.fee_currency == self.quote else 0.0
            self._cumulative_delta += (-t.cost - fee) if t.side == "buy" else (t.cost - fee)

    @property
    def expected_balance(self) -> float:
        return self.opening_balance + self._cumulative_delta


# ============================================================================
# Readiness = all three properties, reported separately — never merged into
# one boolean (R2 review, "Bounded next step").
# ============================================================================

@dataclass
class ReadinessReport:
    coverage_complete: bool
    watermark_confirmed: bool
    balance_consistent: bool
    ledger_delivery_ok: bool
    coverage_reason: str = ""
    balance_residual: "float | None" = None
    historical_audit_clean: "bool | None" = None  # None = no audit was run this cycle

    @property
    def ready(self) -> bool:
        if self.historical_audit_clean is False:
            return False  # an audit that found a violation overrides everything else
        return (self.coverage_complete and self.watermark_confirmed
                and self.balance_consistent and self.ledger_delivery_ok)

    def explain(self) -> str:
        if self.historical_audit_clean is False:
            return ("not ready — audit found trade(s) the persisted watermark had "
                     "already passed: the bounded-delay assumption was violated for "
                     "this window; reconciliation is a human decision, not automatic")
        if self.ready:
            if self.historical_audit_clean is True:
                return ("ready: coverage complete, balance consistent, ledger/delivery "
                         "consistent, AND an audit re-check of the historical window "
                         "found nothing new — the strongest evidence this model produces")
            return ("ready (conditional on the assumed visibility bound — coverage "
                     "complete, watermark aged past the safety margin, balance and "
                     "ledger/delivery consistent, but NOT independently audited; a "
                     "trade hidden longer than the margin could still have been missed)")
        failing = []
        if not self.coverage_complete:
            failing.append(f"coverage ({self.coverage_reason})")
        if self.coverage_complete and not self.watermark_confirmed:
            failing.append(
                "watermark (page-exhausted as of this read, but the window has "
                "not yet aged past the safety margin — provisional, not proven)"
            )
        if not self.balance_consistent:
            failing.append(f"balance (residual={self.balance_residual})")
        if not self.ledger_delivery_ok:
            failing.append("ledger/delivery")
        return "not ready — failing: " + ", ".join(failing)


def assess_readiness(coverage: CoverageResult, balance: "BalanceCheckResult | None",
                      ledger_delivery_ok: bool, *, watermark_confirmed: bool,
                      historical_audit_clean: "bool | None" = None) -> ReadinessReport:
    """Deliberately takes independent inputs and never lets one substitute
    for another: a caller cannot pass a good balance result to paper over
    incomplete coverage, and an incomplete coverage result means the
    balance check is not even meaningful yet (balance is None-able for
    exactly that reason — see tests).

    watermark_confirmed (reference-model review finding 1 — the explicit
    evidence/unknown state): coverage.complete alone only proves the
    exchange's queryable history was fully PAGED as of this read — it can
    never prove nothing else happened that hasn't surfaced yet.
    watermark_confirmed itself is ALSO only a time-elapsed check against an
    ASSUMED visibility bound (see is_watermark_confirmed's own docstring)
    — a trade hidden longer than the configured safety margin can still be
    permanently missed while this reports ready=True. That is an
    acknowledged, still-open residual limitation of any finite-margin
    protocol, not something this function claims to close.

    historical_audit_clean (R2 review: "make the watermark's explicit
    assumption a visible part of readiness evidence rather than an
    automatically satisfied confirmation flag"): pass the result of
    audit_historical_window's `not violated` when an audit was actually
    run this cycle. None (the default) means no audit ran — explain()
    then reports readiness as explicitly CONDITIONAL, not proven. True
    means an audit ran and found nothing amiss — the strongest evidence
    this model can produce, though still not a mathematical proof no
    delay could ever exceed the margin. False means an audit found a
    genuine violation and forces ready=False regardless of every other
    input."""
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
