# Crypto review, pass 6 — recovery, reporting and freeze decision

Reviewed the supplied working-tree/index changes on 2026-09-19. At inspection, HEAD remained `f856f39` and the reported changes were staged, including the new harness; they were not present as a new commit in the visible log. This review covers those staged changes. No implementation, trading settings, orders or live-account state were changed.

## Result and operating recommendation

**738 crypto/shared tests passed in 16.34s.** The harness and independent pending-order reconciliation are substantive improvements. However, the remaining native-stop recovery loss is not merely an unused reporting-counter discrepancy. A real fill and its realized loss disappear from the recovery journal, and the new fee-adjustment table is not consumed by the live-performance report. Additional recovered BUY quantity also lacks guaranteed native-stop resizing.

Keep live strategy entries paused. Continue isolated paper/shadow measurement. Preserve protective exits for any real holdings; a pause on entries should not automatically disable position protection.

Local read-only checks found:

- `logs/HALT` exists.
- `LIVE_TRADING=true`, `DRY_RUN=false`, native stop protection enabled.
- `RISK_HALT_BLOCKS_STOPS=false`.
- Saved BTC/CAD and SOL/CAD state files, timestamped September 19, show zero managed positions, no native stop IDs and no pending submission roles.

These are local configuration/snapshot findings, **not a fresh exchange reconciliation or proof of the running process's configuration**. Current HALT blocks BUYs and strategy SELLs while allowing SL/TP exits. It does not cancel already-open exchange orders or undo fills. Kraken describes stop orders as independent exchange orders that trigger when their conditions are met: [official stop-loss documentation](https://support.kraken.com/au/articles/7699391647892-stop-loss-orders). Maintain the distinction between blocking a new decision and managing an existing obligation.

Execution fixes alone cannot establish a profitable strategy. The saved September 13 research still reports pinned-window net PF 0.82 BTC and 0.94 SOL. Those are historical/model-dependent figures, not a fresh valuation or a reason to expect future gains. The repository's existing resumption rule requires genuinely new post-September-12 validation, net PF at least 1.2 for both assets across its required windows, and comparison with buy-and-hold return/drawdown; March 12, 2027 is the latest review date, not an automatic resume date. No new validation was run in this review.

## 1. P1 — Native-stop startup recovery skips unrecorded executions and realized P&L

Locations: `bot/execution/live_executor.py:1068`–`:1099`, `_verify_resting_stop_on_startup`; `tests/crypto/test_crash_consistency_harness.py:331` onward.

The new tracked-stop startup branch seeds cumulative quantity/cost/fee directly from the exchange after balance synchronization. This avoids deducting the same cash movement again, but it never journals the previously unrecorded execution or updates its realized P&L before declaring that cumulative progress consumed.

The harness currently compares only cash/inventory for protection recovery and explicitly excludes the fee counter. It does not assert equal realized P&L or recovered execution records. This hides a larger problem than the documented counter discrepancy.

**Reproduced using the project's own stateful FakeExchange and crash injection:**

| Result after both stop fills | Uninterrupted | Crash/restart |
|---|---:|---:|
| Cash | $1,155.68 | $1,155.68 |
| Remaining BTC | 0 | 0 |
| Realized gross P&L | **−$14** | **−$7** |
| Journaled SELL quantity | **0.002 BTC** | **0.001 BTC** |
| Journaled executions | **2** | **1** |

Scenario: 0.002 BTC cost basis $85,000; stop fills 0.001 at $78,000 and later the remaining 0.001 at $78,000. Crash after recording the first fill in memory but before its atomic save, then restart as the existing harness does. Balance sync restores cash/inventory; startup reseeding silently consumes the first fill without recovering its execution record or $7 loss.

Required fix: before advancing tracked native-stop baselines, compare persisted execution progress with exchange progress and recover missing fill/fee events and realized P&L. Do not reapply cash movements already included in the synchronized exchange balance. Also cover a tracked stop that fully fills while offline and is absent from open orders, and unresolved protective-placement intents. Ordinary `buy`/`sell` startup recovery does not cover the `protect` role.

Acceptance: cash, inventory, cost basis, realized P&L, fees, execution quantities and final SQLite records converge with the uninterrupted run. “Same balance” is not enough when reports and validation depend on execution history.

## 2. P1 — Live reports still exclude the new fee-adjustments table

Locations: `live_comparison.py:80` (`_load_fills`), `_compute_live_metrics`; `bot/data/trade_log.py:221` (`total_fee_adjustments`) and `summary`.

Fee adjustments now reach a durable table, which closes the earlier persistence gap. However, `live_comparison._load_fills()` still selects only from `fills`. Neither the report's fee allocation nor TradeLog.summary reads the adjustment events. Adding an unused summation helper does not make net reporting complete.

**Reproduced with real temporary SQLite:** one BUY at $100, one SELL at $101, initial fees zero, then a stored $2 SELL fee adjustment. The adjustment table correctly contains $2. The existing report returns **net P&L +$1 and win rate 100%**, while the actual net result is **−$1**.

Required fix: consume adjustments in shared performance accounting and attribute them to the correct execution/closed quantity using persistent order/execution linkage. Preserve currency and incomplete-basis handling. Merely subtracting all adjustments from a total is insufficient to fix per-trade PF/win rate or open-position fee attribution.

Acceptance: the exact case reports a net loss; repeated adjustment replay does not duplicate costs; partial exits and entry-fee adjustments allocate correctly; dashboard/report totals agree with the economic ledger.

## 3. P1 — Independently recovered BUY quantities do not resize native protection

Locations: `bot/main.py:1466`, `_process_discovered_buy_fill`; intra-candle protection handling around `:3340`.

The new BUY consumer updates inventory/risk/ledger but deliberately does not arm or resize protection. Its docstring says existing per-tick machinery will do so within one tick. The loop contains a conditional trailing-stop activation swap, not an unconditional reconciliation of stop coverage after every discovered BUY delta. If ATR protection is active, trailing activation has not happened, or trailing protection is already active, that swap does not repair the quantity mismatch.

**Reproduced with the stateful FakeExchange and actual BUY bookkeeping consumer:** first BUY fill 0.001 BTC with a native stop covering 0.001; remaining BUY quantity fills and `reconcile_pending_orders()` plus `_process_discovered_buy_fill()` run. Executor and PositionManager both hold **0.002 BTC**, while the existing native stop still covers **0.001 BTC**. Protection shortfall is **0.001 BTC**.

Required fix: after every inventory increase, reconcile protection quantity immediately using the established exit policy, without depending on a new entry signal or trailing activation. This does not require recomputing fresh indicators just to resize an already-known stop. For a first recovered fill with no prior protection, establish the configured fallback protection explicitly. Route any fills discovered during replacement through the list consumer.

Acceptance: all recovered inventory is covered by confirmed protection or an explicit unresolved-protection recovery state; test static ATR stops, not-yet-armed trailing stops and already-active trailing stops, including restart and HALT during settlement.

## 4. P2 — The stateful fake exchange miscalculates balances when cumulative average changes

Location: `tests/crypto/test_crash_consistency_harness.py:96`, `FakeExchange.simulate_fill()`.

The fake writes cumulative order cost as `cumulative_filled * avg_price`, but changes quote balance by `delta_qty * avg_price`. The provided average is cumulative, not the latest execution's price. The balance store and order store therefore cease to agree when fills execute at different prices.

**Reproduced:** start with $1,000 and sell 0.001 BTC at cumulative average $90,000, then reach cumulative 0.002 BTC at average $95,000. Order cost is correctly $190, but fake cash is **$1,185**, instead of **$1,190**. All existing harness fill sequences use an unchanged average, so they miss this inconsistency.

Required fix: move balance by the difference between new and old cumulative quote cost, independently of quantity and fee deltas. Model free versus reserved balances for open orders, especially to validate startup sizing. Expand the harness to check real temporary SQLite, PositionManager/state machine/capital pool and native protection, not just executor snapshots. Inject crashes around actual durable writes and acknowledgements as well as selected helper returns.

Acceptance: fake balance changes equal cumulative order cost changes across varying-price partial fills; the uninterrupted and restart runs have equal economics and execution records. Include full offline stop fills and late fee corrections.

## Known open item and next action

`_rearm_native_stop_after_failed_sell()` still discards a discovered fill. The user has correctly disclosed it, but it remains a correctness gap before resuming live entries. Returning a list from execute is one possible design, not the only one: a shared durable fill-event queue/consumer can deliver rearm discoveries while retaining execute's existing return contract.

Recommended order: recover missing native-stop journal/P&L events; include adjustment events in reports; reconcile protection after recovered BUYs; correct and broaden the harness. Retain the current entry HALT while completing these bounded fixes and collecting fresh paper/shadow evidence. Fixing them is necessary for reliable operation, but does not by itself satisfy the strategy's resumption gate.
