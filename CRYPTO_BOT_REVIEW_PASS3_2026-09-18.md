# Crypto review, pass 3 — updated order lifecycle and recovery

Reviewed HEAD `b0ed71d` on 2026-09-18. No trading code, configuration, HALT status or exchange orders were changed. All additional reproductions used mocked exchanges, temporary state files and temporary SQLite databases.

## Verdict

Not ready to close the execution-safety review. The new functions fix several individual scenarios, but the full submission → settlement → bookkeeping → acknowledgement lifecycle still has gaps. Fix the bounded cases below before calling this robustness work complete. The deferred portfolio simulator and signal/backtest redesign are separate work and need not be added to this patch.

Verified:

- `.venv/bin/python -m pytest tests/crypto tests/shared -q`: **692 passed in 15.61s**.
- Strategy hash: **5c6540eccbd2f45f**.
- Previous entry-fee reproduction is now correct: $0.80 BUY fee + $1 gross SELL profit − $0.40 SELL fee produces net **−$0.20**, win rate **0**, PF **0**.
- Entry/exit market fallbacks now use `_create_order_persisted`; pending submission identity is saved for the submission-exception case; partial/open stops retain their IDs; the journal now queues entries and preserves P&L/time; execution fingerprint has actual startup/stamping callers.
- Full repository suite and pinned historical backtest were not independently rerun in this pass. The two stock fixture failures were verified as unrelated in the preceding review.

## 1. P0 — Submission acceptance clears recovery state before settlement/accounting

Location: `bot/execution/live_executor.py:1630`, especially the successful/adopted return branches through `:1735`.

`_create_order_persisted()` clears and saves `_pending_submission` as soon as `create_order()` returns, or a lookup adopts an order. An acknowledgement is not proof of terminal settlement or durable fill accounting. The executor has not yet polled the order, applied quantities/fees, or queued the fill at that point.

**Reproduced:** create and all subsequent polls return an accepted market order with `status="open", filled=0`. Call `execute(BUY, 90000, 0.001)` twice. Both return without a fill, but the exchange mock sees **two submissions**, with `pending_submission=None` and an empty journal. The first order is still unresolved. This scenario does not require a submission exception.

A crash after the marker-clearing save and before fill accounting has the same missing-order-identity problem even if the returned order was already filled. Opposite-side submissions also bypass the same-side pending check and can overwrite its single marker; unknown SELL handling still attempts to rearm protection before determining the SELL outcome.

Fix: persist lifecycle states such as submitted/accepted/partially filled, retaining client and exchange IDs until terminal status and all observed fill deltas are durably accounted. Reconcile pending orders independently of the next strategy signal. Define conflicts between entry, exit and protection orders; never overwrite an unresolved intent simply because its side differs.

Acceptance: accepted-but-still-open order across two calls and a restart produces one submission; an accepted/filled response followed by a process death before accounting is recovered exactly once; partial settlements retain remaining-order tracking.

## 2. P0 — Failed intent persistence still allows submission

Locations: `_create_order_persisted()` at `bot/execution/live_executor.py:1696`; `_save_state()` at `:1289`.

The wrapper invokes `_save_state()` before calling the exchange, but `_save_state()` catches write errors and only flips `_state_write_healthy`. The wrapper does not check that result. The BUY health check happened earlier, before this particular write failed.

**Reproduced:** mock `atomic_write_json` to raise `OSError("disk full")` while calling BUY execution. The exchange still receives **one create-order call**, despite write health becoming false. There is no durable intent for restart recovery.

Fix: make the durable intent write explicitly succeed or fail; abort a new entry before network submission if it fails. Define a separate, documented emergency-exit policy for persistence failures instead of implicitly sharing the same behavior.

Acceptance: failed pre-submit persistence causes **zero entry submissions**. Test a write failure after the entry health check, not only an already-false health flag.

## 3. P0 — Protective order placement bypasses persisted submission tracking

Locations: `_place_native_stop()` at `bot/execution/live_executor.py:1082` and `_place_native_trailing_stop()` at `:1117`.

The new wrapper covers trade-entry/exit submissions and their fallbacks, but not native protective-order creation. These helpers still submit directly, catch a timeout, and lose the attempted order identity. The next protection sync can submit again while the first stop may be live.

**Reproduced:** position 0.002 BTC; both `sync_protective_stop(89000)` calls encounter a create-order timeout. The mock receives **two stop submissions**, and `pending_submission` remains **None**.

Fix: include protective orders in persisted lifecycle/reconciliation with a role/type distinct from ordinary SELLs. Ensure startup adoption and runtime recovery use the same ownership information. Apply this to both static and trailing stops.

Acceptance: response-lost stop placement across repeated sync calls/restart creates at most one stop until the first outcome is established. Test entry and protective orders independently; a stop cannot be mistaken for a strategy exit merely because both use SELL.

## 4. P1 — Normal fill writes and replay do not share idempotency keys

Locations: `bot/main.py:1233`, `:1346`, partial-TP/intracandle logging paths; replay at `:553`; `bot/execution/live_executor.py:1325`.

The unique index and keyed replay are useful, but ordinary `trade_log.log_fill()` call sites still omit `exec_key`. A normal insert therefore stores NULL. A crash before acknowledgement leaves a keyed journal entry; replay inserts it as a distinct row, because NULL and its execution key do not conflict.

**Reproduced with a real temporary TradeLog and executor journal:** record a fill normally, leave its journal unacknowledged, run `_replay_pending_journal_entries()`. Result: **two rows**, keys **["dry_run#1", NULL]**. Replay-to-replay idempotency does not cover normal-write-to-replay idempotency.

There is also a restart collision: `_journal_seq` starts at zero for every executor process and is neither saved nor restored. Successive partial fills of the same stop after restart reuse an earlier `exec_key`.

**Reproduced:** record and acknowledge one delta of `persisted-stop`, restore the executor from that state file, record its next delta. Both keys are **`native-stop:persisted-stop#1`**. A keyed insert/replay can consequently discard a genuinely different fill as a duplicate.

Fix: assign one durable event identity and pass it through `Order`, normal logging, journal replay and acknowledgement. Use exchange execution IDs where available; otherwise use persisted identifiers/cumulative boundaries that remain unique across restarts. Acknowledge the exact event key, not merely the first matching order ID. Preserve chronological event application when protection sync discovers another fill inside an outer fill handler.

Acceptance: normal commit → crash before ack → replay leaves one row; two different deltas of the same order separated by restart leave two rows; repeated replay of either leaves the count unchanged. Exercise actual main-loop logging with real temporary SQLite, rather than mocking `log_fill` throughout.

## 5. P1 — Partial-stop quantity is incremental, but price and fee remain cumulative

Locations: `bot/execution/live_executor.py:994`–`:1030`, `_record_stop_triggered_fill()` at `:850`.

The code subtracts previously recorded quantity, but applies the order's cumulative average price and entire cumulative fee to the new quantity. Changing prices across fills produces wrong proceeds/P&L; successive fee snapshots double-charge earlier fees.

**Reproduced:** first snapshot fills 0.001 BTC at average 90,000 with cumulative fee $0.36. Second snapshot closes at cumulative 0.002 BTC, average 95,000, cumulative fee $0.76. Correct total proceeds are $190 less $0.76 = **$189.24**. Current cash increase is **$183.88**, and charged fees are **$1.12**.

Fix: reconcile individual exchange trades, or persist cumulative quantity, quote cost/proceeds and fee totals, then account for deltas in every dimension. For this example, the second delta's price is 100,000, not 95,000. Preserve these counters across restart. Handle fees finalized after quantity stops changing and fee-currency differences explicitly.

Acceptance: changing-average partial fills reconcile exactly to exchange cumulative cost and fee; repeated polls are no-ops; restart between fills has identical results to uninterrupted processing.

## 6. P1 — Discovered partial exits put an open position into cooldown and erase exit state

Location: `bot/main.py:1291`, `_process_discovered_sell_fill()`.

The new consumer updates inventory, but unconditionally clears trailing/ATR state and calls `sm.on_fill(SELL)`. That enters COOLDOWN regardless of residual inventory, and this helper never restores LONG for a partial fill.

**Reproduced with actual PositionManager/TradingStateMachine:** start LONG with 0.002 BTC and a trail peak of 95,000; process a discovered 0.001 BTC stop fill. Remaining position is **0.001 BTC**, but state is **COOLDOWN**, trail peak **0**, ATR stop **0**. Strategy SELLs are suppressed during cooldown and after it transitions to IDLE; software forced exits are a separate path, so this does not mean every possible exit is disabled.

Fix: derive position state from remaining inventory. Preserve original entry/ATR/trailing state for the residual; transition to cooldown and release capital only when flat. Apply this contract to every partial-exit path, including discoveries nested inside another fill handler.

Acceptance: discovered partial exit leaves LONG with appropriate residual protection; full exit leaves flat/cooldown and releases its slot; a later strategy SELL remains actionable for the residual position.

## Recommended next task and stopping criterion

Give Claude Code these six findings as a bounded follow-up. The central change is to join the already-added pieces into one lifecycle rather than add another independent guard for each example.

Require deterministic integration tests with a fake exchange/clock, real executor and main bookkeeping helpers, and temporary SQLite/state files. Test crashes before submission, after acknowledgement, after partial settlement, after normal DB commit and before journal acknowledgement. Assert the joint invariants: no duplicate live orders, no forgotten active orders, exactly one row per execution event, executor/PositionManager agreement, cash equal to confirmed proceeds minus fees, and LONG whenever bot inventory remains.

Once these scenarios pass, the earlier reproductions stay green, and the 692-test baseline plus new tests passes, it is reasonable to stop this execution-hardening pass. That is a stopping point for this engineering scope—not a finding of profitability or authorization to change HALT. The unchanged narrow strategy hash/pinned backtest does not test these failure-recovery paths.
