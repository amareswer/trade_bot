# Crypto money-readiness review — 2026-09-20

## Result

The new fail-closed startup and reconciliation ordering changes are materially safer. The targeted accounting tests pass (67/67), and `tests/crypto` plus `tests/shared` pass (884/884 in this checkout). This is still **HALT / no live BUYs** until the findings below are resolved and the security, paper/shadow, and profitability gates are completed.

## Confirmed improvements

- `BlockState.reconciled` now defaults to false, and a failed or incomplete cycle returns a fresh blocked state.
- Reconciliation runs before the per-symbol BUY decision.
- New observed trades and the retrieval watermark use one SQLite transaction.
- Balance checks are isolated by scope.
- Four-way verification is consulted by the BUY gate when accounting is enabled.
- Exit sizing is capped by fresh exchange quantity and locally tracked quantity.

## Remaining findings

### P1 — Multi-trade straggler links are not one transaction

`_link_stragglers()` can match a single fills row to several exchange trades, but it calls `store.link_trade_to_fill()` once per trade. Each call commits independently. A crash after the first link leaves a partially linked fill; on restart the fill is still considered unlinked while the already-linked trade is excluded from candidates, so the exact-conservation match can no longer be found and the row can become permanently blocked.

Required follow-up: link the complete matched set plus the ledger-written markers in one transaction, and add a crash test that fails after the first would-be link in a multi-trade match.

### P1 — Straggler matching ignores fee-correction projections

`engine.match_legacy_fill()` compares a fills row's fee against each observed trade's original `fee_cost`. If a delayed trade has a recorded fee correction, the effective fee can conserve exactly while the original fee does not; the periodic matcher will reject or block a valid match. The matcher must use the correction-aware effective fee while preserving the immutable observed payload.

Required follow-up: pass correction-adjusted trade views (or an explicit effective-fee accessor) into the matcher and test a delayed multi-fill whose original fee is corrected before linking.

### P1 — Fee corrections are outside the observation transaction

`reconciliation.run_cycle()` calls `_apply_fee_corrections()` before `store.commit_observation_batch()`. `_apply_fee_corrections()` writes through `TradeLog.log_fee_adjustment()`, while the later observation batch uses a separate SQLite transaction. If the batch fails after a fee correction succeeds, the correction remains durable while the new trades and watermark roll back. This is not the claimed all-or-nothing observation phase and can leave the correction log ahead of the observation checkpoint.

Required follow-up: make fee-correction rows part of the same database transaction as observed-trade and watermark writes, or explicitly persist and replay a durable correction intent with an atomic commit marker. Add a crash test that fails after a correction write and proves restart convergence with no orphan correction.

### P1 — Periodic reconciliation does not implement the promised late-fill matcher

`live_observe.observe_fill()` links immediately visible trades. Its module documentation says the periodic reconciliation cycle will later use `engine.match_legacy_fill()` for propagation-delay stragglers, but `reconciliation.run_cycle()` only upserts observed trades and checks balances; it does not call the matcher or `store.link_trade_to_fill()`. A trade that becomes visible after the immediate observer can therefore remain unlinked. The four-way delivery check can miss this because an observed trade with neither a link nor a written marker is not classified as an error.

Required follow-up: add an explicit, idempotent straggler-linking phase to each cycle, with exact-conservation matching and ambiguity blocking. Add a test where the fill is logged first, exchange visibility is delayed, and the next reconciliation links exactly one trade to the existing fill.

### P1 — Clean state can be stale between scheduled cycles

The main loop runs accounting only when `time.time() - _accounting_last_cycle` exceeds `reconcile_interval_s` (default configuration is one hour). During that interval, the previous `reconciled=True` state remains eligible to approve BUYs even if exchange balances, fills, or API permissions changed immediately after the last successful cycle.

Required follow-up: add a freshness deadline to `BlockState` and make BUYs block once the last successful cycle is older than the configured interval (with a small explicit grace period if needed). Add tests for a clean state aging past the deadline and for a failed scheduled refresh replacing it with a hard block.

## Configuration and operational status

Accounting remains opt-in (`cfg.accounting.enabled` defaults false). When disabled, these gates do not protect live BUYs. When enabled but initialization cannot create the store/adapter, the code disables the subsystem; that must be an explicit operational stop for a money-ready deployment, not silently equivalent to running without accounting.

The following are still outside this code pass: API permission minimization, secret storage and host access review, withdrawal restrictions, incident response, paper/shadow execution, and a fresh net-of-cost profitability gate. HALT should remain engaged until those are independently signed off.
