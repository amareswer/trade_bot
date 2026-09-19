# Follow-up review of the crypto robustness changes

Reviewed HEAD `3cf0154` on 2026-09-18. Review only: no trading implementation, configuration, or HALT changes. Reproductions used mocked exchanges and temporary state/SQLite files.

## Verdict

Useful improvements landed, but P0-1, P0-2, P1-3 and P1-4 should not yet be marked fully fixed. The remaining problems below are observable in the updated implementation, including cases the new tests do not cover.

Verified independently:

- Crypto/shared suite: **657 passed in 29.25s**.
- Weekly stock monitor: **16 passed, 2 failed**. The failing fixtures use September 7 timestamps; `_scan_log()` uses the actual current date and a seven-day lookback. Both the tests and monitor implementation are unchanged between pre-review commit `6fbbbdd` and HEAD. This supports the reported pre-existing, date-dependent failure diagnosis. The full 1,117-test suite was not rerun in this follow-up.
- Strategy hash: **5c6540eccbd2f45f**.
- The claimed byte-identical pinned backtest was not independently rerun here.

## 1. P0 — Unknown submissions are not blocked across calls, and market fallbacks bypass reconciliation

Locations: `bot/execution/live_executor.py:1735`, `:2132`; market fallbacks at `:1556`, `:1601`, `:1628`, `:1719`.

The new unknown-outcome exception correctly prevents the immediate replacement in the repaired limit-submission branch. However, its handler only alerts and returns `None`. It does not persist an unresolved intent/client ID or latch execution into reconciliation-required state. A subsequent call generates a fresh client ID and submits again. Startup cannot recover a request identifier that was never persisted.

**Reproduced:** two consecutive `execute(BUY, 90000, 0.001)` calls, with request timeouts and unavailable reconciliation, produce **two create-order calls**. `pending_journal_entry` remains `None`; this marker describes confirmed fills, not unresolved submissions. The existing new test asserts one submission within only one execute call.

Additionally, four limit-chase market fallback sites still call `create_order(..., "market", ...)` directly without a client ID or the reconciliation helper.

**Reproduced:** failed order-book fetch -> market fallback -> submission timeout returns **REJECTED**, with **zero closed-order reconciliation calls**. It is still possible that the exchange accepted that market order.

Required fix: route every order submission through one durable intent/reconciliation mechanism; retain an unresolved reservation and prevent conflicting submissions across ticks and restarts. Do not rearm a replacement stop on an unknown SELL outcome until residual inventory/order state is reconciled. Add two-call, restart and fallback-path tests, not only one-call tests.

## 2. P0 — A partial stop fill is treated as terminal even when the stop is still open

Location: `bot/execution/live_executor.py:935`.

`_cancel_native_stop()` checks `filled_qty > 0` before requiring a terminal status. It clears the stop ID and calls `_record_stop_triggered_fill()` even when cancellation timed out and `fetch_order()` says `status="open"`.

**Reproduced:** starting position 0.002 BTC; cancel timeout; fetch returns an open stop with cumulative fill 0.001 BTC. Result: outcome **filled**, tracked stop ID **None**, remaining position **0.001 BTC**. The existing exchange order may still fill the remainder. A later protection sync can place a replacement without knowing the original remains open.

Required fix: record only newly observed fill deltas, retain identity and remaining quantity until terminal confirmation, and distinguish partial/open from fully filled or canceled. Test repeated polls of the same cumulative fill and a later additional fill; neither duplicate accounting nor duplicate stops may occur.

## 3. P1 — Journal replay duplicates committed fills and loses recovered SELL metadata

Locations: `bot/main.py:553`; `bot/data/trade_log.py:55`; `bot/execution/live_executor.py:1214`.

The marker fixes one crash window but introduces unsafe replay for another: the DB insert succeeds, then the process crashes before acknowledgement is saved. On restart, replay unconditionally inserts the same fill again. The schema still has no unique execution key.

**Reproduced:** insert a fill into a temporary DB, retain its unacknowledged marker as after this crash window, then run startup replay. There are **two rows for one fill**.

Other issues within the claimed scoped recovery:

- The marker omits realized P&L/cost-basis information, and replay does not pass `pnl`; recovered SELL rows therefore have NULL P&L and are omitted by live metrics selecting SELLs with non-NULL P&L.
- Original fill time goes into notes, while the actual row timestamp becomes replay time.
- Only one pending entry is stored. Replay failure does not prevent a later confirmed fill from overwriting that entry.

Required fix: an idempotent DB insert using a stable execution identity, with durable multiple-entry recovery and original execution/accounting metadata. For future partial fills, an order ID alone is insufficient to distinguish separate execution deltas. Tests must inject failure after DB commit/before acknowledgement, repeat replay, recover SELLs, and preserve pending records when a subsequent fill occurs.

## 4. P1 — Stop fills discovered during protection sync do not reach the main bookkeeping path

Locations: `bot/execution/live_executor.py:1156`, `:770`; `bot/main.py` protection-sync call sites.

`sync_protective_stop()` and `_reconcile_resting_stop_quantity()` assign the returned stop fill to `_fill_order` and discard it. The executor applies its cash/inventory change, but live position manager, state machine, capital pool, risk fill counter and trade log are not updated through the normal fill handler at that point. Startup replay can eventually recover part of the ledger, but is not runtime reconciliation and has the limitations above.

Required fix: route every discovered exchange fill through a common fill-event consumer that updates all representations once. Protection synchronization must surface fills to that consumer, including during stop replacement and resize. Add integration tests proving executor and PositionManager inventory, slot allocations and ledger agree after a stop wins a replacement race.

This finding is based on inspected control flow; the isolated reproduction in item 2 exercised cancellation itself, not this whole main-loop integration.

## 5. P1 — Reported net PF and win rate still omit entry fees

Locations: `live_comparison.py:150`; `bot/data/trade_log.py:112`.

Live comparison subtracts only the SELL fee when classifying wins/losses and computing PF. The docstring calls this an approximation, but the results remain labeled primary net figures. That does not match the backtest convention, which also allocates BUY fees to closed quantities.

**Reproduced:** BUY fee $0.80; SELL gross profit $1.00; SELL fee $0.40. Actual round-trip result is **-$0.20**. The function returns `net_pnl=-0.20`, but **win_rate=1.0 and pf=infinity**.

`TradeLog.summary()` still classifies winners from gross P&L; its fee total also sums different fee currencies without conversion. Subtracting all historical BUY fees from closed-trade P&L is not generally the same as allocating fees to closed positions when some inventory remains open. The August baseline is still hardcoded, although the new report now warns that it is gross.

Required fix: share per-symbol closed-position fee allocation with the backtest accounting conventions; preserve unmatched/unknown-basis status; define treatment of open-position fees; convert or explicitly exclude differently denominated fees. Add the exact losing-round-trip regression, partial closures, interleaved symbols, open inventory and foreign-currency fees. Until then, do not use the approximate PF/win rate as a net-performance gate.

## 6. P2 — New fingerprints exist but are not integrated into validation

Location: `bot/strategy/fingerprint.py:96` onward.

Repository search found no production callers for the new execution/full-run fingerprints; only their definitions and tests. Configuration inclusion is optional. Backtest engine, main-loop behavior and data identity remain outside the new fingerprint's scope. Therefore existing reports/gates do not gain validation protection merely because the helpers were added.

Required fix: build a normalized nonsecret config snapshot at real run/report entry points, persist execution/model/data identities, and actually compare them when consuming validation artifacts. Keep the existing narrow strategy hash for its original purpose.

## What is fixed, and what the backtest claim establishes

The mixed-cap remaining-cash bound and requested partial-SELL quantity changes are present with regression coverage. Startup readiness and ticker-validity guards, fee-column preservation, conservative same-candle SL precedence, eligibility snapshots, corrected shadow-report wording and extra health fields are also real improvements.

The acknowledged deferrals—candle-gap recovery, next-bar fills, shared-capital simulation and loop scheduling—remain follow-up work. They need not be squeezed into this patch, but should remain explicitly open.

An identical pinned result establishes a regression check for that dataset/configuration. It does not validate unresolved-order recovery or net live reporting, and it cannot establish that all historical configurations are unchanged. In particular, same-candle partial-TP ordering was changed: historical runs using that feature can differ even if the default pinned run does not exercise it. Regenerate affected artifacts when their execution assumptions change; do not infer universal revalidation exemption from the three-file strategy hash.

## Claude Code next task

> Address findings 1–5 in CRYPTO_BOT_FOLLOWUP_REVIEW_2026-09-18.md, beginning with persisted unknown-order handling across calls/restarts and all fallback paths. Add regression tests for the reported reproductions, then fix partial/open native-stop tracking and route discovered fills through one bookkeeping consumer. Make journal replay idempotent and preserve SELL P&L/execution time. Correct entry-fee allocation in live PF/win rate. Integrate fingerprints into artifact production/consumption separately. Keep HALT/configuration unchanged. Do not submit real orders. Report remaining scope honestly; passing existing tests is necessary but does not close uncovered scenarios.
