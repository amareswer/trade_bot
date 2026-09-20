"""
Per-cycle reconciliation orchestration (design §2, §7, §9 Tier B).

run_cycle() is the one entry point bot/main.py calls periodically
(cfg.accounting.reconcile_interval_s). It:

  1. Does ONE account-wide, coverage-proof paginated fetch_my_trades pull
     (kraken_adapter's `symbol=None` mode — see that module's docstring for
     why this must be account-wide, not per-symbol, to keep Kraken's own
     `count` meaningful).
  2. Observes every newly-visible trade into `observed_trades`.
  3. Runs the §2 balance-identity check separately for the shared CAD cash
     pool (account level, once) and for each traded symbol's base-asset
     inventory (design §6 — cash is never checked per-symbol).
  4. Detects fee corrections on trades re-observed inside the watermark's
     safety-margin overlap window, recording them via the EXISTING
     TradeLog.log_fee_adjustment (design §4) — never a silent overwrite.
  5. Detects genuine data-integrity anomalies (a trade_id re-observed with a
     different price/amount/side) and blocks rather than guesses.
  6. Returns a BlockState — the ONLY thing bot/main.py's BUY gate needs to
     consult. Exits are NEVER gated by this (see resolve_exit_quantity()).

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
from bot.accounting.engine import ExchangeAdapter, LedgerMovement, ObservedTrade


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
    don't patch."""
    account_cash_blocked:  bool = False
    account_cash_reason:   str  = ""
    symbol_blocked:        dict = field(default_factory=dict)   # symbol -> bool
    symbol_reason:         dict = field(default_factory=dict)   # symbol -> str
    coverage_blocked:      bool = False   # a fetch itself was incomplete this cycle
    coverage_reason:       str  = ""
    last_cycle_at:         str  = ""
    scope_results:         list = field(default_factory=list)   # list[ScopeResult]

    def blocked_for_buy(self, symbol: str) -> bool:
        """§7: account-cash block covers every symbol; a coverage failure
        this cycle is treated the same way (nothing new can be trusted to
        be reconciled while the retrieval itself is incomplete)."""
        return (
            self.account_cash_blocked
            or self.coverage_blocked
            or self.symbol_blocked.get(symbol, False)
        )

    def explain(self) -> str:
        parts = []
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


def _apply_fee_corrections(conn, trade_log, coverage_trades: "list[ObservedTrade]") -> FeeSweepResult:
    """For every re-observed trade already known to observed_trades, compare
    its freshly-fetched fee to the current effective fee and record a
    correction via the EXISTING fee_adjustments table if it changed. Also
    detects a genuine integrity anomaly instead of ever silently
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
        trade_log.log_fee_adjustment(
            order_id=fresh.order_id, symbol=fresh.symbol, delta_fee=delta,
            fee_currency=fresh.fee_currency,
            adjustment_id=engine.fee_correction_adjustment_id(fresh.trade_id, revision),
        )
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

    now_ms: injectable wall-clock override, real time if None. Every
    watermark/safety-margin computation in this function is written in
    terms of this value, never a bare `datetime.now()` call buried deeper
    in the call graph — this is what makes checkpoint-race and
    delayed-visibility scenarios deterministically testable instead of
    depending on real elapsed wall-clock time between test steps."""
    state = BlockState(last_cycle_at=engine.now_iso())
    try:
        prior_watermark_iso = store.recover_watermark(conn, account_scope)
        coverage = engine.retrieve_with_coverage_proof(exchange, None, since=prior_watermark_iso)
    except Exception as exc:
        state.coverage_blocked = True
        state.coverage_reason = f"retrieval raised: {exc}"
        return state

    if not coverage.complete:
        state.coverage_blocked = True
        state.coverage_reason = coverage.reason
        return state

    now_ms = now_ms if now_ms is not None else _now_ms()
    prior_watermark_ms = engine.ts_ms(prior_watermark_iso) if prior_watermark_iso else None
    new_watermark_ms = engine.compute_safe_watermark(
        coverage, now_ms=now_ms, previous_watermark_ms=prior_watermark_ms,
        safety_margin_ms=int(safety_margin_s * 1000),
    )
    if new_watermark_ms is None:
        # No previous watermark AND coverage somehow incomplete is already
        # returned above; this only remains reachable if safety_margin
        # itself is misconfigured to exceed "now" — treat conservatively.
        state.coverage_blocked = True
        state.coverage_reason = "could not compute a safe watermark this cycle"
        return state
    new_watermark_iso = engine.iso_from_ms(new_watermark_ms)
    # Real moment fresh_balance is captured below — DELIBERATELY separate
    # from new_watermark_iso above. new_watermark_iso is the conservative
    # RETRIEVAL cursor (now - safety_margin): it only bounds what `since`
    # the NEXT cycle's account-wide fetch_my_trades pull uses, so a
    # delayed-visibility trade keeps getting re-fetched until it appears.
    # A per-scope checkpoint's own `window_until`, below, must instead be
    # the ACTUAL moment its `fresh_balance` was read — using the
    # conservative watermark there instead (an earlier bug caught while
    # writing this) double-counts any deposit/trade whose real timestamp
    # falls inside the safety-margin gap between the two: it's already
    # baked into `fresh_balance` (read at real "now") but its own
    # timestamp is still later than the conservative watermark, so a
    # naive `since=watermark` re-fetch of deposits/trades next cycle would
    # count it a SECOND time on top of a prior_balance that already
    # reflects it.
    balance_as_of_iso = engine.iso_from_ms(now_ms)

    # Persist the account-wide retrieval watermark itself (a separate,
    # dedicated checkpoint scope — balance_after/covered_trade_ids are
    # unused for this row, it exists purely so store.recover_watermark()
    # has something to recover on the next cycle/restart).
    store.commit_checkpoint(
        conn, currency_scope=account_scope, window_since=prior_watermark_iso,
        window_until=new_watermark_iso, balance_after=0.0, covered_trade_ids=[],
        checkpoint_id=engine.new_checkpoint_id(),
    )

    # Fee-correction / integrity-anomaly sweep over EVERY trade this fetch
    # returned (including ones already known, inside the overlap window).
    sweep = _apply_fee_corrections(conn, trade_log, coverage.trades)

    # Observe every genuinely new trade — but never one flagged anomalous
    # this cycle (it's already represented by its ORIGINAL stored payload;
    # upserting is a no-op for an already-known trade_id anyway, this
    # `continue` is just explicit about why).
    for t in coverage.trades:
        if t.trade_id in sweep.anomalous_trade_ids:
            continue
        store.upsert_observed_trade(conn, t)

    # -- account cash (once, shared across every symbol) -----------------
    cash_result = _reconcile_scope(
        conn, exchange, scope="CAD", asset=quote, side_asset_is_quote=True,
        window_until_iso=balance_as_of_iso, symbols_for_scope=symbols,
        tolerance=exchange.cash_tolerance(quote) if hasattr(exchange, "cash_tolerance") else 0.005,
    )
    state.account_cash_blocked = cash_result.blocked
    state.account_cash_reason  = cash_result.reason
    state.scope_results.append(cash_result)

    # -- per-symbol base-asset inventory -----------------------------------
    for sym in symbols:
        base = base_asset(sym)
        tol = exchange.amount_tolerance(sym) if hasattr(exchange, "amount_tolerance") else 1e-8
        sym_result = _reconcile_scope(
            conn, exchange, scope=sym, asset=base, side_asset_is_quote=False,
            window_until_iso=balance_as_of_iso, symbols_for_scope=[sym], tolerance=tol,
        )
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

    return state


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


def resolve_exit_quantity(exchange: ExchangeAdapter, symbol: str, fallback_qty: float) -> float:
    """design §7: reconciliation-blocked prevents advancing derived
    accounting/journal state on ambiguous evidence; it must NEVER prevent
    refreshing the exchange-authoritative quantity used to size an actual
    exit. Callers sizing a protective SELL while ANY block flag is set
    should use this instead of a locally-derived position quantity. Falls
    back to `fallback_qty` (whatever the caller already had) if the fresh
    read itself fails — an exit must still have SOME number to act on, and
    a failed fresh read is a worse moment to give up sizing an exit
    entirely than to fall back to the last-known value."""
    try:
        base = base_asset(symbol)
        return exchange.fetch_balance_total(base)
    except Exception:
        return fallback_qty
