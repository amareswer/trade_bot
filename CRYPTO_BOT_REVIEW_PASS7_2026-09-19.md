# Crypto bot review — pass 7, September 19, 2026

Reviewed commit `993b63d` with a clean working tree before this report. This is a code review, not approval to resume trading. No implementation, trading settings, live orders, or HALT state changed.

## Verification and conclusion

` .venv/bin/python -m pytest tests/crypto tests/shared -q` completed: **753 passed in 16.50s**. I did not rerun the full repository suite or the pinned backtest in this pass; their reported results remain developer-supplied claims.

The new recovery and reporting paths contain **four independently reproduced defects**. Reproductions used the repository's stateful `FakeExchange`, actual `LiveExecutor` instances, temporary state files, mocked exchange construction/configuration, and disabled Telegram sends. No live exchange calls were made. The report reproduction calls the actual `_compute_live_metrics` function.

Keep the existing live-entry freeze and validation gate unchanged. Passing execution tests does not establish strategy profitability. The fixes below and the previously acknowledged rearm-consumer gap should be addressed before live resumption.

## 1. P0 — Fully executed offline native stop credits proceeds twice and invents profit

Locations: `bot/execution/live_executor.py:357` startup sequence; `:921` cost-basis reset; `:1182` flat-position branch; `:1450` live fill accounting helper. The cash-free recovery helper at `:955` also depends on the current cost basis, which can already have been erased.

Startup synchronizes cash and position before verifying native protection. If the tracked stop fully executed offline, `_sync_position()` sets position and cost basis to zero and saves. `_verify_resting_stop_on_startup()` then takes its early flat-position branch and calls `_cancel_native_stop()`. That method discovers the execution and uses the **live** `_record_stop_triggered_fill()` path, adding sale proceeds to cash that already includes them. The fill quantity is not capped when current position is zero, and realized P&L uses the now-zero basis.

**Reproduction:** Seed 0.002 BTC at an $85,000 basis, $1,000 cash, and a tracked stop at $78,000. Save state. While offline, let the fake exchange fully fill that stop for 0.002 BTC at $78,000 with a $0.32 fee. Construct a new executor against the same exchange and state file.

| Result | Correct | Actual after restart |
|---|---:|---:|
| Cash | $1,155.68 | **$1,311.36** |
| Position | 0 BTC | 0 BTC |
| Gross realized P&L | −$14.00 | **+$156.00** |
| Recovered journal P&L | −$14.00 | **+$156.00** |

**Required change:** Route startup stop recovery through cash-free execution recovery regardless of remaining inventory. Preserve the historical basis needed for that recovery before exchange synchronization can erase it, including across a crash between startup saves. Simply removing the flat-position early return is insufficient because the new cash-free helper would still calculate P&L from zero basis. Keep cancellation of genuinely unfilled stale protection separate from recovery of historical execution.

**Acceptance:** Fully filled offline static/trailing stops, partially filled then canceled stops, and terminal protective-placement intents must converge with uninterrupted execution for cash, inventory, gross P&L, fees, and journal/SQLite records. Repeat restart and inject a crash between position sync and stop recovery; neither should duplicate or lose effects.

## 2. P1 — An order-level fee correction is charged once per partial-fill row

Locations: `live_comparison.py:121` adjustment aggregation; `:194`–`:201` `_fee_or_zero`.

`_load_fee_adjustments()` returns one total correction per order ID. `_fee_or_zero()` adds that entire correction to every fill with the matching ID. Multiple fill rows for one ordinary or protective order are expected after the earlier partial-fill fixes. `_matched_adjustment_order_ids` only tracks attribution; it does not prevent repeated charging.

**Reproduction:** Pass two SELL fill dictionaries to `_compute_live_metrics`, each with `order_id="O1"`, quantity 0.5, gross P&L $0.50, zero original fees, symbol BTC/CAD, fee currency CAD, and valid timestamp/exchange fields. Pass `{"O1": 1.20}` as corrections. Gross profit is $1.00 and the order's correction is $1.20, so total net P&L must be **−$0.20**. Actual output is **−$1.40**, with `unattributed_fee_adjustments=0`.

**Required change:** Allocate each correction exactly once across its associated executions, with an explicit allocation policy. Preserve correct BUY-fee attribution when some entry inventory has already been sold. Applying the whole correction to an arbitrary first row can distort per-exit PF/win rate even if the total is repaired.

**Acceptance:** Real SQLite fixtures with multiple BUY and SELL partial fills sharing an order ID, repeated corrections, native-stop IDs, and partially open inventory. Assert that allocated correction totals equal the stored correction exactly once and that net P&L/PF/win rate agree with the chosen allocation convention.

## 3. P1 — Startup consumes fee-only native-stop corrections without journaling them

Location: `bot/execution/live_executor.py:1024`–`:1041`, `_recover_missed_native_stop_execution`.

The new recovery helper calculates and journals fee changes only inside `if new_delta > 0`. When quantity is unchanged but a fee was finalized while the bot was offline, it still advances `_native_stop_last_recorded_fee`. Future reconciliation therefore considers that unjournaled correction consumed.

**Reproduction:** Seed the same 0.002 BTC position and stop. Fill 0.001 BTC at $78,000 with a provisional zero fee; record that partial fill through `_cancel_native_stop()` while a queued cancellation failure leaves the remainder open. While offline, change cumulative fee to $0.36 without changing filled quantity. Restart, then cancel normally.

Observed: cash correctly reflects **$1,077.64**, persisted fee baseline becomes **$0.36**, but the only fill journal entry still has `fee_cost=0` and **no fee-adjustment event exists**, even after cancellation. The correction is permanently absent from reporting.

**Required change:** Recover independent fee deltas at startup using a cash-free fee-adjustment event, with the same canonical order identity used by the fill. Commit the adjustment and advanced baseline together. Do not call the live cash-deducting helper after balance synchronization.

**Acceptance:** Open and terminal stop snapshots with unchanged filled quantity and a newly finalized fee; repeated restarts; crash around the recovery save; replay into SQLite. Cash must remain exchange-authoritative and reporting must receive the correction exactly once.

## 4. P1 — A temporary final-order lookup failure permanently discards recovery identity

Location: `bot/execution/live_executor.py:1271`–`:1301`, tracked-stop-absent branch.

If the tracked stop is absent from open orders, the new final-state lookup can fail transiently. The exception branch explicitly logs that recovery “will not be retried,” then clears the order ID and cumulative progress and saves. A later healthy restart no longer knows which historical order to recover.

**Reproduction:** Seed 0.002 BTC at $85,000 and a tracked stop. Offline, the stop fills 0.001 BTC at $78,000 with a $0.16 fee and becomes terminal, leaving 0.001 BTC. Make `fetch_order()` raise `ccxt.RequestTimeout` during restart, then restore it and restart again.

Observed after both restarts: tracked ID is `None`, journal is empty, realized P&L remains **$0**, although the missing execution realized a **$7 loss** and cash reflects its proceeds. A transient outage has become permanent history loss.

**Required change:** Persist unresolved historical recovery independently of the currently resting protection slot. Retry its final-state lookup on subsequent ticks/restarts until resolved. An absent open-order listing is not enough to discard its execution history. Replacing protection for residual inventory must not overwrite the unresolved historical reference.

**Acceptance:** Timeout followed by recovery on a later tick and on a later restart; correct execution/P&L/fee journal exactly once; residual protection remains manageable while historical recovery is pending.

## Carried-over correctness gap

`_rearm_native_stop_after_failed_sell()` at `bot/execution/live_executor.py:1790` still ignores the fill returned by `_place_native_stop()`. A rearmed stop that immediately executes updates executor state, but the discovered execution does not reach the normal PositionManager/state-machine/capital-pool consumer. This remains a correctness issue before live resumption, not merely an optional harness enhancement. A durable execution-event consumer can address it without changing every `execute()` return contract; delivery must be idempotent across restarts.

## Suggested Claude Code order

1. Fix startup recovery as one coherent transaction design: full exits, preserved basis, fee-only corrections, and durable unresolved historical orders.
2. Correct order-level fee allocation in reporting.
3. Connect rearm-discovered executions to the bookkeeping consumer.
4. Extend the existing stateful harness to assert real SQLite rows and downstream position/state/capital consistency. Retain targeted regressions for all reproductions above.

Do not change strategy parameters, stamp profitability validation from these tests, or remove HALT as part of this work.
