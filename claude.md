# Personal Crypto Trading Bot (Exchange-Agnostic)

## 🎯 Goal
Build a modular, production-style crypto trading system that:
- Works globally (Canada, India, etc.)
- Uses crypto exchanges via a unified API layer
- Starts simple (rule-based bot)
- Evolves into advanced trading system (risk + indicators + optional AI)
- Runs locally first, then deployable to VPS

---

## 🌍 Key Design Principle (VERY IMPORTANT)

This system must NEVER depend on a single exchange.

We use:
👉 ccxt (universal crypto exchange API)

So exchanges can be swapped easily:
- Kraken
- Binance (where available)
- Coinbase
- KuCoin
- OKX

---

## 🚫 HARD RULES
- No assumptions of profitability
- No AI/ML in early phases
- No real money trading in Phase 1–2
- Always include risk management before execution
- Keep architecture modular and clean
- No exchange-specific hardcoding

---

## 🧱 SYSTEM ARCHITECTURE

/bot
  /data              → market data layer (ccxt)
  /exchanges         → exchange abstraction (ccxt wrapper)
  /strategy          → trading logic
  /risk              → risk management engine
  /execution         → order execution layer
  /indicators        → technical indicators (later)
  /ai                → optional AI module (final phase)
  main.py

---

## ⚙️ PHASE PLAN

### PHASE 1 — Local Simulation Bot
- Simulated price feed (random walk or sample data)
- Basic strategy:
  - BUY if price below threshold
  - SELL if price above threshold
  - HOLD otherwise
- Console logs only
- No API usage

---

### PHASE 2 — Live Market Data (ccxt)
- Replace mock data with real crypto data via ccxt
- Connect to exchange (configurable)
- Still NO real trading
- Only market data + signals

---

### PHASE 3 — Paper Trading Execution
- Simulated orders or exchange sandbox (if available)
- Add execution layer
- Add trade logging system
- Introduce order lifecycle tracking

---

### PHASE 4 — Risk Management Layer (CRITICAL)
- Max % per trade (0.5–1%) — conservative default
- Daily loss limit — blocks new BUYs only, SELL always allowed
- Max drawdown circuit breaker — blocks new BUYs only, SELL always allowed
- Trade approval gate
- Block unsafe trades

Risk engine is MANDATORY before execution.

---

### PHASE 5 — Indicator-Based Strategy
- RSI
- Moving averages (SMA/EMA)
- Trend detection
- Replace simple threshold strategy

---

### PHASE 6 — Optional AI Layer (Advanced)
- AI gives advisory signals only
- AI cannot execute trades
- Risk engine overrides AI decisions
- AI receives:
  - price
  - indicators
  - portfolio state

---

## 🔌 EXCHANGE LAYER RULES

- All exchange communication must go through ccxt
- No direct exchange SDKs
- Exchange must be configurable via environment variable

Example:
- exchange = "kraken"
- exchange = "binance"
- exchange = "kucoin"

---

## 🧾 LOGGING REQUIREMENTS
Every trade must log:
- timestamp
- price
- action
- reason
- risk decision
- exchange used

Logs must be readable and persistent.

---

## 🧠 CODING STYLE
- Python 3.10+ — run everything through `.venv` (Python 3.11, created 2026-07-05).
  The system python3 is 3.9 and must not be used: `X | Y` annotations only survive
  there via `from __future__ import annotations`, and yfinance ≥1.5 needs 3.10+.
  Launch: `caffeinate -i .venv/bin/python -m stock_bot.main` / `.venv/bin/python -m bot.main`
  Test:   `.venv/bin/python -m pytest --tb=short -q`
  Library versions were pinned to match the pre-venv environment (pandas 2.3.3,
  ccxt 4.5.56) so the interpreter was the only variable at switch time. yfinance was
  then upgraded 1.2.0 → 1.5.1 as its own change (2026-07-05). pandas stays 2.3.3 — pip
  wanted 3.0.x on 3.11; upgrade it deliberately, not as a side effect.
- Modular design
- Simple before complex
- Clean separation of concerns
- No premature optimization
- Fully testable components

---

## 🎯 FINAL OBJECTIVE
A robust crypto trading system that:
- Works globally
- Is exchange-independent
- Is safe by design

---

## 📜 Project History

The detailed, dated session-by-session log (research runs, incidents, audits, ops
changes — everything that explains *why* the current config/whitelist/rules look the
way they do) has moved to **`CLAUDE_HISTORY.md`** (split out 2026-07-25 because this
file had grown past 150k characters and was eating context budget every session).
This file now holds only current, actionable state. Consult the history file when you
need the full narrative behind any decision below.

---

## Test Suite Manifest (reconciled 2026-08-18, count updated 2026-08-24)

Expected total: **709 tests** (verified via `pytest --collect-only -q`; table sum below checked to match exactly). If `pytest --collect-only -q` reports a different number, a file has an import error, was deleted, was added without a manifest update, or was excluded from the runner. Investigate before trusting any green suite result. Suite runtime is ~9-26s — if it takes minutes, a test is reading live `.env` config. (2026-08-19: baseline checked at 527 immediately before that session's 7 new tests were added — one higher than the 526 this manifest previously claimed; not investigated further, flagging in case it matters later. 2026-08-24: 629→639, +10 for CapitalPool per-symbol slot caps — this line and the table row below were caught stale during a "what's missing" self-audit in the same session that added the tests; the manifest count and the actual suite had already diverged by the time that session's own report was written. Same self-audit then found the config-layer half of that same feature — `_slot_caps_by_base()` — had zero test coverage at all; closed it same-day, 639→647. Then 647→658, +11 for `rescreen.py`'s new USD leg + the `_alert()` nested-config bugfix — new file `tests/crypto/test_rescreen.py`, this script's first-ever test coverage. 2026-08-25: 658→666, +8 for `stock_bot.main._update_ai_health()` — new file `tests/stock/test_ai_health.py`, found during a "what are we missing" review, not a session that was already touching this code. 2026-08-26: 666→674, +8 for the crypto dashboard multi-symbol combine — new file `tests/crypto/test_dashboard_renderer.py`, `bot/dashboard/renderer.py`'s first-ever test coverage, found while fixing the single-symbol dashboard gap SOL/CAD's promotion exposed. Then 674→676, +2 for the stock bot's RULES-decision log-visibility fix — new file `tests/stock/test_rules_log_visibility.py`, found while answering a "why didn't it buy X" question with no log evidence available to check. 2026-08-27: 676→678, +2 for the maker→taker silent-fallback alert in `bot/execution/live_executor.py` (`tests/crypto/test_live_executor.py` 63→65) — a silent-degradation bug sweep prompted by the 2026-08-26 post-only fee bug, which hid for 2 months precisely because the maker→taker fallback was `logger.warning`-only. Then 678→680, +2 for the MTF-gate fail-open alert — new file `tests/crypto/test_mtf_gate_alert.py`, from the same sweep. Then 680→687, +7 for the blocked-BUY Telegram alert (`bot.main._evaluate_blocked_buy_alert`) — new file `tests/crypto/test_blocked_buy_alert.py`, Track 4 (observability); closes the 2026-08-18 "sat flat through a rally, nobody knew a BUY had been vetoed" gap. Then 687→694, +7 for the stock-bot analog — `stock_bot.main._evaluate_blocked_rule_buys_alert`, an end-of-cycle blocked-rule-BUY ops_alert digest — new file `tests/stock/test_blocked_rule_buys_alert.py`, from extending the same observability review to the stock bot. Then 694→697, +3 for IBKRExecutor TWS-query resilience (`tests/stock/test_ibkr_executor.py` 56→59) — `accountValues()`/`positions()` now serve a last-good cache on a transient failure instead of a fabricated 0.0/{}, from a stock-bot-readiness hardening pass. Then 697→698, +1 for `ibkr_trades.csv` write resilience (59→60) — a failed append buffers the real filled-trade row and retries, not silently drops it. Then 698→706, +8 for the Mistral AI provider + auto-failover (new file `tests/stock/test_ai_failover.py`) — `AI_PROVIDER=mistral` support and a one-shot switch to `AI_FALLBACK_PROVIDER` after 5 consecutive nvidia_nim API failures, closing the "manually swap NVIDIA_MODEL on every degradation" gap. Then 706→709, +3 for the native-stop pre-cancel-on-SELL fix (`tests/crypto/test_live_executor.py` 65→68) — a live SOL/CAD incident where the resting native stop reserved 100% of the coins so every TP/SL SELL failed "Insufficient funds" in a retry loop.)

**Directory layout (2026-08-18):** all 54 files moved out of repo root into `tests/{crypto,stock,shared}/` for
a cleaner root — 54 files loose alongside `bot/`, `stock_bot/`, `config.py`, etc. had gotten hard to scan.
Bucket assignment was by *actual import*, not filename guesswork (`grep`'d every file's `from bot...`/
`from stock_bot...` lines): `tests/crypto/` (22) covers `bot/main.py`, `bot/execution/`, `bot/risk/
risk_manager.py`, `bot/exchanges/`, `bot/portfolio/`, root `config.py`, and the crypto-only research
scripts (`grid_dca_experiment.py`, `grid_stress_test.py`, `shadow_signal.py`); `tests/stock/` (26) covers
everything under `stock_bot/`; `tests/shared/` (6) covers the library code both bots import directly —
`bot/alerts/{telegram,heartbeat,liveness}.py`, `bot/atomic_json.py`, `bot/indicators/indicators.py` (the
math underneath `IndicatorStrategy`, which `stock_bot/strategy/rules.py` reuses directly for its Mode A/B
replay — a separate, unrelated `stock_bot/indicators/indicators.py` also exists for the screener/AI side
and is NOT this module), and `unified_dashboard.py` (renders both bots' cards). `conftest.py` stayed at
repo root — pytest applies an ancestor `conftest.py` to every test below it regardless of nesting, so the
autouse Telegram/file-write safety fixtures still cover all three subdirectories unchanged. No `pytest.ini`/
`testpaths` existed before or after; `python -m pytest` still recurses from repo root and finds everything
the same as before — the run command (below) is unchanged. One file needed a code fix: `test_engine_params.py`
computed its repo-root path via `Path(__file__).resolve().parent`, which assumed root-level placement;
now `.parent.parent.parent`. Verified before/after: same 526 collected, same 526 passed, same ~11s runtime
(confirms no test started reading live `.env` as a side effect of the move).

| File | Tests | What it covers |
|------|-------|----------------|
| `tests/shared/test_indicators.py` | 30 | RSI, EMA, ADX, MACD, ATR calculations; regime-classification self-referential-ATR-baseline regression (2026-08-20 — `_classify_regime()` contract check + a mutation-verified test driving real `evaluate()` end to end) |
| `tests/crypto/test_live_executor.py` | 65 | LiveExecutor: dry-run, market/limit orders, urgent-exit bypass, fee deduction, state save/load, pre-trade min-size guard, restart recovery (seeds position manager + state machine), native stop-loss backstop (placement, cancel, resync, failure alerting, dry-run/flag-off no-ops, restart reconciliation), native TRAILING stop-loss backstop (placement w/ trailingPercent param, priority over static, cancel, resync-on-quantity-change, failure alerting, dry-run no-op, state persist/restore — added 2026-08-19), `native_stop_price` property, multi-stop-order ambiguity detection on startup (alerts on 2+ stop-type orders resting, ignores unrelated non-stop open orders — added 2026-08-20), slippage guard (BUY/SELL unfavorable trip, within-threshold, favorable-direction, disabled, dry-run no-ops), restart-startup resting-stop quantity reconciliation (resize under-sized static/trailing, leave over-sized alone, no-op on match/unknown-qty) + untracked-but-real resting stop adoption (single static, single trailing, multiple-not-adopted, none-found-unchanged — both added 2026-08-20 follow-up), maker→taker silent-fallback alert (fires MAKER FALLBACK Telegram alert when a post-only limit degrades to a market order, no alert on a clean limit fill — added 2026-08-27), native-stop pre-cancel-on-SELL (2026-08-27 live incident: a resting native stop reserves 100% of the base asset so every TP/SL SELL failed "Insufficient funds" — `execute()` now cancels the stop before the sell, re-arms it at the prior static level if the sell is rejected, and does nothing when there's no resting stop) |
| `tests/crypto/test_capital_pool.py` | 37 | CapitalPool: slot allocation, slot cap, release, edge cases. +10 (2026-08-24): per-symbol slot caps (`slot_caps` dict, `slot_cash_for()`) — no-override backward compat, single-symbol-dict matches old single-shared-cap exactly, untouched-symbol falls back to shared default, two-symbols-both-fit, insufficient-total-so-second-gets-remainder, pre-allocation order-dependence, zero=uncapped-per-symbol, property readable, negative-cap validation, release-then-reallocate cycle. +8 (2026-08-24, same-day follow-up — closes a coverage gap found by a "what's missing" self-audit): `config._slot_caps_by_base()` env-var scanner — empty when unset, parses multiple `MAX_SLOT_CASH_CAD_<BASE>` overrides, base uppercased, ignores unrelated keys (incl. the old shared `MAX_SLOT_CASH_CAD`), invalid value raises naming the key; `PortfolioConfig` accepts/validates `max_slot_cash_cad_by_base`, rejects negative, defaults to an empty dict |
| `tests/crypto/test_correlation.py` | 17 | Pearson correlation, pct_returns, fetch_correlation |
| `tests/stock/test_stock_correlation.py` | 5 | `stock_bot/risk/correlation.py`: `fetch_correlation_from_closes` — no-network wrapper reusing bot/risk/correlation.py's pearson/pct_returns unchanged |
| `tests/stock/test_stock_correlation_gate.py` | 8 | `stock_bot.main._check_correlation_gate`: blocks on >0.70 correlation with an open position, allows when uncorrelated/no positions/adding to self-held symbol, fails open on missing candle data (candidate or peer), case-insensitive symbol matching, source-inspection guard confirms `run()` still calls it and blocks on a hit |
| `tests/stock/test_stock_macro_calendar.py` | 14 | `stock_bot/risk/macro_calendar.py`: `jobs_report_dates` (12 Fridays/year, first week, invariant-checked not hardcoded), `parse_user_event_dates` (valid/empty/invalid-skipped/whitespace), `is_macro_blackout` (exact-date/before/after/boundary-inclusive, disabled at 0 or negative, jobs-report-alone triggers, nearest-event-wins when multiple in window) |
| `tests/stock/test_stock_macro_blackout_gate.py` | 6 | `stock_bot.main._is_macro_event_blackout` config-reading wrapper: blocks on user-supplied date, disabled at 0, fails open on bad config value, jobs-report-alone still checked with empty user dates, source-inspection guard confirms `run()` calls it market-wide before the per-symbol earnings check, plus a guard that the fixed test date stays clear of real jobs-report Fridays. **Two of these used the real `date.today()` until 2026-08-07 and failed on every actual jobs-report day (~3 days/month, symmetric window) — production code was always correct, the tests were contaminated by the live calendar. Now pinned to a fixed 2026-09-16 reference via a patched `stock_bot.main.date`; mutation-tested to confirm they still fail if the gate itself breaks.** |
| `tests/stock/test_stock_vix_crisis.py` | 6 | `stock_bot/risk/vix_crisis.py`: `is_vix_crisis` — at/above/below threshold, None fails open, zero/negative threshold disables |
| `tests/stock/test_stock_vix_crisis_gate.py` | 2 | Source-inspection guard: `run()` fetches `^VIX`, computes crisis mode via `is_vix_crisis`, and gates new BUYs on it (reuses the same `_regime_ok` flag as the SPY regime filter) |
| `tests/stock/test_stock_settlement_csv.py` | 11 | Settlement/FX tax record-keeping (Canadian ACB/FX, minimal scope — data capture only, no gain computation): `_next_business_day` T+1-skip-weekends (both paper.py and ibkr.py copies), frozen `paper_trades.csv`/`ibkr_trades.csv` header proven UNCHANGED, new settlement CSV written on BUY and SELL with correct join key (timestamp/symbol/side) back to the frozen CSV, CAD symbol records fx_rate=1.0 |
| `tests/crypto/test_risk_manager.py` | 32 | RiskManager: halt gate, daily loss, position size, SL/TP bypass, state persistence, per-symbol caps, aggregate account breakers, kill-switch tier (sticky, priority-over-halt, never-blocks-SELL, restart persistence), drawdown-halt tier (not sticky), weekly-loss tier (trip/allow/week-rollover), non-blocking drawdown-warning tier |
| `tests/crypto/test_fill_recording.py` | 8 | BUG 1: qty=0 fill — filled priority, amount fallback, guard, TradeLog guard |
| `tests/crypto/test_external_holdings.py` | 6 | External-holdings guard in _sync_position (adopt=false/true) |
| `tests/crypto/test_executor.py` | 6 | PaperExecutor: BUY/SELL, insufficient cash, history |
| `tests/crypto/test_drift_escalation.py` | 17 | Drift: tests REAL `_evaluate_drift()` from bot.main — escalation, ack (no re-alert on unchanged drift), changed-amount re-alert, resolution reset. Plus (2026-08-18) REAL `_update_auth_health()` — the Kraken-auth-outage alert-edge/heartbeat-flag logic from 2026-08-15, extracted out of the inline tick loop specifically to close the "no test coverage" gap noted at the time: below-threshold silence, trip-at-threshold alert + heartbeat flag flip, no re-alert while still failing, recovery alert + counter reset, healthy-path never touches the alerter. Plus (2026-08-20) REAL `_seed_native_stop_state()` — the restart-recovery native-stop `ss`-mirroring helper: static resting, trailing resting, naked/nothing resting, mismatched-price trusted verbatim (not recomputed) |
| `tests/stock/test_tsx_validation.py` | 5 | Stock-bot TSX price sanity check |
| `tests/stock/test_stock_breaker.py` | 14 | Stock-bot circuit breakers (StockPaperExecutor): daily-loss restart baseline includes position marks; weekly-loss/drawdown-halt/kill-switch tiers — reject-on-trip, halt auto-lifts on recovery, kill switch stays sticky through recovery and across restart, SELL never blocked, peak-equity persistence, drawdown_status() warning flag; per-position ATR stop-pct override — defaults to baseline, persists across restart, clears on full close, survives a partial close |
| `tests/crypto/test_candle_watchdog.py` | 7 | Candle watchdog circuit breaker (upgraded 2026-08-07): silent/blocked/no-re-alert-while-stale on the stale side, alert+unblock on recovery, no re-alert once recovered |
| `tests/crypto/test_halt_flag.py` | 5 | Manual halt kill-switch: logs/HALT flag file engage/lift, ownership guard |
| `tests/crypto/test_telegram_control.py` | 30 | Two-way Telegram control (added 2026-08-20): `TelegramCommandPoller` transport (authorized dispatch+reply, unauthorized-chat silent ignore, unrecognized-command silent ignore, handler-exception no-raise, offset advances past both handled and ignored updates, `prime_offset()` drains backlog without dispatching, getUpdates failure doesn't raise/doesn't lose offset, disabled-without-credentials no-network, thread-starter returns None when disabled), source-inspection structural guards (no command body calls any order-placement/modification/cancellation method or bypasses `logs/HALT` via a direct `risk.halt()`/`resume()`, and the poller module itself carries zero trading imports), `_pause_crypto_flag`/`_resume_crypto_flag` (write/remove `logs/HALT`, idempotent, end-to-end proof they drive the SAME `_check_halt_flag()` the tick loop already polls — not a second path), `_status_crypto_text`/`_format_symbol_status` (halt/kill-switch display, per-symbol position/cash/PF/regime, PF n/a-vs-inf-vs-computed edge cases), `_status_stock_text` (paper/IBKR badge formatting, no-state-file case, loader-exception no-raise), `_help_crypto_text`. +2 (2026-08-23): getUpdates failure now backs off `error_backoff_s` (default 5s) before the next poll attempt, and a success never backs off — closes a hot-loop bug where a fast-failing error (e.g. an immediate 502, not a timed-out long-poll) had no pacing at all |
| `tests/crypto/test_orphaned_positions.py` | 5 | Startup orphan check: open position outside this run's symbol list alerts (removed-from-whitelist safety) |
| `tests/crypto/test_universe.py` | 4 | Universe screener: scoring, momentum filter, fallback |
| `tests/crypto/test_main_strategy.py` | 2 | Strategy builder: full config wiring |
| `tests/stock/test_fast_validator_exits.py` | 6 | FastValidator exits: MAX_HOLD live-price fallback, corruption guard, SL regression |
| `tests/stock/test_paper_report.py` | 10 | Expectancy math: IBKR commission model, net-of-cost flip, report rendering, merged paper+IBKR position book, IBKR account section, active-book state synthesis, live-cash-snapshot precedence over stale fill CSV |
| `tests/stock/test_exit_policy.py` | 11 | Stock-bot asymmetric exit bars: single-verdict exit, 2-strike SELL streak, streak resets, AC.TO incident regression |
| `tests/stock/test_stock_backtest_engine.py` | 14 | Stock backtest engine: next-open fills, intra-candle SL/TP, gap handling, slippage/commission math, walk-forward gating. +3 (2026-08-23): optional ATR(14)×mult stop-distance mode (`StockBacktestConfig.atr_sl_mult`, default `None` = unchanged flat behavior) — ATR override diverges from the flat stop on an engineered candle sequence, same-candles flat-mode control proving the two modes are genuinely different, fallback to flat when history is too short for a 14-period ATR |
| `tests/stock/test_stock_rules.py` | 5 | Rule signals: live==backtest replay parity, drop_last (forming candle), determinism, validated-parameter pin |
| `tests/crypto/test_audit_scheduler.py` | 14 | In-bot audit scheduler: tests REAL `_audit_due()` — daily catch-up, once-per-day, Mon-anchored weekly, monthly 1st-anchored (re-screen), missed-run catch-up |
| `tests/crypto/test_limit_chase_recovery.py` | 6 | 2026-07-15 unrecorded-fill regression: market-fallback polling, actual-type amount inference, cancel-race double-fill guard |
| `tests/stock/test_ibkr_executor.py` | 56 | IBKRExecutor (hermetic FakeIB): live-port/paper-account guards, contract mapping (.TO↔TSE/CAD, bare NYSE cross-listings→NYSE), broker-price fills, timeout rejection, cancel-race fill recording, realized-PnL persistence, try_reconnect probe (redial/never-raise/no-op), low-equity FX/margin-minimum guard (CAD exempt), starting_cash auto-rebaseline on external reset/deposit, live-cash snapshot persisted + preserved across disconnect, sector-concentration gate (reject 3rd same-sector position, allow add-on to already-held symbol, allow different sector), weekly-loss/drawdown-halt/kill-switch tiers (reject-on-trip, halt auto-lifts, kill switch sticky + persists across restart, SELL never blocked, peak-equity persistence, warning-status flag), per-position ATR stop-pct override (default/persistence/cleared-on-full-close), check_exposure projected (pending-trade-value) exposure — defaults to current-state-only, catches an oversized single BUY, allows one that stays under cap; LiveTradingGate enforcement (added 2026-08-20) — all-Gates-1-3-pass succeeds, Gate-4-fail still succeeds (not enforced), single-gate FAIL/PENDING blocks with `ValueError`, error names only the actually-failing gate(s), blocked before any TWS connection attempt, paper mode never evaluates the gate (count corrected 48→49 pre-existing then +7 new — the 48 in this manifest predated an untracked 49th test, not investigated further, same class of drift as the 2026-08-19 note above). +3 (2026-08-27, stock-bot-readiness hardening): TWS-query resilience — `positions_snapshot()` serves the last-good cached book on a transient `positions()` failure (not `{}`, which would blind the SL/TP watcher to a real position), `cash` serves last-good on an `accountValues()` failure (not a fabricated `0.0`, which rejects every BUY), `sync_healthy` flips false→true across the failure/recovery edge; no-cache-yet (failure on the very first call) still falls back to `0.0`/`{}`. +1: `ibkr_trades.csv` write resilience — a failed append (`OSError`) buffers the filled-trade row in `_unwritten_csv_rows` and retries (flushing the backlog) on the next fill instead of `logger.warning`-and-drop; `csv_write_healthy` flips while the buffer is non-empty |
| `tests/stock/test_fx_sizing.py` | 14 | USD/CAD sizing fix (2026-07-31): `is_cad_symbol`, `get_usd_cad_rate` (fetch/fallback/cache), StockPaperExecutor mixed-currency `total_value`/`check_exposure`, sector-concentration gate (reject 3rd same-sector position, allow add-on to already-held symbol, allow different sector), check_exposure projected (pending-trade-value) exposure — defaults to current-state-only, catches an oversized single BUY, allows one that stays under cap |
| `tests/stock/test_screener_in_distribution.py` | 5 | In-distribution ATR%/liquidity filter (added 2026-08-23, `stock_bot/data/screener.py`, the replacement safety net after RULE_WHITELIST stopped gating BUYs): normal volatility/liquidity passes with no rejection reason, extreme ATR rejected with a `SCREEN_SKIP` reason naming the symbol, illiquid symbol rejected with a `SCREEN_SKIP` reason, insufficient-candles (<15) passes through without triggering the filter, sanity check that the 4 originally-backtested symbols (MRNA/AMD/RY.TO/PLTR) clear their own reference thresholds |
| `tests/stock/test_accuracy_tracker.py` | 18 | `LiveTradingGate` gate-repair (2026-08-20): Gate 1 (`logs/stock_backtest_latest.json` vs `RULE_WHITELIST`) — missing/malformed JSON, all-symbols-pass, one-symbol-fail, symbol-missing-from-run, non-whitelist-symbol ignored, empty-whitelist; Gate 2 (AI confidence-band edge, repurposed from the retired fast book) — pending/pass/fail on win-rate threshold, LOW/PRE-band trades excluded, structural guard confirming no `_FAST_TRADES_CSV` reference remains; Gate 3 (raised to ≥30 round-trips/PF≥1.2/win≥30%) — pending, all-three-pass, both directions of "2/3 criteria pass but still FAIL" (PF-only-failing, win-rate-only-failing) |
| `tests/stock/test_checkpoint_tracker.py` | 14 | Post-whitelist review checkpoint tracker (added 2026-08-23, `stock_bot/analysis/checkpoint_tracker.py` — dashboard visibility only, no trading-logic change): empty input, original-symbol (MRNA/AMD/RY/RY.TO/PLTR) round-trips excluded from the non-original count, pre-cutoff-date (`<2026-08-23`) trades excluded, on-cutoff-date trades included, below-15-sample never triggers regardless of results, win-rate-gap trigger (≥15pp), PF-gap trigger (non-original PF<1.0 while original PF≥1.2), AI-agreement-gap trigger (≥20pp agree-vs-disagree split, parsed from the `ai=SIGNAL` shadow-vote tag), a healthy 15-trade population that does NOT trigger, `ai=NONE`/untagged reasons excluded from the AI split, progress-bar caps at 100%, `ORIGINAL_SYMBOLS`/`WHITELIST_REMOVED_DATE` constant sanity checks. +1 (2026-08-24): AI-agreement sample-size guard — a 3-vs-12 lopsided split with a real 100pp win-rate gap must NOT contribute to a trigger (3 clears the old shared 3-per-side floor but not the new dedicated `_MIN_TRADES_FOR_AI_SPLIT=5`) |
| `tests/shared/test_heartbeat.py` | 8 | Heartbeat pings (bot/alerts/heartbeat.py): URL-off, success/failure never raise, healthy_fn gate |
| `tests/stock/test_tws_monitor.py` | 6 | TwsConnectionMonitor state machine: blip tolerance, alert-once per outage, recovery notice |
| `tests/crypto/test_atr_sizing.py` | 7 | calc_trade_qty_atr_risk: dollar-risk-at-stop == fixed-SL baseline, tight-stop cap, fallbacks |
| `tests/stock/test_stock_atr_sizing.py` | 7 | Stock-bot analog: `StockConfig.calc_shares_atr_risk` (whole-share sizing) — same invariant, opt-in via PAPER_ATR_SIZING_ENABLED (default false) |
| `tests/stock/test_stock_telegram.py` | 7 | Stock→Telegram relay: root-.env credential sourcing, ops_alert/fill forwarding, HIGH-only filter, channel-off no-ops |
| `tests/shared/test_crash_hardening.py` | 9 | atomic_write_json (valid/replace/no-tmp/parents/old-file-preserved), send_now sync + disabled, crash-alert helpers never raise |
| `tests/crypto/test_engine_params.py` | 8 | `engine_kwargs_from_cfg` builder: keys accepted by engine.run, ATR keys sourced from cfg, previously-drifted keys present, macd_enabled + Mode A/B entry params sourced from cfg, generic parity test (every StrategyConfig∩IndicatorConfig field reaches the backtest), validation scripts use the builder — now 4 scripts (backtest.py, walkforward.py, validate_symbol.py, and `screen_universe.py` added 2026-08-26 after it was caught hand-listing stale kwargs — see CLAUDE_HISTORY.md) |
| `tests/stock/test_alert_evaluator.py` | 4 | AlertEvaluator EARNINGS_SOON: held-vs-not-held priority/message, live-executor-only held-position source (no static PORTFOLIO tracker) |
| `tests/crypto/test_crypto_telegram.py` | 2 | TelegramAlerter.fill() reason line: included when given, omitted when absent |
| `tests/shared/test_liveness.py` | 7 | LivenessTracker (bot/alerts/liveness.py): touch/is_alive/staleness boundary, simulated hang between touches |
| `tests/stock/test_ai_engine_timeout.py` | 2 | nvidia_nim AI client is constructed with `timeout=_TIMEOUT_S`; empty `completion.choices` degrades to a HOLD verdict instead of raising TypeError |
| `tests/stock/test_ai_failover.py` | 8 | Mistral provider + auto-failover (added 2026-08-27 — see "AI provider auto-failover" below): `AI_PROVIDER=mistral` configures the OpenAI-compatible endpoint / disabled without `MISTRAL_API_KEY` / a call produces a `provider="mistral"` verdict; auto-failover — switches to `AI_FALLBACK_PROVIDER` only after `_FALLBACK_AFTER` (5) consecutive API failures and the retry succeeds on the new provider, no switch without `AI_FALLBACK_PROVIDER` set, no switch when the fallback key is missing, `_switch_to_fallback()` is one-way (2nd call returns False), a parse error does NOT count as an API failure (a failover wouldn't fix it) |
| `tests/stock/test_earnings_cache.py` | 4 | Earnings-fetch cache: failures use a short 1h TTL (retry soon) vs successes using the full 24h TTL, boundary behavior for both, concurrent fetches serialized by `_yf_lock` |
| `tests/stock/test_yf_client_retry.py` | 4 | `fetch_with_retry`: generic exceptions now retried with a short delay (not zero retries), give up after max_attempts, short delay ≠ rate-limit backoff, rate-limit path unchanged |
| `tests/stock/test_research_aggregator_timeout.py` | 1 | Per-source research-fetch timeout: earnings gets a wider budget (45s) than news (15s) |
| `tests/crypto/test_kraken_retry.py` | 4 | `bot/exchanges/retry.fetch_with_retry`: succeeds without retrying, retries on failure and can recover, raises the last exception after exhausting attempts, custom attempts/delay respected |
| `tests/crypto/test_shadow_signal_retry.py` | 3 | `shadow_signal.shadow_replay` Kraken fetch now wrapped in `fetch_with_retry` (2026-08-05 fix — a single fetch hiccup used to waste the whole day's shadow audit): transient failure recovers, persistent failure still returns `[]` but only after retrying (not a silent single miss), first-try success doesn't retry |
| `tests/shared/test_unified_dashboard.py` | 9 | `unified_dashboard._read_gate_stats`/`_gate_tracker_section` shadow-match-rate parsing (2026-08-05 fix — an unbounded regex let an "N/A" match-rate row fall through to a fabricated reading pulled from an unrelated number later in the report): passing/failing real percentages still parse correctly, N/A no longer bleeds into the unrelated BACKTEST_FEE_PCT number, N/A renders a distinct message from "never run", latest-by-filename report selection; `_crypto_card` STALE-vs-NO-FILLS badge (2026-08-06 fix — state-file age alone flagged a healthy week-quiet bot as STALE): old state + fresh trade_bot.log → "NO FILLS · Nd — bot alive", old state + stale log → still "STALE ... check the bot", fresh state → "LIVE" regardless of log age |
| `tests/stock/test_stock_position_mark_refresh.py` | 4 | Stock-bot daily-loss breaker staleness fix: tests REAL `_mark_positions_to_market()` from stock_bot.main via a mocked `_fetch_symbol_data` — breaker trips from a price move alone (no fill), stays silent within limit, no-ops when executor is None, source-inspection guard confirms `run()` still calls it |
| `tests/stock/test_sl_tp_watcher_audit_log.py` | 9 | First behavioral coverage of `_check_open_positions_sl_tp` (previously untested) plus the 2026-08-06 INFO-level "N/M positions priced" audit log — added after a yfinance outage broke the main scan loop for a full day with no direct evidence either way on whether this separate `get_live_price()` path (fast_info, independent thread) was also blind. Covers full/partial/total pricing failure counts, no-log-when-no-positions, zero-share exclusion, None-executor no-op, and basic STOP_LOSS/TAKE_PROFIT trigger sanity |
| `tests/crypto/test_grid_stress_test.py` | 14 | `grid_stress_test.py` pure helpers (crypto research tooling, not the live pipeline): crash-period date parsing, buy-and-hold P&L calc, PASS/MARGINAL/FAILED classification. Hermetic — the actual stress run against Binance is a separate manual step |
| `tests/crypto/test_grid_dca_experiment.py` | 12 | `grid_dca_experiment.py` standalone backtest engines (crypto research tooling, not the live pipeline): grid strategy fills/reopens/floor-stop, capital split across slots, fee math on both legs, DCA safety-order averaging + cycle restart, empty-candle edge cases |
| `tests/crypto/test_rescreen.py` | 11 | `rescreen.py` (added 2026-08-24, first-ever coverage for this script): `_crypto_usd_whitelist()` — empty when only CAD whitelisted, filters `/USD` suffix, empty-string input, and a regression check that `_crypto_whitelist()`'s own (pre-existing) behavior is unaffected; the new USD leg — runs with `extra_env={"SCREEN_QUOTE": "USD"}`, its results land in a correctly-formatted `## crypto-usd` report section, a gate-script failure on the USD leg reports the same way the existing rc≠0 handling already does; regression check that the CAD leg's own report section/whitelist-comparison is unchanged; `RESCREEN_SKIP_USD` skip flag; `_alert()`'s nested-config-attribute bugfix (`cfg.alerts.*` not flat `cfg.*`) — no `AttributeError` swallowed, `TelegramAlerter` constructed with the correct values |
| `tests/shared/test_telegram_retry.py` | 3 | `TelegramAlerter._send()` retry (added 2026-08-17, closes known-gaps #17): healthy send calls `requests.post` once with no retry, a transient failure recovers on retry, a persistent failure still degrades to a warning-only no-raise after exhausting attempts |
| `tests/stock/test_ai_health.py` | 8 | `stock_bot.main._update_ai_health()` (added 2026-08-25, closes the stock-bot analog of the 2026-08-15 Kraken-auth-outage gap — nvidia_nim has degraded 3 separate times on this project, each only ever caught by manually testing the API by hand, never by the bot itself): below-threshold silence, trip-at-3-consecutive-fully-failed-cycles alert, no re-alert while still failing, recovery alert + counter reset, healthy-path never touches the notifier, blank-detail formatting; two source-inspection wiring guards — `run()` only evaluates health on a cycle that actually attempted an AI call (`_ai_attempted_n > 0`), and deliberately does NOT wire `_ai_health` into either heartbeat's `healthy_fn` (AI is advisory-only — an outage must not misreport "the bot is down") |
| `tests/crypto/test_dashboard_renderer.py` | 8 | `bot/dashboard/renderer.py` (added 2026-08-26, first-ever coverage for this module — 0 tests existed before, part of why the single-symbol dashboard gap below went unnoticed): the multi-symbol combine (`write_multi()`, replacing the old single-symbol-only `dashboard.html` render path that left SOL/CAD with zero dashboard visibility after its promotion) — both symbols render on one shared page shell (one `<html>`/`<style>`, not two documents), single-symbol case still works, list order is the render order, the position-protection panel appears only inside the symbol actually holding a position (not leaking across symbol blocks when one is flat and one isn't — the real cross-contamination risk this refactor had to avoid), no panel when flat, fills/fees render for the correct symbol, the single-symbol `write()` wrapper produces equivalent content to `write_multi()` with a one-element list (not a diverging second code path), parent-directory auto-creation |
| `tests/stock/test_rules_log_visibility.py` | 2 | Wiring guard (added 2026-08-26): the per-symbol `📐 RULES:` decision line (BUY/SELL/HOLD + RSI/ADX/trend/regime — "why did/didn't the bot buy symbol X today") was console-`print()`-only, never reaching `logs/stock_bot.log`, found while answering exactly that question with no log evidence to check. Source-inspection guard confirms `run()` now also `logger.info()`s it, with the symbol name embedded (the console print relies on a separate header line printed just before it for symbol context, which doesn't survive being read out of that order in a log file) |
| `tests/crypto/test_mtf_gate_alert.py` | 2 | MTF-gate fail-open alert (added 2026-08-27, silent-degradation bug sweep): source-inspection guards that `run()`'s MTF (1D BEARISH) veto fires an `alerter.error()` **MTF GATE BYPASSED** alert when the daily-candle fetch fails with no cached closes (risk gate silently bypassed on a live BUY), and that the alert is guarded by the `if not _mtf_has_cache:` branch so the cached-closes path (gate still runs on slightly older data) does not alert |
| `tests/crypto/test_blocked_buy_alert.py` | 7 | `bot.main._evaluate_blocked_buy_alert` (added 2026-08-27, Track 4 observability — closes the 2026-08-18 "bot sat flat through a $90k→$108k BTC rally, nobody knew a real BUY had been vetoed" gap; that incident's fix was logging + `live_signals.csv` only, nothing pushed it): edge-triggered `alerter.error()` when the raw strategy signal is BUY but a gate holds it — alerts once, no re-alert while the same (symbol, gate) blocks, re-alerts when the gate changes, clears the flag when the raw signal stops being BUY or the BUY clears (fresh block after that re-alerts), no alert when the BUY is approved, unknown gate still alerts with the raw name, source-inspection guard that `run()` calls it from the candle-close branch after the CSV write |
| `tests/stock/test_blocked_rule_buys_alert.py` | 7 | `stock_bot.main._evaluate_blocked_rule_buys_alert` (added 2026-08-27, stock-bot analog of the crypto blocked-BUY alert — the recurring "why didn't the bot buy X" question, previously answerable only from console `print()` output): end-of-cycle **digest** (universe is ~40 symbols, not 2) — one `notifier.ops_alert` listing every symbol whose rule BUY a gate held this cycle (MACRO/EARNINGS_BLACKOUT, REGIME_SKIP, VIX_CRISIS, MAX_EXPOSURE/MAX_POSITIONS, CORRELATION, SIZE_SKIP), edge-triggered on the `{symbol: gate}` mapping so a stable "market NEUTRAL, 6 want in" is one message; re-alerts when a symbol is added or its gate changes, distinct all-clear message when the set empties, silent when nothing's blocked and nothing changed; source-inspection guard that `run()` collects `_blocked_rule_buys` at the gate sites (inside an `if _rule_buy:` guard) and calls the evaluator at cycle end |

Run: `python -m pytest --tb=short -q` — must show **709 passed**. (This line had drifted to a
stale "543 passed" — corrected 2026-08-23 to match the manifest total above, which was
already at 605 before this session's +2. Not investigated why the two numbers had diverged.
2026-08-24: 629→639→647 for the CapitalPool per-symbol-cap tests plus the config-layer
coverage gap found right after — caught by a "what's missing" self-audit, not by re-running
the suite in the session that made the change. 647→658 same day, for rescreen.py's new USD
leg + _alert() bugfix. 658→666 2026-08-25, for `_update_ai_health()` — see "AI provider
health monitoring" below. Same day, later: SOL/CAD's promotion added `MAX_SLOT_CASH_CAD_SOL`
to real `.env`, which broke `test_slot_caps_by_base_ignores_unrelated_keys`'s assumption that
no per-symbol override exists in the environment — test-isolation gap, not a code bug; fixed
by explicitly `delenv`-ing that key in the test. Count unchanged, still 666.
2026-08-26: 666→674, +8 for the crypto dashboard multi-symbol combine — new file
`tests/crypto/test_dashboard_renderer.py`. See "Crypto dashboard — multi-symbol combine"
below. Then 674→676, +2 for the stock bot RULES-decision log-visibility fix — new file
`tests/stock/test_rules_log_visibility.py`. 2026-08-27: 676→678→680, +2 for the maker→taker
silent-fallback alert (`tests/crypto/test_live_executor.py` 63→65) and +2 for the MTF-gate
fail-open alert (new file `tests/crypto/test_mtf_gate_alert.py`) — see the addendum to
the "Post-only param bug" section below.)

---

## Current Live Configuration

### Active .env settings — backtest/validation (do not change without re-running validation)
```
ADX_THRESHOLD=18
RSI_FILTER_ENABLED=true
MIN_EMA_SPREAD_PCT=0.004
VOLUME_K=0
STOP_LOSS_PCT=0.015          # fallback only — ATR_SL_MULT takes priority when set
TAKE_PROFIT_PCT=0.10
ATR_SL_MULT=2.0              # adopted live 2026-07-17, walk-forward validated
ATR_SIZING_ENABLED=true      # adopted live 2026-07-17, caps qty at fixed-SL-baseline dollar risk
BACKTEST_LIMIT=5000
BACKTEST_TIMEFRAME=4h
EXCHANGE=binance
SYMBOL=BTC/USDT
```

### Live trading settings (Kraken — separate from backtest)
```
EXCHANGE=kraken
SYMBOL=BTC/CAD
CANDLE_MINUTES=240            # 4h — the only validated live timeframe (1h FAILED walk-forward, see history)
RISK_PER_TRADE_PCT=0.10       # capital-allocation dial, NOT % risked — combined with SL% the real
                              # dollar risk per trade is ~0.15% of cash (see history 2026-07-20 audit)
STOP_LOSS_PCT=0.015           # fallback only — ATR_SL_MULT=2.0 takes priority whenever ATR is
                              # available at entry (bot/main.py:1855-1870)
TAKE_PROFIT_PCT=0.10
ORDER_TYPE=limit / LIMIT_ORDER_ENABLED=true   # BUY entries limit-chase for maker rate;
                              # ALL SL/TP exits forced to market via urgent=True
```
**Post-only param bug, live 2026-06-22 → found and fixed 2026-08-26.** The limit-chase
BUY path (`_place_limit_order()`, `bot/execution/live_executor.py`) had been sending
`{"timeInForce": "PO"}` to request a post-only limit order — ccxt's Kraken adapter passes
`timeInForce` through nearly verbatim into Kraken's own `timeinforce` field, which only
accepts `GTC`/`IOC`/`GTD`; `"PO"` isn't one, so Kraken rejected every single attempt with
`EGeneral:Invalid arguments:timeinforce`, silently falling back to a plain market order
every time — invisible for over two months because it degrades gracefully (no crash, no
alert). Found via SOL/CAD's first live fill (2026-08-26, BUY 0.080808 @ $134.02) showing
exactly this fallback in the log. **Practical effect: every BUY entry (and every non-urgent
strategy-driven SELL) since 2026-06-22 paid market/taker fees (~0.80%) instead of the
intended maker fees (~0.25–0.40%)** — the "limit-chase for maker rate" line above was never
actually true in practice, though the code's *intent* and the rest of the retry/reprice
logic around it were sound. Fixed: `{"postOnly": True}` (ccxt's actual unified param,
translates to Kraken's `oflags=post`) — verified two ways before landing: (1) local, no
real-order-placement proof via `verify_kraken_postonly_param.py` (`ccxt.kraken.
order_request()`, pure request-dict builder, only network call is the public
`load_markets()`) showing the buggy call produces both the invalid `timeinforce='PO'` *and*
a correctly-derived `oflags='post'` — ccxt's own unified layer partially recognizes `"PO"`
as post-only shorthand, which is *why* the bug was subtle rather than obviously broken —
while the fixed call produces a clean request with `oflags='post'` and no `timeinforce`
field at all; (2) a real authenticated `validate=true` round-trip against Kraken's live
AddOrder endpoint (`verify_kraken_postonly_live_validate.py`, same zero-execution technique
already used for the trailing-stop feature) — PASS, `id: None` (nothing executed), Kraken's
own engine echoed back a well-formed `'buy 2.65609 SOLCAD @ limit 138.74'` with no error.
A sibling, already-correct code path (the non-chase "simple" limit-order path, used when
`LIMIT_ORDER_ENABLED=false` but `ORDER_TYPE=limit`) already used `{"postOnly": True}`
correctly — the bug was confined to the limit-chase path alone. Tests: `tests/crypto/
test_live_executor.py` — 1 test (`test_limit_order_fills_on_first_attempt`) had been
asserting the buggy value and was silently locking in the bug; corrected to assert
`{"postOnly": True}`. Suite unaffected in count (666), one assertion fixed. No
`bot/strategy/*` touched — no walk-forward needed, this is execution-layer only.

**Monitoring addendum, 2026-08-27 — maker→taker silent-fallback alert.** A follow-up
silent-degradation bug sweep (the post-only bug hid for 2 months *because* the maker→taker
fallback was `logger.warning`-only, invisible on the dashboard and Telegram). `_place_limit_order()`
in `bot/execution/live_executor.py` has 4 paths that fall back from a post-only limit to a
plain market order — orderbook-fetch failure, spread-too-tight-for-post-only, exchange
rejection of the limit, and limit-chase timeout after all retries. Each now sets a
`self._maker_fallback_reason` string (cleared per `execute()` call and at the top of
`_place_limit_order()` so a stale flag from a qty=0-guard early-return can't misfire);
`execute()` reads it after the fill resolves and, if set, fires a `self._alerter.error()`
**MAKER FALLBACK** Telegram alert naming the reason. Post-fill only — the trade already
happened, this can't block anything, same shape as the slippage guard. BUYs are ~weeks apart
so alert volume is trivial and every one is worth a human glance ("did I just pay 2x fees,
and why"). Tests: `tests/crypto/test_live_executor.py`, +2 (alert fires on market fallback,
no alert on a clean limit fill). Suite 676→678. No `bot/strategy/*` touched.

**Sweep also checked and cleared:** `_sync_cash`/`_sync_position` → `starting_cash` fallback
already alerts (fixed 2026-07-28); crypto AI is `AI_ENABLED=false` (not in play).

**Second sweep fix, 2026-08-27 — MTF gate fail-open alert.** `bot/main.py`'s MTF (1D
BEARISH) veto silently *failed open* — allowed the BUY — when the daily-candle fetch failed
**and** no cached closes existed (`logger.warning` only). Fail-open on missing data is the
deliberate design for every gate here, and this needs a rare triple-coincidence to fire (a
BUY signal, a fetch failure at that instant, and no daily closes cached yet this process),
but a bypassed risk gate on a live-money BUY shouldn't be invisible. The `except` handler now
fires an `alerter.error()` **MTF GATE BYPASSED** alert in exactly the no-cache branch — the
cached-closes path (gate still runs, just on slightly older daily data) does not alert.
Tests: new file `tests/crypto/test_mtf_gate_alert.py`, 2 source-inspection guards (same idiom
as the VIX/macro/correlation/auth-health guards — `run()` needs a full live stack to
exercise behaviorally). Suite 678→680. No `bot/strategy/*` touched.

### Blocked-BUY Telegram alert (crypto — added 2026-08-27, Track 4 observability)
Closes the 2026-08-18 incident's real gap: the bot sat flat through a $90k→$108k BTC rally
while a genuine BUY signal fired and was *correctly* vetoed by the MTF daily-trend gate — and
the only way to find that out afterward was reading `logs/live_signals.csv`. That incident's
fix added logging + the `blocked_gate` CSV column but nothing *pushed* the information.
`bot.main._evaluate_blocked_buy_alert(ss, sym, raw_signal_was_buy, block_gate, alerter)`
(extracted helper, same pattern as `_update_auth_health`/`_evaluate_drift`) now fires a
**"BUY signal blocked [sym]"** `alerter.error()` when the raw strategy signal is BUY but a
gate holds it. **Edge-triggered on (symbol, gate)** — one alert per fresh block, no re-alert
while the same gate keeps blocking (e.g. SOL/CAD firing BUY every 4h while already holding is
one message, not six a day), re-alerts if the blocking gate *changes*, and
`ss['last_buy_block_alert']` clears the moment the raw signal stops being BUY or the BUY
clears (so the next fresh block re-alerts). Strategy-internal HOLDs (RSI/ADX/MACD/trend not
aligned) never reach it — `raw_signal` is only BUY once the strategy itself wants in; this
fires only for the *external* gates (state_machine, capital_pool, risk_manager, correlation,
candle_watchdog, mtf_trend, external_signal, regime), each with a plain-language reason from
`_BUY_BLOCK_REASONS`. Called once per candle close from `run()`'s section-7b block, right
after the `live_signals.csv` write. Not persisted — resets on restart like the other
per-process `ss` flags. Tests: `tests/crypto/test_blocked_buy_alert.py`, 7 cases. Suite
680→687. No `bot/strategy/*` touched — alerting/ops-layer only.

### Blocked rule-BUY digest (stock bot — added 2026-08-27, observability)
Stock-bot analog of the crypto blocked-BUY alert above, from extending the same review to
the stock bot. Same driving question — *"why didn't the bot buy X"* — which the 2026-08-26
RULES-log-visibility fix only half-answered (it made the rule *signal* visible in the log,
but a signal that fires and then gets held by a gate was still `print()`-only). The stock
universe is ~40 symbols vs the crypto bot's 2, so this is an **end-of-cycle digest**, not a
per-(symbol,gate) alert: `_blocked_rule_buys` ({symbol: gate_label}) is collected at the 8
BUY-gate sites in `run()`'s scan loop (each addition is a single `if _rule_buy:` line —
MACRO_BLACKOUT, EARNINGS_BLACKOUT, REGIME_SKIP, VIX_CRISIS, MAX_EXPOSURE, MAX_POSITIONS,
CORRELATION, SIZE_SKIP), and `_evaluate_blocked_rule_buys_alert(current, state, notifier)`
fires **one** `notifier.ops_alert` at cycle end listing them — edge-triggered on the whole
`{symbol: gate}` mapping (`state['seen']`, kept across cycles), so a stable "market NEUTRAL,
6 symbols want in" is one message, not one every 120s loop. Re-alerts when a symbol is added
or its gate changes; a distinct "no longer blocked" message when the set empties; silent
when nothing's blocked and nothing changed. Also covers the SPY-regime-fetch-failure case
(regime → `UNKNOWN` → REGIME_SKIP for every rule BUY → the digest names it). Tests:
`tests/stock/test_blocked_rule_buys_alert.py`, 7 cases. Suite 687→694. No `bot/strategy/*`
or `stock_bot/strategy/*` touched — alerting/ops-layer only.

### IBKR executor TWS-query resilience (stock bot — added 2026-08-27, readiness hardening)
From a "keep the stock bot ready for live" pass. `IBKRExecutor._account_value()` and
`positions_snapshot()` (both query TWS via `_call(..., timeout=10)`) used to return a
**fabricated `0.0` / `{}`** on any transient failure (timeout, brief disconnect), `logger.warning`
only. Consequences once IBKR is live: `cash == 0.0` → `est_cost > self.cash` → **every BUY
rejected** as "insufficient cash"; `positions_snapshot() == {}` → the bot thinks it holds
**nothing** → the SL/TP watcher (`_check_open_positions_sl_tp`) goes **blind to a real
position whose stop just triggered**, and the drawdown/daily-loss breakers compute against a
wrong equity. This is the same class as the crypto `_sync_cash`/`_sync_position` gap fixed
2026-07-28. Fix: both methods now cache the last-good result (`_acct_values_cache` /
`_positions_cache`, with `*_cache_valid` flags) and serve it on failure — strictly safer,
since a stale cash/position figure only ever produces a broker-side reject, which alerts.
`_note_sync(ok)` flips a public `executor.sync_healthy` flag on the failure/recovery edge
(`logger.error` on trip, `logger.info` on recovery); `stock_bot/main.py`'s scan loop polls it
once per cycle and fires an edge-triggered `notifier.ops_alert` ("IBKR data sync failing" /
"…recovered"). The only case that still returns `0.0`/`{}` is a failure on the very first
call before any cache exists (startup — already covered by the connection guards).

**Also, same pass — `ibkr_trades.csv` write resilience.** `_record_trade()`'s CSV append was
`logger.warning`-and-continue on `OSError` — a real filled trade would be **missing from the
frozen 9-column CSV** the LiveTradingGate / ConfidenceBandTracker / accuracy pipeline read
exactly, so the readiness gate would under-count. Now `_write_trade_row()` buffers a failed
row in `self._unwritten_csv_rows` and retries (flushing the backlog first) on the next fill;
`executor.csv_write_healthy` is False while the buffer is non-empty, edge-alerted from
`stock_bot/main.py` the same way as `sync_healthy`. `logger.error` (not warning) names the
buffered-row count. The in-memory `_trade_log` list already held the trade within the process;
this makes it survive to disk. **The order-timeout path was checked and left as-is** — a
timeout raises `RuntimeError` → caller returns a rejected `Order` → `main.py` fires
`ops_alert("Order rejected")`, and the cancel-race grace window (RY incidents 2026-07-31 /
08-19) already records a fill that beats the cancel. **`_log_settlement_csv()` left as
warning-only** — it's the tax-record file, not the gate schema, and its docstring already
documents it as best-effort.

Tests: `tests/stock/test_ibkr_executor.py`, +4 total (56→60). Suite 694→698. No strategy
files touched.

### Risk-gate config (live — RiskManager, `bot/risk/risk_manager.py`)
```
RISK_MAX_POSITION_PCT=0.20    # BUY blocked if it would push position above 20% of slot value
                              # (module default is 5% — .env overrides it 4x looser; slot is
                              # capped at $77 by MAX_SLOT_CASH_CAD so absolute exposure stays small)
RISK_DAILY_LOSS_LIMIT=0.01    # halt new BUYs if portfolio down >1% from today's UTC-midnight open
                              # (SELL always allowed — breaker never blocks exits)
RISK_MAX_DRAWDOWN=0.05        # DRAWDOWN-HALT tier — halt new BUYs if portfolio down >5% from
                              # all-time peak. Not sticky — auto-lifts the moment equity
                              # recovers. (SELL always allowed — breaker never blocks exits)
RISK_MAX_TRADES_PER_DAY=5     # hard cap on BUY fills per calendar day (per-symbol when
                              # multi-symbol; SELL fills are not capped)
COOLDOWN_TICKS=6              # state-machine cooldown between a fill and the next signal
                              # evaluation (bot/strategy state machine, not RiskManager itself)
RISK_HALT_BLOCKS_STOPS=false  # NOT set in .env — using the config.py default (false).
                              # Toggles whether the manual HALT flag (logs/HALT) also blocks
                              # SL/TP exits. Default false = SL/TP exits still fire during a
                              # manual halt; only BUYs and strategy-signal SELLs are blocked
                              # (matches the _check_halt_flag() alert text: "BUY and strategy
                              # SELL blocked; SL/TP exits still fire"). Set true only if you
                              # want a manual halt to freeze exits too — not recommended, since
                              # it would leave open positions exposed with no stop.
RISK_WEEKLY_LOSS_LIMIT=0.05   # NOT set in .env — using the config.py default (5%). Added
                              # 2026-08-07. Halt new BUYs if portfolio down >5% from this
                              # ISO-week's UTC-Monday-open value. Not sticky — resets fresh
                              # every week regardless of a prior trip. SELL always allowed.
RISK_DRAWDOWN_WARNING=0.03    # NOT set in .env — using the config.py default (3%). Added
                              # 2026-08-07. Non-blocking — Telegram alert only (fired once
                              # per episode from bot/main.py's tick loop), trading continues.
                              # Must be < RISK_MAX_DRAWDOWN.
RISK_KILL_SWITCH=0.15         # NOT set in .env — using the config.py default (15%). Added
                              # 2026-08-07. Halt new BUYs if portfolio down >15% from
                              # all-time peak. STICKY — persisted to logs/risk_state.json,
                              # survives restart, does NOT auto-clear on recovery. Requires
                              # manually editing kill_switch_tripped to false in that file to
                              # resume BUYs. Must be > RISK_MAX_DRAWDOWN. Config validation
                              # enforces RISK_DRAWDOWN_WARNING < RISK_MAX_DRAWDOWN <
                              # RISK_KILL_SWITCH strictly increasing.
```
Four-tier breaker upgrade added 2026-08-07, mirroring the stock bot's identical upgrade from
2026-08-05 — crypto had fallen behind with only a single non-sticky drawdown check even
though (unlike the stock bot, still paper-only) it trades real money. Crypto's tier
thresholds are intentionally much tighter than the stock bot's (3%/5%/15% vs the stock bot's
10%/15%/20%) since the existing `RISK_MAX_DRAWDOWN=0.05` halt tier was already tighter —
scaled the new warning/kill-switch tiers to match rather than reusing the stock bot's
numbers verbatim. Check order inside `RiskManager.evaluate()` is now (most severe first):
HALT → KILL_SWITCH → MAX_DRAWDOWN (halt) → WEEKLY_LOSS → DAILY_TRADE_CAP → DAILY_LOSS →
POSITION_SIZE — when multiple tiers trip simultaneously the most severe `block_reason` is
reported, not whichever happened to be checked first historically. `peak_value`,
`week_open_value`, and `kill_switch_tripped` all persist in `logs/risk_state.json` (existing
file — old on-disk copies load cleanly, new fields default fresh on first `evaluate()` after
upgrading). Tests: `test_risk_manager.py`, 12 new cases (kill-switch trip/sticky/persist/
priority-over-halt/never-blocks-SELL, drawdown-halt now has dedicated tests for the first
time, weekly-loss trip/allow/week-rollover, warning-tier status + non-blocking confirmation).

### Native exchange-side stop-loss (crypto — ON since 2026-08-15)
```
NATIVE_STOP_LOSS_ENABLED=true    # Set in .env 2026-08-15 (was false/unset — config.py
                                  # default is still false). Added 2026-08-07 from a gap
                                  # review against external crypto-bot best-practice
                                  # research: the software SL/TP path only works while the
                                  # bot process is alive and polling — a crash loop, VPS
                                  # outage, or extended network partition left an open
                                  # position with zero protection until the bot came back.
                                  # Flipped on 2026-08-15 while the user was traveling with
                                  # reduced ability to babysit the bot, without the planned
                                  # prior live-validation window — position was flat (0 BTC)
                                  # at flip time so there was nothing to retroactively
                                  # protect; takes effect on the next BUY fill. Re-confirm
                                  # behavior on that first live fill rather than assuming
                                  # the deferred validation happened.
```
When enabled, `bot/execution/live_executor.py`'s `sync_protective_stop()` rests a real
Kraken stop order (`create_order(..., params={"stopLossPrice": X})`, executes as market on
trigger — same "never sit in a limit book during a stop" reasoning as `urgent=True`
elsewhere in this file) after every BUY fill, at whatever SL price `bot/main.py` already
computed (ATR if available, else flat `STOP_LOSS_PCT`). **Usually static** — it does NOT
reprice mid-trade by default; it's pure insurance for "the bot itself is unreachable," not a
second copy of the live SL/TP logic. Cancelled the moment the bot closes the position itself
(strategy SELL, software SL/TP, partial TP), so under normal operation it never fires —
Kraken's own trigger only matters when the bot can't get there first. Order id/price persist
in `logs/live_state_BTC_CAD.json` and are reconciled on every restart: a still-open saved
order is kept as-is (level never touches down); a saved-but-now-gone order (filled while the
bot was down — this feature working exactly as intended) is cleared; a held position with no
resting stop at all (feature just enabled, or the bot crashed before it could place one) gets
a same-startup fallback at flat `STOP_LOSS_PCT` off cost_basis, replaced with the more precise
ATR-based level on the next real BUY for that symbol. Placement/cancel failures alert to
Telegram but never raise, so a Kraken-side rejection degrades to "no backstop, software SL/TP
still fully works while the bot is up," never a crashed trading loop — including the current
Kraken auth outage (see "Known live incident" below): if that's still unresolved when the
next BUY fires, the stop placement will fail the same way and alert, not crash. `PARTIAL_TP_PCT`
is unset → 0 = disabled today, so the quantity-tracking (resize-on-partial-fill) half of
`sync_protective_stop` is defensive/future-proofing rather than exercised live currently.

**Native trailing-stop, added 2026-08-19 (protection-gap fix):** a code + Kraken API research
review found the static backstop never followed the software trailing stop as it rose —
`ss['native_stop_price']` was set once at BUY-fill and never updated as `ss['trail_peak']`
climbed, so a bot outage mid-trade after a favorable move would only be protected at the
*original* entry-relative level, not the trailed-up one. Fix, scoped narrowly: this only
matters when `ss['atr_sl'] == 0` — with ATR SL available (as it always is in live config,
`ATR_SL_MULT=2.0`), `_trail_sl_level` in `bot/main.py` uses the fixed ATR level regardless of
`TRAILING_STOP_PCT`, so the software trailing logic itself is dormant and a flat native stop
already mirrors it with no gap. When trailing IS the active software level and
`NATIVE_STOP_LOSS_ENABLED=true`, `sync_protective_stop()` now accepts an optional
`trailing_pct` and places a genuine Kraken `trailing-stop` order
(`params={"trailingPercent": "X.XXXX"}`) instead — Kraken's own matching engine tracks the
peak server-side from placement, so unlike the static path this needs no repeated repricing
calls, only a one-shot swap the instant `trail_peak` first arms (`bot/main.py`, guarded by a
new `ss['native_stop_is_trailing']` flag). Quantity-changing events (partial TP, a partial
fill on an urgent SL/TP exit) still cancel + re-place — ccxt/Kraken has no in-place volume
amend — via a new `_resync_native_stop()` helper; a trailing re-place restarts the
exchange-tracked peak from the price at re-placement time (accepted precision loss, same
shape as the static order's own per-resize snapshot). `TRAILING_STOP_PCT=0` in `.env` today,
so this path is currently dormant live — pre-emptive, not incident response. Tests:
`test_live_executor.py`, static-path 11 cases (placement, cancel, resync-on-quantity-change,
failure alerting, dry-run/flag-off no-ops, restart reconciliation for all three states above)
+ 7 new trailing-path cases (placement param, priority-over-static, dry-run no-op, cancel,
resync-on-quantity-change, failure-alert, state persist/restore across restart).

**Restart-seeding gap CLOSED, 2026-08-20** (was flagged, deliberately left unfixed, in the
2026-08-19 session above). The original note here said "a restart always re-arms static
regardless of what kind was resting before" — that claim is now **false** for the native-stop
side specifically (still true for the unrelated software `trail_peak`/`atr_sl` side, which
this fix does not touch — those still reset to 0 in-memory every restart, so the *software*
trailing SL level stays dormant until `trail_peak` re-arms from live ticks post-restart; the
*native* backstop is unaffected by that since Kraken tracks its own peak server-side,
independent of the bot process). Root cause: `LiveExecutor`'s own native-stop bookkeeping
(`native_stop_order_id`/`native_stop_price`/`native_stop_is_trailing`) was already correctly
reconciled against Kraken's real open orders on every restart (`_verify_resting_stop_on_startup`,
built into the original 2026-08-07 feature) — but `bot/main.py`'s *separate* `symbol_state`
copy of the same two fields (`ss['native_stop_price']`/`ss['native_stop_is_trailing']`) was
never re-seeded from it, always defaulting to `None`/`False` after a restart. Concrete risk:
`_resync_native_stop(ss)` (fired by a partial TP or a partial fill on an urgent SL/TP exit)
trusts `ss`'s stale copy, not the executor's real one — if such an event fired after a restart
but before the next BUY fill (the only other place these `ss` fields get set), it would call
`sync_protective_stop(None)` with no `trailing_pct` either, which **unconditionally cancels
whatever's actually resting on Kraken and then places nothing** — a real position, previously
protected, left naked. Fixed: a new `_seed_native_stop_state(executor)` helper
(`bot/main.py`, same "extract for testability" pattern as `_evaluate_drift`/
`_update_auth_health`) purely mirrors the executor's already-reconciled state into `ss` inside
the existing "Restart recovery" block, right where `pm`/`sm` already get seeded — no new
network calls, no recomputation, no second source of truth. Deliberately trusts whatever
price is actually resting verbatim rather than recomputing a fresh one from `avg_entry`/ATR —
matches the pre-existing documented decision a few paragraphs up ("a still-open saved order
is kept as-is — level never touches down"), now correctly propagated into `ss` too.

**Also found and fixed in the same pass:** `_verify_resting_stop_on_startup()` only ever
checked whether its own single tracked order id was still open — it never scanned for *extra*
stop-type orders that shouldn't exist (this bot's own logic only ever cancels-then-places, so
more than one resting stop-type order at a time should be structurally impossible via normal
operation). Now also counts stop-type orders (`descr.ordertype` in `{stop-loss,
trailing-stop}`, read from the same already-fetched `fetch_open_orders()` call — no extra API
cost) among everything open on the symbol; if more than one is found, alerts loudly via
Telegram and leaves the existing single-id confirm/clear logic to run unchanged underneath —
deliberately does **not** try to auto-resolve the ambiguity by picking one, per the explicit
"fail loud, don't silently guess" decision for this case.

Two items were deliberately left unfixed in this pass, flagged for later — **both since
CLOSED, 2026-08-20 same-day follow-up** (see below and `.memory/execution_layer.md` for the
full writeup): whether a still-resting order's *quantity* matches the position's actual size
post-restart, and adopting an untracked-but-real resting stop order when the state file's own
tracked id is missing/lost. Tests for the original pass:
`tests/crypto/test_live_executor.py` (+3: multiple-stop-orders alert, unrelated-open-order
no-false-positive, `native_stop_price` property) and `tests/crypto/test_drift_escalation.py`
(+4: `_seed_native_stop_state()` — static resting, trailing resting, naked/nothing resting,
mismatched-price-trusted-verbatim). Suite 536→543. No `bot/strategy/*` touched — no walk-forward
revalidation needed, confirmed via unchanged `bot/strategy/fingerprint.compute_strategy_hash()`.

**Both remaining gaps CLOSED, 2026-08-20 (same-day follow-up pass).** Re-traced both against
the actual code before changing anything — confirmed real, not assumed.

*Quantity mismatch:* `_verify_resting_stop_on_startup()` only ever checked order-id membership,
never volume, even though `_sync_position()` (called just before it) may have already
reconciled the position to a different size than what the resting order was placed for.
ccxt's installed `kraken.py` confirms the unified `order['amount']`/`order['remaining']`
fields are populated straight from Kraken's raw `vol`/`vol_exec` — used directly, no `info`
parsing needed for this half. Fixed with a hybrid policy (explicit choice presented and made,
not decided unilaterally): always alert on any mismatch beyond a small tolerance; auto
cancel+replace (at the *same* price/trailing-pct, sized up to the real position) only when the
resting order is **under-sized** — the genuinely unprotected direction. An over-sized resting
order is left alone: benign (an exchange can't oversell a position that isn't there) and, for
a trailing stop, replacing it would forfeit Kraken's server-tracked trail-peak progress for no
protective benefit. The replacement level is read back from the resting order's own raw fields
(`info['stopprice']` for static, `info['descr']['price']`'s `"+X.XXXX%"` string for trailing —
ccxt nulls its own unified `price` for a trailing order) — never recomputed from `avg_entry`/
ATR, consistent with the existing "a resting order's level never touches down" rule elsewhere
in this feature.

*Untracked-but-real order:* confirmed the multi-order ambiguity alert above does **not** cover
this case — `_verify_resting_stop_on_startup()`'s no-tracked-id branch returned before ever
calling `fetch_open_orders()`, so the ambiguity scan was structurally unreachable whenever
`native_stop_order_id` was `None`. Traced the concrete consequence: `bot/main.py`'s startup
reconciliation would call `sync_protective_stop(fallback_sl)`, whose first step
(`_cancel_native_stop()`) is a no-op with no tracked id — so a real untracked order was never
touched, and a second stop got placed alongside it. Fixed: the method now always fetches open
orders and scans for stop-type orders even with no tracked id — exactly one found is adopted
verbatim (id/price/trailing-flag trusted from the exchange, same philosophy as
`_sync_position`/`_sync_cash`); two or more reuses the same ambiguity alert, adopting nothing;
zero found keeps the pre-existing behavior. Tests: `tests/crypto/test_live_executor.py`, +9
(5 for the quantity fix, 4 for the adoption fix). Suite 543→552. No `bot/strategy/*` touched,
no walk-forward needed.

**`trailingPercent` param verified against real ccxt source, 2026-08-19 — re-run to reproduce,
don't just trust this paragraph:** `.venv/bin/python verify_kraken_trailing_stop_param.py`
(repo root) imports the ACTUAL installed ccxt (asserts version `4.5.56`, no network calls —
`order_request()` is a pure request-dict builder) and asserts it turns
`params={"trailingPercent": "2.0000"}` into
`{'ordertype': 'trailing-stop', 'price': '+2.0000%', 'trigger': 'last', ...}` — Kraken's
native trailing-stop shape, not a made-up param ccxt silently drops. Exits non-zero with a
clear message if a future ccxt upgrade ever changes this. This replaced an earlier
"verification" pass that only restated the claim in prose with citations, no literal source
or runtime evidence actually sitting in the repo — the script + the literal source excerpt
and runtime output now embedded in `_place_native_trailing_stop()`'s docstring
(`bot/execution/live_executor.py`) are the actual proof. Cross-checked the same day against
Kraken's own AddOrder REST docs (docs.kraken.com/api/docs/rest-api/add-order/): `ordertype`
enum includes `trailing-stop` with no spot/margin distinction; `price` documented as this
same relative `+X%` format. ccxt's docstring labels the param "*margin only*" (kraken.py:1637)
— confirmed not code-enforced anywhere in `create_order()`/`order_request()`, same as the
sibling `stopLossPrice` param already running live on spot BTC/CAD under the identical
annotation. No fix needed. Kraken spot has no open public sandbox (only Kraken Futures does),
but `AddOrder` supports a `validate=true` param for a zero-risk request-shape check against
the real endpoint — no execution, per Kraken's own docs: *"If set to `true` the order will be
validated only, it will not trade in the matching engine."*

**Final step done, 2026-08-19 — real authenticated Kraken server round-trip, PASS.** Before
running: found that `validate=True` (Python bool) is unsafe — ccxt's `urlencode_nested()`
(used for this private POST body) has no bool→string normalization, so a Python `True`
serializes as the literal `validate=True` (capital T) on the wire, not Kraken's documented
`true`. Verified directly: `ccxt.Exchange.urlencode_nested({'validate': True})` →
`'validate=True'`, vs. `{'validate': 'true'}` → `'validate=true'`. ccxt's own `kraken.py` hits
this exact pitfall for `reduce_only`/`post_only` and hardcodes the lowercase string for the
same reason (`kraken.py:2122`,`:2187`). Script always passes the **string** `'true'`, never
the bool, for this reason.

Ran `verify_kraken_trailing_stop_live_validate.py --i-understand-this-makes-a-real-kraken-api-call`
once, sizing at the real `MAX_SLOT_CASH_CAD=$77` cap against the live BTC/CAD price
($95,568.70 → 0.000806 BTC). **Kraken's literal raw response:**
```
{'id': None, 'clientOrderId': None,
 'info': {'descr': {'order': 'sell 0.00080 XBTCAD @ trailing stop -2.0000%'}},
 'symbol': 'BTC/CAD', 'type': 'trailing stop', 'side': 'sell', 'amount': 0.0008, ...}
```
`id: None` — no order id returned, confirming nothing executed (matches Kraken's documented
validate-only behavior). Kraken's own engine parsed and echoed the request back as
`'sell 0.00080 XBTCAD @ trailing stop -2.0000%'` — a fully well-formed trailing-stop order
(the `+2.0000%` sent was correctly flipped to the sell-side `-2.0000%` in Kraken's own
description, matching their docs: *"direction will be automatic based on if the original
order is a buy or sell"*). No shape/format error of any kind. This is the real server
confirming the exact request `_place_native_trailing_stop()` builds — the local-only
ccxt-source verification (above) plus this real round-trip are now both PASS.
`verify_kraken_trailing_stop_live_validate.py` stays in the repo as the historical record of
this exact run (guarded behind an explicit `--i-understand-...` flag, not in pytest/CI, not
meant for casual re-runs) — this result doesn't need reproducing again absent a ccxt/Kraken
API change.

**LIVE INCIDENT 2026-08-27 — native stop deadlocked every SELL. FIXED.** SOL/CAD's first
position (the one that surfaced the post-only bug) rode up to +10.8% and hit `TAKE_PROFIT_PCT`.
The software TP path fired an urgent market SELL for the full 0.080808 SOL — **and Kraken
rejected every attempt with `EOrder:Insufficient funds`**, retrying every ~32s for 8+ minutes.
Root cause: the resting native stop order (`OXMTMW-33KMI-D5PQ4Y`, `sell 0.08080800 SOLCAD @
stop loss 127.04`) **reserves 100% of the base asset** on Kraken (`fetch_balance`:
`SOL free=0.0 used=0.080808`), so no SELL for the position can execute while it rests — and
`bot/main.py`'s SL/TP block only cancelled the native stop **after** a successful fill
(`if _ic_order.status == FILLED: ... sync_protective_stop(None)`), which could never happen.
A pure deadlock. Same latent bug in the strategy-SELL and partial-TP paths. **First time the
native-stop feature was exercised against a real software TP** — the "re-confirm on the first
live fill" note above was exactly the right instinct. Resolved live by manually cancelling
`OXMTMW-33KMI-D5PQ4Y` via ccxt; the bot's next tick then sold cleanly (SOL/CAD closed
**+$1.27 / +10.9%**, fill #2, first completed round-trip). Fix: `LiveExecutor.execute()` now
cancels any resting native stop **before** placing a SELL (covers all three exit paths at
the executor layer, not scattered call sites) — full close leaves it gone, partial fill has
`_resync_native_stop` re-place it smaller, and a **rejected** SELL triggers
`_rearm_native_stop_after_failed_sell()` which puts the static stop back at its prior level
(a trailing stop can't be recovered → loud "NAKED POSITION" alert instead; trailing is
dormant). `bot/main.py`'s post-fill `sync_protective_stop(None)` is now a belt-and-suspenders
no-op. Tests: `tests/crypto/test_live_executor.py` +3 (65→68: cancel-before-sell ordering,
re-arm-on-rejection, no-cancel-without-a-resting-stop). Suite 706→709. No `bot/strategy/*`
touched. **Requires a crypto-bot restart to take effect** (running process has the buggy code).

### Slippage guard (crypto — post-fill alert, on by default)
```
MAX_SLIPPAGE_PCT=0.01   # NOT set in .env — using the config.py default (1%). Added
                        # 2026-08-07, closing the third finding from the same crypto-bot
                        # research review. 0 = disabled.
```
`LiveExecutor.execute()` compares every live fill's actual price against the price the bot
was evaluating the signal against, direction-aware (only an unfavorable fill counts — paying
less on a BUY or receiving more on a SELL is never flagged). **Post-fill only, never
blocks** — unlike every other risk gate in this codebase, there's nothing to block by the
time slippage is known; the fill has already happened. Every real (non-dry-run) fill logs
its expected-vs-filled delta at INFO (`Fill vs expected [...] slippage=+X.XXX%`) regardless
of size, so the audit trail exists even when nothing crosses the threshold; a Telegram alert
only fires when the unfavorable deviation exceeds `MAX_SLIPPAGE_PCT`. Shipped **on** by
default (unlike native stop-loss) — it's pure observability, can never change trading
behavior or place an order, same reasoning as the stock bot's correlation/macro-blackout
gates shipping active. Threshold picked to sit above ordinary friction (Kraken's own fee is
0.40–0.80%, BTC/CAD spread is typically well under 0.1%) so it only fires on a genuine
anomaly — thin liquidity, a bad fill, or a fast-moving market during a multi-minute
limit-chase — not routine timing drift between signal and fill. Complements, not replaces,
`shadow_signal.py`'s existing retrospective fidelity check (part of the 15-fill capital-gate
criteria: "fill prices within 0.5% of signal-candle close") — that one runs once a day
against candle closes; this one fires immediately against the live decision price, closing
the gap between a bad fill happening and the next scheduled audit catching it. Tests:
`test_live_executor.py`, 6 new cases (BUY/SELL unfavorable-direction trip, within-threshold
no-alert, favorable-direction never alerts regardless of size, disabled-via-zero no-op,
dry-run no-op).

### Candle watchdog — now a real circuit breaker (crypto — always on)
Upgraded 2026-08-07, the fourth and last finding from the same crypto-bot research review.
Previously alert-only: `_check_candle_watchdog()` fired a Telegram notice when no new candle
had arrived for 2× `CANDLE_MINUTES` (8h at the live 4h setting), then reset its own timer to
avoid spamming — but a stale feed and a healthy one were otherwise treated identically by the
strategy. Now it's a real breaker: while the feed is stale, new BUYs are blocked (gate 2g,
same "BUY-only, SELL always allowed" shape as every other breaker here) — SELL/exits are
untouched because they read the independent live-tick price feed, not the candle feed, and
must always be able to close a position. State (`ss['candle_feed_stale']`) persists across
ticks in-memory (not written to a state file — resets on restart same as `trail_peak`/
`atr_sl`, appropriate for a per-process feed-health flag); alerts fire once on each
stale→fresh transition (both directions — a "feed recovered, BUYs re-enabled" notice is new),
not every tick. No config flag — always on, same reasoning as the correlation/macro gates:
it only ever makes a BUY more conservative, never loosens anything. Tests:
`test_candle_watchdog.py`, rewritten for the new `(ss, ...) -> bool` signature — 5 → 7 cases
(silent/blocked/re-alert-suppressed on the stale side, unchanged; two new: alerts + unblocks
on recovery, and a recovered feed doesn't re-alert on subsequent fresh ticks).

### Two-way Telegram control (crypto — built 2026-08-20, ENABLED live 2026-08-20)
```
TELEGRAM_CONTROL_ENABLED=true    # Set in .env 2026-08-20 (was false/unset — config.py
                                  # default is still false). Separate from TELEGRAM_ENABLED
                                  # (outbound alerts only): this one starts an INBOUND
                                  # getUpdates poller — a real control surface, not just
                                  # notifications, so it shipped opt-in rather than silently
                                  # active on upgrade, same reasoning as
                                  # NATIVE_STOP_LOSS_ENABLED. Turned on the same day it was
                                  # built, after the live smoke test below passed clean.
```
**Live smoke test, 2026-08-20 (bot restarted PID 57954, 07:20 local):** startup log confirmed
`bot.alerts.telegram_control INFO Telegram control thread 'telegram-control-crypto' started`
right alongside the heartbeat/dashboard/audit threads, no errors. A bare `/help` (missing the
`_crypto` suffix) was correctly silently ignored (`Telegram control: unrecognized command
ignored: '/help'`, logged server-side, no reply) — live proof the namespacing/ignore path
works, not just the happy path. `/help_crypto` and `/status_crypto` were then sent from the
configured chat and replied to correctly; `/status_crypto`'s reply (position 0.0, cash $77.00,
halt clear, regime VOLATILE) was cross-checked against `logs/live_state_BTC_CAD.json` (cash
77.0, position 0.0) and the absence of `logs/HALT` — matched exactly, confirming the reply
reflects real live state, not stale or fabricated data. `/pause_crypto`/`/resume_crypto` were
deliberately NOT live-tested (would have engaged the real halt mid-session) — covered by the
28 unit tests instead (see below), including the end-to-end proof they drive the same
`_check_halt_flag()` the tick loop already polls.
Closes the gap flagged during the 2026-08-19 Freqtrade comparison: Telegram was alert-only
(`bot/alerts/telegram.py`'s `TelegramAlerter`, outbound `sendMessage` only), no way to query
status or control the bot remotely without SSH. New module `bot/alerts/telegram_control.py`
(`TelegramCommandPoller`) long-polls `getUpdates` — no webhook/public endpoint, fits the
home/VPS-undecided deployment — in its own daemon thread (same pattern as
`start_heartbeat_thread`), started from `bot/main.py` only when the flag above is true.

**Commands:** `/status_crypto` (mode, halt/kill-switch state, then per symbol: position,
cash, total value, realized/unrealized P&L, profit factor from closed-trade history, current
regime), `/pause_crypto`, `/resume_crypto`, `/status_stock` (read-only stock-bot snapshot),
`/help_crypto`.

**Auth:** every inbound message's `chat.id` is compared against the configured
`TELEGRAM_CHAT_ID`. A mismatch is silently ignored — no reply, logged at INFO only —
replying would confirm to a stranger that a live, responsive bot exists at this token. An
authorized chat sending an unrecognized command (typo, or a foreign namespace like a future
`/pause_stock`) gets the identical silent-ignore treatment.

**`/pause_crypto`/`/resume_crypto` reuse `logs/HALT` exactly** — they only `open()`/
`os.remove()` the same flag file `_check_halt_flag()` already polls every tick (`bot/main.py`).
No parallel halt path, no direct `risk.halt()`/`risk.resume()` call from a command handler.
Same latency as a manual SSH `touch logs/HALT`: takes effect on the next tick (≤
`LOOP_INTERVAL`, 30s), not instantly — the reply text says so rather than claiming immediate
effect.

**`/status_crypto` is structurally read-only**, not just by convention: `bot/main.py`'s
command-body functions (`_status_crypto_text`, `_format_symbol_status`, `_status_stock_text`,
`_help_crypto_text`, plus the two flag-file functions above) take plain data and either only
read attributes or only touch the halt flag file — none of them import or reference
`LiveExecutor`, and `telegram_control.py` itself carries zero trading imports at all.
Verified by a source-inspection test, not just a behavioral one (see test list below).

**Shared-token constraint with the stock bot — read before ever adding a second poller.**
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are the SAME credentials the stock bot's outbound-only
`TelegramAlerter` also uses (`stock_bot/alerts/notifier.py`'s `_make_telegram()`, "one
token/chat source for both bots" since 2026-07-17). Telegram's `getUpdates` `offset` is a
**server-side, per-token** acknowledgment — passing `offset=N` permanently discards every
update with `update_id < N` for that token, for *every* caller, not just whoever passed it.
Two independent processes each tracking their own local offset against this same token WILL
corrupt each other: whichever advances its offset further can silently erase updates the
other hasn't processed yet. This is Telegram's documented single-consumer-per-token model,
not an edge case to code around. Consequence: **exactly one process may ever run a
`TelegramCommandPoller` against this token** — today that's the crypto bot only. The stock
bot has no inbound polling at all and, separately, has no `logs/HALT`-equivalent flag file to
pause against yet (`/status_stock` is answered by the crypto poller reading
`stock_bot/paper_state.json`/`ibkr_state.json` directly — the same read-only cross-bot file
read `unified_dashboard.py` already does from one process — not by a second poller). If
stock-bot two-way *control* (not just read-only status) is ever added, it must either route
through this same crypto-owned poller (more handlers here) or use a second, dedicated
Telegram bot token — never a second independent `getUpdates` loop against this token. This
constraint is documented in `bot/alerts/telegram_control.py`'s module docstring too.

Tests: `tests/crypto/test_telegram_control.py`, 28 cases — see Test Suite Manifest above.
Suite 552→580. No `bot/strategy/*` touched, no walk-forward needed — alerting/ops-layer only.

**Hot-loop bug fixed 2026-08-23.** Live logs showed a continuous flood of identical
`Telegram control: getUpdates failed: 502 Server Error: Bad Gateway ...` lines — Telegram's
own API infrastructure returning transient 502s. Root cause was in this bot, not Telegram:
`poll_once()` caught the `getUpdates` exception, logged it, and returned with no pause at
all. The long-poll's own `timeout=25` normally paces the loop, but a *fast-failing* error
(a 502 that comes back immediately, unlike a real long-poll that blocks up to 25s) bypassed
that pacing entirely — the outer `while True` loop in `start_telegram_control_thread()`
retried instantly, as fast as the network round-trip allowed, for as long as the outage
lasted. This hammers Telegram's API during exactly the kind of transient server-side issue
retries should back off from, and floods `logs/trade_bot.log`. Fixed: `poll_once()` now
sleeps `error_backoff_s` (new constructor param, default 5.0s) after a failed `getUpdates`
call before returning; a successful call never sleeps. Tests: `tests/crypto/
test_telegram_control.py`, +2 (failure triggers exactly one `time.sleep(5.0)` call; success
never calls `time.sleep`); the pre-existing failure test was given `error_backoff_s=0` so it
stays instant. Suite 605→607. No `bot/strategy/*` touched.

### Risk-gate config (stock bot — `StockPaperExecutor` / `IBKRExecutor`, both in `stock_bot/execution/`)
Both executors implement the same tiers independently (accepted duplication — same pattern
as the sector-concentration gate). All tiers block new BUYs only; SELL/exits are never
blocked by any breaker (mirrors the crypto RiskManager's hard rule above).
```
PAPER_DAILY_LOSS_PCT=0.03          # (default; not set in stock_bot/.env) halt new BUYs if
                                    # portfolio down >3% from session start. Session-lifetime,
                                    # not calendar-day — resets on process restart, not at UTC
                                    # midnight (a real difference from the crypto RiskManager;
                                    # not yet unified — see roadmap)
PAPER_WEEKLY_LOSS_PCT=0.05         # added 2026-08-05. Halt new BUYs if portfolio down >5%
                                    # from this ISO-week's opening equity. Resets Monday-anchored.
PAPER_DRAWDOWN_WARNING_PCT=0.10    # added 2026-08-05. Non-blocking — ops_alert only (sent from
                                    # stock_bot/main.py's SL/TP watcher loop, not the executor).
                                    # Trading continues.
PAPER_DRAWDOWN_HALT_PCT=0.15       # added 2026-08-05. Halt new BUYs if portfolio down >15% from
                                    # all-time peak equity. NOT sticky — auto-lifts the moment
                                    # equity recovers above the threshold.
PAPER_KILL_SWITCH_PCT=0.20         # added 2026-08-05. Halt new BUYs if portfolio down >20% from
                                    # all-time peak equity. Sticky — persisted to
                                    # paper_state.json/ibkr_state.json, survives restart, does
                                    # NOT auto-clear on recovery. Requires manually editing
                                    # kill_switch_tripped to false in the state file to resume BUYs.
```
peak_equity and week_open_equity are also persisted (unlike the pre-existing daily-loss
baseline) — a crash-restart must not silently reset the all-time peak or re-arm a tripped
kill switch. Config validation enforces `warning < halt < kill_switch` strictly increasing.
Added to close punch-list item #3 (only one breaker tier existed) from the trading-spec
gap review — see `.memory/` for the fuller comparison against that spec.

### ATR-based stop distance + risk-capped sizing (stock bot — opt-in, closes punch-list #2)
```
PAPER_ATR_SIZING_ENABLED=false     # added 2026-08-05. Default OFF — do not enable live
                                    # without a stock_backtest.py walk-forward PASS first.
                                    # Live behavior today (RY, CM) is unaffected: flat
                                    # PAPER_STOP_LOSS_PCT (5%) + flat PAPER_RISK_PCT (20%)
                                    # notional sizing, identical to before this change.
PAPER_ATR_SL_MULT=2.0              # added 2026-08-05. Stop distance = ATR(14) * this mult,
                                    # only used per-position when the flag above is true.
```
When enabled: at BUY time, `StockConfig.calc_shares_atr_risk()` (mirrors the crypto bot's
`calc_trade_qty_atr_risk`) caps share count so a stop at `ATR*mult` away never risks more
dollars than the flat-5%-baseline would; the resulting ATR stop % is stored per-position via
`executor.set_position_stop_pct(symbol, pct)` (persisted in paper_state.json/ibkr_state.json,
cleared on full close, survives a partial close) and the SL/TP watcher
(`_check_open_positions_sl_tp`) reads it back via `get_position_stop_pct()` instead of the
flat baseline — sizing and the actual exit trigger must agree, or the risk cap at entry means
nothing. Positions opened before this feature (or with the flag off) use the flat baseline —
`get_position_stop_pct()` falls back to it when no override is recorded.

### Correlation gate (stock bot — always on, closes punch-list #4)
`stock_bot/risk/correlation.py`, wired into the BUY path in `stock_bot/main.py` (`run()`,
`_check_correlation_gate`). No feature flag — unlike ATR sizing, this only ever makes a BUY
*more* conservative and never changes exit/stop behavior for existing positions, so it
shipped active by default (same reasoning as the sector-concentration gate, item #1). Blocks
a new position when its 30-day daily-return correlation with any already-open position
exceeds `CORRELATION_THRESHOLD=0.70` (reused unchanged from `bot/risk/correlation.py` — same
constant, same Pearson math as the crypto gate). Fail-open on missing data (no candles this
cycle for either symbol → allow the BUY), matching the crypto gate's philosophy. Unlike the
crypto version, makes **zero extra network calls** — reuses candle closes the scan cycle
already fetched via yfinance rather than issuing a fresh fetch per open-position pair, since
stock bot is already yfinance-rate-limit-sensitive (see dashboard log noise re: AC.TO/DLTR).

### Macro economic event blackout (stock bot — always on, closes punch-list #5)
```
MACRO_BLACKOUT_DAYS=1               # added 2026-08-05. Symmetric window (days before AND
                                     # after) around an event date. 0 or negative disables
                                     # the feature entirely.
MACRO_EVENT_DATES=                  # added 2026-08-05. Empty by default. Comma-separated
                                     # ISO dates for FOMC/CPI/GDP — user-maintained, this
                                     # module does NOT fabricate them (they don't follow a
                                     # clean weekday rule the way jobs reports do). Add real
                                     # ones as published:
                                     #   FOMC: federalreserve.gov/monetarypolicy/fomccalendars.htm
                                     #   CPI:  bls.gov/schedule/news_release/cpi.htm
                                     #   GDP:  bea.gov/news/schedule
                                     # A two-day FOMC meeting needs both days listed.
```
`stock_bot/risk/macro_calendar.py`. Two date sources: (1) `jobs_report_dates()` — the U.S.
Non-Farm Payrolls report is always the first Friday of the month, computed algorithmically
(same style as `_us_holidays()`/`_ca_holidays()` in `stock_bot/main.py` — no external data,
no yearly maintenance), so this half of the gate works out of the box with zero config; (2)
the user-maintained `MACRO_EVENT_DATES` list above for FOMC/CPI/GDP. Market-wide, not
per-symbol — blocks ALL new BUYs (checked once per symbol in the scan loop via
`_is_macro_event_blackout(cfg)`, market-wide in effect since it doesn't depend on which
symbol is being evaluated) before the per-symbol earnings-blackout check. Fail-open on any
error (bad config value, etc.) — same philosophy as earnings blackout. Shipped active by
default since, like the correlation gate, it can only make a BUY more conservative.

### VIX crisis mode (stock bot — always on, closes punch-list #8)
```
VIX_CRISIS_ENABLED=true          # added 2026-08-05. Default ON — only ever blocks BUYs,
                                  # never loosens anything, same reasoning as the correlation
                                  # and macro-blackout gates.
VIX_CRISIS_THRESHOLD=35.0        # added 2026-08-05. CBOE VIX level, matches the spec's
                                  # "Crisis mode: VIX >35" verbatim. 0 or negative disables.
```
`stock_bot/risk/vix_crisis.py` — pure threshold check only (`is_vix_crisis`). VIX data (Yahoo
Finance `^VIX`) is fetched once per scan cycle in `stock_bot/main.py`, same `fetch_with_retry`
pattern as the existing SPY regime-filter fetch, right next to it. Market-wide — reuses the
same `_regime_ok` flag the SPY BULL/BEAR/NEUTRAL filter already uses to gate new BUYs, so
crisis mode and the regime filter share one code path rather than adding a second parallel
gate. Fetch failure fails open (`_vix_now=None` → not crisis). Printed every cycle next to the
regime line (`Regime: BULL 🟢   VIX: 18.4 🟢`) for visibility, same as regime already was. This
implements "disable aggressive trading" literally as a full BUY block, not partial size
reduction — consistent with how every other market-wide gate in this codebase (regime filter,
macro blackout) is a binary block rather than a sizing dial, and is the stricter reading of
the spec's own language.

### Stock bot `regime()` live-gating + offline-audit note (2026-08-20)
The SPY BULL/BEAR/NEUTRAL regime line referenced above (`_regime_ok`, shared with VIX crisis
mode) is computed by `regime()` in `stock_bot/indicators/indicators.py` —
`stock_bot/main.py:1038`, called live every scan cycle on freshly-fetched SPY closes. This is
worth naming explicitly because it's easy to assume that module is purely an offline-backtest
artifact (it also has its own standalone backtest tool, `stock_bot/backtest.py` — see below)
when in fact this one function inside it is live and directly gates real BUYs. The same
module's `rsi()`/`trend()`/`adx()`/`macd()` are ALSO called live every cycle
(`stock_bot/main.py:235-240`) but only feed the console/log indicator line
(`stock_bot/main.py:1241-1262`, `print`/`logger.info`) — display only. The actual rule-based
trade trigger comes from a separate module, `IndicatorStrategy` in
`bot/strategy/indicator_strategy.py` (the crypto strategy module, imported directly by
`stock_bot/strategy/rules.py`) — already fixed for the self-referential-ATR-baseline bug in
the 2026-08-20 crypto session, and confirmed via a code comment in `stock_bot/main.py`
distinguishing "the trade trigger" from the indicator-line display values above it.

**Audited 2026-08-20 (read-only, no bugs found):** every function in
`stock_bot/indicators/indicators.py` (`sma`, `ema`, `rsi`, `macd`, `adx`, `atr`, `trend`,
`regime` — 8 total) is a pure, stateless, full-recompute-per-call — none carry any
instance/module state across calls. That's the specific property that rules out the crypto
bot's self-referential-ATR-baseline bug class here: that bug required a *persisted rolling
history* (`self._atr_history`) that a new value got folded into before being compared against
its own baseline — no such construct exists anywhere in this file, so the bug class is
structurally impossible, not just absent by luck. No lookahead found either: `stock_bot/
backtest.py`'s `compute_indicators()` slices `closes[:i+1]`/`highs[:i+1]`/`lows[:i+1]`
(inclusive of bar `i`, nothing beyond) for every indicator call, and the SPY regime path
(`_fetch_spy_regimes`/`_spy_regime_at`) does the same growing-slice `spy_closes[:i+1]` bounded
to each day, with `_spy_regime_at` only ever walking backward to bridge weekend/holiday gaps.
Wilder-smoothing boundary cases in `adx()`/`atr()` (the minimum-valid-length edge, the class
of bug most likely to hide in this kind of code) were checked by hand and partition cleanly
with no gap or overlap. Full findings: `.memory/decisions/stock-offline-audit-2026-08-20.md`.

**`stock_bot/backtest.py` itself (the standalone file with its own indicator pipeline) is
confirmed DEAD TOOLING** — zero importers anywhere in the codebase, a standalone CLI only.
The real, load-bearing walk-forward gate for whitelist additions is the root-level
`stock_backtest.py` script (see "Workflow after a strategy change" / symbol re-entry rules
elsewhere in this file), which imports `stock_bot/backtest/engine.py` — a different file, in
the `stock_bot/backtest/` *package*, not this `stock_bot/backtest.py` *module* — and that
engine already imports `bot/strategy/indicator_strategy.py` directly, so it stays in sync
with the live strategy automatically and was never exposed to this audit's question in the
first place. `stock_bot/backtest.py`'s own docstring is corrected accordingly (2026-08-20) —
no logic changes, since the audit found nothing to fix.

### Settlement date + FX-rate tax record-keeping (stock bot — closes punch-list #9)
Minimal scope, deliberately: captures the missing data fields only — no ACB/capital-gain
computation, no CRA-compliant report. Full tax reporting (superficial-loss rules, T5008
matching, professional review) was explicitly descoped 2026-08-05 as its own undertaking,
separate from a punch-list gap-fill, and the bot is still paper trading (not live/real money)
so there's nothing to file yet.

**`paper_trades.csv` / `ibkr_trades.csv` are UNCHANGED** — that 9-column schema is frozen (see
hard rules: `ConfidenceBandTracker`/accuracy pipeline depend on it exactly; "never add,
remove, or rename columns"). New data goes into a separate file per executor instead:
`paper_trades_settlement.csv` / `ibkr_trades_settlement.csv`, columns `timestamp, symbol,
side, settlement_date, fx_rate_at_trade` — joined back to the frozen CSV by
`(timestamp, symbol, side)`. Written on every BUY and SELL fill, best-effort (a write failure
never blocks or fails the trade itself). `settlement_date` is T+1, skipping weekends only —
does NOT account for market holidays (a deliberate simplification; the real holiday calendar
already exists in `stock_bot/main.py`'s `_us_holidays()`/`_ca_holidays()` but isn't wired in
here). `fx_rate_at_trade` is `1.0` for CAD-denominated symbols, the live USD/CAD rate
otherwise — the same `is_cad_symbol()`/`get_usd_cad_rate()` helpers already used for exposure
sizing, just persisted per-trade now instead of only used transiently.

### LiveTradingGate — stock bot readiness check (repaired + enforced 2026-08-20)
`stock_bot/analysis/accuracy_tracker.py`, surfaced on the dashboard and in the weekly email.
DISPLAY-ONLY — not wired into `IBKRExecutor` or `IBKR_ALLOW_LIVE`; see "Enforcement — still
open, deferred" below. A 2026-08-20 investigation found two of the four gates were
structurally broken (checking stale/dead data, or a book that could never produce another
trade) — this entry documents the fix, not the investigation itself
(`.memory/decisions/livetradinggate-gate-repair-2026-08-20.md` has the full trail).

**Gate 1 — backtest walk-forward (current strategy).** Was validating a stale (2026-06-29),
disconnected strategy config via `stock_bot/backtest.py --walkforward` — confirmed dead
tooling, zero importers, in the same day's offline-code audit. Now reads
`logs/stock_backtest_latest.json`, a new fixed-path (always overwritten) machine-readable
output `stock_backtest.py` writes on every run alongside its existing dated `.md` report — the
CURRENT walk-forward tool, which imports `bot/strategy/indicator_strategy.py` directly (same
module `rules.py`'s live signal path uses), so this gate now validates the strategy the bot
actually runs. Checks every symbol in the CURRENT `RULE_WHITELIST` has `verdict: PASS` in the
latest run — **all** of them, not a quorum — trusting `stock_backtest.py`'s own computed
verdict rather than re-deriving pass thresholds a second time (the kind of two-copies-of-one-
threshold drift this codebase has been bitten by before). Replaces the old hardcoded
`("AAPL", "SPY")` pair, which wasn't even in `RULE_WHITELIST`. First real run under the new
logic (2026-08-20, all 16 whitelist symbols, ~4m45s): **15/16 PASS, AMD FAIL** — a genuine,
actionable result now, not a number describing a different strategy.

**Gate 2 — AI confidence-band edge.** Was reading `fast_trades.csv`, the retired swing/fast
book (`FAST_ENABLED=false`, frozen since 2026-07-22, last row's exit reason literally
`MANUAL_CLOSE_SWING_BOOK_RETIRED`) — structurally could never reach its 20-trade minimum
again. Repurposed to a genuinely different signal than Gate 3: reads the same active position
book (`paper_trades.csv` + `ibkr_trades.csv`) but asks whether the AI's confidence score is
actually predictive — ≥10 completed MED/HIGH-confidence (80+) round-trips with a ≥55% win
rate, mirroring `ConfidenceBandTracker.recommendation()`'s own "AI HAS EDGE" threshold
(implemented as a structured check here rather than parsing that method's return string, so
the gate stays testable independent of its exact wording). Answers "is AI confidence
calibrated," independent of whether the rules-based strategy itself has edge — not a
duplicate of Gate 3.

**Gate 3 — position book (live).** Threshold raised from 5 round-trips (far below any real
readiness signal) to the bar already documented elsewhere in this file and never wired up:
**≥30 completed round-trips, PF≥1.2, win rate≥30%, all three required** — see "Stock Phase A
gate" / "IBKR live go-live" in the Roadmap below, same number stated twice, now actually
implemented. Below 30 trades: `PENDING` with a progress count, same pattern as before. Label
corrected from "Swing paper (daily)" (stale — reads the active Mode A/B position book, not the
retired swing/fast book) to "Position book (live)".

**Enforcement — RESOLVED 2026-08-20 (same day, second pass), hard block.** `IBKRExecutor.
__init__()` (`stock_bot/execution/ibkr.py`) now extends its existing `port in _LIVE_PORTS and
not allow_live` guard: when a live port (7496/4001) is requested **and** `allow_live=True`,
it additionally calls `LiveTradingGate().evaluate()` and raises `ValueError` — same exception
type, same "refuse to start" style as the existing guard — naming every non-PASS gate (status
+ detail) if Gates 1-3 aren't all `PASS`. Runs before any TWS connection is attempted (fail
fast, no dangling event-loop thread on a blocked start). **Gate 4 (infrastructure
importability) is deliberately excluded** — a broken smoke-test import shouldn't block someone
otherwise cleared to go live over an unrelated issue; confirmed with the user before building,
not assumed. The check only fires inside the `allow_live=True` branch — paper-mode callers
(the default, `IBKR_ALLOW_LIVE=false`) never reach it, confirmed by a dedicated test that makes
`LiveTradingGate.evaluate()` raise if called and shows paper construction still succeeds.
`stock_bot/.env`'s `IBKR_ALLOW_LIVE` comment updated to say this is now code-enforced, not a
human-honor-system note. Tests: `tests/stock/test_ibkr_executor.py`, 7 new (all-3-gates-pass
succeeds, Gate-4-fail still succeeds, single-gate-FAIL blocks, PENDING blocks same as FAIL,
error message names only the actually-failing gates, blocked before any connection attempt,
paper mode never evaluates the gate at all). Suite 598→605.

Tests for what the gates *measure* (the first, same-day pass): `tests/stock/
test_accuracy_tracker.py`, 18 cases (Gate 1: missing/malformed JSON, all-pass, one-symbol-fail,
symbol-missing-from-run, non-whitelist-symbol-ignored, empty-whitelist; Gate 2: pending/pass/
fail on the win-rate threshold, LOW/PRE-band trades excluded, structural guard confirming no
reference to the retired fast book remains; Gate 3: pending, all-three-pass, and both
directions of "2 of 3 criteria pass but still FAIL" — PF-only-failing and win-rate-only-failing
with trade count already satisfied). Suite 580→598.

### How to verify the config is active
Run: `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py`
Expected (current, since the self-referential ATR regime-baseline fix, 2026-08-20):
**31 trades, PF 2.19, 38.7% win rate.** Hash `b30f2f9e769c8d41` unchanged.
If RSI_FILTER_ENABLED=false accidentally: trade count jumps significantly, PF drops below 1.2.

Reproducible pinned-window verification (deterministic — data range fixed, so this result
does NOT drift as calendar time passes, unlike the rolling run above):
```
EXCHANGE=binance SYMBOL=BTC/USDT BACKTEST_SINCE=2024-03-07 BACKTEST_UNTIL=2026-06-20 python backtest.py
```
Expected: **30 trades, PF 1.94, 40.0% win rate** (5010 candles, 2024-03-07 → 2026-06-19),
hash `b30f2f9e769c8d41`. (Corrected 2026-08-27 — this line used to claim "identical result to
rolling run"; that was true on 2026-08-20 when the rolling 5000-candle window happened to
align with this pinned range, but the rolling window has since advanced to ~2024-05-16 →
present and now covers a different slice, hence the different-but-still-valid 31/2.19/38.7%.
Both PFs are well above the 1.72 fingerprint floor; the strategy is unchanged. Use the
rolling run for the canonical fingerprint, this pinned run for "did my environment/data
change break something" reproducibility.)

### Canonical strategy fingerprint (BTC/USDT)
- **Strategy hash:** `b30f2f9e769c8d41`
- **Hashed files (behavior-defining only):** `bot/strategy/indicator_strategy.py`,
  `bot/strategy/threshold_strategy.py`, `bot/indicators/indicators.py`
  (fingerprint.py and __init__.py excluded — non-behavioral)
- **Current result:** 31 trades, PF 2.19 (see "How to verify" above). Full research
  trail (why the trade count moved 58→39→35→32 across sessions) is in `CLAUDE_HISTORY.md`.
- Stamp after each passing walk-forward: `python stamp_strategy.py` → `logs/validated_strategy_hash`
- If the bot or backtest prints `STRATEGY CODE DIFFERS`, re-run walk-forward before trusting any PF numbers

**2026-08-20 hash change — self-referential ATR regime baseline fixed.** Found during a
deep-verification pass (see `.memory/decisions/expert-practices-benchmark.md`'s 2026-08-19
lookahead/recursive-bias addendum): `IndicatorStrategy.evaluate()` appended the current
candle's ATR to `self._atr_history` **before** `_classify_regime()` compared it against that
same history's mean — the VOLATILE check was judging a spike against a baseline the spike
itself had already been folded into (self-inclusion bias, worth ~1/20th of the spike's own
pull on the 20-entry rolling history). Not a lookahead bug — nothing future was used — but it
made a genuine volatility spike marginally harder to detect than comparing against the
strictly-prior candles. Fixed by moving the `self._atr_history.append(atr_val)` call to
strictly after `_classify_regime()` returns, so the current bar's ATR only enters the
baseline for *future* candles' comparisons (`bot/strategy/indicator_strategy.py`). Trade
count moved 32→31 (one fewer setup taken — a marginal case now correctly reads VOLATILE and
sits flat instead of trading), aggregate PF moved 1.72→2.19. Walk-forward (Binance BTC/USDT,
Kraken BTC/CAD has insufficient history for the training window — see "Exchange Setup" above
for why Binance is the standing proxy): **PASS**, PF holds out-of-sample (training 1.20 →
validation 2.99, both ≥1.0). Re-stamped via `stamp_strategy.py`. Tests:
`tests/shared/test_indicators.py`, 2 new — `test_classify_regime_excludes_current_bar_from_baseline`
(direct `_classify_regime()` contract check) and
`test_evaluate_regime_excludes_current_bar_from_baseline` (the actual regression test —
drives real `evaluate()` end to end with a spike sized to land between the exclusive- and
self-inclusive-mean thresholds; mutation-verified to FAIL under the pre-fix append-before-
classify ordering, confirmed manually before landing). Suite 534→536.

### AI provider health monitoring (stock bot — added 2026-08-25)
Closes a gap surfaced during a "what are we missing" review, not by a live incident this time
— the AI provider (`nvidia_nim`) has degraded three separate times on this project already
(see the `NVIDIA_MODEL` incident history above), and every one was only ever caught by
manually testing the API by hand, never by anything the bot itself surfaced. `stock_bot/
main.py`'s Phase 3 (AI calls) was already computing `_ai_nvidia_n`/`_ai_fallback_n`/
`_ai_failed_n` every scan cycle, but only ever printing them to the console — a 4th
degradation would have gone unnoticed the same way as the first three.

New `_update_ai_health()` (`stock_bot/main.py`, same extract-for-testability pattern as
`bot/main.py`'s `_update_auth_health()` from the 2026-08-15 Kraken auth outage): evaluated
once per scan cycle that actually attempted at least one AI call (a cycle where everything
was gated out — all-RANGING, market closed — is skipped, not treated as a failure or a
success). Tracks consecutive fully-failed cycles (zero successful AI calls out of at least
one attempted); at `_AI_HEALTH_THRESHOLD=3` consecutive fully-failed cycles, fires an
edge-triggered `notifier.ops_alert()` (Telegram + terminal + desktop, the same channel
`stock_bot/main.py` already uses for every other ops alert) and flips a health flag; a single
successful cycle after that flips it back and fires one recovery alert. Alerts once per
transition, never every cycle — same anti-spam shape as every other edge-triggered alert in
this codebase (candle watchdog, drawdown-warning tiers, the crypto auth-health flag itself).

**Deliberately NOT wired into either heartbeat's `healthy_fn`** — unlike the Kraken auth case,
this AI is advisory-only (`RULE_TRADING_ENABLED=true` means the rule engine trades regardless
of the AI's state), so a degraded AI provider flipping healthchecks.io unhealthy would
misreport "the bot is down" when rule-based trading is actually unaffected. It gets its own
distinct alert channel instead, kept separate from real-outage severity. A source-inspection
test (`test_run_does_not_wire_ai_health_into_heartbeat`) locks this decision in so it can't
regress by accident.

Detection only, not auto-failover (that was added 2026-08-27 — see "AI provider auto-failover"
below). At the time this was written, a 4th nvidia_nim degradation would alert immediately but
still need a manual model swap. Tests: `tests/stock/test_ai_health.py`, 8 cases
(below-threshold silence, trip-at-threshold alert, no re-alert while still failing, recovery
alert + counter reset, healthy-path never touches the notifier, blank-detail formatting, plus
the two wiring guards above). Suite 658→666. No `bot/strategy/*` touched — alerting/ops-layer
only, no walk-forward needed.

**Prediction confirmed, 2026-08-27:** the "a 4th degradation would have gone unnoticed" concern
above played out for real the very next day — `meta/llama-3.1-8b-instruct` hit end-of-life
2026-08-26, and this monitor caught it automatically (alert fired 15:56 UTC the same day the
model died), the first of the four nvidia_nim incidents ever caught without manually testing
the API by hand. See the "Current operational status" stock bot entry and `stock_bot/.env`'s
`NVIDIA_MODEL` comment for the swap details (now on `nvidia/nemotron-3-nano-30b-a3b`).

### AI provider auto-failover — Mistral (stock bot — added 2026-08-27)
Closes the loop the health monitor above only half-closed: it *detected* a 4th degradation but
still needed a manual `NVIDIA_MODEL` swap. Now `stock_bot/ai/ai_engine.py` has a real one-shot
failover. **nvidia_nim stays primary.** After `_FALLBACK_AFTER=5` consecutive *API* failures
(not parse errors — a failover can't fix a model that returns garbage JSON) the engine
switches to `AI_FALLBACK_PROVIDER` for the rest of the session and retries the current symbol
on it; `_update_ai_health` still fires its Telegram alert on the same cycle. The switch is
one-way per process (`_switch_to_fallback()` → `_fallback_active`), so a flapping primary
can't thrash between providers.

**New provider: `mistral`** (`AI_PROVIDER=mistral` standalone, or as the failover target).
OpenAI-compatible endpoint `https://api.mistral.ai/v1/chat/completions`, `MISTRAL_API_KEY`
(root `.env`, alongside `OPENROUTER_API_KEY`), `MISTRAL_MODEL` default `mistral-small-latest`,
2s rate-limit spacing (free "Experiment" tier is ~1 req/s; the bot's calls are already
sequential per symbol so this is comfortable). Chosen over OpenRouter because OpenRouter's
free tier caps at 50 req/day — too low for a ~28-40 symbol universe — while Mistral's free
tier is ~1 req/s / ~1B tokens/month, ~10x this bot's realistic volume.

**ACTIVATED live 2026-08-27** — `MISTRAL_API_KEY` added to root `.env`,
`AI_FALLBACK_PROVIDER=mistral` set in `stock_bot/.env`. Verified before enabling: key
authenticates (`/v1/models` 200, 54 models), `mistral-small-latest` available, and a live
round-trip through the bot's real `_parse()` on a bullish + a bearish scenario returned
**BUY conf 85 / SELL conf 90** (correct directions — notably `llama-3.1-8b` failed exactly
this bearish test twice by returning HOLD), 1.2–1.3s latency. Takes effect on the stock bot's
next restart. Still only fires after 5 consecutive nvidia_nim failures — nvidia_nim remains
primary. AI is advisory-only, so a dark AI layer never blocks trading regardless.

Same pass: removed the genuinely-dead `_fallback_openrouter()`/`_fallback_to_openrouter()`
(zero callers, dead model string — replaced by `_switch_to_fallback()`); made
`OPENROUTER_MODEL` env-configurable (its old `:free` slug 404'd); fixed stale default model
strings in `ai_engine.py`'s docstring + nvidia branch. `stock_bot/main.py`'s `_ai_fallback_n`
now counts any non-primary provider that answered (was hardcoded to `== "openrouter"`), and
the AI-summary print names the actual failover provider. Tests: `tests/stock/test_ai_failover.py`,
8 cases. Suite 698→706. No `bot/strategy/*` or `stock_bot/strategy/*` touched — advisory-layer
only, no walk-forward needed.

### Crypto dashboard — multi-symbol combine (added 2026-08-26)
`dashboard.html` (the detailed per-tick page `unified_dashboard.py` embeds) was hardcoded to
render only `_active_symbol` — always the first entry in `UNIVERSE_WHITELIST`, i.e. BTC/CAD.
When SOL/CAD went live (2026-08-25) it had zero visibility on this page despite holding a
real position from its first fill — found the next day while checking on the fill. Root
cause went deeper than the render call itself: `tick_log`/the "sticky" indicator display
values (`_dash_signal`/`_dash_rsi`/etc.) were shared module-level state written only for the
active symbol, and the fixed-alias `executor`/`state_machine`/`position_manager` variables
the render closure read from were permanently bound to `_active_symbol` at startup — a
second symbol wasn't just unrendered, the plumbing had no way to render it correctly even if
un-gated.

**Fix, by explicit request ("crypto all together in one page" — a single combined page was
chosen over separate per-symbol pages after discussing the tradeoff: separate pages would
have been the smaller change, but a single page needing no tab-switching to see both symbols
at once was worth the larger one):**
- `bot/dashboard/renderer.py` rewritten around a new `write_multi(path, exchange, strategy,
  tick, symbols: list[dict], ...)` — one shared page shell (title/style/exchange header,
  built once) wrapping one full content block per symbol (position-protection panel, metric
  cards, state/indicator/regime row, candle table, fills table, tick log table — all the
  same per-symbol detail as before, just stacked instead of single). The single-symbol
  `write()` signature is kept as a thin wrapper (`write_multi(symbols=[one dict])`) — not
  used by the live bot today (always ≥1 symbol via the list form) but kept for any future
  single-symbol caller.
- `bot/main.py`: `tick_log` entries now carry a `"sym"` tag (`candle_log` already did);
  the sticky display values moved from module-level globals into `symbol_state[sym]['dash_*']`
  — genuinely per-symbol now, not shared/stale across symbols; `_render_dashboard(sym, ...)`
  takes an explicit symbol, updates that symbol's entry in a new `_dash_snapshots` cache, and
  re-renders the FULL combined page from `_dash_snapshots` on every call — so the page always
  reflects the latest known state for every symbol, not just whichever one just ticked. The
  `if sym == _active_symbol:` gates were removed from the tick-log/dashboard path specifically
  (three call sites) while left untouched on the *console* print calls they used to also
  guard (`display.next_candle()` stays active-symbol-only — a deliberate, narrower console UX
  choice, not an oversight).
- Verified with a real two-symbol smoke test before landing (one flat symbol, one holding a
  position, same page) — confirmed one shared `<html>`/`<style>` (not two documents), correct
  symbol ordering, and specifically that the position-protection panel renders inside the
  holding symbol's block only, not leaking into the flat symbol's section — the concrete
  cross-contamination risk this stacked-fragment design had to avoid.
- `unified_dashboard.py` needed no change — it already just embeds `dashboard.html` via one
  iframe; that page now contains both symbols on its own.
- Tests: `tests/crypto/test_dashboard_renderer.py` (new file, 8 cases — this module's
  first-ever coverage, itself part of why the gap went unnoticed for a full day). Suite
  666→674. No `bot/strategy/*` touched — dashboard/display-layer only, no walk-forward needed.

### Stock bot RULES-decision log visibility (added 2026-08-26)
The per-symbol rule-signal summary printed every scan cycle (`📐 RULES: BUY/SELL/HOLD` +
RSI/ADX/trend/regime — `stock_bot/main.py`, `run()`) was `print()`-only, never written to
`logs/stock_bot.log`. Found when asked "why isn't the stock bot buying other stocks" and
today's actual market-hours log had zero evidence to check — only whichever terminal
happened to be running the bot at the time carried that output, and it was gone once that
terminal's scrollback was gone. Fixed: the same line is now also `logger.info("RULES [%s]:
...", symbol, ...)`, with the symbol name embedded explicitly — the console `print()` relies
on a separate header line printed just before it in the same terminal for symbol context,
which doesn't survive being read out of that visual order in a log file. The adjacent
SCREEN_SKIP-with-reason path was checked too and was already fine — `screener.py`'s
`_reason` string already embeds the symbol name and was already `logger.warning()`'d; this
fix only covers the RULES line, which was the actual gap. Tests: `tests/stock/
test_rules_log_visibility.py` (new file, 2 cases — source-inspection wiring guards, same
pattern as the VIX/macro/correlation/AI-health guards elsewhere in this manifest, since
`run()` needs a live yfinance/IBKR stack to exercise behaviorally). Suite 674→676. No
`bot/strategy/*` touched — logging-visibility only, no walk-forward needed.

### Stock bot scan universe widened (2026-08-27)
`UNIVERSE_SIZE` (top movers scanned per cycle, on top of the 28 `WATCHLIST` symbols) raised
15 → 30, user request — wanted more candidates evaluated per cycle. **Scan breadth only, not
a loosened entry bar or a day-trading change**: the rule signal's own criteria (ADX/RSI/trend/
EMA-spread thresholds) and the in-distribution ATR%/liquidity screener (`SCREENER_ENABLED`,
same filter documented under "Current stock bot RULE_WHITELIST" above) are both unchanged —
more symbols get checked against the identical bar, nothing gets waved through. `CANDLE`
timeframe/interval untouched (`interval=1d`, per the standing "no day-trading" policy). No
`bot/strategy/*` touched, no walk-forward needed. Prompted by a conversation pushing for
faster/bigger short-term profit ("make as much as we can in 2 days," "buy whatever's giving
money") — that framing was declined (would mean forcing unvalidated trades or day-trading,
both explicitly ruled out elsewhere in this file); widening the scan was the one legitimate
lever that increases opportunity without touching the validated strategy or risk gates.

### Current operational status (as of 2026-07-28)
- **Crypto bot:** live on Kraken, BTC/CAD ($77 slot cap) + SOL/CAD ($376 slot cap, added
  2026-08-25 — see "Live Symbol Universe" above), capital gate at 0/15 fills on BTC/CAD
  (strategy trades ~every 1–3 weeks; two BUY signals fired since 2026-07-05, both lost to
  execution fragility that's now fixed — see history 2026-07-24 entry). **SOL/CAD has 1 live
  fill** (BUY 0.080808 @ $134.02, 2026-08-26 — the fill that surfaced the post-only bug;
  position still open, native stop resting at ~$127.04) — its own 15-fill capital gate is at
  1/15, 0 completed round-trips. (Corrected 2026-08-27 — this line had said "zero fills yet"
  while the "Post-only param bug" section above already documented the fill; the two had
  diverged.) ATR SL 2.0 + ATR sizing both live. Telegram (t.me/amaresh_tradebot) + healthchecks.io heartbeat live.
  Retry resilience added on Kraken depth/candle/ticker fetches 2026-07-24; extended to
  startup balance/position sync (`fetch_balance` in `_sync_cash`/`_sync_position`) 2026-07-28,
  which also now alerts to Telegram on persistent failure instead of console-only. Order
  rejections (insufficient funds, exchange minimums, exchange errors) also now alert to
  Telegram, not just console — closes a gap where a rejected SL/TP exit could leave a
  position open with no notification. Limit-chase cancel-race verification also hardened:
  an unverifiable post-cancel state now always aborts the re-place instead of only doing so
  when `cancel_order` itself failed — closes a double-fill risk window. `_sync_position`'s
  cost_basis reseed no longer writes a fabricated 0.0 on a ticker-fetch failure (was silently
  overstating realized P&L on the next SELL) — it now warns and leaves cost_basis at the
  saved value instead. A related `None.reject_reason` crash risk in the rejected-order branch
  (bot/main.py) is also fixed — a future qty<=0 edge case now alerts cleanly instead of
  crashing the loop. Full detail in `.memory/decisions/known-gaps.md` (gaps #9, #10).
  Still deliberately deferred: several risk-gate `.env` keys undocumented in CLAUDE.md, and
  fee-currency-mismatch silent cash drift (see gap #9).
  Native exchange-side stop-loss added 2026-08-07 (see "Native exchange-side stop-loss"
  above) — closes the top finding from a gap review against external crypto-bot best-practice
  research: software SL/TP only protects a position while the bot process is alive. Shipped
  off pending live validation, then flipped **on** 2026-08-15 (see "Known live incident"
  below and the dated section above) ahead of that validation, traded off against reduced
  ability to babysit the bot while traveling. Risk-engine tiering upgrade done the same day (see "Risk-gate config" above)
  — crypto's RiskManager gained the weekly-loss/drawdown-warning/kill-switch tiers the stock
  bot got 2026-08-05, closing the second research finding. Slippage guard added the same day
  too (see "Slippage guard" above) — post-fill Telegram alert when a live fill lands >1%
  worse than the price the signal was evaluated against, shipped **on** by default (alert-
  only, can't change trading behavior). Candle watchdog upgraded to a real circuit breaker
  the same day too (see "Candle watchdog" above) — now blocks new BUYs while the feed is
  stale instead of only alerting, closing the fourth and last finding from that research
  pass. All four items from the 2026-08-07 crypto-bot gap review are now closed.
  Two-way Telegram control built AND enabled live 2026-08-20 (see "Two-way Telegram control"
  above) — `/status_crypto`, `/pause_crypto`, `/resume_crypto`, `/status_stock`,
  `/help_crypto` via a `getUpdates` long-poller, closing the Freqtrade-comparison gap
  (Telegram was alert-only). `TELEGRAM_CONTROL_ENABLED=true` in `.env`, bot restarted, live
  smoke test passed (`/help_crypto`/`/status_crypto` verified against real on-disk state).
- **Known live incident, 2026-08-15 (Kraken auth + monitoring blind spot):** while the user
  was traveling (bot host machine still running, still had internet — public Kraken
  ticker/candle calls kept succeeding throughout), every *authenticated* Kraken call
  (`BalanceEx` / balance & position sync) started failing with `EGeneral:Permission denied`
  starting 2026-08-11, escalating to continuous failure by the morning of 2026-08-15. Public-
  vs-private-only failing is the signature of an API-key IP restriction (see the Kraken setup
  note above — the key is restricted to an IP) or a revoked/reset key, not a network outage.
  Position was flat (0 BTC) throughout, so nothing was left unprotected, but had a BUY signal
  fired during the outage, order placement (also authenticated) would very likely have failed
  the same way. Root cause needed a Kraken-side check (API key page) — never resolved from
  the code side, but the log shows the last `Permission denied` at 2026-08-15 10:22 UTC and
  none since (checked 2026-08-18) — auth calls are succeeding again on their own, whether
  from the IP restriction being lifted or the key being reset outside this repo. What WAS
  found and fixed at the time: this failure was invisible to every monitoring layer
  in the bot — the drift-check failure only ever logged (`logger.warning`), never alerted via
  Telegram, and the heartbeat's `healthy_fn` only checks that the main loop is ticking, not
  that authenticated calls work, so healthchecks.io stayed green the whole four days. Fixed
  in `bot/main.py` (position-drift-check block): a new shared `_auth_health` flag
  now (1) fires an `alerter.error()` Telegram alert on entering/leaving the failure state
  (edge-triggered, not per-check — same alert-once-per-episode pattern as the candle
  watchdog/drawdown-warning tiers elsewhere in this file), and (2) feeds into the heartbeat's
  `healthy_fn`, so a repeat of this would flip healthchecks.io unhealthy too instead of
  staying silently green. **2026-08-18: the "no test coverage" gap noted here is now closed**
  — the alert-edge/heartbeat-flag logic was extracted into `_update_auth_health()` (same
  pattern as `_evaluate_drift()`) and given 5 direct unit tests in `test_drift_escalation.py`
  (below-threshold silence, trip-alert + flag flip, no re-alert while still failing, recovery
  alert + counter reset, healthy-path no-op) — behavior is unchanged, only the missing
  verification was added. Suite is now 526 total.
- **Stock bot:** live on IBKR paper (DUQ273338, reset to $5,000 CAD 2026-07-20). Swing book
  retired (`FAST_ENABLED=false`) — position book (rule-based, Mode A/B) is the only active
  book. TSX symbols are **permanently** advisory-only — CIRO regulation blocks API orders on
  Canadian exchanges (never re-add `.TO` symbols to RULE_WHITELIST). AI provider `nvidia_nim`,
  model `nvidia/nemotron-3-nano-30b-a3b` (swapped 2026-08-27 — see `stock_bot/.env` comment
  above `NVIDIA_MODEL` for the full incident history: this is the **fourth** model on this
  account to fail, but the first time the cause was genuine end-of-life rather than capacity
  congestion — `meta/llama-3.1-8b-instruct` (live since 2026-08-07) returned a real `410 Gone`
  from NVIDIA on 2026-08-26, "reached its end of life." **First degradation ever caught
  automatically** — the 2026-08-25 `_update_ai_health()` monitor fired within the same scan
  session the model died (15:56 UTC, 2026-08-26), no manual discovery needed this time.
  Queried NVIDIA's live model catalog and verified the replacement directly before swapping:
  `nvidia/nemotron-3-nano-30b-a3b` — 5/5 calls succeeded, 0.4-0.5s latency, and round-tripped
  cleanly through the bot's real `_parse()` with a realistic JSON-verdict prompt (3/3 correct).
  Two other catalog candidates (mistral-7b-instruct-v0.3, granite-3.0-8b-instruct) turned out
  not enabled on this account (404). AI is advisory-only throughout — zero trading impact
  during the outage, `RULE_TRADING_ENABLED` signals were unaffected the whole time.
  OpenRouter was re-researched as an independent provider this time (this codebase already has
  a full implementation + a live `OPENROUTER_API_KEY` in root `.env`): its own hardcoded free
  model is now ALSO discontinued (404), and more importantly its free tier caps at 50
  requests/day unless $10 in lifetime credits has been purchased (then 1000/day) — likely too
  low for a single scan pass over this bot's ~28-40 symbol universe, so staying on nvidia_nim
  remains the right call; the dormant `_fallback_openrouter()`/`_fallback_to_openrouter()` in
  `stock_bot/ai/ai_engine.py` is still unwired and would need its own model-string fix before
  it could ever work — flagged, not fixed. **Detection of a future degradation WAS closed 2026-08-25**
  (see "AI provider health monitoring" below) — the auto-failover itself remains unbuilt, but a
  4th degradation will now alert instead of requiring another manual catch. Daily-loss breaker now marks open positions to
  live scan-cycle prices every cycle (`refresh_position_marks()`), not just at fill time —
  was previously stale between fills, could under-detect real intraday drawdown. Restarted
  2026-07-28 (PID 25877) after an apparent ~6h scan-loop stall turned out most likely to be
  normal `AFTER_HOURS`-mode silence (see `.memory/decisions/known-gaps.md` gap #11) — either
  way, Phase 1's price-fetch now logs a clear `"cycle N failed: ..."` line on total fetch
  failure instead of completing an empty cycle silently. Circuit breakers expanded 2026-08-05
  from a single daily-loss tier to four (daily/weekly/drawdown-halt/kill-switch — see
  Risk-gate config above); sector-concentration gate (max 2 positions/sector, already live
  since before this session) also gained test coverage the same day. ATR-based stop distance
  + risk-capped sizing added the same day too, opt-in and OFF by default (`PAPER_ATR_SIZING_
  ENABLED=false`) — RY/CM behavior unaffected until a stock_backtest.py walk-forward PASS.
  Correlation gate (>0.70, 30-day daily returns, no extra network calls) added the same day
  and shipped active by default — it can only tighten a BUY, never loosens anything. Macro
  economic event blackout (FOMC/CPI/GDP/jobs report, market-wide) also added the same day —
  jobs-report dates are computed algorithmically (always correct, zero maintenance); FOMC/CPI/
  GDP dates are user-maintained via `MACRO_EVENT_DATES` and ship empty (not fabricated). All
  five items were found via a gap review against an external "Trading Bot Master Spec" the
  user shared — punch list is now clear through P0. P1 investigated next: the long-term/DCA
  bucket item was confirmed by-design (two-bucket policy, not a gap); the cash-reserve item
  turned out already satisfied (`PAPER_MAX_EXPOSURE_PCT=0.25` default = 75%+ cash floor,
  already gating BUYs) — the one real precision gap found was `check_exposure()` checking
  current state only, not the pending trade, so a single large BUY could blow past the cap
  in one shot before the *next* BUY got caught. Fixed 2026-08-05: both executors'
  `check_exposure()` now take an optional `pending_trade_value` (defaults to 0.0, so all
  other callers are unaffected); `stock_bot/main.py` computes the target allocation before
  the exposure gate (was previously computed after, inside the sizing branch) and passes it
  through. P2 closed the same day: VIX crisis mode (`^VIX` >= 35 blocks all new BUYs
  market-wide, shares the SPY regime filter's gate) and settlement/FX tax record-keeping
  (T+1 + FX rate per fill in a NEW separate file — `paper_trades.csv`/`ibkr_trades.csv` stay
  frozen, deliberately scoped to data capture only, not a CRA-compliant report). Punch list
  from the "Trading Bot Master Spec" gap review is now fully closed through P2 — only P3
  (Postgres/Docker rewrite) remains, and that's off the table per the user's own call. See
  conversation history / `.memory/` for the fuller comparison.
- **Unrelated fix, same day:** `unified_dashboard.py`'s Gate 3 "Shadow Match" briefly showed a
  fabricated 0.8% — the dashboard's regex fell through an "N/A" match-rate row (that day's
  shadow audit found 0 comparable candles — a Kraken fetch error) to an unrelated number
  later in the report (`BACKTEST_FEE_PCT: 0.80%`). Root cause traced to `shadow_signal.py`'s
  Kraken OHLCV fetch having no retry — one transient hiccup wasted the whole day's audit.
  Both fixed: `shadow_signal.py` now uses `fetch_with_retry` (same helper as live candle/
  ticker/depth fetches) on that call, and the dashboard regex is bounded to the "Match rate"
  table row with an explicit N/A case instead of silently grabbing the next X.XX% it finds.
  Re-running the audit after the fix showed the real number was 100.0% PASS all along — the
  underlying strategy fidelity was never actually degraded, only the report generation was.
- **Test-pollution incident, same day:** the settlement-CSV feature (P2 #9) broke test
  isolation — 4 pre-existing test files' `sandbox` fixtures (`test_stock_breaker.py`,
  `test_fx_sizing.py`, `test_stock_position_mark_refresh.py`, `test_ibkr_executor.py`)
  predated `_SETTLEMENT_CSV` and were never updated to redirect it, so every suite run
  silently appended fake RY/CM.TO/KO test rows into the REAL
  `stock_bot/paper_trades_settlement.csv` / `ibkr_trades_settlement.csv` — caught when the
  user noticed the file and asked what it was. Confirmed zero real trades were mixed in
  (frozen `ibkr_trades.csv`/`paper_trades.csv` untouched since 07-31/07-17) before resetting
  both to header-only. Fixed at two levels: the 4 fixtures now redirect it explicitly, AND
  `conftest.py` gained a new autouse fixture (`_block_real_stock_bot_file_writes`) that
  redirects every known paper/ibkr executor file-path global to a tmp default for every test
  — same shape of fix as the existing `_block_real_telegram_sends` fixture there, which exists
  for the identical reason (2026-07-29 incident: a test forgetting to mock Telegram sent a
  real message). A future new persisted-file addition can't repeat this by omission again.
- **Stock bot yfinance outage, 2026-08-05, and dashboard fixes found while checking it
  (2026-08-06):** the main scan loop's `yf.download()` (Phase 1 price fetch) failed for the
  entire NYSE trading session (09:30–16:00+ ET, 174 consecutive "0/28 symbols returned data"
  cycles) — yfinance returned "possibly delisted" for real large caps (CVX, GM, etc.),
  characteristic of a Yahoo-side rate-limit/block, not actual delistings. The bot's own
  detection worked correctly (logged clearly, fired a Telegram OPS ALERT every cycle) and
  yfinance was confirmed working again by the next check. Two follow-on fixes: (1)
  `unified_dashboard.py`'s BTC/CAD "STALE" badge only looked at crypto state-file age
  (which only updates on a fill/restart) — a perfectly healthy bot quiet for a week showed
  the same red alarm as an actually-hung one; now cross-checks `logs/trade_bot.log`
  freshness and shows amber "NO FILLS · Nd — bot alive" instead when the bot is confirmed
  alive but just hasn't traded. (2) `_check_open_positions_sl_tp` (the SL/TP watcher —
  separate from Phase 1, uses `get_live_price()`/`fast_info`, a different yfinance endpoint,
  independent 30s thread) had zero log evidence of whether it was also blind during the
  outage — per-symbol failures only logged at debug, uncaptured by the file handler. Added
  an always-visible INFO "SL/TP check: N/M positions priced" line every tick so a future
  outage leaves direct proof instead of requiring after-the-fact code-path inference. Also
  the first behavioral test coverage `_check_open_positions_sl_tp` has ever had.
- **Both bots:** crash-alert + atomic state writes + SIGTERM graceful shutdown + liveness
  tracking (detects hung loops, not just dead processes) all live.

---

## Live Symbol Universe

### Approved for live trading
| Symbol | Status | Basis |
|--------|--------|-------|
| BTC/CAD | ACTIVE | Walk-forward re-confirmed on current code: all windows PF > 1.0. Original validated pair. |
| SOL/CAD | ACTIVE (2026-08-25) | Promoted after all preconditions cleared same-day: fresh 3-window walk-forward PASS on current strategy code (TRAIN PF 1.32 / VALIDATION PF 1.46, both ≥1.2, ATR×2.0 stop + dollar-risk-capped sizing — `logs/atr_oos_SOL_2.0_sized_20260825.md`); capital verified live via `check_kraken_balance.py` ($553.39 CAD real balance, confirmed a $400 deposit landing exactly); FX-conversion precondition confirmed N/A (SOL/CAD is a direct CAD-quoted Kraken spot market, `quote: CAD`, no USD leg involved). `.env`: `UNIVERSE_WHITELIST=BTC/CAD,SOL/CAD`, `MAX_SLOT_CASH_CAD_SOL=376` (per-symbol `CapitalPool` cap, BTC's $77 untouched), `MAX_CONCURRENT_POSITIONS=2` (raised from 1 — required for a second slot to open at all), `STARTING_CASH=553.39`, `MONITOR_SYMBOLS=BTC/CAD,SOL/CAD`. Full precondition trail: `.memory/decisions/multi-symbol-validation.md`. |

### Watchlist (not yet tradeable — monitored for re-validation)
| Symbol | Status | Reason |
|--------|--------|--------|
| XRP/CAD | WATCHLIST | Walk-forward failed on current Mode A/B strategy: 87% SL-exit rate. Re-entry requires a full 3-window walk-forward pass on current strategy code. Re-verified 2026-08-26 via `validate_symbol.py XRP`: still fails (5000c PF 0.99, 3000c PF 0.50); Kraken liquidity also now narrowly fails on spread alone (0.18% vs 0.15% max, volume itself is fine at $709k). |

### Blocked (walk-forward failed)
| Symbol | Status | Reason |
|--------|--------|--------|
| ETH/CAD | BLOCKED | Walk-forward failed on all windows; no edge on ETH over the full 2024–2026 period. Re-verified 2026-08-26 via `validate_symbol.py ETH`: still fails (5000c PF 0.67); Kraken liquidity itself is clean (passes both volume and spread) — this is purely a strategy-edge failure, not a market-structure one. |

### Screened out — liquidity gate
| Symbol | 24h Vol (CAD) | Gate | Reason |
|--------|--------------|------|--------|
| DOGE/CAD | $6,228 | $50,000 | **Moved here 2026-08-26** (was miscategorized under "Blocked (walk-forward failed)" — that reason had gone stale). Fresh `validate_symbol.py DOGE` run: Kraken liquidity now fails hard (volume $6,228 vs $50k required, spread 1.01% vs 0.15% max) — but walk-forward on the current strategy code actually **passes** the two reliable windows (5000c PF 1.41, 3000c PF 1.42; only the 1000c window is unreliable at 5 trades). The blocker today is market structure (Kraken's DOGE/CAD order book is too thin/wide), not strategy edge — re-check liquidity before re-litigating this if Kraken volume ever recovers. |
| PEPE/CAD | $941 (was $1,659) | $50,000 | Failed liquidity gate — re-verified 2026-08-26: volume dropped further, spread now also checked (1.52%, fails). Walk-forward also now run (wasn't before) and fails too (5000c PF 0.82, 3000c PF 0.81) — no edge here even setting liquidity aside. |
| XDC/CAD | $25,709 (was $10,288) | $50,000 | Failed liquidity gate — re-verified 2026-08-26: volume roughly doubled but still well under the gate; spread also fails (0.34%). Walk-forward still can't be run — no XDC/USDT market on Binance. |

### Implementation
- `.env`: `UNIVERSE_WHITELIST=BTC/CAD,SOL/CAD` (SOL/CAD added 2026-08-25)
- `regime_monitor.py`: `MONITOR_SYMBOLS=BTC/CAD,SOL/CAD` (traded), `MONITOR_WATCHLIST=XRP/CAD` (health metrics only, labeled NOT TRADED)
- Screen tooling: `screen_universe.py`, run monthly via the in-bot `rescreen.py` scheduler (never auto-changes whitelists — flags decay/new-qualifiers only).

### Current stock bot RULE_WHITELIST
`MRNA,AMD,RY,PLTR,GLD,TD,CM,CSCO,KO,T,CAT,GOOGL,WMT,MSFT,GM,CVX` — all US-listed/API-tradeable
(no `.TO` symbols — see TSX API block below). Watchlist is a superset including AC.TO,
SHOP.TO, BNS, SU (advisory-only, never rule-buyable — TSX regulatory block, unrelated to the
paragraph below). Full screen history (affordable-symbol screen, large-cap screen, metals/
currency screen) is in `CLAUDE_HISTORY.md`. GM,CVX added 2026-07-31 from a 20-candidate batch
screen (`logs/stock_backtest_20260731.md`, 2/20 passed — GM PF 1.31-1.94, CVX PF 1.43-inf).
The other 18 (JPM, V, MA, PG, JNJ, SBUX, NKE, ORCL, IBM, QCOM, TXN, PYPL, UPS, PEP, VZ, ABBV,
MO, F) failed and are not whitelisted.

**RULE_WHITELIST no longer gates rule-based BUY entry (removed 2026-08-23).** The paragraph
above still describes what's IN this list and how those symbols historically got there, but
"adding a symbol requires a fresh `stock_backtest.py` PASS" is no longer true for trading
eligibility — `stock_bot/main.py`'s `_rule_buy` now fires on `rule_v.signal == "BUY" and
rule_v.warmed_up` alone, for ANY symbol in that cycle's scan universe (watchlist + universe
top-movers + held positions — see "Scan universe" note below), regardless of whether it's in
this list. Explicit user request: full-universe trading, no per-symbol backtest precondition.
`RULE_WHITELIST` itself is still loaded and still means something — `LiveTradingGate.
check_gate1()` (`stock_bot/analysis/accuracy_tracker.py`) still validates every symbol in it
against the latest `stock_backtest.py` walk-forward as part of the code-enforced IBKR
live-trading readiness gate — but it is no longer the safety net on what the paper bot can buy
day to day. Full detail: `CLAUDE_HISTORY.md` (2026-08-23 entry) and `.memory/decisions/
stock-whitelist-gate-removed-2026-08-23.md`.

**Replacement/remaining safety net, current as of the 2026-08-23 hardening pass (same day):**
1. **In-distribution ATR%/liquidity filter** — `stock_bot/data/screener.py` rejects a
   non-watchlist symbol whose ATR% exceeds 3× the range observed on the 4 originally-PASSed
   symbols (~30.8%) or whose avg $ volume is below $50M/day (~1/20th of the thinnest of the
   4). Rejections are visible on the dashboard ("🔬 Screened Out" section), not silently
   dropped. Held positions and watchlist symbols are exempt (same scoping as the pre-existing
   screener) so an existing position never loses its ability to generate a rules-engine SELL.
2. **Position sizing** — still flat notional (`PAPER_RISK_PCT=0.20` of account value).
   `calc_shares_atr_risk()` (`stock_bot/config.py`) already exists and would size inversely to
   ATR%, capped at the flat baseline — gated behind `PAPER_ATR_SIZING_ENABLED` (still `false`).
   User asked for the walk-forward validation first (this also swaps the SL trigger from flat
   5% to ATR×2.0, not just position size). Run 2026-08-23 via a new `validate_atr_sizing.py` —
   **result: 14/16 RULE_WHITELIST symbols PASS, but AMD (one of the original 4 backtest-PASS
   symbols) and KO both FAIL under ATR×2.0** (AMD full-window PF 1.05 < 1.2 — a genuine
   regression from its original flat-stop PASS). Flag left **off** — enabling it as-is would
   put a symbol live under a stop distance that just failed its own validation. See
   CLAUDE_HISTORY.md for the full per-window table and options going forward.
3. **Risk-gate tiers** (`PAPER_DAILY_LOSS_PCT`/`PAPER_WEEKLY_LOSS_PCT`/
   `PAPER_DRAWDOWN_HALT_PCT`/`PAPER_KILL_SWITCH_PCT`) — audited 2026-08-23, confirmed
   unchanged from their existing values (see "Risk-gate config (stock bot)" above), left as-is
   pending a user decision on tightening.
4. **Sector-concentration + correlation gates** — audited 2026-08-23, confirmed genuinely
   generic (live `yfinance` sector lookups, Pearson correlation over fetched candles) — no
   hardcoded symbol mapping, so both already cover the full newly-opened universe with no gap.
5. **AI shadow-vote review criteria** — a documented, dated trigger (not yet met, not
   automatic) for revisiting whether to reinstate a lighter validation gate: ≥15 completed
   round-trips on symbols outside {MRNA, AMD, RY.TO, PLTR} AND a material win-rate/PF/
   AI-agreement gap vs. the originally-backtested symbols. See `.memory/decisions/
   stock-whitelist-gate-removed-2026-08-23.md` for the exact thresholds.

---

## Capital Sizing Rules

### Starting capital
$100 CAD per symbol (general rule). Each live symbol trades independently with its own capital allocation, trade counter, and sizing tier. Currently: BTC/CAD ($77, pre-existing) + SOL/CAD ($376, added 2026-08-25 — a documented SOL-specific exception to the generic $100 Stage-1 figure, since SOL's own volatility interacting with the live ATR-risk sizer requires a larger slot to reliably clear Kraken's order minimum; see "Live Symbol Universe" and `.memory/decisions/multi-symbol-validation.md`). SOL/CAD's own 15-fill/$250 promotion gate starts from zero live fills, independent of BTC/CAD's.

### First increase — $100 → $250 CAD per symbol
Requires ALL of the following on live fills (not backtest):
- Minimum 15 completed trades on that symbol
- Live profit factor ≥ 1.2
- No single trade loss exceeding 3% of account
- Regime monitor showing PASS on all metrics for at least 2 consecutive readings before the increase

### Second increase — $250 → $500 CAD per symbol
Requires ALL of the following:
- Minimum 30 completed live trades on that symbol
- Live profit factor ≥ 1.3 sustained over last 20 trades
- Maximum drawdown on live account ≤ 5% at any point

### Hard rules that override everything
- **Never increase capital after a winning streak** — only increase after the trade count threshold is met
- **Never increase capital on both symbols simultaneously** — increase one, wait 10 trades, then evaluate the second
- **If live PF drops below 1.0 over any 10-trade window**, reduce back to previous capital tier immediately regardless of overall account performance
- **Symbol removal must never implicitly increase surviving symbol allocation.** Use `MAX_SLOT_CASH_CAD` in `.env` to hard-cap per-slot cash. Current value: `MAX_SLOT_CASH_CAD=77`. Implemented via `CapitalPool(slot_cap=...)` in `bot/portfolio/capital_pool.py`.
- **`CapitalPool` is a single shared pool split N ways, not N independent pools** (`slot_cash = min(total_capital / max_concurrent, slot_cap)`). When adding a second symbol, raise `STARTING_CASH` AND `MAX_CONCURRENT_POSITIONS` together in the same change — never one without the other. Raising concurrency alone silently shrinks every existing symbol's slot; raising capital alone leaves the new symbol with no slot to open in. (Found during multi-coin prep 2026-07-21, full detail in `CLAUDE_HISTORY.md`.)
- **Personal holdings in the same Kraken account are invisible to the bot by default.** `ADOPT_EXTERNAL_HOLDINGS=false` (default) ensures `LiveExecutor` only manages positions it opened itself. Never set `ADOPT_EXTERNAL_HOLDINGS=true` unless you explicitly want the bot to trade all assets in the account.

---

## Exchange Setup
- Backtesting: EXCHANGE=binance, SYMBOL=BTC/USDT
- Live trading: EXCHANGE=kraken, SYMBOL=BTC/CAD
- Reason: Kraken OHLCV history limited to ~720 candles, Binance has 5000+
- Price diff confirmed: 0.048% — negligible
- Kraken API key: generate at Security → API once KYC clears
  - Enable: Query Funds, Query Orders, Create Orders, Cancel Orders
  - Disable: Withdrawals (never enable on bot key)
  - Restrict to your IP address

---

## Validation Discipline

**Any commit that touches strategy-logic files invalidates all fingerprints and symbol ACTIVE statuses until walk-forward is re-run and the hash re-stamped.**

Strategy-logic files = anything in `bot/strategy/`. Changes to config, execution, risk, data, or tests do NOT invalidate the hash.

### Workflow after a strategy change
1. Edit `bot/strategy/*.py`
2. Run full backtest: `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` — confirm PF ≥ 1.72 (current fingerprint)
3. Run walk-forward: `python walkforward.py` — confirm all windows PF > 1.0
4. Stamp the hash: `python stamp_strategy.py`
5. Update CLAUDE.md "Canonical strategy fingerprint" with the new hash + result
6. For any symbol in UNIVERSE_WHITELIST: re-run walk-forward on that symbol too before assuming ACTIVE status still holds
   - A symbol validated on strategy version X is NOT automatically valid on version Y

### Re-entry gate for watchlisted / blocked symbols
Full 3-window walk-forward pass (all windows PF > 1.0) on the CURRENT strategy code.
A passing result on an older strategy version does not count.

### Capital gate evaluation (15-fill threshold)

The 15-fill capital gate ($100 → $250) requires **ALL THREE**, not just PF:
1. **Live PF ≥ 1.2** over ≥15 completed round-trips (net of fees)
2. **Shadow match rate ≥ 95%** — `python shadow_signal.py` verifies the live bot's
   candle-close decisions match a fresh strategy replay. Runs automatically daily via the
   in-bot scheduler (SHADOW_AUDIT_TIME, default 12:05 local).
3. **Fee and slippage within assumptions** — fill prices within 0.5% of signal-candle close;
   round-trip cost consistent with 0.40% maker BUY + 0.80% taker SELL = 1.20%.

**PF alone is insufficient at 15-trade sample sizes:**
- A **failing PF with clean fidelity** (≥95% match, slippage on-spec) means **variance, not
  strategy failure**. Extend the window to 25–30 trades rather than demoting or scaling back.
- A **passing PF with poor fidelity** means the live bot may not be executing the validated
  strategy — investigate before promoting capital.
- A **failing PF with poor fidelity** requires investigation of execution problems before any
  capital decision.

### Why this matters (incident log)
- XRP/CAD: validated on old RSI < 30 strategy. Mode A/B entry logic was added without
  re-running XRP walk-forward. XRP traded live with real money for weeks on a stale,
  passing-but-now-failed validation before being caught and removed 2026-07-02.

### Deflated Sharpe Ratio / CSCV — deferred, re-evaluated 2026-08-20, deferral stands

Deflated Sharpe Ratio (DSR) and Combinatorially Symmetric Cross-Validation / Probability of
Backtest Overfitting (CSCV/PBO) are institutional-grade corrections for multiple-testing
bias — inflated backtest results from trying many strategy variations and keeping the
best-performing one. Surfaced 2026-08-18 during a benchmark against outside research, judged
premature at the time (single BTC/CAD symbol, small personal capital, no active
multi-parameter search) and deferred, revisit condition: *"the strategy search space grows
materially (e.g., multi-parameter grid optimization across many symbols)."*

**Re-checked 2026-08-20 — condition hasn't fired, deferral stands.** Every script capable of a
parameter search (`validate_symbol.py`, `universe_manager.py`, `screen_universe.py`,
`rescreen.py`, `walkforward.py`) is untouched since 2026-07-18, and untouched by the
2026-08-19/20 sessions (ATR self-referential-baseline fix, native-stop gap fixes, Telegram
control, capital-gate/signal-drought checks — none involved parameter tuning). The one real
parameter sweep in the repo (`swing_backtest.py`, 6 SL/TP combinations) is dormant since
2026-07-03 and tied to the now-retired swing book.

**One question worth answering precisely, not dismissing:** `screen_universe.py` screens up
to `SCREEN_MAX_CANDIDATES=15` symbols (monthly, via `rescreen.py`) against one fixed strategy
config (`cfg.strategy.*`/`cfg.risk.*`, identical across every candidate and window — confirmed
by reading `_run_window()`). Structurally, this **is** the same multiple-testing/selection-bias
mechanism DSR/CSCV correct for — trying N things and keeping the ones that pass inflates
false positives whether the free variable is a parameter or a symbol. Not a categorically
different problem. It stays low-value to formalize anyway because: the trial count (~15) is
far below where DSR's correction diverges meaningfully from a naive threshold; the pass bar is
already a genuine 3-window walk-forward, not an in-sample fit; and the real gate is downstream
of the screen anyway — the 15-fill live capital gate above (PF≥1.2 **and** shadow-match≥95%)
is an empirical version of exactly what DSR/CSCV approximate statistically ("don't trust the
backtest selection alone"). The one documented false positive (XRP/CAD, above) was caught by
the re-validate-on-every-strategy-change rule, not something a screen-time statistical
correction would have flagged differently.

Effort if ever revisited: DSR added to `screen_universe.py`'s output — well under a day
(Sharpe already derivable from existing trade stats, standard formula, ~30-50 lines). CSCV/PBO
— several days (combinatorial train/test partitioning per candidate, multiplies backtest
runtime). Revisit trigger unchanged: multi-parameter grid search actually *combined with*
multi-symbol screening — symbol-screening alone, at this scale, with these downstream
mitigations, doesn't clear that bar. Full writeup: `.memory/decisions/expert-practices-benchmark.md`.

---

## Standing Policies

### No day-trading (1h or faster)
1h walk-forward FAILED 2026-07-10 (63% SL-exit rate, PF < 1.0 on the two largest windows).
`CANDLE_MINUTES=240` (4h) stays the only validated live timeframe. Do not revisit without a
new/modified strategy (its own fresh walk-forward + hash stamp) or materially more 1h history.

### TSX symbols — permanently API-blocked
CIRO rule DMR 3200 A.1.(b)(i) prohibits IBKR Canada clients from placing orders on Canadian
exchanges via ANY automated system. This is regulatory, not a settings fix. Never re-add a
`.TO` symbol to `RULE_WHITELIST` — TSX names may only be watch-listed (advisory) or traded
manually in TWS.

### No automated IPO trading
The bots never trade IPOs or recent listings via any special path. New listings earn entry
exactly like every other symbol: accumulate history → screener eligibility → full
`stock_backtest.py` walk-forward PASS → whitelist. No exceptions for famous names. Full
SPCX case study in `CLAUDE_HISTORY.md`.

### Investment philosophy — two-bucket policy
- **Bucket 1 — wealth building (personal, outside the bots):** Buffett's guidance for
  non-professionals — low-cost broad index fund, regular contributions, hold for decades.
  The bots are NOT the wealth engine.
- **Bucket 2 — trading system (this repo):** capped, gate-controlled experiment. Capital
  grows only through the documented fill-count / net-PF gates — never through conviction,
  streaks, or excitement.
- Buffett rule mapping already enforced in code: capital protection = risk engine + breakers
  + slot caps · circle of competence = default-deny whitelists + walk-forward gates ·
  patience = HOLD through weak regimes (ADX gate) · margin of safety = PF ≥ 1.2 net-of-fee
  gates + small sizing. Full detail + plan queue history in `CLAUDE_HISTORY.md`.

### Sizing-visibility rule (stock bot)
Never "fix" a `SIZE_SKIP` (signal valid but rounds to 0 shares) by raising `PAPER_RISK_PCT`,
adding fractional-share support, or special-casing a minimum share count. That bypasses the
margin-of-safety sizing rule. The correct lever is letting the account grow through the
Phase A gate, or not whitelisting symbols unaffordable at current account size.

---

## USD Expansion (contingent)

**Status: no qualifying symbols as of last full screen (2026-07-03).** Screen run with strategy hash `659d1c03987b72fd`. Full per-symbol results table and the ATR near-miss follow-up experiment are in `CLAUDE_HISTORY.md`. Summary: 603 Kraken USD spot pairs → 178 cleared the $50,000/day liquidity gate → top 15 by volume walk-forwarded → **zero passed** (dominant failure mode: 79–90% SL-exit rate on every alt tested — the Mode A/B pullback entry has no edge on these assets). Closest near-misses (PF only, still failed SL gate): SYN/USD (PF 1.80/2.56/2.39, SL 79%), LINK/USD (PF 1.54/2.19/1.28, SL 79%).

Later ATR-stop research (2026-07-16/17) showed SYN and SOL both clear the full gate in-sample and hold out-of-sample at ATR×2.0–2.5 — see `CLAUDE_HISTORY.md` "SOL/BTC/SYN ATR OOS validation" entries. SOL's OOS HOLDS result was re-confirmed 2026-08-24 with dollar-risk-capped position sizing applied on top of the ATR stop (see precondition #5 below) — still HOLDS, narrow margin. These remain conditional candidates, not promotions.

### Preconditions for any USD pair promotion
All of the following must be met before adding any USD pair to UNIVERSE_WHITELIST:
1. A future screen run produces a 3-window PASS (PF ≥ 1.2 all windows + trades ≥ 10 + SL ≤ 70%)
2. Capital ≥ $100 CAD available for a new Stage-1 starting slot, without reducing BTC/CAD's
   existing `MAX_SLOT_CASH_CAD=77` allocation (raised together with
   `MAX_CONCURRENT_POSITIONS` — see Capital Sizing Rules CapitalPool note). **Corrected
   2026-08-24 (same day, later pass)** — this precondition previously read "$500," which is
   the Stage-3 scale-up threshold ($250→$500, requires 30 *live* trades on that symbol at
   PF≥1.3) from Capital Sizing Rules above, not the entry requirement for a symbol that has
   never traded live. A new symbol starts at Stage 1 ($100), exactly like BTC/CAD originally
   did — it earns its way to $250 then $500 only after its own live fill history clears those
   gates, same as BTC/CAD would have to.

   **SOL-specific number, replacing the "$100" placeholder above — researched 2026-08-24
   (follow-up session, same day):** $100 is the correct GENERAL Stage-1 rule, but it is
   **not enough for SOL specifically** to reliably avoid a min-order rejection. Kraken's
   real SOL/CAD minimum (`ccxt load_markets()`, checked live) is `amount.min = 0.06 SOL`
   (~$7.88 CAD notional at the time checked, price ≈$131.26) — trivial on its own. The
   actual binding constraint is the live ATR-risk position sizer
   (`calc_trade_qty_atr_risk()`, `RISK_PER_TRADE_PCT=0.10`, `STOP_LOSS_PCT=0.015` baseline,
   `ATR_SL_MULT=2.0`), which caps BUY quantity by dollar-risk-at-stop — and SOL's ATR is a
   much larger fraction of its price than BTC's, so that cap bites hard. Solving for the
   slot cash needed to clear `amount.min` under this formula, across SOL/CAD's own last 30
   real 4h candles (~5 days): **$110 CAD at the calmest reading observed, $240 at the
   30-candle mean, $323 at the latest reading, $334 at the most volatile reading observed**
   — i.e. a $100 slot would SIZE_SKIP on essentially every normal-to-current-volatility
   day, not just occasionally. To also clear the bot's own pre-trade `MIN_SIZE_SAFETY_MARGIN`
   guard (1.5×, an early warning before outright rejection —
   `bot/execution/live_executor.py`), the range shifts to **~$165–$501 CAD** across the same
   volatility window. **Fee cross-check (same logic already documented for BTC):** Kraken
   fees are pure percentage-of-notional with no fixed per-trade floor, so round-trip fee
   drag (~1.20% live: 0.40% maker BUY + 0.80% taker SELL, per
   `.memory/decisions/fee-structure.md`) does not itself get worse at a smaller dollar slot
   — SOL is not "fee-strangled" the way the original June 2026 screen found other alts to be
   (that was a strategy-edge problem, not a sizing problem). SOL's own OOS-validated PF
   (1.32 train / 1.46 validation, `.memory/decisions/multi-symbol-validation.md`) already
   used `BACKTEST_FEE_PCT=0.008` per side (≈1.6% round-trip) — harsher than the real ~1.20%
   live figure — and still cleared PF≥1.2, so fee drag is not the constraint here; the ATR-
   risk-sizing/exchange-minimum interaction above is. This is volatility-dependent, not a
   fixed number — re-check if SOL's realized volatility regime shifts materially before
   trusting these figures. Full working and the raw script output:
   `.memory/decisions/multi-symbol-validation.md`.
3. Documented decision on CAD→USD conversion cost and ongoing FX exposure (Kraken charges
   ~0.20% conversion; USD P&L requires separate tracking from CAD base)
4. Full 3-window walk-forward pass on the CURRENT strategy code at promotion time (a pass on
   an older hash does not count)
5. SL-distance-based position sizing — already built generically (`calc_trade_qty_atr_risk()`,
   confirmed symbol-generic 2026-07-21), no new code needed, live for BTC/CAD since 2026-07-17
   (`ATR_SIZING_ENABLED=true`). **2026-08-24: specifically exercised against SOL** —
   `atr_oos_validation.py` gained an opt-in `ATR_RISK_SIZING` flag wiring the same already-live
   formula into the OOS-split backtest (it previously tested the ATR *stop distance* only, with
   flat notional sizing — the paired dollar-risk cap had never actually been run against SOL's
   2026-07-17 HOLDS result). BTC/USDT regression-checked first (canonical fingerprint 31
   trades/PF 2.19/hash `b30f2f9e769c8d41` reproduced exactly; same-window OOS split with sizing
   on vs. off showed near-identical PF, same trade count — sizing barely perturbs BTC). SOL/USDT
   **still HOLDS with sizing applied**: TRAIN PF 1.32, VALIDATION PF 1.46 (both ≥1.2, same trade
   count as unsized, margin narrow not wide). This precondition is satisfied as built AND as
   specifically validated for SOL. Full trade-level detail:
   `logs/atr_oos_SOL_2.0_sized_20260824.md`, `logs/atr_oos_BTC_2.0_sized_20260824.md`.

**Removed 2026-08-24: "BTC/CAD live gates met: ≥15 fills + live PF ≥ 1.2" (was precondition
#2).** This was a deliberate correction to an unexamined default, not a loosening of
standards — the evidentiary bar itself (PF ≥ 1.2, full walk-forward validation) is unchanged
and applies exactly as strictly to every remaining precondition above. What was removed was a
*coupling*: requiring BTC/CAD's own live trade count to clear 15 fills before SOL (or any
other independently-validated USD/new symbol) could be promoted, regardless of that symbol's
own edge. A dated investigation (`.memory/decisions/multi-symbol-validation.md`, 2026-08-24
"Fill-frequency reality check" addendum) quantified why this coupling didn't hold up: real
recent BTC/CAD fill frequency is 35–42 days/trade (not the "roughly every 1–3 weeks" this gate
was informally justified against after the fact), making 15 live fills a realistic 1.1–1.7+
year wait — and as of 2026-08-24, 65 days had already elapsed with zero progress toward fill
#1. Searching CLAUDE.md, CLAUDE_HISTORY.md, and every `.memory/decisions/*.md` file found no
record of "15" ever being *derived* from BTC/CAD's own expected frequency or any other
calculation specific to this gate — it was a reused round number, asserted once and
retroactively rationalized, not a deliberate risk-sizing decision. Requiring it added no
evidence about SOL's own edge (SOL clears the real bar — walk-forward PF ≥ 1.2 across
train/validation, with proper ATR-risk sizing applied, precondition #5 above — independently
of anything BTC/CAD does); it only tied SOL's promotion timeline to BTC/CAD's unrelated trade
frequency. **This does not unblock SOL.** Capital (precondition #2 above) was the sole unmet
precondition for SOL/CAD specifically at the time this paragraph was written — see the
2026-08-24 correction directly above precondition #2 (the $500 figure this paragraph
originally cited was the wrong threshold; corrected the same day) and
`.memory/decisions/multi-symbol-validation.md` for the current live-capital numbers. No `.env`
or `UNIVERSE_WHITELIST` change was made as part of this removal — SOL/CAD is not being added
to live config by this edit.

**Update, 2026-08-25 — SOL/CAD promoted.** The capital gap described above closed via a real
$400 CAD deposit (verified live against Kraken, `check_kraken_balance.py`: $553.39 CAD total).
All remaining preconditions were then re-checked same-day: walk-forward re-run fresh (not
reused from 2026-08-24) — still PASS; FX-conversion precondition (#3) confirmed N/A, since
SOL/CAD is a direct CAD-quoted Kraken spot market with no USD leg. `UNIVERSE_WHITELIST` and
`.env` were updated — see "Live Symbol Universe" above for the live config and full trail.

### Automated USD re-screen (added 2026-08-24)
Closes a real automation gap found during a 2026-08-24 doc-accuracy check: CLAUDE.md had long
claimed USD re-screening was "automated monthly via `rescreen.py`," but the code never actually
passed `SCREEN_QUOTE=USD` anywhere — `rescreen.py` only ever called `screen_universe.py` with no
env override, which defaults to CAD. The USD side was manual-only since the last real USD screen,
2026-07-16. `rescreen.py` now runs `screen_universe.py` a second time with
`extra_env={"SCREEN_QUOTE": "USD"}`, producing its own `## crypto-usd` report section in the same
monthly markdown output (identical format to the CAD section — PASS list, whitelist comparison,
decay/new-qualifier flags, gate-output tail). Since no USD pair is live-whitelisted today, the USD
leg's whitelist comparison is always against an empty set — every USD PASS surfaces as a **NEW
QUALIFIER**, never a decay, until/unless a USD pair is ever manually promoted to
`UNIVERSE_WHITELIST` (`RESCREEN_SKIP_USD=true` skips this leg, same pattern as the existing
`RESCREEN_SKIP_CRYPTO`/`RESCREEN_SKIP_STOCKS`). **Same "never auto-changes a whitelist" rule
applies identically** — a USD PASS is flagged for a human to look at, exactly like every other
finding this script has ever produced. Load impact measured before shipping: adds up to ~7 minutes
to the monthly job (well under the existing 2400s per-leg subprocess timeout) and 2 extra Kraken
API calls (negligible) — full measurement in CLAUDE_HISTORY.md. A second, unrelated live bug was
found and fixed in the same pass: `rescreen.py`'s Telegram alert helper read
`cfg.telegram_bot_token`/`telegram_chat_id`/`telegram_enabled` directly, but those fields live
under `cfg.alerts.*` — every attention-worthy rescreen result (the runs where alerting matters
most) had been silently raising `AttributeError`, caught and reduced to a console-only line nobody
reads, since this runs unattended. Fixed; the monthly markdown report itself was never affected by
this, only the Telegram push. Tests: `tests/crypto/test_rescreen.py` (new file, 11 cases). Full
trail: CLAUDE_HISTORY.md, `.memory/decisions/multi-symbol-validation.md`.

**`screen_universe.py` engine-kwargs drift bug — found and fixed 2026-08-26.** The script this
whole automated leg depends on had been hand-listing its own `engine.run()` kwargs instead of
using the shared `engine_kwargs_from_cfg()` builder, missing `macd_enabled`, all 7 Mode A/B
entry params, and `atr_risk_sizing`/`atr_sizing_baseline_sl_pct` — every walk-forward it ran
since 2026-07-20 was validating a more permissive strategy shape than what's actually live.
Caught via a live disagreement with `validate_symbol.py` over LINK/USD (PASS under the stale
kwargs vs. FAIL under the correct ones). Traced impact: no automated USD report had actually
run yet under this bug (the leg above was only added 2026-08-24, monthly scheduler hadn't
fired since), and the CAD leg's only 2 non-decided candidates (PEPE, XDC) fail on liquidity
before ever reaching the walk-forward step — so no past promotion decision was made on a false
result. Fixed to use the shared builder, same pattern as `validate_symbol.py`. Re-running the
USD screen before/after confirmed a real, two-directional effect (not a one-sided bug):
LINK/USD flipped PASS→FAIL, PENGU/USD flipped FAIL→PASS. Current fresh USD candidate:
**PUMP/USD** (PF 1.83–2.04 across all 3 windows, 20–29% SL rate, $6.2M/day volume) —
informational only, not promoted; same full precondition list as SYN/USD applies before it
could ever be considered. `test_validation_scripts_use_the_builder()`
(`tests/crypto/test_engine_params.py`) — which should have caught this — now also covers
`screen_universe.py`. Full trail: CLAUDE_HISTORY.md.

### Re-screen triggers
- Strategy code change (new hash after walk-forward) — re-screen all alts before assuming new results
- SL-exit rate cap relaxed (would require separate validation that high-SL symbols are genuinely profitable)
- **Automated monthly via `rescreen.py` (in-bot scheduler) — covers BOTH the CAD and USD legs, flags
  decay/new-qualifiers, never auto-changes whitelists.** Corrected 2026-08-24: this bullet used to sit
  next to a separate "run `SCREEN_QUOTE=USD python screen_universe.py`" manual-trigger bullet, which
  contradicted it — the code never actually passed `SCREEN_QUOTE=USD` anywhere, so the USD side was
  manual-only despite this line's claim. Now genuinely true — see "Automated USD re-screen" below.
- An out-of-cycle check before the next monthly run (e.g. a new high-volume symbol spotted on Kraken
  USD) can still be run manually: `SCREEN_QUOTE=USD python screen_universe.py` — same gate the
  automated leg calls, just on demand instead of waiting for the 1st of the month.

---

## Roadmap (open items only)

| # | Item | Status |
|---|------|--------|
| F | VPS logrotate (`/etc/logrotate.d/trade_bot`) | Config ready (2026-08-21): `deploy/logrotate_trade_bot.conf` fixed to the canonical `/opt/trade_bot` path (was a `/path/to/your/project` placeholder) and `VPS_SETUP.md` step 7 now copies that one file instead of duplicating a slightly different inline copy (the two had drifted — the inline version was missing `delaycompress`). Nothing left to do until a VPS actually exists — migration itself is still deferred per [[expert-practices-benchmark]] |
| G | Stock-bot headless deploy (IB Gateway + IBC) | **Scoped + written 2026-08-27** — `deploy/IBKR_GATEWAY_SETUP.md` (Docker path recommended via `gnzsnz/ib-gateway-docker`, native path as alternative; 2FA + daily-restart handling; verification via `ibkr_smoke.py --port 4002`) and `deploy/stock_bot.service` (systemd unit with an API-port wait as the readiness gate). **No bot code change needed** — `IBKRExecutor` already connects by host:port and covers Gateway. The only bot edit is `IBKR_PORT=7497 → 4002` in `stock_bot/.env`. Est. ~4h hands-on + a day's observation (Docker path). Not started — deferred with the rest of the VPS migration; the crypto bot is the one that moves first (it trades real money and needs no local broker software). |
| H | Ollama Cloud key revoke | Confirmed unused 2026-07-16; user parked indefinitely — don't re-raise unprompted |
| I | IBKR live go-live | Gate-blocked (30 paper trades + PF ≥ 1.2) — `LiveTradingGate` Gates 1-3 now CODE-ENFORCED in `IBKRExecutor.__init__()` (2026-08-20); `IBKR_ALLOW_LIVE=true` on a live port raises `ValueError` unless all three PASS. Current real status: Gate 1 15/16 (AMD fails), Gates 2-3 PENDING (insufficient live trades) |
| J | USD symbol re-screen | Automated monthly via rescreen.py — **now actually true as of 2026-08-24** (previously false: the code never passed `SCREEN_QUOTE=USD`; fixed, see "Automated USD re-screen" above) |
| K | ATR SL experiment for SYN/LINK/PUMP | **SOL/CAD PROMOTED to ACTIVE 2026-08-25** — see "Live Symbol Universe" above. **SYN/USD groundwork done the same day (NOT promoted)** — fresh walk-forward on current strategy hash still HOLDS (PF 1.75 train/validation, stronger than SOL's), but needs its own new capital ($250-$690 depending on volatility, on top of what BTC+SOL already committed), a real FX-conversion step (SYN/USD is genuinely USD-quoted — confirmed Kraken `USD/CAD` conversion cost 0.20%/leg), and its 24h liquidity ($49,371 on 2026-08-25, later re-read at $99,389 on 2026-08-26 but with spread now failing 0.179% vs 0.15% max) is not comfortably clean like SOL/BTC — re-check before acting. **PUMP/USD found + scoped 2026-08-26** (surfaced by the `screen_universe.py` engine-kwargs drift fix, see "Automated USD re-screen" above): walk-forward PASSES cleanly (PF 1.83–2.04 all 3 windows, 20–29% SL rate) and Kraken liquidity is clean on both metrics ($6.2M/day volume, 0.041% spread — better than SYN's borderline case). But its capital need is the largest of the three: $786–$1,618 depending on volatility (vs SOL's $110–$334, SYN's $250–$690) — Kraken's `amount.min=2200 PUMP` against its ~$0.0048 price forces the ATR-risk sizer's dollar cap much higher to clear the unit-count floor. Against the ~$100 CAD currently uncommitted (BTC $77 + SOL $376 = $453 of $553.39), PUMP/USD is the least capital-actionable of the three despite the cleanest liquidity/edge. None of SYN/PUMP/LINK are promoted. Full detail: `.memory/decisions/multi-symbol-validation.md`. |
| — | Crypto capital gate | BTC/CAD: 0/15 live fills — strategy trades ~every 1–3 weeks; keep watching, don't force it. SOL/CAD: 1/15 live fills (BUY 2026-08-26 @ $134.02, position open, 0 completed round-trips) — its own independent gate. |
| — | Stock Phase A gate | Position book counting toward 30 completed trades, PF ≥ 1.2, win rate ≥ 30% — now the literal `LiveTradingGate` Gate 3 threshold, current status 5/30 |

Everything else from the original near-term roadmap (swing book features, IBKR paper
executor, dashboard work, heartbeat/alerting, held-position visibility, rule-based rebuild)
is DONE — see `CLAUDE_HISTORY.md` for how/when.
