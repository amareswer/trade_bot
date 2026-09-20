# Crypto money-readiness review — September 19, 2026

Reviewed the newly added `bot/accounting` package and its integration into `bot/main.py`. The focused accounting tests pass: **59 passed in 2.07s**. No live calls, orders, configuration changes, or HALT changes were made.

## Verdict

The bot is **not ready for real money** and must remain in HALT. The accounting package is a useful prototype, but its live integration currently leaves important safety guarantees disabled or bypassable.

## Blocking findings

### P0 — BUYs can occur before the first successful reconciliation

`bot/main.py` initializes `_accounting_state` as an unblocked default. The accounting cycle runs after the per-symbol signal and BUY execution logic. Therefore, when accounting is enabled, the first tick can approve a BUY before any reconciliation has completed. If a later cycle raises, the code keeps the previous state; an initially unblocked state therefore stays permissive after a failed reconciliation.

Required behavior: initialize accounting as `unknown/blocked`, run the first reconciliation before evaluating BUY signals, and fail closed on initialization or cycle errors. A successful cycle must explicitly clear the block. Add a test proving no BUY is possible before the first successful cycle and after a failed cycle.

### P1 — The accounting protection is disabled by default

`AccountingConfig.enabled` defaults to `False`. With the current default, the new reconciliation layer does not run and cannot protect live BUYs. Enabling it without fixing the P0 ordering still leaves the startup window.

Required behavior: enable it only in paper/shadow mode first; require an explicit readiness check before allowing live mode. Do not silently treat disabled accounting as money-ready.

### P1 — Four-way verification and per-fill linking are not wired into the live fill path

`four_way.run_four_way_verification()` has tests but no production call site. `live_observe.observe_fill()` is implemented but is not called from `bot/main.py`. Consequently, observed exchange trades can exist without links to the existing `fills` rows, and the four-way check is not part of the operational BUY gate or health verdict.

Required behavior: link confirmed live fills by exchange trade ID, run four-way verification on a defined schedule, and make an unverified/missing result block BUYs. Do not let an empty or pre-migration ledger appear healthy merely because there are no link errors.

### P1 — The account watermark is committed before the cycle has reconciled successfully

`reconciliation.run_cycle()` persists the account watermark before observing trades and before account/base balance checks complete. Later failures can leave a durable cursor ahead of uncommitted observations or an unresolved balance result. The next cycle may start after that cursor and lose the opportunity to retrieve the missing window.

Required behavior: commit the watermark, observed trades, checkpoint result, and retrieval cursor in one transaction after all required checks succeed. On any failure, preserve the previous cursor and mark the cycle blocked.

### P1 — Exit-size reconciliation is not integrated

`resolve_exit_quantity()` exists, but the main loop does not use it at the protective-exit call sites. The design requires fresh exchange quantity when reconciliation is blocked; that requirement is currently documented but not enforced.

Required behavior: wire the helper into every protected exit sizing path, with explicit handling for reserved quantities, external holdings, and failed fresh reads.

## Strategy and CTI status

No CTI case-study document was present in the repository search, so there is no CTI-specific control set to assess. The current review covers execution/accounting safety. It does not establish profitability, exchange-permission readiness, key security, withdrawal controls, or operational incident response.

The existing net-of-cost profitability gate remains separate and has not been passed by this accounting work.

## Required sequence before real-money consideration

1. Fix the P0 startup/failure gate and transactional watermark handling.
2. Wire live fill observation and four-way verification.
3. Wire fresh-balance exit sizing and explicit blocked-state behavior.
4. Run the integrated path in paper/shadow mode with no live orders.
5. Perform a manual account reconciliation and security review: API permissions, withdrawal disabled, secret storage, host access, alerting, restart behavior, and kill switch.
6. Run the existing fresh post-2026-09-12 net-of-cost strategy validation.
7. Consider live entries only when both accounting readiness and strategy validation pass, with a small capped pilot and immediate rollback procedure.

Until then, keep `logs/HALT` engaged and preserve protective exits for any actual holdings.
