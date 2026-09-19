# Crypto bot review — pass 11, September 19, 2026

Reviewed the supplied staged changes on top of HEAD `2872759`. The executor, main consumer, and two test files were modified; the pass-10 report was staged. This review concerns that staged implementation, not a new commit. No trading settings, HALT state, or live orders changed.

## Verification

`.venv/bin/python -m pytest tests/crypto tests/shared -q`: **773 passed in 19.23s**. Full-repository tests, fingerprint, and pinned backtest were not independently rerun here.

Three findings were independently reproduced. Recovery reproductions used actual LiveExecutor instances, the repository's stateful FakeExchange, temporary state files, mocked CCXT construction/configuration, and disabled Telegram sends. The protection-failure reproduction used the actual discovered-BUY consumer, real PositionManager/state machine/capital pool, and the test suite's fake protection executor. No live exchange calls were made.

## 1. P0 — Persisted checkpoint cap is reused after a newer restart balance sync

Locations: `bot/execution/live_executor.py:1309`–`:1335` cap-based accounting; `:1590` onward flat/retry recovery; `_retry_unresolved_stop_recoveries` passing each persisted entry's `checkpoint_qty_cap`; startup balance synchronization preceding these calls.

The cap describes how much quantity was covered by a particular balance checkpoint. It is persisted with the unresolved order, but a later restart synchronizes balances again without updating that checkpoint relationship. The old cap can then mark a fill as live even though the new sync already includes it. Quantity classification now overrides caller mode, so this happens inside startup recovery itself.

**Reproduction:**

1. Seed $1,000 cash and 0.003 BTC at an $85,000 basis, with a tracked stop at $78,000.
2. Offline, let the stop fill 0.001 BTC at $78,000, zero fees, leaving it open.
3. Restart with an empty open-order listing and direct-order lookup timeout. Balance sync correctly produces $1,078 and 0.002 BTC. Recovery is queued with cap 0.001.
4. Before another restart, let cumulative fill reach 0.002 BTC at the same price, terminal; exchange now holds $1,156 and 0.001 BTC.
5. Restart with healthy lookups. No further fills occur.

Actual executor: **$1,234 cash, zero BTC**. Correct exchange state: **$1,156 cash, 0.001 BTC**. The old cap covers only the first 0.001; recovery applies the second 0.001 as live on top of the fresh balance checkpoint.

**Required change:** Bind recovery accounting to the balance checkpoint it describes and reconcile that relationship whenever a newer authoritative sync occurs. Preserve journal progress separately from balance inclusion. A persisted numeric cap is not sufficient without checkpoint identity and lifecycle semantics. Do not erase missing journal/P&L just to align the cursor.

**Acceptance:** Repeated restarts while recovery is pending, with additional fills between restarts, both healthy and failing lookups, partial and full closure, and multiple pending orders. Executor balances and downstream inventory must converge with the exchange without applying any movement twice.

## 2. P1 — Quantity split applies the cumulative average price to both sides of the checkpoint

Location: `bot/execution/live_executor.py:1300`–`:1335`, especially `delta_price = delta_cost / new_delta` and the two accounting calls using that same price.

Even if the covered quantity is known exactly, it does not establish the covered cost or fee. The implementation applies one cumulative average price to both historical and live quantities and allocates fees proportionally. That is not exact when execution prices or fee settlement differ across the checkpoint.

**Reproduction:** Use the same initial position and first failed-lookup restart as finding 1. The first 0.001 BTC executes before the checkpoint at $78,000, giving synchronized cash $1,078. After startup, a second 0.001 executes at $82,000. FakeExchange then reports cumulative quantity 0.002, cumulative average $80,000, cumulative cost $160, and zero fees. Resolve the queued order on a live tick.

The cap correctly distinguishes 0.001 historical and 0.001 live quantity, but the code credits **$80** for the live part instead of **$82**. Actual executor cash is **$1,158**, versus correct exchange cash **$1,160**. Both report 0.001 BTC remaining. The journal also assigns the wrong individual prices/P&L to the historical and live portions.

**Required change:** Recover actual execution/cost boundaries, or reconcile against an authoritative economic snapshot rather than infer cost timing from quantity proportions. A cumulative order average plus a quantity cap cannot uniquely reconstruct per-checkpoint proceeds. Fee allocation has the same information limitation. Do not describe this split as exact without the additional evidence required to establish those dimensions.

**Acceptance:** Different prices and fees before/after the checkpoint, multiple executions per read, late fee corrections, and recovery through both the queue and adoption. Assert actual cash and per-execution P&L, not only quantities or a constant-price aggregate.

## 3. P1 — Ledger failure now prevents recovered-BUY protection from running

Location: `bot/main.py:1514` onward in `_process_discovered_buy_fill`: `trade_log.log_fill`, journal acknowledgment, and `alerter.fill` all run before the protection-sync block.

The successful-path ledger ordering is repaired, but the statement that protection timing is unaffected is incorrect. PositionManager is updated for a real BUY, then fallible reporting work executes before protection. If TradeLog raises, control never reaches stop placement/resizing. The previous ordering reached protection before that log write.

**Reproduction:** Call the actual consumer with a recovered BUY of quantity 1 at $100, no existing protection, native protection enabled, and STOP_LOSS_PCT 0.02. Use the existing fake protection executor and make `trade_log.log_fill()` raise `OSError('disk full')`.

Observed: PositionManager quantity **1**, exception propagates, and the executor's protection `sync_calls` is **empty**. The acquired position receives no stop from this consumer. This reproduction establishes the skipped protection attempt, not the behavior of a live exchange.

**Required change:** Preserve causal ledger ordering while ensuring logging/notification failures cannot skip required position protection. Retain unacknowledged durable events for ordered replay; keep reporting acknowledgment separate from whether protection was attempted. Do not simply return to writing SELL before BUY. An alert must not be a prerequisite for protecting an acquired position either.

**Acceptance:** Real temporary SQLite plus injected insert failure/lock failure; notification failure; first recovered fill and additional partial BUY requiring resize; immediate protective SELL; crash/replay. Protection must still be attempted, and successful replay must preserve BUY-before-SELL ordering and exactly-once fees.

## Design implication

The checkpoint quantity bound still rests on assumptions that are not universal: a net position drop does not identify one order's execution, and a flat account does not prove its stop fully executed (external closes/transfers were already covered in earlier review scenarios). The numerical reproductions above do not even require those external actors: repeated checkpoints and ordinary varying fill prices are sufficient to break the current split.

For the next pass, define checkpoint identity, execution/cost provenance, ledger progress, and live delivery as separate obligations. Extend the stateful harness across successive process lifetimes and real consumer/SQLite failure paths instead of validating only one checkpoint with a constant price.

Keep HALT and the existing resumption gate unchanged. These tests provide no new strategy-profitability evidence.
