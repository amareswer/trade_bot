# Crypto review, pass 4

Reviewed HEAD `f856f39` on 2026-09-18. Review only; no trading implementation, configuration or live account changes. All reproductions used mocked exchange responses and temporary state files. No real orders were submitted.

## Result

**707 crypto/shared tests passed in 15.76s.** The latest fixes improve the previously reported scenarios, including failed pre-submit persistence, normal-write/replay execution keys and partial-stop accounting at changing quantities. However, the execution lifecycle still has the five issue groups below. These remain within the existing hardening scope; they are not requests to expand into strategy tuning or a new portfolio simulator.

The full repository suite and pinned historical backtest were not independently rerun during this pass.

## 1. P0 — Terminal-state handling is still incorrect for positive partial trade fills and protective-order responses

### Ordinary partial fills

Evidence: `bot/execution/live_executor.py:2750`–`:2768`.

The successful execution tail calls `_resolve_pending_submission()` for any positive fill. Its comment explicitly equates “a real fill, whatever its quantity” with a terminal outcome. That equivalence is false: an order may have executed some quantity and remain open for the rest.

**Reproduced:** create/poll responses contain `status="open", filled=0.001, amount=0.002, average=90000`. `execute(BUY, 90000, 0.002)` returns an Order labeled FILLED, position becomes 0.001 BTC, and `pending_submissions` becomes `{}`. The remaining live quantity is no longer tracked by the pending lifecycle. A later fill cannot be reliably reconciled as a delta, and a later request can place another order.

Fix: separate recording an execution delta from closing an order lifecycle. Persist cumulative quantity/cost/fee accounting for ordinary trade orders as well as native stops; retain the pending order until confirmed terminal and all deltas are durably accounted. Do not infer terminality from positive quantity or a local OrderStatus.FILLED label.

Acceptance: open partial ordinary orders remain tracked, repeated snapshots are no-ops, later fills add only deltas, and restart midway gives identical cash/inventory/ledger to uninterrupted processing.

### Protective-order responses

Evidence: `_place_native_stop()` at `bot/execution/live_executor.py:1154`; analogous trailing-stop placement.

Placement/adoption assigns the returned ID to `_native_stop_order_id`, reports protection and clears the submission role without inspecting whether the response is already filled/canceled. Recovery through the wrapper can legitimately return a closed historical order.

**Reproduced:** `sync_protective_stop(89000)` receives a closed order with `filled=0.002` for the entire 0.002 BTC position. Afterwards, position remains **0.002**, `has_resting_stop` is **True**, and the fill journal remains empty. The exchange response says the stop already executed; local state says there is a held, protected position.

Fix: dispatch protective responses by actual status and cumulative fills. Adopt an open stop's real quantity/trigger/fees; book terminal fills through the shared consumer; do not label canceled/rejected orders as resting protection. A later sync might detect the error, but immediate incorrect tracking is not acceptable recovery.

Acceptance: open, open-partial, closed-filled, canceled-partial, canceled-empty and rejected protective responses all produce consistent holdings/protection/journal state without a second signal being required.

## 2. P0 — A missing result in the latest ten closed orders is treated as proof of non-submission

Evidence: `_find_untracked_entry_order()` at `bot/execution/live_executor.py:1708` onward; recovery at `:1842`.

Pending records now store `order_id`, but recovery ignores that known ID. It searches open orders and only the latest ten closed orders by client ID. If neither list contains a match, `confirmed_empty=True` permits a fresh submission. An order can be absent from this limited history page without never having existed, especially after a restart or a long outage.

**Reproduced:** seed a pending BUY with `order_id="old-order"`; mock the limited lists as empty while `fetch_order("old-order")` would return its completed fill. Execution creates **one new order**. The only ID actually queried via `fetch_order` is **`new-order`**, not the stored prior ID.

Fix: query the persisted exchange order ID first. When only client ID is known, reconcile a sufficient history interval with pagination/trade evidence or retain UNKNOWN; absence from a limited page is not affirmative rejection. Do not classify temporarily invisible submissions as confirmed failures.

Acceptance: an older completed order outside the latest page is recovered with zero new submissions; missing/unavailable direct lookup remains unresolved until there is adequate evidence; real confirmed rejections can still release the intent.

## 3. P1 — Restart balance synchronization and recovered-fill accounting double-apply cash effects

Evidence: constructor `_sync_cash()`/`_sync_position()` and recovery in `_create_order_persisted()` followed by the normal accounting tail in `execute()`.

On startup, cash is overwritten with the current exchange free balance. If an order filled before a crash but before local accounting, that refreshed cash already includes the fill's cost. Recovering the pending BUY through ordinary execution deducts its cost again.

**Reproduced:** original cash $1,000; exchange accepts/fills a 0.001 BTC BUY at $90,000; simulate process loss after the wrapper persisted acceptance but before executor fill accounting. Restart with exchange CAD cash **$910** and BTC **0.001**. Adopt the pending closed order and execute recovery. There are **zero new submissions**, but local cash becomes **$820**, rather than $910. The external-holdings guard initially classifies the newly acquired BTC as external because its pending intent is not reconciled before position sync.

Fix: define an explicit accounting baseline/order for startup. Reconcile pending executions and bot ownership with the exchange snapshot without both replacing balances and reapplying already-included cash effects. Recovered fills still require ledger rows and cost basis; that does not mean their cash delta should be deducted from an already-post-fill balance. Preserve external holdings and multi-symbol slot semantics.

Acceptance: real executor restart tests with post-fill mocked balances for both BUY and SELL; include full and partial fills, fees and shared capital. Verify cash, managed inventory, cost basis, reservations and ledger—not only restoration of the pending dictionary. In this reproduction recovery must finish at $910 cash and 0.001 BTC with one execution record.

## 4. P1 — Confirmed canceled limit attempts are re-adopted instead of replaced

Evidence: `_place_limit_order()` at `bot/execution/live_executor.py:2090`–`:2165`.

After cancellation is confirmed terminal with zero fills, the chase increments its retry counter but leaves the pending role populated. On the next attempt, the wrapper finds and adopts that canceled order in closed history. The final market fallback can adopt it again instead of submitting a market order. This is a functional regression caused by adding persistent tracking without releasing the intent at the chase's confirmed-zero-fill cancellation boundary.

**Reproduced:** one allowed reprice; first limit order never fills; cancellation returns `status="canceled", filled=0`; closed-order history contains it. With a fake zero-duration timeout to skip waiting, `_place_limit_order()` makes **one create call**, all submissions are **limit**, and the supposed final fallback returns the **canceled** order. No replacement limit or market order is sent.

Fix: resolve exactly the canceled attempt after confirming terminal status and processing any fills; then allow the intended next attempt to receive a new identity. Do not clear it on an unconfirmed cancellation. Ensure direct canceled-zero-fill paths resolve too.

Acceptance: confirmed canceled/unfilled attempt → a fresh reprice and eventual market fallback according to policy; canceled-with-fill → record it without duplicating size; unresolved cancel → no replacement.

## 5. P1 — Fee updates without additional filled quantity are discarded

Evidence: `_cancel_native_stop()` at `bot/execution/live_executor.py:1077`–`:1104`.

The new cumulative cost/fee accounting runs only inside `if new_delta > 0`. Fees can become available or be corrected while the filled quantity is unchanged. On a terminal snapshot the code clears the counters/ID, permanently losing that adjustment.

**Reproduced:** first snapshot has an open stop, 0.001 BTC filled at 90,000, fee zero. Second snapshot is canceled with the same 0.001 BTC filled and final fee **$0.36**. Recorded fees remain **$0**, and the stop ID is cleared.

Fix: reconcile quantity, cost and fee deltas independently. Represent fee-only adjustments without fabricating zero-quantity fills; persist their own idempotency/provenance. Handle late final fees on ordinary orders as well. Unknown fees should not silently become confirmed zero costs.

Acceptance: unchanged quantity plus increased fee adjusts cash/reporting once; repeated terminal snapshots do not reapply it; restart between the quantity fill and fee settlement preserves the result.

## Recommended implementation approach

The remaining defects are at transitions between the new components. Write a small, explicit transition table used by all order roles: unknown submission, accepted/open, partial/open, canceled-empty, canceled-partial, closed-filled, and fee-finalized. For each transition specify whether to retain intent, record a delta, change protection, release funds and allow another submission.

Use stateful fake-exchange integration tests that maintain a real order/history/balance relationship, rather than independently mocking each response without connecting its economic effects. Run each sequence uninterrupted and with a restart at every persistence boundary; final economic state must match.

Keep this follow-up bounded to these lifecycle fixes and their regression tests. Once they and the earlier cases pass, stop this engineering pass without mixing in the deferred strategy projects. An unchanged historical backtest does not exercise these recovery scenarios.
