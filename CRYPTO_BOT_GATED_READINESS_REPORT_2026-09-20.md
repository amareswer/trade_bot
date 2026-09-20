# Crypto bot — gated paper/shadow readiness review — 2026-09-20

Prepared per the explicit instruction: audit security posture, verify/add a paper/shadow
runbook, add operational checks proving accounting/four-way/BUY-block behavior, re-run the
pinned backtest with a fresh net-of-cost validation, and report blockers clearly.

**No live trading was enabled. No order was placed. No strategy parameter was changed. HALT
(`logs/HALT`) was never touched — engaged before this review, engaged after it.**

## Status: HALT — correct, and it stays that way

None of the five sequential gates in the original readiness plan (freeze code → security
review → paper/shadow → profitability → limited pilot) are all satisfied yet. Per-gate detail
below.

## Gate 1 — Freeze the code

**Done, already in place before this review.** `git status` is clean; the accounting/
reconciliation subsystem (`bot/accounting/`) and its 11+ prior review passes (2026-09-13
through 2026-09-19) are all committed (`git log` through `f8630e8`). Strategy hash
unchanged at `5c6540eccbd2f45f` — nothing in `bot/strategy/` was touched by this pass or the
accounting work that preceded it. This pass adds two small, execution-layer-only changes on
top (both below, in "What changed this pass") — neither touches `bot/strategy/`.

## Gate 2 — Security review

**See `CRYPTO_BOT_SECURITY_REVIEW_2026-09-20.md` for full detail. Summary:**
- Secret storage: **PASS** — `.env` files gitignored and confirmed never committed; zero
  hardcoded credentials found across all tracked files.
- Log exposure: **PASS** — `logs/` and `*.db` fully gitignored.
- **Kraken API key permission scope: UNVERIFIED — requires a one-time manual check in the
  Kraken UI (Settings → API) that nothing on record confirms was ever actually done.** A
  checklist is provided; Kraken exposes no API endpoint to check a key's own permissions
  remotely, and a behavioral probe against the withdraw endpoint was deliberately not
  attempted (real financial risk for a check that's free and instant via the UI).
- Host/process access: not started, but not currently relevant — the bot runs locally with no
  deployment target yet; the (unused) VPS service file has partial hardening, noted for later.
- Key rotation + incident response procedures: **did not exist before this review — both
  drafted from scratch** in the security review doc.

**This gate is not closeable by me** — it ends on a checklist only the account owner can walk
through against the real Kraken key.

## Gate 3 — Paper/shadow mode

**Runbook written (`deploy/PAPER_SHADOW_RUNBOOK.md`), but the shadow run itself has not
happened.** That's real elapsed-time operator work, not something this pass could responsibly
do unattended against a live process. What this pass did:
- Confirmed all five required scenarios (restart, delayed fill, fee correction, API failure,
  stale-state expiry) already have dedicated, passing, deterministic tests in
  `tests/crypto/test_accounting_reconciliation.py` — the readiness bar is confirming these
  hold under a real exchange connection and real elapsed time, not hoping to witness them.
- Found and fixed a real gap in the BUY-block wiring (see "What changed this pass" below).
- Built `scripts/accounting_shadow_report.py`, a read-only local inspection tool, and ran it
  against the current database: **it found 10 pre-existing unlinked fills (8 BTC/CAD, 2
  SOL/CAD)** — not a new problem, but direct confirmation that `migrate_legacy_fills.py` has
  genuinely never been run (matches the existing auto-memory note). The runbook makes running
  this migration a precondition for starting a shadow session, so its known backlog doesn't
  mask a real new residual produced during shadow mode.

## Gate 4 — Profitability (net-of-cost validation, fresh run)

**Re-run today, same strategy code (hash `5c6540eccbd2f45f`), same deployment gate CLAUDE.md
already documents. Result: unchanged from the 2026-09-12 fee-accounting fix — no drift, and
the underlying conclusion stands.**

| Check | Result | vs. CLAUDE.md's documented number |
|---|---|---|
| BTC/USDT pinned window (2024-03-07 → 2026-06-20) | 27 trades, **net PF 0.82** (gross 1.87), 33.3% win rate | Exact match |
| BTC/USDT rolling window | 29 trades, **net PF 1.17** (gross 2.46), 34.5% win rate | Exact match |
| BTC/USDT walk-forward | TRAINING net PF **0.67** (gross 1.37) / VALIDATION net PF **1.54** (gross 3.41) — "✓ holds" per the tool's own verdict | Exact match |
| SOL/USDT rolling window | 43 trades, **net PF 1.05** (gross 1.78), 39.5% win rate | Exact match |

**This reconfirms, it does not newly discover, the existing documented position:** BTC's
training window is a net loss (0.67) even though validation clears the bar, and SOL is barely
above breakeven everywhere. Neither symbol clears the fingerprint's own stated floor
(PF ≥ 1.72 backtest / walk-forward PF ≥ 1.2 on the training side too) once fees are correctly
attributed. **This is not a regression to fix — it's the reason the bot is halted, and the
existing 2026-09-12 review-deadline decision (`.memory/project_review_deadline_2026-09-12.md`)
already requires a fresh OUT-OF-SAMPLE walk-forward (data strictly after 2026-09-12) beating
PF ≥ 1.2 on every window for both symbols, benchmarked against buy-and-hold, before BUYs
resume — a bar this re-run does not attempt to clear, because doing so needs data that doesn't
exist yet (not enough time has passed since 2026-09-12).**

**Conclusion for this gate: profitability floor is still not met. This alone is sufficient
to keep the bot halted, independent of gates 2/3/5.**

## Gate 5 — Limited capital pilot

**Not reached.** Gates 2–4 are not all closed, so this gate is not evaluated.

## What changed this pass

Two changes, both execution-layer/test-layer only — **no `bot/strategy/` file touched, hash
unaffected, no config flag flipped from its safe default.**

1. **Real bug found and fixed: the dynamic-universe ranked-BUY execution path
   (`_execute_ranked_dynamic_buys` in `bot/main.py`) never consulted the accounting
   `BlockState` before executing a BUY** — it received `accounting_enabled`/`conn`/`adapter`
   only for fee-recording after a fill, unlike the fixed-roster BUY path (section 7a of
   `run()`), which already refuses a BUY while accounting is unreconciled/stale. Currently
   **unreachable in production** — `DYNAMIC_UNIVERSE_ENABLED=false` by default and the feature
   is explicitly parked (`.memory/project_dynamic_universe_2026-09-13.md`) — but it means the
   accounting BUY-block was NOT actually "active when configured" for that code path, which
   is precisely what this review was asked to verify. Fixed by threading the same
   `accounting_state`/`block_buys_on_unreconciled`/`max_age_ms` through to that function's one
   call site and adding the identical block-and-skip check it was missing. Documented in the
   function's own docstring.
2. **Added a run()-level source-guard test suite**
   (`tests/crypto/test_accounting_buy_gate_wiring.py`, matching the existing house convention
   for wiring checks on the giant `run()` function — see `tests/stock/test_tsx_rule_buy_block.py`
   for the established pattern) proving: the accounting gate exists and runs strictly between
   risk approval and order execution, fires only on BUY (never SELL/exit), rejects by replacing
   `approval` rather than raising, and that the dynamic-buy path (fixed in #1) now wires the
   same state through. Plus two new behavioral tests in `test_dynamic_live_integration.py`
   proving the fix itself: a blocked `BlockState` stops every dynamic-ranked candidate with
   `execute()` never called, and `accounting_enabled=False` is a true no-op (today's default
   behavior is unchanged).

**Full suite: 1364 passed, 2 failed** (`.venv/bin/python -m pytest --tb=short -q`) — the 2
failures are `tests/stock/test_weekly_monitor.py::test_scan_log_buckets_faults_vs_noise` and
`::test_scan_log_respects_time_window`, pre-existing and unrelated (stock-bot weekly-monitor
log-scanning, untouched by this pass — also flagged as pre-existing in the 2026-09-19 auto-
memory note). All crypto/accounting-relevant tests pass, including the 7 new ones added here.

## Blockers, plainly

1. **Profitability floor not met (Gate 4)** — the documented, re-confirmed net-of-fee numbers.
   Needs either a genuinely out-of-sample post-2026-09-12 walk-forward pass (data doesn't
   exist yet — needs elapsed time) or a decision to retire/redesign per the existing
   2026-09-12 review-deadline policy.
2. **Kraken key permission scope unverified (Gate 2)** — a 2-minute manual UI check only the
   account owner can do; blocks the security gate until done.
3. **No shadow-mode run has actually happened yet (Gate 3)** — infrastructure and runbook are
   ready; running it is real-time operator work, not something completed by writing this
   report.
4. **`migrate_legacy_fills.py` has never been run** — confirmed operationally this pass
   (10 unlinked legacy fills found). Must run before a shadow session, or its own residual
   backlog will be indistinguishable from a real new finding.

**Until all four clear: correct status is HALT.** Nothing in this pass changes that, and
nothing in this pass was intended to.

---

## Follow-up pass, same day — migration hardening

Continuation of the same readiness process: harden `migrate_legacy_fills.py` /
`bot/accounting/migration.py` (dry-run workflow, live-DB backup, exact-match-only matching
verification, idempotency/restart-recovery/duplicate-prevention/rollback tests), re-verify the
dynamic-universe accounting gate, and consolidate into one final blocker report.

**No live trading enabled. No order placed. No strategy parameter changed. HALT untouched.
The live migration was NOT applied to the real `logs/trades.db` — see item 3 below for why.**

### What changed this pass

1. **Real bug found and fixed: a partial-crash restart-recovery gap in the orphan-backfill
   path of `bot/accounting/migration.py`.** The four writes a genuine orphan trade (one with
   no prior `fills` row at all) needs — `upsert_observed_trade`, `log_fill` (a SEPARATE
   connection via `TradeLog`), `fills_row_by_exec_key`, `link_trade_to_fill` — are each
   individually idempotent, but were not wrapped in one transaction, and the loop's guard
   (`if store.get_observed_trade(...) is not None: continue`) treated "already observed" as
   "already fully done." A crash between the first write succeeding and the rest completing
   would leave the trade observed-but-unlinked forever — every future migration run would see
   the observed row and skip it, permanently. Fixed: the guard now distinguishes a trade this
   migration itself partially created (`source == "migration"`, safe to resume — every
   remaining step is idempotent) from a trade observed via a different source (e.g. "live",
   left alone — forcibly backfilling it would double-count a trade the live path already
   knows about). Proven by two new tests reproducing each crash point exactly
   (`test_migration_recovers_an_orphan_stranded_after_observe_but_before_fills_row`,
   `..._after_fills_row_but_before_link` — the second of which turned out to already self-heal
   via the pre-existing general exact-match loop, which runs first and treats any now-existing
   unlinked fills row like any other legacy row; only the first crash point needed the new
   code path).
2. **`migrate_legacy_fills.py --apply-to-live` now takes an unconditional backup first.**
   Before this pass, `--apply-to-live` pointed straight at the real `logs/trades.db` with no
   backup step at all — the dry-run copy helper (`_copy_db`) only ever ran in the default
   (safe) mode. Added `_backup_live_db()`, called unconditionally at the start of an
   `--apply-to-live` run, writing `logs/trades_pre_migration_backup_<timestamp>.db` (named
   distinctly from the dry-run's `trades_migration_copy_<timestamp>.db` so the two are never
   confused on disk). Restore path: copy the backup back over `trades.db`. 3 new tests on the
   pure file-copy helpers (no network needed).
3. **Report output improved for manual review.** Blocked rows now print their full candidate
   trade-id list alongside the reason; linked rows now print the exact fill-id → trade-id(s)
   mapping being proposed, not just an aggregate count — so a human reviewing a dry-run report
   can actually verify each proposed match individually, per the explicit "manually review
   every proposed match" instruction.
4. **Confirmed, not changed: matching was already exact-conservation-only.**
   `bot/accounting/engine.match_legacy_fill` already refuses anything but an exact
   quantity+fee-conserving subset (tolerance 1e-6) and blocks — never guesses — on no match,
   ambiguity, or an oversized candidate pool. No code change was needed here; verified by
   reading it and confirmed by the existing `test_match_legacy_fill_blocks_on_ambiguity` /
   `test_match_legacy_fill_blocks_on_no_match` / `test_migration_blocks_on_ambiguous_legacy_row`
   tests, which already passed before this pass.
5. **5 new tests added to `tests/crypto/test_accounting_migration.py`**: orphan-backfill
   idempotency across two full runs, the two restart-recovery crash points above,
   duplicate-prevention (a live-observed unlinked trade is left alone, not backfilled), and
   rollback (an injected exception mid multi-trade link leaves zero partial state, confirmed
   by direct row counts, and a clean re-run afterward completes correctly). **3 new tests** in
   `tests/crypto/test_migrate_legacy_fills_cli.py` for the new backup helper.
6. **Dynamic-universe accounting gate re-verified with the full suite** (not re-changed this
   pass — it was fixed in the prior same-day pass): `tests/crypto/test_accounting_buy_gate_
   wiring.py` (5 tests) and the 2 behavioral tests in `test_dynamic_live_integration.py` all
   still pass.

**Full suite: 1372 passed, 2 failed** (`.venv/bin/python -m pytest --tb=short -q`) — the same
2 pre-existing, unrelated failures as the earlier pass today
(`tests/stock/test_weekly_monitor.py::test_scan_log_buckets_faults_vs_noise` and
`::test_scan_log_respects_time_window`, stock-bot weekly-monitor log scanning, untouched by
any crypto/accounting work). **Tracked separately from crypto-release evidence, per explicit
instruction** — not a crypto-readiness blocker, but also not fixed by, or masked by, this pass.

### Migration: dry-run built and hardened, NOT applied to the live database

**Deliberately not run against the real `logs/trades.db` this pass.** The instruction's own
sequencing — dry-run first, a human manually reviews every proposed match, only then decide
whether to apply — puts the actual apply-to-live decision on the operator, not on an
automated pass. What's ready now: `migrate_legacy_fills.py` (default dry-run mode, operating
on a disposable copy, zero risk to the live file) can be run at any time to produce the
match-by-match report described above; `--apply-to-live` is available with the new automatic
backup once that report has been reviewed and the operator is ready to apply it.

### Final blocker report

| # | Blocker | Status | What's needed to close it |
|---|---|---|---|
| 1 | Kraken withdrawal permission sign-off | **Open** | 2-minute manual check in the Kraken UI (Settings → API) — checklist in `CRYPTO_BOT_SECURITY_REVIEW_2026-09-20.md` §1. No automated check exists (Kraken exposes no permission-introspection endpoint); nothing on record confirms this was ever done against the live key. |
| 2 | Paper/shadow run — duration and results | **Open, not started** | Runbook is ready (`deploy/PAPER_SHADOW_RUNBOOK.md`): `LIVE_TRADING=true` + `DRY_RUN=true` + `ACCOUNTING_ENABLED=true`, HALT still engaged as a second guarantee. Needs real elapsed time — a restart, a multi-hour stretch for staleness/scheduling, ideally a real API hiccup (this account has hit two before) — and a clean `scripts/accounting_shadow_report.py` result throughout. Zero hours logged so far. |
| 3 | Migrated legacy fills and residuals | **Tooling hardened this pass; migration NOT yet applied** | 10 pre-existing unlinked legacy fills confirmed (8 BTC/CAD, 2 SOL/CAD). Dry-run workflow ready with exact-match-only guarantees, automatic pre-apply backup, and a per-match report for manual review. Next step is an operator running the dry-run, reviewing every proposed match by hand, and deciding whether to `--apply-to-live`. |
| 4 | Profitability floor | **Open — re-confirmed this pass, unchanged from 2026-09-12** | Pinned net PF 0.82, rolling net PF 1.17, walk-forward TRAINING net PF 0.67 (BTC); rolling net PF 1.05 (SOL) — all below the documented floor once fees are correctly attributed. Needs either a genuinely out-of-sample post-2026-09-12 walk-forward pass (data doesn't exist yet) or a formal retire/redesign decision per the existing 2026-09-12 review-deadline policy. Not something a code pass can resolve by re-running the same historical window again. |

**Money-readiness is not declared. All four blockers remain open. Correct status: HALT.**
