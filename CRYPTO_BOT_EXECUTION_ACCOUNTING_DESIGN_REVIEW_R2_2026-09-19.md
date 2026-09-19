# Execution-accounting design revision 2 — review

Reviewed the revised design and the relevant installed CCXT 4.5.56, CapitalPool, and reconcile_ledger source. No live calls, implementation changes, or tests were performed. HALT remains untouched.

## Decision

The separate SQLite obligations and explicit account-level blocking are substantial improvements. Proceed with a disposable, offline protocol prototype and its tests, not production integration or historical migration. Resolve the following concrete issues in that prototype/specification. This does not require another full rewrite of the design.

## 1. Balance convergence is not proof of complete event coverage

Section 2 still overclaims that a matching identity confirms the known event set and fails closed whenever the assumptions are violated. Two events can offset each other. For example, an unseen deposit and withdrawal of the same asset/amount can leave the balance unchanged; the identity passes while history is incomplete. Both can eventually appear in the required APIs, so eventual visibility does not exclude this counterexample. An unexplained event need not produce a nonzero residual.

Describe the identity as a balance-consistency check, with a separate history-completeness condition. Specify bounded retrieval windows, overlap, pagination under concurrent arrivals, and how coverage is established. A count of retrieved records is not proof that they came from one stable window. CCXT's unified fetch_my_trades result also discards the raw response count, so the proposed count-driven paginator needs an explicit retrieval interface.

Tier A supplies stronger evidence: CCXT parse_ledger_entry exposes raw balance as unified `after`, and signed raw amount becomes `amount` plus `direction`. It does not alone prove the fetched ledger is complete, latest, or a simultaneous cross-asset snapshot. Validate per-asset continuity, fee arithmetic, pagination and linkage before promising that it removes all reconciliation races. Permission availability remains unverified; the existing report text is not a live permission check.

## 2. The proposed migration is unsafe and contradicts the joint-check invariant

Actual `reconcile_ledger.py:155` onward matches a row to the nearest trade within five minutes using symbol and side. It **does not compare quantity or cost**, despite the design describing timestamp/side/amount matching. It even counts unmatched rows as matched for reporting. This heuristic must not decide durable migration identity.

An existing aggregate fill row can represent several exchange trades. Conversely, several similar fills can share the same time window. A one-to-one proximity match can both misassociate history and reinsert economics already present in an aggregate row. Require explicit migration links, conserved quantity/cost/fee totals, and manual resolution or blocking for ambiguity. Rehearse on a copy.

There is also a direct specification contradiction: migration marks `ledger_written_at` against an existing UUID-keyed row without replacing its key, while section 9 requires every ledger-written observed trade to have a row whose exec_key equals trade_id. Define a normalized migration mapping or an explicit rewrite procedure and update the invariant accordingly.

## 3. Deterministic ordering is not always sufficient for cost accounting

The claim that nothing depends on same-timestamp tie direction is false for the existing entry-fee allocation. A BUY followed by its SELL, both sharing the normalized timestamp, can be sorted SELL-first by opaque ID and recreate the unallocated-entry-fee error. CCXT's normalized timestamp is millisecond precision even if raw Kraken time has finer precision.

Retain available source precision and causal evidence. Define how ambiguous ties and late trades cause a fold to be rebuilt or blocked; lexical order is only a reproducibility convention. Sorting readers does not by itself repair an already-stored incorrect realized-P&L value. Test the actual fee and basis calculations, not just stable ordering.

## 4. The opening snapshot and shared-capital quantities remain underspecified

An opening balance must be a durable baseline with a defined included event set and cost basis, not a newly chosen current balance at every restart followed by replay of historical trades. Specify its creation, preservation, and replay boundary. ADOPT_EXTERNAL_HOLDINGS decides ownership policy; it does not establish historical acquisition cost or event coverage. Also state how ongoing delivery/rebuild works between restarts and how strategy state such as trailing peaks is preserved or deliberately reset.

CapitalPool tracks allocation budgets: `_slots` stores cash allocated to a position, not necessarily cash still held after the BUY. Allocated budgets plus free pool cash therefore cannot directly be compared to exchange quote cash. Specify current cash movements separately from budgets/invested principal, and do not add trade deltas again to a pool value that already incorporates them.

Fresh `fetch_balance().total` under a block is not by itself safe exit sizing: it includes external holdings and units reserved by existing orders. Require bot ownership, outstanding-order reconciliation and an explicit cancellation/verification path. If fresh reads fail, preserve known exchange protection and retain the unresolved state rather than guessing quantity.

## 5. Finish the transaction and precision contract before wiring it in

The schema has a checkpoint_id reference but no durable checkpoint table containing balances, currency scope, covered event set/window, and commit state. Define it and commit the checkpoint with its membership changes atomically. Account/exchange scope must be present in identity keys. Retrieval cursors must never advance past observations that failed to commit.

Fee-correction counters need persistent, idempotent revision state: repeatedly fetching the same revised fee must not emit a new `<n>` adjustment each time. Include correction state in the transaction tests.

Price tick size is not cash-ledger precision, and fiat ledger amounts need not be rounded to retail cents. Use exact decimal values from exchange payloads and validated currency/ledger rules. Do not hide accounting errors by introducing an assumed half-cent tolerance.

## Bounded next step

Write a small offline reference model using synthetic exchange events and an isolated temporary SQLite database. Test: offsetting unseen events; a fill between API reads; multi-page/late history; same-timestamp BUY/SELL; aggregate legacy-row migration; checkpoint commit failure; repeated fee revision; cash budget versus current cash; and three successive restarts.

The prototype should yield three separate results: history coverage, balance consistency, and ledger/delivery consistency. Readiness requires all three, not just a zero residual. Once these traces and invariants are concrete, use them as the implementation contract for replacing the production recovery path. No live-account access, permission changes, migration, or HALT change is needed to build this prototype.
