# Crypto bot review — pass 9, September 19, 2026

Reviewed commit `d4bb1bd` (`Refactor unresolved stop recovery handling and enhance tests`). Working tree was clean before this report. No implementation, settings, HALT state, or live orders changed.

## Verification and result

`.venv/bin/python -m pytest tests/crypto tests/shared -q`: **766 passed in 16.47s**.

The new classification helper and multi-entry recovery collection address the previous review's immediate scenarios. However, **two P1 defects remain**, both independently reproduced using the repository's stateful FakeExchange, actual LiveExecutor instances, temporary persisted state, mocked CCXT construction/configuration, and disabled Telegram sends. No live exchange calls were made. Full-repository tests and the pinned backtest were not rerun in this review.

The shared helper still assumes every recovered execution is already reflected in startup balances, even when it is called later during live ticks. Also, deduplication within the recovery collection does not prevent the same exchange order from acquiring a second owner in active native-stop tracking.

## 1. P1 — Live retries treat post-startup execution as already synchronized history

Locations: `bot/execution/live_executor.py:703` (`reconcile_pending_orders`), `:1337` (`_reconcile_historical_stop_order`), `:1465` (`_retry_unresolved_stop_recoveries`).

Every queued recovery calls the cash-free historical helper. That is appropriate for an execution already included in the startup balance snapshot. It is not appropriate for further fills or finalized fees arriving after that snapshot. The same queue now deliberately retains open orders, so those orders can execute during the current process. The retry journals the execution and advances its baseline but leaves executor cash/inventory stale. It also returns no discovered Order to the downstream PositionManager/state-machine/capital-pool consumer.

**Reproduction A — a fill after startup:**

1. Seed $1,000 cash, 0.002 BTC at an $85,000 cost basis, and an open tracked stop `O1` at $78,000. Save state.
2. During restart, make the open-order listing return no orders and the direct order lookup raise `ccxt.RequestTimeout`. This exercises the explicitly supported inconclusive-list/direct-lookup branch: `O1` moves into unresolved recovery. Balances still correctly show $1,000 and 0.002 BTC.
3. Restore healthy queries. Call `reconcile_pending_orders()` once while `O1` remains open and unfilled.
4. The fake exchange then fills 0.001 BTC at $78,000, fee $0.16, leaving the order open. Call `reconcile_pending_orders()` again.

| Result | Exchange / correct | Executor / actual |
|---|---:|---:|
| Cash | $1,077.84 | **$1,000.00** |
| BTC position | 0.001 | **0.002** |
| Fill quantity journaled | 0.001 | 0.001 |
| Discovered events returned to caller | One SELL | **Empty list** |

The execution is consumed by the recovery cursor but never applied to current holdings or delivered through the live bookkeeping path.

**Reproduction B — no new quantity, only a later fee:**

Let `O1` fill 0.001 BTC at $78,000 with provisional zero fee and become terminal before restart. Make its final lookup time out during startup; exchange cash synchronizes to $1,078. After startup, finalize its fee at $0.36 and retry successfully. Actual exchange cash is **$1,077.64**, executor cash remains **$1,078.00**, and the journal records the $0.36 fee. The new balance movement is never applied locally.

**Required fix:** Explicitly distinguish execution already covered by balance synchronization from new movement after it. Preserve the relevant accounting checkpoint and ensure unresolved orders transition to ordinary live reconciliation, or perform an authoritative reconciliation that accounts for the transition coherently. New live fills must reach the normal bookkeeping consumer exactly once. Do not simply switch every queued event to cash-mutating accounting: that would reintroduce double-crediting pre-startup executions.

**Acceptance:** An unresolved order that stays open after startup and then partially/fully fills; fee-only changes after startup; fills between balance/order queries; repeated retries/restarts. Assert executor cash/inventory, PositionManager/state machine, capital allocation, fee/P&L journal, and SQLite rows against the exchange and uninterrupted execution.

## 2. P1 — Recovery queue and active-stop adoption independently book the same order

Locations: `bot/execution/live_executor.py:1588` startup retry; `:1622` onward untracked-stop adoption; `:1784` (`_adopt_untracked_stop`); `:1465` recovery retry; `:1912` live stop-fill accounting.

On restart, an unresolved queued order that now reads back open stays in `_unresolved_stop_recoveries`. The later open-order discovery can also adopt that exact order into `_native_stop_order_id`. Adoption does not transfer ownership or remove/merge its recovery entry. The two paths subsequently maintain separate cumulative baselines and independently journal the same delta with different UUID execution keys.

**Reproduction:**

1. Seed the same unfilled `O1`, inventory, and basis as reproduction A.
2. Restart with an empty open-order listing and direct-lookup timeout so `O1` becomes queued unresolved.
3. Restore healthy queries and restart again. Observe both:
   - `_native_stop_order_id == 'O1'`;
   - `_unresolved_stop_recoveries` still contains `order_id='O1'` with its own baseline.
4. Let `O1` partially fill 0.001 BTC at $78,000, fee $0.16.
5. Run `reconcile_pending_orders()`; then queue a cancellation failure in FakeExchange and run `_cancel_native_stop()`, which independently discovers the same still-open partial fill.

Actual journal contains **two** `native-stop:O1` SELL events of **0.001 BTC** and **−$7 P&L each**. Executor realized P&L is **−$14**, although only **0.001 BTC** executed and its true gross loss is **−$7**. Different generated `exec_key` values mean the SQLite unique index cannot collapse these duplicate economic events.

**Required fix:** Enforce one authoritative execution-progress owner per exchange order across pending submissions, historical recovery, adoption, and active protection. Adoption must atomically transfer or merge recovery state rather than create a second independent cursor. Ensure a crash during ownership transfer cannot revive both paths or discard unconsumed deltas. Deduplicating only by generated fill UUID or only within the historical list is insufficient.

**Acceptance:** Queued-open order adopted on restart, followed by incremental fills and cancellation; run consumers in both orders; crash before and after ownership transfer; real SQLite replay. Exactly one journal execution and one downstream economic effect for each exchange fill delta.

## Next implementation pass

For Claude Code, make the accounting invariants explicit before changing callers:

- One execution-progress record per exchange order, regardless of which subsystem discovered it.
- A clear boundary between movements included in synchronized balances and movements that must still update current cash/inventory.
- Live discovered executions delivered to downstream consumers, with replay semantics tested alongside persisted executor state.

Keep the existing HALT and resumption gate unchanged. The passing suite demonstrates the tested scenarios, not complete execution correctness or new evidence of strategy profitability.
