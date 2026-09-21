# Crypto bot — ledger-movement integration proposal (2026-09-21)

**Status: PROPOSAL ONLY. No production file is changed by this document.**
`bot/main.py`, `bot/accounting/reconciliation.py`, `bot/accounting/four_way.py`,
`bot/accounting/store.py`, and `bot/backtest/metrics.py` are all untouched. `logs/HALT`
remains engaged. The profitability gate (`CRYPTO_BOT_REVIEW_2026-09-12`'s review-deadline
decision) is completely independent of everything below and is not affected by it either way.

## 0. What this closes, and what it explicitly does not

This month's offline work (`bot/accounting/asset_movement_analysis.py`, built and hardened
across six same-day review passes, 54 tests) proved — using the account's own real BTC/CAD
and SOL/CAD history — three things production reconciliation cannot currently see:

1. A real BTC deposit (0.00037766 BTC, 2026-06-26T12:43:35Z) explains a shortfall the
   production `engine.causal_order()` correctly refuses to resolve on trades alone.
2. Four real SOL staking-reward ledger entries explain the SOL balance-agreement gap down to
   the exact digit (`0.0000035758`).
3. Trade `TDCRFZ-MWTNB-2NVHO6`'s ledger shows a fee genuinely settled in BTC quantity while
   `observed_trades.fee_currency` reports CAD — a **reporting valuation, not a real cash
   deduction** — and naive P&L math silently discards the fee-consumed quantity's real
   economic cost unless corrected (a conservation bug found and fixed in the offline module
   itself this same day).

None of this is wired into the live reconciliation cycle today. **This is the bounded next
step: a concrete proposal for how it could be, without weakening any existing guarantee** —
not a request to implement it, and not a request to let any of this touch a trading decision.

## 1. The three separations this integration must preserve

### a) Exchange asset balances
Ground truth, unchanged: `ExchangeAdapter.fetch_balance_total(asset)`, already used by
`_reconcile_scope()`'s bootstrap and balance-check paths. This proposal adds no second way to
read a balance and does not touch this function.

### b) Bot-owned inventory
Currently: `engine.fold_position()` / `fold_position_gross()`, driven **only** by
`observed_trades` rows linked to bot fills via `trade_fill_links`. **This proposal does not
change what counts as "the bot's own position."** Deposits, withdrawals, and rewards are
evidence that explains the *gap* between bot-owned inventory and the exchange balance — they
are never merged into bot-owned inventory itself. `PositionManager` / `LiveExecutor`'s own
live-tracked position (the number actually used for sizing and risk checks) is untouched;
this integration is read-only / reporting-only in this bounded pass.

### c) Known vs. unresolved cost basis and P&L
`asset_movement_analysis.py`'s `PnlAvailability` / `SellAttribution` (with its
`known_qty` / `unknown_qty` / `unmatched_qty` and the same three-way split now also applied to
fee-consumed quantity) already makes this distinction rigorously, offline. Production's
`bot/backtest/metrics.py` has no such concept — it computes a number or the strategy simply
isn't evaluated. **This proposal introduces a new, separate reporting concept
("external-inventory-adjusted P&L availability") that never touches `metrics.py` or any
PF/win-rate gate.** The existing, already-validated profitability floors (PF ≥ 1.2 net of
fees, etc.) keep meaning exactly what they mean today: bot-trade-only performance.

## 2. Proposed architecture (concrete, not implemented)

### 2.1 New schema — additive only
A new `ledger_movements` table, mirroring `observed_trades`'s own idempotency pattern:

```sql
CREATE TABLE ledger_movements (
    entry_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,           -- 'deposit' | 'withdrawal' | 'reward'
    asset TEXT NOT NULL,
    amount REAL NOT NULL,         -- signed, exactly as engine.LedgerMovement already models
    timestamp TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL          -- 'live' | 'manual_review' (see 2.3)
);
```

No change to `observed_trades`, `fills`, `trade_fill_links`, or `checkpoints`.

### 2.2 New observation step
A new `bot/accounting/ledger_observe.py`, mirroring `live_observe.py`'s own discipline:
`observe_ledger_movements(exchange, conn, asset, since)` calls `fetch_deposits` /
`fetch_withdrawals` (and, only where the account's "Query ledger entries" permission is
enabled, `fetch_ledger` filtered to `type == "staking"` for rewards), then upserts each into
`ledger_movements` using the **same conflict-vs-duplicate discipline already proven in
`asset_movement_analysis._dedup_by_id`**: an identical re-observation of a known `entry_id` is
a silent no-op; a **different** payload for a known `entry_id` raises rather than overwrites.
Gated behind a new `cfg.accounting.ledger_movements_enabled` flag, default `False` — inert
until explicitly turned on, mirroring `cfg.accounting.enabled`'s own existing pattern.

### 2.3 Base-currency-fee corrections stay human-reviewed, not auto-inferred
The cross-currency-fee check built this session (`scripts/ledger_reconciliation_audit.py`)
only ever reports "strongly suggests one real fee... not proven beyond this arithmetic check."
**This proposal does not promote that arithmetic check into an automatic correction.** A
`bot/accounting/known_fee_corrections.py` module holds a plain, version-controlled literal:

```python
# Each entry requires a human to have reviewed the ledger cross-check evidence
# (scripts/ledger_reconciliation_audit.py's own report) before adding it here.
KNOWN_BASE_CURRENCY_FEE_CORRECTIONS: dict[str, float] = {
    "TDCRFZ-MWTNB-2NVHO6": 0.00000044,   # reviewed 2026-09-21, see
                                          # logs/ledger_reconciliation_audit_20260921.md
}
```

This is a deliberate scope limit, not an oversight: auto-inferring "this fee was really
base-currency-settled" from a same-trade arithmetic match is exactly the class of unproven
inference this whole review chain has been careful never to silently trust.

### 2.4 New reconciliation step — read-only with respect to trading
A new `bot/accounting/ledger_reconciliation.py::run_asset_movement_check(conn, asset)`:
1. Reads `observed_trades` for `asset` (existing table, existing loader).
2. Reads `ledger_movements` for `asset`, split by `type` into deposits/withdrawals/rewards.
3. Calls `asset_movement_analysis.analyze_with_asset_movements()` **verbatim, unmodified** —
   this proposal reuses that already-tested function rather than reimplementing any of its
   logic.
4. Looks up `base_currency_fee_qty` from `KNOWN_BASE_CURRENCY_FEE_CORRECTIONS` (2.3) — never
   computed live.
5. Writes the result to a new, separate file, `logs/asset_movement_status_<ASSET>.json`, using
   the SAME two-phase (`in_progress` marker, then outcome) write discipline
   `bot/accounting/cycle_status.py` already established — never written into `checkpoints` or
   `observed_trades`, and **never read by `_accounting_enabled`'s existing BUY-blocking
   check.** Purely observational in this pass; gating BUYs on it is explicitly a separate,
   later proposal this one does not make.
6. Runs on the SAME `reconcile_interval_s` cadence as the existing accounting block, as an
   ADDITIONAL step after `run_cycle()` — its own try/except boundary, so a failure here can
   never affect the existing, already-validated accounting cycle's own success/failure.

### 2.5 Observability
`unified_dashboard.py` gains one new card, mirroring `_dynamic_universe_card()`'s existing
pattern, showing four numbers **that are never collapsed into one**: exchange balance,
bot-owned inventory, explained external inventory (net deposits + rewards − withdrawals), and
unresolved quantity (`unmatched_qty` + `fee_consumed_unmatched_qty` from the analyzer).

## 3. Acceptance fixtures (the plan; not yet built)

Using the evidence already captured and version-controlled in this session's own artifacts:

- **BTC fixture** — the 10 real `observed_trades` rows + the real deposit
  (`bde604f7268e2edec96adfacc4707cbc6d5acb219d33e88bd5162bb480c5c6bb`, 0.00037766 BTC) + the
  reviewed `TDCRFZ-MWTNB-2NVHO6` correction (2.3). Expected: `fetch_balance_total("BTC")==0.0`;
  `causal_order()` on trades alone still returns `None` (unchanged — this fixture must prove
  the EXISTING safety behavior is untouched, not just that the new check passes);
  `run_asset_movement_check` reports `ok=True`, `balance_agreement.agrees=True`.
- **SOL fixture** — the 2 real `observed_trades` rows + the 4 real staking-reward ledger
  entries. Expected: `ok=True`, `balance_agreement.agrees=True` against the live balance
  `0.0000035758`, `pnl_availability.available=True` (no unknown-basis sale in this round trip).
- **Restart fixture** — run `run_asset_movement_check` once, terminate the process, restart it
  against the SAME sqlite file, run again. Expected: byte-identical `AssetMovementResult`
  fields, zero new rows inserted into `ledger_movements` for already-seen `entry_id`s (relies
  on the primary-key upsert from 2.1/2.2, already proven idempotent at the pure-function level
  by the offline module's own `test_repeated_calls_are_reproducible_a_restart_replaying_evidence_is_safe`).
- **Duplicate-event fixture** — invoke `observe_ledger_movements` twice with overlapping
  `since` windows (simulating two overlapping polls). Expected: the second call's upsert of an
  already-seen `entry_id` with an **identical** payload is a silent no-op; a **conflicting**
  payload for the same `entry_id` (defensive case — should never happen from a real exchange)
  raises rather than silently overwriting, exactly mirroring
  `asset_movement_analysis._dedup_by_id`'s existing, tested contract.

## 4. Staged rollout — shadow first, always

1. Build `ledger_movements` + `ledger_observe.py` + `ledger_reconciliation.py` +
   `known_fee_corrections.py` as new, inert files — **no call site added to `bot/main.py` in
   this step.**
2. Point them at `logs/shadow/trades.db` — the SAME isolated database this session's shadow-mode
   work already established (see `deploy/PAPER_SHADOW_RUNBOOK.md`) — and run them manually via a
   standalone script, in the same style as `scripts/ledger_reconciliation_audit.py`, against
   real shadow-mode account activity across at least one real restart. This is the load-bearing
   validation step: it must demonstrate clean, repeatable, restart-safe results against a real
   (if isolated) account before anything touches the production path.
3. Only after that shadow run is clean: wire `run_asset_movement_check()` into `bot/main.py`'s
   existing accounting block, still behind its own off-by-default flag, still not read by
   `_accounting_enabled`'s BUY-blocking logic.
4. A **separate, later proposal** — not this one — would be required before this check is
   ever allowed to influence a trading decision (BUY-blocking, sizing, or the profitability
   gate). This document does not request that, and building it is out of scope here.

## 5. Explicitly out of scope

- No automatic inference of base-currency-fee corrections from live arithmetic — human-reviewed
  only (2.3).
- No change to `bot/backtest/metrics.py`, any PF/win-rate gate, or the review-deadline decision.
- No change to `PositionManager` / `LiveExecutor`'s own live position tracking or sizing.
- No BUY-blocking change of any kind.
- No production file touched by writing this proposal. `logs/HALT` untouched. No live order,
  no state mutation, no git commit made in producing this document.
