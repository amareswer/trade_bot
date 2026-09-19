# Crypto review, pass 5 — working-tree executor changes

Reviewed 2026-09-19, including the uncommitted executor/test changes on top of `f856f39`. No implementation, configuration or live account changes were made by this review. Reproductions used mocked exchanges and temporary state/SQLite files; no real orders or Telegram messages were sent.

## Verdict and verification

**716 crypto/shared tests passed in 12.85s.** The new code addresses several reported scenarios in uninterrupted execution: ordinary partial fills retain pending state, known exchange order IDs are queried directly, empty canceled orders can be replaced, protective placement responses are dispatched, and native-stop late fees update executor cash.

This is not yet a complete fix. The previous restart cash finding was omitted from the implementation summary and remains reproducible. New intermediate saves introduce crash windows that lose or duplicate fill effects. Fee corrections and discovered fills still do not consistently reach the full bookkeeping path.

The full repository suite, strategy hash and pinned backtest were not independently rerun in this pass. A passing backtest would not exercise these persistence/recovery scenarios.

## 1. P0 — Progress and economic effects are persisted in separate, inconsistent snapshots

### Ordinary fills can disappear after a crash

Locations: `bot/execution/live_executor.py:2184` (`_record_order_delta`), `:2886` (caller), `:2895` (terminal clearing), and the later portfolio/journal writes in `execute`.

`_record_order_delta()` advances cumulative quantity/cost/fee and saves state **before** the corresponding inventory/cash changes and fill journal entry exist. A process death immediately after that save leaves progress saying the fill was accounted, although its economic effects were never applied. On restart the same exchange fill becomes a zero delta.

**Reproduced with failure injection:** start an ordinary BUY of 0.002 BTC; exchange reports an open partial fill of 0.001 BTC. Wrap `_record_order_delta()` to call the original and then raise a custom `BaseException`, simulating process death after its persisted snapshot. Restart using post-fill exchange balance and the same order snapshot. Recovery returns `None`, managed position remains **0 BTC**, and the journal has **0 entries**, despite **0.001 BTC** having executed.

Terminal processing also clears progress before the new fill effect has been persisted. That is another inconsistent intermediate boundary, not an atomic transition.

### Native-stop fills can be applied twice after a crash

Locations: `_cancel_native_stop()` at `bot/execution/live_executor.py:1133` and `_record_stop_triggered_fill()` at `:903`.

The latest native-stop implementation has the reverse ordering: it calls `_record_stop_triggered_fill()`, which saves the changed portfolio and new journal entry, **before** advancing `_native_stop_last_recorded_*`. A crash after the inner save leaves old progress alongside already-applied economics. Restart treats the same cumulative fill as new again, assigning it a fresh UUID that the ledger cannot identify as a duplicate execution.

**Reproduced with failure injection and temporary state:** start with 0.002 BTC and $1,000 cash; the stop fills 0.001 BTC at $90,000. Crash immediately after the original `_record_stop_triggered_fill()` returns, before its caller updates progress. Restore saved state and poll the identical fill. Result: position **0**, cash **$1,180**, journal **2 entries**. Correct values are **0.001 BTC**, **$1,090**, and **1 entry**. This test deliberately uses dry-run state restoration to isolate persisted bookkeeping from the separate live-balance problem below; cancellation/fill processing uses mocked exchange snapshots.

Fee-only adjustment likewise saves changed cash before its caller updates cumulative-fee progress, leaving an analogous repeated-charge crash window.

**Required fix:** assemble progress, economic effect, execution identity and journal event as one coherent durable transition. Do not write a snapshot declaring a delta consumed before its effect exists, or an effect applied with progress still behind. If using the existing atomic JSON snapshot, mutate the entire coherent state before one replacement write; ensure failure handling prevents later transitions from silently treating an uncommitted state as committed. A transactional journal/database is another option.

Acceptance: inject process death after every actual persistence call during open-partial and terminal fills, for BUY/SELL/protect roles. Restart must match an uninterrupted run in cash, managed inventory, cost basis, fill identities, ledger and cumulative progress. UUID uniqueness alone is insufficient: the same execution reprocessed after restart must retain its identity.

## 2. P1 — Previous restart cash double-deduction is still unfixed

Locations: constructor balance synchronization and pending-order recovery in `_create_order_persisted()`/`execute()`.

This was finding 3 in pass 4. It is absent from the latest list of fixes, and the relevant startup/accounting interaction remains unchanged.

**Reproduced again:** start with $1,000. A 0.001 BTC BUY at $90,000 fills; crash after the wrapper persists acceptance but before local fill accounting. Restart with exchange CAD **$910** and BTC **0.001**, then recover the pending completed order by its known exchange ID. Local cash becomes **$820**, not $910, because refreshed exchange cash already included the debit and normal fill accounting debits it again. The purchased BTC is initially classified as external holdings because ownership reconciliation precedes pending-order recovery.

**Required fix:** define a coherent startup baseline: reconcile pending executions/managed ownership and the post-fill balance snapshot without applying cash effects twice. Preserve cost basis and ledger history while distinguishing already-reflected cash movement from unapplied local bookkeeping. Test both BUY and SELL, partial fills, fees and shared slot accounting.

Acceptance: this exact recovery finishes at $910 and 0.001 managed BTC with one execution record and no fresh submission.

## 3. P1 — One protection sync can discover two fills, but returns only one

Location: `bot/execution/live_executor.py:1584`–`:1595`, `sync_protective_stop()`.

A cancel can reveal a terminal partial fill from the old stop. The remaining inventory can then be protected by a replacement that fills immediately. Both are real execution events. The final expression returns `placement_fill` when it exists, discarding `fill_order` from cancellation.

**Reproduced:** position 0.002 BTC. Old stop returns canceled with 0.001 BTC filled. Replacement response is closed with another 0.001 BTC filled. Executor inventory correctly becomes zero and its journal contains **two 0.001 BTC entries**, but the method returns only the **replacement's 0.001 BTC Order**. Its main-loop consumer therefore learns only half the actual inventory reduction, and the old-stop event is left pending until some other recovery path.

**Required fix:** emit/drain a sequence of execution events, not a single optional Order. Process every event exactly once in chronological order across PositionManager, state machine, capital pool, risk counter and TradeLog. Do not combine unrelated orders into a fictitious single exchange fill.

Acceptance: the two-fill scenario leaves executor and PositionManager both flat, releases the slot, writes both keyed fills and acknowledges both journal entries.

The separately acknowledged `_rearm_native_stop_after_failed_sell()` issue at `:1199` is still present: it discards `_place_native_stop()`'s discovered fill. Resolve it through the same event-consumption mechanism rather than leaving another exceptional bookkeeping path. This is an acknowledged open item, not a newly discovered claim.

## 4. P1 — Fee corrections are not consistently journaled or reflected in reports

### Native stops: cash is corrected, but the ledger is not

Location: `bot/execution/live_executor.py:974`, `_apply_native_stop_fee_only_adjustment()`.

The helper changes cash/fees and saves state, but emits no durable accounting-adjustment event for TradeLog. Once the original quantity fill has been logged and acknowledged, the late fee never reaches the database used for net reporting.

**Reproduced with temporary SQLite:** log and acknowledge an open native-stop fill with fee zero; poll the same quantity with a final $0.36 fee. Executor fees become **$0.36**, the database fee total remains **$0**, and the pending journal is empty. Thus the earlier fee-reporting problem remains even though executor cash now looks correct.

### Ordinary orders: fee-only deltas are still discarded outright

Location: `bot/execution/live_executor.py:2896` onward.

When `_delta_qty <= 0`, execution returns without applying `_delta_fee` or `_delta_cost`. A terminal snapshot also clears tracking. Ordinary orders consequently retain the original late-fee bug.

**Reproduced:** ordinary BUY initially open/partially filled at 0.001 BTC, fee zero; next snapshot canceled with the same filled quantity and fee $0.36. Recorded fees remain **$0**, and the pending submissions dictionary is emptied.

**Required fix:** use an idempotent accounting-adjustment event for fee/cost corrections, linked to the exchange execution/order and consumed by cash and ledger/reporting together. It should not fabricate a zero-quantity trade. Persist corrected cumulative progress atomically with the adjustment. Reconcile corrections independently of quantity for every role.

Acceptance: late fees update cash and the reporting ledger once; repeated snapshots and restarts do not duplicate the correction. Cost-only corrections and fee refunds need an explicit policy rather than silently disappearing behind positive-quantity checks.

## 5. P1 — Pending ordinary fills still rely on a future same-side strategy execution

Evidence from call-site review: `_pending_submissions`/`_order_progress` processing lives in `LiveExecutor.execute()` and its submission helper. `bot/main.py` replays pending journal entries at startup, but has no independent consumer polling ordinary pending submissions.

The new partial BUY code returns its first fill; normal main-loop bookkeeping then marks the position LONG. Subsequent BUY signals are suppressed by the state machine, so the second call to `execute(BUY, ...)` that the new delta tests manually make is not guaranteed to occur in production. A remaining order can fill without promptly updating inventory or resizing protection. A pending SELL whose exchange balance is already zero on restart can also be blocked by the executor's no-position guard before its pending order is reconciled.

This is a control-flow finding, not an additional full-loop reproduction in this pass. Existing drift alerts and startup journal replay do not supply the missing order-event consumer.

**Required fix:** reconcile pending orders independently of entry signals and before evaluating new orders. Route discovered fills/adjustments through the same consumer. Do not require a fresh trade decision merely to finish accounting for a previously submitted order.

Acceptance: one BUY decision starts an order, its first partial fill makes the bot LONG, and its remainder fills later while every later strategy signal is HOLD. Without calling execute with a second BUY, the loop must update holdings, fees, ledger and residual protection. Include restarts while orders are partially filled and pending sells that finish while offline.

## Recommended next step

Build the requested stateful fake-exchange/restart harness now, before another round of isolated fixes. It is the missing way to verify the core contract, not optional polish: the current regressions demonstrate that restarting **between completed function calls** does not test crashes **inside their multiple state writes**.

Prioritize atomic fill transitions and the still-open startup cash finding, then implement a common event queue/consumer covering ordinary execution, protection changes and accounting adjustments. Run uninterrupted and crash-injected versions of the same sequence and compare all economic state. Keep HALT and trading parameters unchanged. No strategy retuning or historical simulator expansion is required to close this bounded execution work.
