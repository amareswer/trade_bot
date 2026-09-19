# Execution-accounting design review — September 19, 2026

Reviewed `CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_2026-09-19.md` against the repository and installed CCXT 4.5.56 source. No implementation, tests, HALT, or exchange state changed. No live API calls were made. Tests were not rerun for this documentation review.

## Decision

Keep the direction: per-execution evidence, canonical order ownership, explicit reconciliation blocking, and joint verification are appropriate. **Revise the specification before implementation.** The proposed schema/fold does not yet establish its claimed checkpoint guarantees. This is a design review, not another request to patch the live recovery path.

## 1. Blocking: a checkpoint ID does not prove which trades its balance includes

Section 3.3 reads balances, then trades, and labels all fetched trades covered by that checkpoint. These calls are not one atomic exchange snapshot.

Counterexample: balance returns $1,000 and 1 unit; a SELL then executes for $100; trade history returns that SELL. Marking its ID checkpoint-covered leaves cash at $1,000 instead of $1,100. Applying every newly observed trade instead fails in the complementary case where the SELL occurred before the balance response and was already included. Unique trade IDs establish identity, not snapshot inclusion. Order aggregate checksums can pass in both cases.

Required specification: define how a checkpoint's included event set is established or verified, how API visibility lag and simultaneous fills are handled, and what happens when that proof cannot be obtained. A locally generated timestamp/counter is an identifier, not an exchange sequencing guarantee. Any proposed repeated-read or bounded-history protocol needs explicit assumptions, failure behavior, and adversarial tests; do not claim exactness merely from matching one pair of reads.

Add trades occurring between every pair of API reads, delayed visibility, and movements unrelated to tracked orders to the matrix.

## 2. Blocking: one applied-ID set still conflates independent obligations

The schema defines `applied_trade_ids` as folded into cash/position/journal, while SQLite delivery can fail independently of the JSON save and runtime PositionManager delivery. A checkpoint can cover an execution that has not yet been journaled; a journal row can exist before a downstream consumer succeeds. These are different states.

Specify separately: observed canonical execution data; verified balance-checkpoint coverage; committed accounting progress; durable pending ledger delivery; and downstream delivery/reconstruction. These can be represented by a transaction model rather than numerous flags, but their crash semantics must be explicit. Store the immutable execution payload, not only IDs plus aggregate caches that require future network access to reconstruct.

Choose the persistence authority and outbox/acknowledgment protocol. SQLite plus a separate JSON write is not automatically one transaction. State what happens after each possible successful write and before the next. Include bootstrap/migration from current synthetic exec keys and aggregate historical fill rows so introducing exchange IDs does not insert the same historical economics again.

## 3. Blocking: identity does not establish ledger order or immutable fees

Section 2.3 says trade-ID keys make replay follow chronological timestamps. A uniqueness key does not sort records. `live_comparison.py` currently reads `fills ORDER BY id`; later insertion of a historical BUY can still follow its SELL. Current Order.exec_key values default to UUIDs (`bot/execution/executor.py`), rather than the described order-id-plus-role keys.

Specify durable event ordering, same-timestamp handling, late arrivals, and how affected cost-basis/fee calculations are rebuilt. Timestamp order alone needs a tie/ambiguity policy; opaque IDs are not presumed chronological.

Also specify corrections to an already-seen trade's fee/cost and separate fee events. Set membership plus duplicate-insert suppression will otherwise discard a corrected execution payload. Terminal status must not automatically mean all fee reconciliation is finished. Define currency-aware fees and precision rules; a quantity epsilon does not define cost/fee tolerance.

## 4. Correct the verified CCXT facts and define complete retrieval

Direct inspection of installed CCXT 4.5.56 establishes:

- `fetch_my_trades()` calls `privatePostTradesHistory`, then injects each response dictionary key as raw `trade['id']`. For private records with `ordertxid`, `parse_trade()` uses `id`, falling back to `postxid`; it does not choose the raw numeric `trade_id` in that branch. Requiring raw `trade_id` would reject valid records even when the injected trade transaction ID is present.
- `fetch_order_trades()` is labeled `emulated` in capabilities, but its implementation requires supplied trade IDs and calls **`privatePostQueryTrades`** in batches. It does not simply filter `fetch_my_trades()` as the document states.
- `fetch_my_trades(since=...)` expects milliseconds and converts `since / 1000` to an integer API start value. An opaque highest trade ID cannot be passed as `since`. Its `limit` is supplied to parsing/filtering; the implementation does not show an automatic pagination loop. Symbol filtering is performed after the account trade-history response is fetched.

Define account/exchange-scoped canonical keys, timestamp overlap, pagination/end conditions, same-second fills, and a coverage cursor that advances only after completeness is established. Low bot trade frequency does not prove the account history fits a single page. A checksum may detect incompleteness but does not itself retrieve the missing records or prevent perpetual blocking.

These findings were verified offline through `inspect.getsource(ccxt.kraken.fetch_my_trades)`, `parse_trade`, and `fetch_order_trades`; no live capability/permission guarantees were tested.

## 5. Blocking: account balance is not a complete bot position ledger

Section 2.4 proposes reconstructing position state from balances plus later trades; section 6 compares PositionManager cost basis with the exchange balance. A balance does not encode acquisition cost, realized P&L, ownership of pre-existing holdings, or trailing/state-machine state.

Define opening inventory/cost basis, external/manual fills, transfers, deposits/withdrawals, fee currencies, shared quote cash across symbols, and total versus available/reserved balances. Reconstructible position accounting needs an explicit opening state and all relevant movements. Risk/state-machine reconstruction may also need persisted policy state beyond executions.

The four-way check should compare like-for-like quantities at one reconciliation boundary. Reusing the same history retrieval is useful, but matching four derived views against one incomplete input stream does not prove completeness; retain independent balance/order coverage checks. Unknown external movements must remain explicit, not be attributed to one bot order.

## 6. Specify blocked-state and exit behavior, without triggering implementation now

A per-symbol BUY block is a useful default, but unreconciled shared cash can affect other symbols too. Define the scope of each discrepancy and which consumer clears it. An unresolved account-level cash mismatch must not be treated as safe merely because another symbol's own record is clean.

Preserve protective exits, but specify sizing/cancellation when local holdings are uncertain so a stale local quantity does not authorize an incorrect exit. This needs explicit handling, not a blanket removal of exits.

Section 6's "blocked symbols aside" pass condition must not allow a global resumption check to pass while an affected symbol or shared funding pool remains unresolved. Report unresolved status distinctly from verified convergence. HALT stays engaged and no deployment, live-account operation, or profitability decision follows from approving this design.

## Recommended next deliverable

Revise the specification with:

1. A concrete checkpoint inclusion/verification protocol and counterexample traces showing why it is sound under its stated assumptions.
2. A transaction/outbox model separating economic coverage from ledger and downstream delivery, including migration and correction events.
3. A source-verified retrieval/identity/pagination contract.
4. Explicit opening balances, ownership, basis, shared-capital and blocked-state rules.
5. Table-driven tests for those protocols in isolation, followed by joint exchange/executor/PositionManager/SQLite checks.

Only then implement the replacement. "By construction" should be the conclusion of those invariants and tests, not an assumption attached to replacing a scalar with a set.
