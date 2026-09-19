# Crypto bot review — pass 8, September 19, 2026

Reviewed commit `0bdef22` (`fix: address correctness gaps identified in PASS-7 review`). Working tree was clean before this report. No implementation, configuration, HALT state, or live orders changed.

## Verification

`.venv/bin/python -m pytest tests/crypto tests/shared -q`: **761 passed in 15.53s**. The prior two-partial-fill fee reproduction now returns the correct net P&L, approximately **−$0.20**, rather than −$1.40. The tracked, terminal, fully filled stop now has a dedicated cash-free startup path and tests for preserved basis across a crash. Fee-only recovery and rearm-fill delivery also have concrete implementation changes.

Nevertheless, **three remaining P1 defects were independently reproduced** using the repository's stateful `FakeExchange`, actual `LiveExecutor` constructors, temporary state files, mocked CCXT construction/configuration, and disabled Telegram sends. No live exchange interaction was used. The full repository suite and pinned backtest were not independently rerun in this pass.

## 1. P1 — A pending protective placement still recovers a full exit using zero cost basis

Locations: `bot/execution/live_executor.py:1194` (`_resolve_pending_protect_submission_at_startup`), especially its call to `_journal_native_stop_execution_without_cash_effect`; `:393` startup basis-capture guard.

The preserved startup basis is supplied by the new **tracked-stop** recovery path, but the separate **pending protect submission** path still calls the cash-free journal helper without a basis override. `_sync_position()` has already zeroed the current portfolio basis when the placement filled the entire position while offline. Cash is now correct, but the journal still fabricates profit.

**Reproduction:**

1. Seed $1,000 cash, 0.002 BTC, and an $85,000 cost basis.
2. Create a protective order at $78,000. Persist its identity in `_pending_submissions['protect']`, with no `_native_stop_order_id` yet. This models placement accepted before tracking was committed.
3. Let the fake exchange fully fill 0.002 BTC at $78,000 with a $0.32 fee while the executor is offline.
4. Construct a new executor with the same state file and exchange.

Actual: cash **$1,155.68**, position zero, journal and executor gross P&L **+$156**. Correct gross P&L is **−$14**. This is not the fixed double-credit path; it is the other startup entry point into the same historical recovery operation.

**Required fix:** Use the preserved historical basis in pending-placement recovery too. Preserve it across restart even when the outstanding identity lives in `pending_submissions['protect']` rather than `_native_stop_order_id`. The current capture guard treats “no tracked native ID” as permission to overwrite the snapshot; after an interrupted startup this can overwrite the pending placement's preserved basis with zero.

**Acceptance:** Pending protective placement with full terminal fill, partial fill, and crash after position sync but before recovery. Repeat restart. Assert cash, realized P&L, journal fields, and real SQLite rows, not only balance convergence.

## 2. P1 — Flat-position recovery forgets a stop that is still open on the exchange

Locations: `bot/execution/live_executor.py:1266`–`:1320`, `_recover_flat_native_stop_at_startup`; related status handling in `_retry_unresolved_stop_recovery` at `:1322`.

The new flat-position helper fetches an order, recovers any execution, then unconditionally clears tracking. It never checks whether the returned order is terminal and never cancels an open remainder. A position being flat does not establish that its independent resting stop has terminated.

**Reproduction:**

1. Seed 0.002 BTC and a tracked open stop `O1`; persist executor state.
2. Set the fake exchange's BTC balance to zero to model an external close/transfer, leaving `O1` open.
3. Restart against that exchange.

Actual: executor `_native_stop_order_id=None`, journal empty, but `fetch_open_orders('BTC/CAD')` still returns **`O1`**. The bot has lost ownership of a live order. It may affect inventory acquired later; at minimum its cancellation/outcome is no longer managed. This reproduction establishes the orphaned order, not a claim that Kraken permits an unfunded execution.

**Required fix:** Separate recovery of historical fill deltas from cancellation of still-live protection. If the exchange returns open/partial/unknown status, retain durable ownership and confirm cancellation before dropping the identity. Account for fills racing cancellation without reapplying cash movements already covered by the startup balance snapshot. Likewise, a successful fetch in the unresolved-history retry must not be treated as proof of terminal settlement.

**Acceptance:** Flat account plus open-unfilled stop, open-partial stop, cancellation timeout, and cancellation racing a fill; static and trailing orders. Every nonterminal order remains tracked or explicitly queued for resolution. Clearing its identity requires a confirmed terminal outcome.

## 3. P1 — A second unresolved historical stop overwrites the first

Locations: `bot/execution/live_executor.py:318` single `_unresolved_stop_recovery` dictionary; `:1295` and `:1556` assignments; `:1332` retry consumer.

The historical reference is now independent of current protection, which is necessary, but it holds only one order. While that order's lookup continues failing, the bot can establish replacement protection for residual inventory. If the replacement later needs historical recovery too, the next assignment silently replaces the first order's frozen baseline and basis.

**Reproduction:**

1. Seed 0.002 BTC at $85,000. Stop `O1` fills 0.001 BTC at $78,000, fee $0.16, and becomes terminal while offline.
2. Make `fetch_order()` raise `ccxt.RequestTimeout` during restart. `_unresolved_stop_recovery` correctly stores `O1`; remaining position is 0.001 BTC.
3. Establish and persist replacement stop `O2` for the residual. Let it fill 0.0005 BTC at $78,000, fee $0.08, and become terminal.
4. Restart while final-order lookups still fail. The pending reference now contains **only `O2`**.
5. Restore healthy lookups and restart again.

Actual recovered journal: only `native-stop:O2`, quantity **0.0005**, gross P&L **−$3.50**. Correct totals for the two executions are quantity **0.0015**, gross P&L **−$10.50**. Cash reflects both executions, but `O1` and its $7 loss disappear permanently from reporting.

**Required fix:** Use a durable collection keyed by order identity, with independent baseline/basis per historical order. Merge rather than overwrite, retry entries independently, and retire each only after its recovery transition is durably committed. Migrate the existing single-entry persisted representation.

**Acceptance:** At least two unresolved historical orders plus a current replacement; recover them in either order, with intermittent failures and crashes. Assert all execution quantities, fees, P&L, and SQLite records exactly once while current protection remains independent.

## Rearm queue and next work

The former ignored-return path now queues rearm-discovered fills and the live tick loop routes them to the bookkeeping consumer. That closes the direct in-process omission previously reported. The queue itself is **in memory**, not a persisted exactly-once event queue: `drain_discovered_fills()` clears it before consumer processing. Its documentation calls it durable while also acknowledging it is not persisted. Restart safety therefore relies on the separate persisted fill journal and reconstruction of downstream state. Do not describe a durable, acknowledged consumer guarantee as proven by the current drain test; broader restart/consumer integration coverage remains useful. This is a qualification of the implementation, not a fourth independently reproduced defect in this review.

For Claude Code: implement one consistent startup recovery policy across tracked stops, pending placements, and queued historical identities. Test terminal versus nonterminal status, flat versus residual inventory, and repeated crashes across every entry point. The recurring failures here come from separate callers applying different rules to the same exchange outcome.

Keep HALT and the existing resumption gate unchanged. These findings concern execution/accounting correctness, and these tests supply no new strategy-profitability evidence.
