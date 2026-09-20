# Crypto bot — paper/shadow readiness runbook

Gate item 2/3 of the gated readiness review (`CRYPTO_BOT_GATED_READINESS_REPORT_2026-09-20.md`).
Covers how to run the execution-accounting reconciliation layer (`bot/accounting/`, built
2026-09-19/20) in shadow mode — real market data, zero order submission — and how to verify
each of the five required operational scenarios (restart, delayed fill, fee correction, API
failure, stale-state expiry) before trusting it with real capital.

**This is documentation + a read-only inspection script only. No config was flipped, no
process was started, as part of writing this file.** `logs/HALT` remains engaged.

## 1. How the five scenarios are already proven, and what shadow mode adds

Each scenario has a dedicated, deterministic test in `tests/crypto/test_accounting_reconciliation.py`
(part of the 899-test crypto+shared suite, all passing as of the 2026-09-20 review) that
constructs the failure condition directly and asserts the correct block/recovery behavior —
this is why the readiness bar is "run shadow mode long enough to build operational confidence
in real conditions," not "run long enough to eventually witness these events," since witnessing
them is not gated by luck:

| Scenario | Proven today by | What a live shadow run additionally proves |
|---|---|---|
| Restart | `test_three_restarts_recover_identical_position_purely_from_persisted_store` | The real process actually restarts cleanly under a real supervisor (SIGTERM, launchd/systemd crash) and rebuilds identical state, not just that the pure function does |
| Delayed fill | `test_delayed_visibility_blocks_then_self_resolves`, straggler-linking tests | A REAL Kraken API propagation delay (not a synthetic one) resolves within the same window the tests assume |
| Fee correction | `test_fee_correction_recorded_via_existing_fee_adjustments_table`, `test_fee_correction_crash_leaves_no_orphan_and_retry_converges` | A real Kraken fee-correction event (if one occurs) is handled the same way |
| API failure | `test_observation_phase_exception_returns_a_blocked_not_a_permissive_state` | Real Kraken outages/rate-limits (this account has hit two real auth outages — see `.memory/project_kraken_auth_outage_2026-09-04.md`) fail closed in practice, not just in a mocked exception test |
| Stale-state expiry | `test_clean_state_blocks_once_older_than_max_age`, `test_failed_scheduled_refresh_replaces_a_stale_clean_state_with_a_hard_block` | The real scheduling loop (`reconcile_interval_s` + `stale_grace_s` against real wall-clock time, not `freezegun`) behaves as designed over hours/days |

Shadow mode's job is therefore narrower than "wait to observe five rare events" — it's
confirming the already-proven mechanisms hold up against a real exchange connection, a real
process lifecycle, and real elapsed time, plus surfacing anything the unit tests' fixtures
didn't anticipate.

## 2. Running shadow mode

Shadow mode = real market data + real account reads (so accounting has something real to
reconcile against) + **zero order submission**. This repo's existing `dry_run` executor flag
already does exactly this — it is not new infrastructure for this runbook.

```
# In the LIVE .env (not the backtest/validation one):
LIVE_TRADING=true        # builds a real ccxt exchange connection (needed so accounting
                          # has a real account to reconcile against — see below)
DRY_RUN=true              # LiveExecutor simulates every fill locally; create_order() is
                          # never called — no real order reaches Kraken under any condition
ACCOUNTING_ENABLED=true   # the subsystem this runbook is validating
```

`_accounting_enabled` in `bot/main.py` is `cfg.accounting.enabled and cfg.exchange.live_trading
and live_exchange is not None` (source: `bot/main.py` "Execution-accounting reconciliation"
block) — accounting genuinely requires `LIVE_TRADING=true` to build the real exchange handle
it reconciles against, independent of whether `DRY_RUN` blocks actual order submission. This
is why `DRY_RUN=true` is the actual non-trading guarantee here, not `LIVE_TRADING=false`.

**Belt-and-suspenders, keep this on regardless:** leave `logs/HALT` engaged for the entire
shadow run. `DRY_RUN=true` already makes order submission structurally impossible (no code
path calls `create_order`), but HALT is the second, independent, already-battle-tested
control and costs nothing to also have on. Do not lift it for this step.

Start the bot exactly as documented in CLAUDE.md ("Python 3.10+" section):
```
caffeinate -i .venv/bin/python -m bot.main
```

## 3. What to watch during the run

1. **Reconciliation cycle firing on schedule.** Every `reconcile_interval_s` (default per
   `AccountingConfig`), `bot/main.py` runs `accounting_reconciliation.run_cycle(...)`. A
   non-ready result fires `alerter.error("ACCOUNTING RECONCILIATION: ...")` (Telegram, if
   configured) — silence across a run is a good sign, but confirm at least once per session
   that the cycle is actually executing (check process logs for the cycle's own log lines,
   don't just infer "no alert = no cycle ran").
2. **`scripts/accounting_shadow_report.py`** (new, this pass) — run periodically during the
   shadow window:
   ```
   .venv/bin/python scripts/accounting_shadow_report.py
   ```
   Read-only, local SQLite only, no network call, safe to run anytime. Reports unlinked fills
   (residuals) per symbol, the last checkpoint per currency scope, and total fee corrections.
   Exits non-zero if any unlinked fill exists. **Run against the current DB before starting
   shadow mode confirmed 10 pre-existing unlinked legacy fills (8 on BTC/CAD, 2 on SOL/CAD) —
   this is expected and NOT a shadow-mode finding: `migrate_legacy_fills.py` has never been
   run (confirmed 2026-09-20 — no `trades_migration_copy_*.db` exists, live `trades.db` is
   0 bytes on disk; auto-memory's "migration script not yet run" note is still accurate).**
   Run the migration BEFORE starting a shadow session, or the shadow report's residual count
   will include this known pre-existing backlog and mask any NEW residual shadow mode itself
   produces. Migration is its own live Kraken read-only call (real trade-history fetch) —
   review `migrate_legacy_fills.py`'s docstring and run it against a timestamped copy first
   (its default mode), inspect the result, and only pass `--apply-to-live` once satisfied.
3. **Four-way verification.** `run_four_way_verification()`'s `.ready`/`.explain()` needs live
   executor position state that only `bot.main.run()` has — it is not exposed by the
   standalone script above. Confirm it from the bot's own alerting: the "ACCOUNTING
   RECONCILIATION" alert text (step 1) already IS `BlockState.explain()`, and a fully-ready
   four-way result produces no alert at all — treat an alert-free multi-hour run, cross-checked
   against the shadow-report script showing zero NEW residuals, as the operational proof.

## 4. Exit criteria (per the original readiness-gate instruction)

Run long enough to observe or exercise: at least one full restart (kill the process, restart
it, confirm `scripts/accounting_shadow_report.py` shows the same residual count before and
after), at least one multi-hour stretch to confirm the reconcile-interval scheduling and
staleness detection behave over real elapsed time, and ideally a natural Kraken API hiccup
(this account has a documented history of transient auth/connectivity issues — see the
Kraken-auth-outage memory referenced above — so this is not a purely hypothetical wait).

Declare the shadow gate passed only when, across the run:
- **Zero unexplained residuals** — `scripts/accounting_shadow_report.py` shows no unlinked
  fill beyond the pre-existing migration backlog (resolved by running the migration first,
  per step 2), and no NEW unlinked fill persists across two consecutive script runs.
- **Zero ambiguous matches** — no `ACCOUNTING RECONCILIATION` alert citing an ambiguity
  (the straggler matcher and migration matcher both block-and-alert rather than guess on
  ambiguity, per `bot/accounting/engine.py`'s `match_legacy_fill`) went unresolved.
- **Zero ledger-delivery failures** — no `FourWayReport.ledger_delivery` failure (orphaned
  marker or unwritten trade) surfaced in an alert.

If any of the three shows a real (non-legacy-backlog) failure during shadow mode, that is a
blocker for the readiness review, full stop — fix and restart the shadow window, don't
carry a known residual into the profitability or capital-pilot gates.

## 5. What this runbook does not cover

- It does not itself declare shadow mode complete — that requires actually running it for a
  real elapsed window, which is operator work outside what a single Claude Code session can
  responsibly do unattended against a live account/process.
- It does not cover the security review (Kraken permissions, secrets, host access) — see
  `CRYPTO_BOT_SECURITY_REVIEW_2026-09-20.md`.
- It does not cover the profitability gate — see the pinned-backtest re-validation in
  `CRYPTO_BOT_GATED_READINESS_REPORT_2026-09-20.md`.
