"""
Per-cycle reconciliation orchestration (design §2, §7, §9 Tier B).

run_cycle() is the one entry point bot/main.py calls periodically
(cfg.accounting.reconcile_interval_s). It:

  1. Does ONE account-wide, coverage-proof paginated fetch_my_trades pull
     (kraken_adapter's `symbol=None` mode — see that module's docstring for
     why this must be account-wide, not per-symbol, to keep Kraken's own
     `count` meaningful).
  2. Observes every newly-visible trade into `observed_trades`, ATOMICALLY
     with the retrieval-watermark checkpoint AND any fee-correction rows
     detected this cycle (store.commit_observation_batch, one SQLite
     transaction — money-readiness review 2026-09-20 P1: fee corrections
     used to be written separately via TradeLog's own connection, so a
     later failure in the SAME cycle could leave a correction durable
     while the trades/watermark it was detected alongside rolled back).
  3. Runs the §2 balance-identity check separately for the shared CAD cash
     pool (account level, once) and for each traded symbol's base-asset
     inventory (design §6 — cash is never checked per-symbol).
  4. Detects fee corrections on trades re-observed inside the watermark's
     safety-margin overlap window (design §4) — never a silent overwrite.
  5. Detects genuine data-integrity anomalies (a trade_id re-observed with a
     different price/amount/side) and blocks rather than guesses.
  6. Links delayed-visibility "stragglers" — a trade whose real exchange id
     became observable only AFTER live_observe.py's synchronous, exact
     order-id match already ran (or never ran at all, e.g. a
     broker-triggered native-stop fill) — to their existing `fills` row via
     the same exact-conservation matcher migration.py uses. Idempotent;
     blocks (does not link) on ambiguity or a non-conserving candidate set
     (money-readiness review 2026-09-20 P1: this was documented as the
     periodic cycle's job but never actually wired in).
  7. Returns a BlockState — the ONLY thing bot/main.py's BUY gate needs to
     consult. A clean state EXPIRES after max_age_ms of real wall-clock
     time (BlockState.is_stale / blocked_for_buy's now_ms/max_age_ms
     kwargs — money-readiness review 2026-09-20 P1: a successful cycle
     used to authorize BUYs indefinitely until the NEXT scheduled cycle
     happened to run, with no independent staleness check of its own).
     Exits are NEVER gated by this (see resolve_exit_quantity()).

Bootstrap (first-ever cycle for a scope, no committed checkpoint yet): there
is no trustworthy prior balance to diff against without item 8's migration
having run first. This mirrors what bot/execution/live_executor.py's
_sync_position already does for POSITION quantity (design §6: "_sync_
position ... remains the sole authority for the opening boundary") — the
first cycle for a scope commits an OPENING checkpoint (balance_after =
the currently-fresh exchange balance, covering every trade already known
for that scope) instead of claiming an identity result, and is reported as
`bootstrapped=True`, not `blocked=True`. Every subsequent cycle has a real
prior checkpoint and runs the full identity check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from bot.accounting import engine, store
from bot.accounting.engine import ExchangeAdapter, LedgerMovement, ObservedTrade, UnlinkedFill


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


@dataclass
class ScopeResult:
    scope: str
    bootstrapped: bool = False
    blocked: bool = False
    reason: str = ""
    residual: Optional[float] = None


@dataclass
class BlockState:
    """The single artifact bot/main.py's BUY gate consults. Recomputed
    fresh every cycle (never incrementally mutated) — replace-in-place,
    don't patch.

    `reconciled` defaults to False — money-readiness review 2026-09-19,
    P0 finding: the bare-default `BlockState()` used to read as fully
    unblocked, so a BUY evaluated before the first reconciliation cycle
    ever completed (or right after a cycle that raised) sailed through
    with nothing to check against. `run_cycle()` sets this True ONLY once
    a full observation phase (coverage-proof retrieval + fee-correction
    sweep + the atomic trade/watermark commit) has genuinely completed —
    never on an early return, never on a caught exception. `blocked_for_buy`
    treats "never reconciled" identically to "actively blocked".

    `computed_at_ms` + `is_stale`/`blocked_for_buy`'s `now_ms`/`max_age_ms`
    kwargs — money-readiness review 2026-09-20, P1 finding: a clean,
    successful cycle used to authorize BUYs indefinitely until the NEXT
    scheduled run_cycle() call happened to fire (up to
    cfg.accounting.reconcile_interval_s later, or longer if a scheduled
    refresh itself hung), with nothing checking whether the state was
    still fresh in between. A caller that wants this protection passes
    both `now_ms` (real current time) and `max_age_ms` (reconcile_interval
    + grace period) to `blocked_for_buy`/`explain` at the moment of
    consulting the state — omitting either keeps the OLD behavior
    (no staleness check), so existing callers/tests are unaffected unless
    they opt in."""
    reconciled:             bool = False
    account_cash_blocked:  bool = False
    account_cash_reason:   str  = ""
    symbol_blocked:        dict = field(default_factory=dict)   # symbol -> bool
    symbol_reason:         dict = field(default_factory=dict)   # symbol -> str
    coverage_blocked:      bool = False   # a fetch itself was incomplete this cycle
    coverage_reason:       str  = ""
    last_cycle_at:         str  = ""
    computed_at_ms:        int  = 0       # wall-clock ms this state became valid — see is_stale()
    scope_results:         list = field(default_factory=list)   # list[ScopeResult]

    def is_stale(self, *, now_ms: int, max_age_ms: int) -> bool:
        """True once more than max_age_ms of REAL elapsed wall-clock time
        has passed since this state was computed — independent of
        whatever cadence the caller's own scheduling loop intended, and
        independent of `reconciled`/the block flags (a clean state can be
        stale; a blocked state is already blocked regardless)."""
        return (now_ms - self.computed_at_ms) > max_age_ms

    def blocked_for_buy(
        self, symbol: str, *, now_ms: "int | None" = None, max_age_ms: "int | None" = None,
    ) -> bool:
        """§7: account-cash block covers every symbol; a coverage failure
        this cycle is treated the same way (nothing new can be trusted to
        be reconciled while the retrieval itself is incomplete). A state
        that has never completed a successful cycle at all — the startup
        default, or the result of an exception this cycle — blocks every
        symbol unconditionally (P0 fix). Passing both `now_ms` and
        `max_age_ms` additionally blocks a state that has simply gone
        stale, even if it was clean the moment it was computed (P1 fix,
        2026-09-20)."""
        if now_ms is not None and max_age_ms is not None and self.is_stale(now_ms=now_ms, max_age_ms=max_age_ms):
            return True
        return (
            not self.reconciled
            or self.account_cash_blocked
            or self.coverage_blocked
            or self.symbol_blocked.get(symbol, False)
        )

    def explain(self, *, now_ms: "int | None" = None, max_age_ms: "int | None" = None) -> str:
        parts = []
        if now_ms is not None and max_age_ms is not None and self.is_stale(now_ms=now_ms, max_age_ms=max_age_ms):
            age_s = (now_ms - self.computed_at_ms) / 1000.0
            parts.append(f"state is stale (age={age_s:.0f}s > max_age={max_age_ms / 1000:.0f}s)")
        if not self.reconciled and not self.coverage_blocked:
            parts.append("no reconciliation cycle has completed yet")
        if self.coverage_blocked:
            parts.append(f"coverage incomplete ({self.coverage_reason})")
        if self.account_cash_blocked:
            parts.append(f"account cash unreconciled ({self.account_cash_reason})")
        for sym, blocked in self.symbol_blocked.items():
            if blocked:
                parts.append(f"{sym} unreconciled ({self.symbol_reason.get(sym, '')})")
        return "; ".join(parts) if parts else "ok"


def base_asset(symbol: str) -> str:
    return symbol.split("/")[0]


def _detect_integrity_anomaly(stored: ObservedTrade, fresh: ObservedTrade) -> Optional[str]:
    """design §4: anything other than fee changing on an already-observed
    trade id is a data-integrity problem, never silently overwritten."""
    if stored.price != fresh.price or stored.amount != fresh.amount or stored.side != fresh.side:
        return (
            f"trade {stored.trade_id} re-observed with different economics: "
            f"stored(price={stored.price}, amount={stored.amount}, side={stored.side}) "
            f"vs fresh(price={fresh.price}, amount={fresh.amount}, side={fresh.side})"
        )
    return None


@dataclass
class FeeSweepResult:
    notes: "list[str]" = field(default_factory=list)
    anomalous_trade_ids: set = field(default_factory=set)
    anomalous_symbols: set = field(default_factory=set)
    pending_corrections: "list[store.FeeCorrectionWrite]" = field(default_factory=list)


def _apply_fee_corrections(conn, coverage_trades: "list[ObservedTrade]") -> FeeSweepResult:
    """For every re-observed trade already known to observed_trades, compare
    its freshly-fetched fee to the current effective fee and, if it
    changed, compute the correction to record — returned, NOT written here
    (money-readiness review 2026-09-20, P1 finding: writing directly via
    TradeLog's own separate connection let a fee correction persist even
    when the SAME-cycle trade/watermark commit later rolled back). The
    caller commits `pending_corrections` atomically alongside the trade
    upserts and watermark checkpoint via store.commit_observation_batch.
    Also detects a genuine integrity anomaly instead of ever silently
    overwriting the immutable observed_trades payload."""
    out = FeeSweepResult()
    for fresh in coverage_trades:
        stored = store.get_observed_trade(conn, fresh.trade_id)
        if stored is None:
            continue  # newly observed this cycle — handled by the caller's upsert, not a correction
        anomaly = _detect_integrity_anomaly(stored, fresh)
        if anomaly:
            out.notes.append(f"ANOMALY: {anomaly}")
            out.anomalous_trade_ids.add(fresh.trade_id)
            out.anomalous_symbols.add(fresh.symbol)
            continue
        if abs(stored.fee_cost - fresh.fee_cost) < 1e-12:
            continue  # observed_trades.fee_cost is the frozen ORIGINAL value — compare against it directly
        deltas = store.fee_correction_deltas_for_trade(conn, fresh.trade_id)
        current_effective = engine.effective_fee_cost(stored.fee_cost, deltas)
        if abs(current_effective - fresh.fee_cost) < 1e-12:
            continue  # already recorded — idempotent, not a new revision
        all_ids = store.all_fee_correction_adjustment_ids(conn)
        revision = engine.next_fee_correction_revision(all_ids, fresh.trade_id)
        delta = fresh.fee_cost - current_effective
        out.pending_corrections.append(store.FeeCorrectionWrite(
            order_id=fresh.order_id, symbol=fresh.symbol, delta_fee=delta,
            fee_currency=fresh.fee_currency,
            adjustment_id=engine.fee_correction_adjustment_id(fresh.trade_id, revision),
        ))
        out.notes.append(
            f"FEE CORRECTION: trade {fresh.trade_id} ({fresh.symbol}) "
            f"delta={delta:+.8f} {fresh.fee_currency} (revision {revision})"
        )
    return out


def run_cycle(
    exchange: ExchangeAdapter, conn, trade_log, *, quote: str, symbols: "list[str]",
    safety_margin_s: float = 900.0, account_scope: str = "__account__",
    now_ms: "int | None" = None,
) -> BlockState:
    """One full reconciliation cycle. Never raises — a failure anywhere
    (network, parsing, an exchange error) is caught and reported as a
    coverage-blocked cycle, since "we don't know" must never be silently
    treated as "everything's fine". conn must already have store.init_db()
    run against it.

    trade_log: accepted for call-site/signature stability only — fee
    corrections are now written directly via `conn` inside the atomic
    observation-batch commit (money-readiness review 2026-09-20 P1), not
    through TradeLog's own separate connection. Kept as a parameter rather
    than removed so bot/main.py's existing call sites and every test that
    already constructs one don't need to change for an internal detail.

    now_ms: injectable wall-clock override, real time if None. Every
    watermark/safety-margin computation in this function is written in
    terms of this value, never a bare `datetime.now()` call buried deeper
    in the call graph — this is what makes checkpoint-race and
    delayed-visibility scenarios deterministically testable instead of
    depending on real elapsed wall-clock time between test steps.
    Resolved once, right here, so it's available for `state.computed_at_ms`
    even if the very first exchange call fails."""
    now_ms = now_ms if now_ms is not None else _now_ms()
    state = BlockState(last_cycle_at=engine.iso_from_ms(now_ms), computed_at_ms=now_ms)
    # reconciled=False — fail-closed until proven otherwise

    # ── Observation phase: retrieval, fee-correction sweep, and the
    # atomic trade+watermark commit. Money-readiness review 2026-09-19,
    # P1 finding: the watermark used to be committed BEFORE trades were
    # observed and BEFORE this phase was known to succeed — a later
    # exception could leave a durable cursor ahead of data that was never
    # actually saved. Everything in this phase now either all commits
    # together (store.commit_observation_batch, one transaction) or none
    # of it does, and `state.reconciled` is set True only after that
    # commit has actually happened — never speculatively, never on an
    # early return or a caught exception.
    try:
        prior_watermark_iso = store.recover_watermark(conn, account_scope)
        coverage = engine.retrieve_with_coverage_proof(exchange, None, since=prior_watermark_iso)
        if not coverage.complete:
            state.coverage_blocked = True
            state.coverage_reason = coverage.reason
            return state

        prior_watermark_ms = engine.ts_ms(prior_watermark_iso) if prior_watermark_iso else None
        new_watermark_ms = engine.compute_safe_watermark(
            coverage, now_ms=now_ms, previous_watermark_ms=prior_watermark_ms,
            safety_margin_ms=int(safety_margin_s * 1000),
        )
        if new_watermark_ms is None:
            # No previous watermark AND coverage somehow incomplete is
            # already returned above; this only remains reachable if
            # safety_margin itself is misconfigured to exceed "now" —
            # treat conservatively.
            state.coverage_blocked = True
            state.coverage_reason = "could not compute a safe watermark this cycle"
            return state
        new_watermark_iso = engine.iso_from_ms(new_watermark_ms)
        # Real moment fresh_balance is captured below — DELIBERATELY
        # separate from new_watermark_iso above. new_watermark_iso is the
        # conservative RETRIEVAL cursor (now - safety_margin): it only
        # bounds what `since` the NEXT cycle's account-wide fetch_my_trades
        # pull uses, so a delayed-visibility trade keeps getting
        # re-fetched until it appears. A per-scope checkpoint's own
        # `window_until`, below, must instead be the ACTUAL moment its
        # `fresh_balance` was read — using the conservative watermark
        # there instead (an earlier bug caught while writing this)
        # double-counts any deposit/trade whose real timestamp falls
        # inside the safety-margin gap between the two.
        balance_as_of_iso = engine.iso_from_ms(now_ms)

        # Fee-correction / integrity-anomaly sweep over EVERY trade this
        # fetch returned (including ones already known, inside the
        # overlap window) — reads existing observed_trades/fee_adjustments
        # state, unaffected by trades this cycle hasn't inserted yet.
        # Computes what to write; does not write anything itself (see its
        # own docstring — money-readiness review 2026-09-20 P1).
        sweep = _apply_fee_corrections(conn, coverage.trades)

        # Atomic: every genuinely new trade (never one flagged anomalous
        # this cycle — it stays represented by its ORIGINAL stored
        # payload), the account-wide retrieval-watermark commit, AND any
        # fee corrections detected this cycle — together, in ONE
        # transaction (money-readiness review 2026-09-20 P1: a correction
        # written through a separate connection could persist even when
        # the trades/watermark it was detected alongside later rolled
        # back; now all three are the same atomic write).
        new_trades = [t for t in coverage.trades if t.trade_id not in sweep.anomalous_trade_ids]
        checkpoint_id = engine.new_checkpoint_id()
        store.commit_observation_batch(
            conn, new_trades=new_trades, retrieval_scope=account_scope,
            window_since=prior_watermark_iso, window_until=new_watermark_iso,
            checkpoint_id=checkpoint_id, fee_corrections=sweep.pending_corrections,
        )
        state.reconciled = True
    except Exception as exc:
        state.coverage_blocked = True
        state.coverage_reason = f"observation phase raised: {exc}"
        return state

    # ── Balance-identity phase: each scope is checked and isolated
    # independently — a scope's own exception (e.g. a transient
    # fetch_balance_total failure) blocks ONLY that scope, never aborts
    # the whole cycle (which would otherwise silently skip every OTHER
    # scope's check this cycle) and never gets treated as a pass either.
    try:
        cash_result = _reconcile_scope(
            conn, exchange, scope="CAD", asset=quote, side_asset_is_quote=True,
            window_until_iso=balance_as_of_iso, symbols_for_scope=symbols,
            tolerance=exchange.cash_tolerance(quote) if hasattr(exchange, "cash_tolerance") else 0.005,
        )
    except Exception as exc:
        cash_result = ScopeResult(scope="CAD", blocked=True, reason=f"cash scope check raised: {exc}")
    state.account_cash_blocked = cash_result.blocked
    state.account_cash_reason  = cash_result.reason
    state.scope_results.append(cash_result)

    # -- per-symbol base-asset inventory -----------------------------------
    for sym in symbols:
        base = base_asset(sym)
        tol = exchange.amount_tolerance(sym) if hasattr(exchange, "amount_tolerance") else 1e-8
        try:
            sym_result = _reconcile_scope(
                conn, exchange, scope=sym, asset=base, side_asset_is_quote=False,
                window_until_iso=balance_as_of_iso, symbols_for_scope=[sym], tolerance=tol,
            )
        except Exception as exc:
            sym_result = ScopeResult(scope=sym, blocked=True, reason=f"scope check raised: {exc}")
        state.symbol_blocked[sym] = sym_result.blocked
        state.symbol_reason[sym]  = sym_result.reason
        state.scope_results.append(sym_result)

    if sweep.anomalous_symbols:
        anomaly_text = "; ".join(sweep.notes)
        # An anomaly makes the account-cash figure suspect too (cash's own
        # trade set spans every symbol) — fail closed rather than report a
        # clean cash check built on a payload just flagged untrustworthy.
        state.account_cash_blocked = True
        state.account_cash_reason = (state.account_cash_reason + "; " if state.account_cash_reason else "") + anomaly_text
        for sym in sweep.anomalous_symbols:
            if sym in state.symbol_blocked:
                state.symbol_blocked[sym] = True
                state.symbol_reason[sym] = (state.symbol_reason.get(sym, "") + "; " if state.symbol_reason.get(sym) else "") + anomaly_text

    # ── Delayed-visibility straggler linking (money-readiness review
    # 2026-09-20, P1): live_observe.py's synchronous, exact order-id match
    # handles the common case; this is the promised periodic safety net
    # for whatever it missed (a fetch_my_trades propagation delay, or a
    # fill discovered outside the normal execute() path with no
    # synchronous observer call at all). Runs regardless of this cycle's
    # own coverage/balance outcome — it only reads/links what's ALREADY
    # persisted, so it's still worth attempting even on an otherwise
    # degraded cycle. Never raises; a failure here blocks the affected
    # symbol rather than the whole cycle.
    try:
        straggler_result = _link_stragglers(conn, symbols)
    except Exception as exc:
        straggler_result = StragglerLinkResult(
            blocked={sym: f"straggler-linking raised: {exc}" for sym in symbols},
        )
    for sym, reason in straggler_result.blocked.items():
        if sym in state.symbol_blocked:
            state.symbol_blocked[sym] = True
            state.symbol_reason[sym] = (
                (state.symbol_reason.get(sym, "") + "; ") if state.symbol_reason.get(sym) else ""
            ) + f"straggler-link: {reason}"

    return state


@dataclass
class StragglerLinkResult:
    linked: "dict[str, list[str]]" = field(default_factory=dict)   # symbol -> [trade_id, ...] newly linked
    blocked: "dict[str, str]" = field(default_factory=dict)        # symbol -> reason (last blocking reason this cycle)


def _link_stragglers(conn, symbols: "list[str]", *, window_s: float = 300.0) -> StragglerLinkResult:
    """Delayed-visibility straggler linking (money-readiness review
    2026-09-20, P1: "the periodic reconciliation cycle does not implement
    the promised late-fill matcher"). For each symbol, finds `fills` rows
    with no trade_fill_links row yet (store.unlinked_fills) and tries to
    match each one against currently-unlinked observed_trades using the
    SAME exact-conservation matcher migration.py uses
    (engine.match_legacy_fill) — never a nearest-timestamp guess.

    Idempotent: an already-linked trade is excluded from the candidate
    pool (store.already_linked_trade_ids, refreshed after each successful
    link within this same call so two fills rows in one cycle can never
    both claim the same trade), and a fills row with nothing new to link
    against is silently skipped, not re-reported, every subsequent call.

    "no candidate trades in window" is NOT reported as blocked — it is the
    ORDINARY, expected state for a fill whose real trade simply hasn't
    been observed yet (the common case this function exists to eventually
    resolve, not evidence of a problem). Every OTHER match_legacy_fill
    outcome (no exact-conserving subset despite candidates existing,
    ambiguous, or too large a pool) DOES block that symbol — a genuine
    data-quality signal, not routine latency."""
    result = StragglerLinkResult()
    already_linked = store.already_linked_trade_ids(conn)
    for sym in symbols:
        unlinked_rows = store.unlinked_fills(conn, sym)
        if not unlinked_rows:
            continue
        candidate_trades = [t for t in store.load_observed_trades(conn, sym) if t.trade_id not in already_linked]
        for row in unlinked_rows:
            unlinked = UnlinkedFill.from_fills_row(row, window_s=window_s)
            match = engine.match_legacy_fill(unlinked, candidate_trades, already_linked=already_linked)
            if match.blocked:
                if match.reason != "no candidate trades in window":
                    result.blocked[sym] = f"fills.id={match.fill_id}: {match.reason}"
                continue
            matched = [t for t in candidate_trades if t.trade_id in match.matched_trade_ids]
            for t in matched:
                store.link_trade_to_fill(conn, t.trade_id, match.fill_id)
            result.linked.setdefault(sym, []).extend(match.matched_trade_ids)
            already_linked = already_linked | set(match.matched_trade_ids)
            candidate_trades = [t for t in candidate_trades if t.trade_id not in match.matched_trade_ids]
    return result


def _scope_trades(conn, symbols_for_scope: "list[str]") -> "list[ObservedTrade]":
    """Reads from the PERSISTED store, not this cycle's fetch — a trade
    from a window a previous cycle already upserted but couldn't checkpoint
    (that scope was blocked, e.g. a different scope's anomaly) must remain
    visible here even after the account-wide watermark has since advanced
    past it and stopped re-fetching it. Sourcing this from `coverage.trades`
    instead would silently and permanently drop that window from every
    later scope-level identity check — caught while writing this, not by a
    test that happened to exercise it, so it's called out explicitly here."""
    return [t for sym in symbols_for_scope for t in store.load_observed_trades(conn, sym)]


def _reconcile_scope(
    conn, exchange: ExchangeAdapter, *, scope: str, asset: str, side_asset_is_quote: bool,
    window_until_iso: str, symbols_for_scope: "list[str]", tolerance: float,
) -> ScopeResult:
    prior = store.latest_checkpoint(conn, scope)
    fresh_balance = exchange.fetch_balance_total(asset)

    if prior is None:
        # Bootstrap: pin every currently-known trade for this scope as
        # already-covered by the opening balance — nothing to diff against
        # yet (see module docstring).
        known_ids = [t.trade_id for t in _scope_trades(conn, symbols_for_scope)]
        checkpoint_id = engine.new_checkpoint_id()
        store.commit_checkpoint(
            conn, currency_scope=scope, window_since=None, window_until=window_until_iso,
            balance_after=fresh_balance, covered_trade_ids=known_ids, checkpoint_id=checkpoint_id,
        )
        return ScopeResult(scope=scope, bootstrapped=True)

    window_since_iso = prior["window_until"]
    window_trades = [
        t for t in _scope_trades(conn, symbols_for_scope)
        if engine.ts_ms(t.exchange_timestamp) > engine.ts_ms(window_since_iso)
    ]
    deposits: "list[LedgerMovement]" = exchange.fetch_deposits(asset, since=window_since_iso) or []
    withdrawals: "list[LedgerMovement]" = exchange.fetch_withdrawals(asset, since=window_since_iso) or []

    fee_deltas: "list[float]" = []
    if side_asset_is_quote:
        for t in window_trades:
            fee_deltas.extend(store.fee_correction_deltas_for_trade(conn, t.trade_id))

    result = engine.check_balance_consistency(
        prior_balance=prior["balance_after"], trades=window_trades, deposits=deposits,
        withdrawals=withdrawals, fresh_balance=fresh_balance, side_asset_is_quote=side_asset_is_quote,
        quote=asset, tolerance=tolerance, fee_correction_deltas=fee_deltas,
    )
    if not result.consistent:
        return ScopeResult(
            scope=scope, blocked=True,
            reason=f"residual={result.residual:.10f} (expected={result.expected_balance:.10f}, actual={result.actual_balance:.10f})",
            residual=result.residual,
        )
    checkpoint_id = engine.new_checkpoint_id()
    store.commit_checkpoint(
        conn, currency_scope=scope, window_since=window_since_iso, window_until=window_until_iso,
        balance_after=fresh_balance, covered_trade_ids=[t.trade_id for t in window_trades],
        checkpoint_id=checkpoint_id,
    )
    return ScopeResult(scope=scope, blocked=False, residual=result.residual)


def resolve_exit_quantity(exchange: ExchangeAdapter, symbol: str, tracked_qty: float) -> float:
    """design §7: reconciliation-blocked prevents advancing derived
    accounting/journal state on ambiguous evidence; it must NEVER prevent
    refreshing the exchange-authoritative quantity used to size an actual
    exit. Callers sizing a protective SELL while ANY block flag is set
    should use this instead of a locally-derived position quantity.

    Money-readiness review 2026-09-19, P1 finding ("explicit handling for
    reserved quantities, external holdings, and failed fresh reads"):

    - Reserved quantities: fetch_balance_total() reads the exchange's
      `total` (design §6 — never `free`/`used`), the same figure a
      resting native stop's 100%-reservation doesn't reduce. The exit's
      own order placement already cancels any resting native stop BEFORE
      selling (bot/execution/live_executor.py, the 2026-08-27 deadlock
      fix), so by the time this quantity is actually used the reservation
      is gone — sizing against `total` up front is correct, not a race.
    - External holdings: the fresh EXCHANGE balance can legitimately be
      LARGER than `tracked_qty` — someone else's coins in the same
      account (ADOPT_EXTERNAL_HOLDINGS=false, the default) or an
      under-tracked fill — and this function must never let an
      unreconciled accounting state cause the bot to sell more than it
      itself believes it owns. The result is therefore capped at
      `min(fresh, tracked_qty)`: only ever corrects DOWNWARD, toward
      exchange-confirmed reality (protecting against a stale/overstated
      local quantity that would otherwise get rejected as an oversell),
      never upward into inventory the bot has no basis to claim.
    - Failed fresh reads: falls back to `tracked_qty` unchanged — an exit
      must still have SOME number to act on, and a failed fresh read is a
      worse moment to give up sizing an exit entirely than to fall back
      to the last-known value."""
    try:
        base = base_asset(symbol)
        fresh = exchange.fetch_balance_total(base)
    except Exception:
        return tracked_qty
    if fresh < 0:
        return tracked_qty
    return min(fresh, tracked_qty)
