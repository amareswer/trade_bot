# Isolated Ledger Observer — Readiness Report (2026-09-22)

## Purpose

This consolidates everything built and verified for the **isolated Decimal ledger
observer** — a checker that independently walks Kraken's own ledger history for
one account/asset, verifies every balance transition is internally consistent,
confirms the persisted observation itself is complete (no missing rows, no
partial batch), and checks that a freshly-read wallet balance agrees with the
chain's computed endpoint. **It does not compare against the bot's own production
fill records (`logs/trades.db` / `observed_trades`), and it does not by itself
prove any given trade belongs to this bot** — that comparison is a different,
separate concern (see "Subsystem relationship" below). This report states
plainly what remains before this checker — or any live trading decision — can
be considered fully accepted.

**This report does not authorize live trading, does not change `logs/HALT`, and
does not make or imply any profitability claim.** Those are separate, independent
gates, discussed at the end.

### Subsystem relationship (corrected 2026-09-22)

This is a **different, independent subsystem** from the earlier
execution-accounting reconciliation engine (`bot/accounting/reconciliation.py` /
`four_way.py`, `ACCOUNTING_ENABLED=false`, built 2026-09-19/20 — see
`CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_2026-09-19.md`). That engine reconciles
exchange trades against the bot's local fill ledger for execution-accounting
purposes, **and it IS wired into `bot/main.py`** (`accounting_reconciliation.
run_cycle()` at `bot/main.py:3700`, `accounting_four_way.run_four_way_verification()`
at `bot/main.py:3770`, config-gated behind `ACCOUNTING_ENABLED`, default off). An
earlier version of this report incorrectly stated neither subsystem was wired
into `bot/main.py` — that is only true of the NEW one described here.

This one (`ledger_quantity_reconciliation.py` / `ledger_shadow_run.py` /
`kraken_ledger_fetch.py`) is a from-scratch, Decimal-exact observer of the
exchange's own ledger history, built via a separate design process
(`CRYPTO_BOT_LEDGER_MOVEMENT_INTEGRATION_PROPOSAL_2026-09-21.md`). **This one —
and only this one — has zero import relationship with `bot/main.py`**,
confirmed by `grep` (no match) and by a source-guard test in each of its own
test files. The two subsystems are not wired to each other either.

---

## What was built

| File | Role |
|---|---|
| `bot/accounting/ledger_quantity_reconciliation.py` | Core primitive: Decimal-exact ledger chain walker. Parses raw Kraken ledger entries (`parse_raw_ledger_entry`, id taken from the response envelope's own key — not a field), walks a chain of entries verifying `running + amount − fee == balance` at every step, resolves same-timestamp ties by balance-transition arithmetic (never by sorting on id), requires an opening-balance verification (`OpeningCheckpoint` with a written `evidence` string, or a bare `zero_opening_confirmed` flag — see caveat below) before ever reporting a full pass, and persists atomically (`persist_batch` + a completion manifest, `verify_batch_completeness`, `batch_is_complete`) so a crash mid-fetch can never masquerade as a complete, trusted chain. |
| `bot/accounting/ledger_shadow_run.py` | Orchestrator: composes one full **observation cycle** (fetch → persist → reconcile → publish) under a fresh `observation_id` every time, binds "trusted" to that *specific* cycle (an older successful cycle can never vouch for a new one), publishes a status file that never masks a failure behind a stale success, and — after review — also keeps an **append-only history log** (`history.jsonl`) so no individual run's outcome is ever lost, distinct from the "latest only" status file. |
| `bot/accounting/kraken_ledger_fetch.py` | The real, read-only Kraken adapter: pages `privatePostLedgers` with a coverage proof (unique-identity based, not raw row count), resolves asset aliases symmetrically (`BTC`/`XBT`/`XXBT` all resolve identically) and normalizes to one canonical code, and reads the account balance via `fetch_balance()` strictly *after* the ledger fetch completes. |
| `scripts/ledger_shadow_run.py` | CLI entry point. Off by default (`LEDGER_SHADOW_ENABLED=true` required). `--fixture` (no network) and `--live` (real, read-only Kraken calls) modes, each with **separate default storage paths and account identities** so the two can never collide by accident. |
| `scripts/ledger_reconciliation_audit.py`, `scripts/asset_movement_discrepancy_report.py`, `bot/accounting/asset_movement_analysis.py` | Earlier-session, complementary offline tooling (float-based attribution analysis, and the original rigorous Decimal audit script this observer's pagination discipline is modeled on). Unchanged by this work. |
| `CRYPTO_BOT_LEDGER_MOVEMENT_INTEGRATION_PROPOSAL_2026-09-21.md` | The design document (v1→v4) this observer implements. |

**None of the files in this table (the new ledger observer) are wired into
`bot/main.py`, `reconciliation.py`, or `four_way.py`** — see "Subsystem
relationship" above for the important distinction from the earlier accounting
engine, which is wired into `bot/main.py`. Every new-observer module carries a
source-guard test proving no import relationship exists for itself.
No code path here can place, modify, or cancel an order — only
`privatePostLedgers` and `fetch_balance` (both read-only query endpoints) are
ever called.

---

## Design properties, each added after a real review finding

Built across many review rounds (mine and an external reviewer's), each finding
reproduced against the pre-fix code before being fixed:

- **Trust is bound to one specific observation, not "any recent success."** An
  older, unrelated successful cycle can never vouch for a newer, failed one —
  even when the wallet balance happens to look unchanged (which is consistent
  with *either* no activity *or* offsetting activity the failed fetch missed).
- **A crash mid-fetch can never look like a complete, trusted chain.** Batch
  persistence is atomic and independently verified (`verify_batch_completeness`,
  `batch_is_complete`) — chain arithmetic alone was proven insufficient (a
  truncated prefix can coincidentally net to the same total).
- **Pagination coverage is checked by unique identity, not raw row count** —
  duplicate rows across pages used to be able to satisfy a naive count check.
- **A failure occurring AFTER a successful fetch never leaves a stale success
  on disk** — an in-progress placeholder is published for every observation
  before persistence/reconciliation begins, so a later failure always
  overwrites that placeholder, never an unrelated earlier cycle's result. **One
  documented exception**: if the very FIRST publish of that in-progress
  placeholder itself fails (e.g. a disk error), the whole cycle aborts
  immediately and nothing new is written at all — in that specific case,
  whatever a PRIOR cycle already published (which could be an old success)
  remains on disk, because no attempt to record the new failure ever
  completed. A failure to publish a terminal result is never silently
  swallowed — it propagates rather than being masked.
- **Evidence-mode separation**: a database is stamped `fixture` or `live` on
  first use and refuses a cycle of the other mode against it, even under an
  explicit path override naming the same file — closing a real reproduction
  where a fixture's synthetic deposit silently corrupted what was meant to be
  real observation history.
- **Asset alias resolution is symmetric** — `BTC`, `XBT`, and `XXBT` (Kraken's
  legacy and modern codes for the same asset) all resolve identically and
  normalize to one canonical stored code, after a real asymmetry was found and
  fixed (the CLI's own default value silently fell through a weaker code path
  than its canonical alias).
- **The opening-balance conditional label** (added after the correction below):
  any pass that rests on the bare `zero_opening_confirmed` flag now carries an
  explicit `NOTE` inside its own `reason` field, not just a verbal caveat.

**All of the fault-handling properties above (crash mid-fetch, publication
failure, evidence-mode conflict) were validated exclusively through OFFLINE
tests that deliberately inject exceptions and simulated failures.** The real
BTC/SOL live runs described later in this report never exercised any of these
failure paths — each one was a straightforward successful fetch (or the
expected, non-error "opening balance not verified" outcome). The live runs
validate real-data correctness on the happy path; they do not additionally
prove the fault-tolerance machinery against real-world failure conditions,
which remains offline-only evidence.

---

## A correction made during this work (stated plainly, not buried)

After the first real BTC observation passed, it was described to the user as
resting on "genuine evidence" that the account's BTC history started at zero.
**That overstated what was actually shown.** Walking the fetched chain and
finding every transition self-consistent when assumed to start at zero proves
only that *the oldest entry the fetch retrieved implies a zero balance
immediately before it* — it does not independently prove that entry was the
account's true first-ever activity in that asset, and it cannot rule out
earlier history outside whatever window the ledger endpoint actually returned
(retention limits, an earlier funding path not exposed via this endpoint, etc.).

This was corrected in two ways:
1. **Told to the user directly**, retracting the overclaim.
2. **Encoded into the software itself** — every terminal result that rests on
   the bare `zero_opening_confirmed` flag (rather than a genuine
   `OpeningCheckpoint` with independent evidence) now carries an explicit note
   to this effect inside its own `reason` field, so the qualification survives
   into the published artifact and can't be dropped from a future summary.

**Every "trusted" result produced so far — both BTC and SOL — rests on this
bare, unverified flag and must be read as conditional**, not as independently
confirmed proof of a zero starting balance.

---

## Offline test coverage

| File | Tests |
|---|---|
| `tests/crypto/test_ledger_quantity_reconciliation.py` | 36 |
| `tests/crypto/test_ledger_quantity_reconciliation_integration.py` | 17 |
| `tests/crypto/test_ledger_shadow_run.py` | 37 |
| `tests/crypto/test_kraken_ledger_fetch.py` | 21 |
| `tests/crypto/test_ledger_reconciliation_audit.py` (pre-existing, unchanged) | 10 |
| `tests/crypto/test_asset_movement_analysis.py` (pre-existing, unchanged) | 54 |

All pass. Full repository suite: **1639 collected, 1637 passed**, 2 failed — both
pre-existing and unrelated (`tests/stock/test_weekly_monitor.py`, present before
this work began).

---

## Live validation performed (real Kraken account, read-only)

Explicitly authorized before each live call. Only `privatePostLedgers` and
`fetch_balance` were ever invoked. `logs/HALT`, `logs/trades.db`,
`logs/risk_state.json`, and both `logs/live_state_*.json` files were confirmed
byte-identical (same mtimes) before and after every live run.

### BTC/CAD — 3 cycles, 2 restarts

| Cycle | Flag | fetch | coverage | manifest | chain | wallet agrees | trusted |
|---|---|---|---|---|---|---|---|
| 1 | none | ok | ok | complete | n/a — opening unverified | True | **False** ("opening balance not verified") |
| 2 (restart) | `--zero-opening-confirmed` | ok | ok | complete | True | True | **True** *(conditional — see correction above)* |
| 3 (restart) | `--zero-opening-confirmed` | ok | ok | complete | True | True | **True** *(same, consistent)* |

11 real BTC ledger entries fetched and independently walked; every transition
matched Kraken's own reported running balance exactly, ending at 0 (matching
the account's real current BTC balance — consistent with the account being
currently flat on BTC). The base-currency fee entry (`TDCRFZ-MWTNB-2NVHO6`)
found and explained earlier this session appears correctly in this chain.

### SOL/CAD — 2 cycles, dedicated storage

| Cycle | Flag | fetch | coverage | manifest | chain | wallet agrees | trusted |
|---|---|---|---|---|---|---|---|
| 1 | none | ok | ok | complete | n/a — opening unverified | True | **False** ("opening balance not verified") |
| 2 | `--zero-opening-confirmed` | ok | ok | complete | True | True | **True** *(conditional — see correction above)* |

6 real SOL ledger entries: 2 trades (one full round-trip, netting to zero) and
**4 real staking reward entries**, each with its own fee deducted from the
reward — fetched with no special-case code (the adapter pulls every entry for
the asset regardless of `type`; this was confirmed by inspecting the actual
type distribution, not assumed). Every transition matched exactly, ending at
`0.0000035758` SOL — the same figure independently established by the earlier
session's `ledger_reconciliation_audit.py` work. Both terminal outcomes (the
untrusted first run and the conditionally-trusted second) are preserved intact
in `logs/shadow/ledger_reconciliation/live/sol/history.jsonl`.

---

## What this milestone establishes, and what it does not

**Established**: the observer's happy-path logic — coverage proof, atomic
persistence, manifest completeness, per-observation trust binding, and the
correctly-labeled "opening not verified" result on a fresh account/asset — has
now been exercised successfully against real exchange data for two real assets,
across multiple restarts, with no code defect surfaced by the live data itself.
The failure-handling paths (crash mid-fetch, publication failure, evidence-mode
conflict) remain offline-only evidence — see "Design properties" above.

**Not established** — three separate, still-open prerequisites before live
trading could ever be considered on the strength of this observer:

1. **Independently verified opening-balance evidence.** Both accepted results
   above rest on the bare, unverified `zero_opening_confirmed` flag. No
   evidence *independent of the chain's own self-consistency* has been
   obtained for either asset. What would actually count as independent
   evidence (e.g., cross-referencing Kraken's stated ledger retention window,
   confirming no earlier account activity exists via a different data source)
   has not been investigated and is not solved by this report.
2. **Broader operational shadow acceptance.** What exists so far is a handful
   of manually-triggered runs, not a sustained observation period. A real
   acceptance bar would look like running the shadow observer on a schedule
   over real, ongoing account activity for a meaningful stretch of time, and
   confirming it continues to reconcile cleanly (or fails informatively) as
   real trades and staking credits keep happening — not just a few point-in-time
   checks.
3. **Fresh net-of-cost profitability evidence.** Completely independent of
   everything in this report. Per the standing 2026-09-12 decision already on
   record (`CLAUDE.md` → "Review deadline & keep/retire criteria"), the crypto
   bot remains HALTed and BUYs stay paused until a walk-forward on data
   strictly after 2026-09-12 clears PF≥1.2 net of fees on all windows for both
   BTC/USDT and SOL/USDT, evaluated against a buy-and-hold benchmark — by
   2027-03-12 at the latest. Nothing in this ledger-observer work moves that
   gate at all; it was never meant to.

**`logs/HALT` remains engaged. No trading is authorized by this report or by
anything built in this effort.**

---

## File manifest (this effort, in build order)

```
CRYPTO_BOT_LEDGER_MOVEMENT_INTEGRATION_PROPOSAL_2026-09-21.md   (design doc, v1→v4)
bot/accounting/asset_movement_analysis.py                        (earlier, float-based attribution — unchanged this pass)
scripts/asset_movement_discrepancy_report.py                     (earlier — unchanged this pass)
scripts/ledger_reconciliation_audit.py                           (earlier — unchanged this pass)
bot/accounting/ledger_quantity_reconciliation.py                 (core observer)
tests/crypto/test_ledger_quantity_reconciliation.py              (36 tests)
tests/crypto/test_ledger_quantity_reconciliation_integration.py  (17 tests)
bot/accounting/ledger_shadow_run.py                              (orchestrator + history log)
tests/crypto/test_ledger_shadow_run.py                           (37 tests)
bot/accounting/kraken_ledger_fetch.py                            (real Kraken read-only adapter)
tests/crypto/test_kraken_ledger_fetch.py                         (21 tests)
scripts/ledger_shadow_run.py                                     (CLI)
logs/shadow/ledger_reconciliation/live/{observations.db,status.json,history.jsonl}       (real BTC evidence)
logs/shadow/ledger_reconciliation/live/sol/{observations.db,status.json,history.jsonl}   (real SOL evidence)
```

No production file (`logs/HALT`, `logs/trades.db`, `logs/risk_state.json`,
`logs/live_state_*.json`) was ever written to by any part of this effort.
