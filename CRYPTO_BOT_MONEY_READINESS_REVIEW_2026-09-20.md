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

## Findings from the prior review

The three prior P1 findings are now addressed: fee corrections are included in the observation transaction, delayed fills have an explicit reconciliation matcher, and the BUY/exit consultations apply a freshness deadline.

The two follow-up issues identified in that implementation are also addressed:

- Multi-trade straggler links now use one transaction, so a mid-group crash rolls back the entire group.
- Straggler matching uses correction-adjusted effective fees without mutating immutable observed trades.

The current targeted accounting tests pass (66/66), and `tests/crypto` plus `tests/shared` pass (899/899 in this checkout).

## Remaining findings

### Resolved — Multi-trade straggler links are not one transaction

`_link_stragglers()` can match a single fills row to several exchange trades, but it calls `store.link_trade_to_fill()` once per trade. Each call commits independently. A crash after the first link leaves a partially linked fill; on restart the fill is still considered unlinked while the already-linked trade is excluded from candidates, so the exact-conservation match can no longer be found and the row can become permanently blocked.

Resolved with `store.link_trades_to_fill()` and crash/retry tests.

### Resolved — Straggler matching ignores fee-correction projections

`engine.match_legacy_fill()` compares a fills row's fee against each observed trade's original `fee_cost`. If a delayed trade has a recorded fee correction, the effective fee can conserve exactly while the original fee does not; the periodic matcher will reject or block a valid match. The matcher must use the correction-aware effective fee while preserving the immutable observed payload.

Resolved with correction-adjusted candidate copies and a regression test.

### Resolved — Fee corrections are outside the observation transaction

`reconciliation.run_cycle()` calls `_apply_fee_corrections()` before `store.commit_observation_batch()`. `_apply_fee_corrections()` writes through `TradeLog.log_fee_adjustment()`, while the later observation batch uses a separate SQLite transaction. If the batch fails after a fee correction succeeds, the correction remains durable while the new trades and watermark roll back. This is not the claimed all-or-nothing observation phase and can leave the correction log ahead of the observation checkpoint.

Resolved by writing computed corrections through `commit_observation_batch()` on the same connection and transaction, with rollback and retry tests.

### Resolved — Periodic reconciliation does not implement the promised late-fill matcher

`live_observe.observe_fill()` links immediately visible trades. Its module documentation says the periodic reconciliation cycle will later use `engine.match_legacy_fill()` for propagation-delay stragglers, but `reconciliation.run_cycle()` only upserts observed trades and checks balances; it does not call the matcher or `store.link_trade_to_fill()`. A trade that becomes visible after the immediate observer can therefore remain unlinked. The four-way delivery check can miss this because an observed trade with neither a link nor a written marker is not classified as an error.

Resolved with `_link_stragglers()`, exact-conservation matching, ambiguity blocking, and delayed-visibility tests.

### Resolved — Clean state can be stale between scheduled cycles

The main loop runs accounting only when `time.time() - _accounting_last_cycle` exceeds `reconcile_interval_s` (default configuration is one hour). During that interval, the previous `reconciled=True` state remains eligible to approve BUYs even if exchange balances, fills, or API permissions changed immediately after the last successful cycle.

Resolved with `computed_at_ms`, `is_stale()`, and consult-time BUY/exit checks.

### P2 — Operator comments are stale

`bot/main.py` still contains a comment saying `resolve_exit_quantity()` is “not yet wired,” although it is now used at all three exit-sizing sites. The surrounding accounting configuration documentation also describes the old scope. This cannot authorize a trade, but it can mislead an operator reviewing deployment readiness.

Required follow-up: update the comments/docstrings and add a lightweight documentation check to prevent the old claim from returning.

## Configuration and operational status

Accounting remains opt-in (`cfg.accounting.enabled` defaults false). When disabled, these gates do not protect live BUYs. When enabled but initialization cannot create the store/adapter, the code disables the subsystem; that must be an explicit operational stop for a money-ready deployment, not silently equivalent to running without accounting.

The following are still outside this code pass: API permission minimization, secret storage and host access review, withdrawal restrictions, incident response, paper/shadow execution, and a fresh net-of-cost profitability gate. HALT should remain engaged until those are independently signed off.
