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
(part of the full crypto+shared suite — see CLAUDE.md's "Test Suite Manifest" for the current
total, which has grown substantially across the accounting-review passes since this table was
first written) that
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
reconcile against) + **zero order submission** + **fully isolated from every real production
file** (state, risk breaker, trade database, dashboard — see §2a). This repo's existing
`dry_run` executor flag already does exactly this — it is not new infrastructure for this
runbook.

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

**Note the PID immediately** (`pgrep -f 'bot\.main'`) — §3 needs it for the process-liveness
check, which the status file alone cannot substitute for.

### 2a. Isolation (fixed 2026-09-21, after a real corruption incident)

`LIVE_TRADING=true` + `DRY_RUN=true` (and not paper mode) is detected as `_SHADOW_MODE` in
`bot/main.py` and redirects every state-bearing path to `logs/shadow/`: executor state
(`live_state_*.json`), the risk-breaker state (`risk_state.json`), the trade log + accounting
database (`trades.db`), the dashboard, and the cycle-status file (§3). Real live trading and
paper mode are completely unaffected. The regime-monitor and scheduled-audits background
threads are disabled outright in shadow mode (they run subprocesses against hardcoded
production paths outside this boundary).

This isolation did not always exist — an earlier shadow run this same day corrupted the REAL
`logs/live_state_BTC_CAD.json` / `logs/live_state_SOL_CAD.json` / `logs/risk_state.json` (a
capital-pool sizing bug compounded by non-isolated paths tripped the real kill-switch). No real
money was at risk (HALT + `DRY_RUN` already made trading impossible), but the real files had
to be manually corrected afterward. Confirm you're running the fixed version before starting
a real acceptance window — `git log` should show the sixth/seventh/eighth-pass accounting
review commits.

## 3. What to watch during the run

1. **The process is still alive.** A behavioral test (`tests/crypto/test_shadow_isolation.py`,
   eighth-pass finding) proved the status file below is **not sufficient on its own**: if the
   thing that fails is the ABILITY to persist a cycle's outcome, the file can be left holding a
   stale, still-fresh-looking PASSED result while the process has already exited. In shadow
   mode specifically, a failure to persist the cycle-status "in progress" marker now stops the
   process outright (a real nonzero exit, via the existing `__main__` crash handler) rather
   than just logging — but you still need to independently confirm the process is up:
   ```
   pgrep -f 'bot\.main'
   ```
2. **`scripts/accounting_shadow_report.py`** — run periodically during the shadow window,
   pointed at the isolated shadow database and the PID you noted at startup:
   ```
   .venv/bin/python scripts/accounting_shadow_report.py --db logs/shadow/trades.db --pid <PID>
   ```
   Genuinely read-only (opens SQLite via its own `mode=ro` URI — cannot create a schema even
   against an empty/missing file), local only, no network call. Prints an explicit verdict —
   `PASSED` / `FAILED` / `STALE` / `NOT_VERIFIED` / `PROCESS_STOPPED` — never inferred from
   "nothing looks wrong"; `PASSED` requires a FRESH, cryptographically-identity-matched
   (`db_identity`), fully-complete cycle outcome covering exactly the requested symbols, with
   zero unlinked fills. Before starting a new acceptance window, run this once against the
   live database (`--db logs/trades.db`, no `--pid`) and read the residual count it currently
   reports — do not assume a specific number from an old note here; verify fresh each time,
   since the legacy-fills migration status and any manually-reviewed permanent residuals can
   change. Check CLAUDE.md's "Execution-accounting reconciliation" section for the current
   migration status before relying on any residual count as a known-backlog baseline.
3. **Four-way verification is no longer a "you had to be there" limitation.** Every
   reconciliation cycle attempt (success or failure) persists its own outcome to
   `logs/shadow/accounting_cycle_status.json` — including whether four-way verification ran
   at all and whether it passed — so the standalone script above IS now sufficient to check
   the real verdict; you don't need to cross-reference the bot's own Telegram/log alerts for
   this specifically, though they remain a good independent check.

## 4. Exit criteria (per the original readiness-gate instruction)

Run long enough to observe or exercise: at least one full restart (kill the process, restart
it, confirm `scripts/accounting_shadow_report.py --db logs/shadow/trades.db --pid <new PID>`
shows the same residual count before and after, and a fresh PASSED verdict against the new
process), at least one multi-hour stretch to confirm the reconcile-interval scheduling and
staleness detection behave over real elapsed time, and ideally a natural Kraken API hiccup
(this account has a documented history of transient auth/connectivity issues — see the
Kraken-auth-outage memory referenced above — so this is not a purely hypothetical wait).

Declare the shadow gate passed only when, across the run:
- **The standalone script's own verdict says `PASSED`**, checked repeatedly, not just once at
  the end — this now folds in process-liveness, database identity, freshness, symbol coverage,
  and unlinked-fill count into one call, per §3.2 above. A `PROCESS_STOPPED`, `STALE`, or
  `NOT_VERIFIED` verdict at any check-in is itself a finding to investigate, not something to
  wait out.
- **Zero unexplained residuals** — beyond whatever known-legacy backlog the migration status
  in CLAUDE.md documents as of the day you run this (re-check it fresh, don't rely on a
  number written into this runbook at some earlier point), no NEW unlinked fill persists
  across two consecutive script runs.
- **Zero ambiguous matches** — no `ACCOUNTING RECONCILIATION` alert citing an ambiguity
  (the straggler matcher and migration matcher both block-and-alert rather than guess on
  ambiguity, per `bot/accounting/engine.py`'s `match_legacy_fill`) went unresolved.
- **Zero ledger-delivery failures** — no `FourWayReport.ledger_delivery` failure (orphaned
  marker or unwritten trade) surfaced in an alert.

If any of the above shows a real (non-legacy-backlog) failure during shadow mode, that is a
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
