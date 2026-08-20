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

## Test Suite Manifest (reconciled 2026-08-18)

Expected total: **580 tests** (verified via `pytest --collect-only -q`; table sum below checked to match exactly). If `pytest --collect-only -q` reports a different number, a file has an import error, was deleted, was added without a manifest update, or was excluded from the runner. Investigate before trusting any green suite result. Suite runtime is ~9-11s — if it takes minutes, a test is reading live `.env` config. (2026-08-19: baseline checked at 527 immediately before that session's 7 new tests were added — one higher than the 526 this manifest previously claimed; not investigated further, flagging in case it matters later.)

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
| `tests/crypto/test_live_executor.py` | 63 | LiveExecutor: dry-run, market/limit orders, urgent-exit bypass, fee deduction, state save/load, pre-trade min-size guard, restart recovery (seeds position manager + state machine), native stop-loss backstop (placement, cancel, resync, failure alerting, dry-run/flag-off no-ops, restart reconciliation), native TRAILING stop-loss backstop (placement w/ trailingPercent param, priority over static, cancel, resync-on-quantity-change, failure alerting, dry-run no-op, state persist/restore — added 2026-08-19), `native_stop_price` property, multi-stop-order ambiguity detection on startup (alerts on 2+ stop-type orders resting, ignores unrelated non-stop open orders — added 2026-08-20), slippage guard (BUY/SELL unfavorable trip, within-threshold, favorable-direction, disabled, dry-run no-ops), restart-startup resting-stop quantity reconciliation (resize under-sized static/trailing, leave over-sized alone, no-op on match/unknown-qty) + untracked-but-real resting stop adoption (single static, single trailing, multiple-not-adopted, none-found-unchanged — both added 2026-08-20 follow-up) |
| `tests/crypto/test_capital_pool.py` | 19 | CapitalPool: slot allocation, slot cap, release, edge cases |
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
| `tests/crypto/test_telegram_control.py` | 28 | Two-way Telegram control (added 2026-08-20): `TelegramCommandPoller` transport (authorized dispatch+reply, unauthorized-chat silent ignore, unrecognized-command silent ignore, handler-exception no-raise, offset advances past both handled and ignored updates, `prime_offset()` drains backlog without dispatching, getUpdates failure doesn't raise/doesn't lose offset, disabled-without-credentials no-network, thread-starter returns None when disabled), source-inspection structural guards (no command body calls any order-placement/modification/cancellation method or bypasses `logs/HALT` via a direct `risk.halt()`/`resume()`, and the poller module itself carries zero trading imports), `_pause_crypto_flag`/`_resume_crypto_flag` (write/remove `logs/HALT`, idempotent, end-to-end proof they drive the SAME `_check_halt_flag()` the tick loop already polls — not a second path), `_status_crypto_text`/`_format_symbol_status` (halt/kill-switch display, per-symbol position/cash/PF/regime, PF n/a-vs-inf-vs-computed edge cases), `_status_stock_text` (paper/IBKR badge formatting, no-state-file case, loader-exception no-raise), `_help_crypto_text` |
| `tests/crypto/test_orphaned_positions.py` | 5 | Startup orphan check: open position outside this run's symbol list alerts (removed-from-whitelist safety) |
| `tests/crypto/test_universe.py` | 4 | Universe screener: scoring, momentum filter, fallback |
| `tests/crypto/test_main_strategy.py` | 2 | Strategy builder: full config wiring |
| `tests/stock/test_fast_validator_exits.py` | 6 | FastValidator exits: MAX_HOLD live-price fallback, corruption guard, SL regression |
| `tests/stock/test_paper_report.py` | 10 | Expectancy math: IBKR commission model, net-of-cost flip, report rendering, merged paper+IBKR position book, IBKR account section, active-book state synthesis, live-cash-snapshot precedence over stale fill CSV |
| `tests/stock/test_exit_policy.py` | 11 | Stock-bot asymmetric exit bars: single-verdict exit, 2-strike SELL streak, streak resets, AC.TO incident regression |
| `tests/stock/test_stock_backtest_engine.py` | 11 | Stock backtest engine: next-open fills, intra-candle SL/TP, gap handling, slippage/commission math, walk-forward gating |
| `tests/stock/test_stock_rules.py` | 5 | Rule signals: live==backtest replay parity, drop_last (forming candle), determinism, validated-parameter pin |
| `tests/crypto/test_audit_scheduler.py` | 14 | In-bot audit scheduler: tests REAL `_audit_due()` — daily catch-up, once-per-day, Mon-anchored weekly, monthly 1st-anchored (re-screen), missed-run catch-up |
| `tests/crypto/test_limit_chase_recovery.py` | 6 | 2026-07-15 unrecorded-fill regression: market-fallback polling, actual-type amount inference, cancel-race double-fill guard |
| `tests/stock/test_ibkr_executor.py` | 48 | IBKRExecutor (hermetic FakeIB): live-port/paper-account guards, contract mapping (.TO↔TSE/CAD, bare NYSE cross-listings→NYSE), broker-price fills, timeout rejection, cancel-race fill recording, realized-PnL persistence, try_reconnect probe (redial/never-raise/no-op), low-equity FX/margin-minimum guard (CAD exempt), starting_cash auto-rebaseline on external reset/deposit, live-cash snapshot persisted + preserved across disconnect, sector-concentration gate (reject 3rd same-sector position, allow add-on to already-held symbol, allow different sector), weekly-loss/drawdown-halt/kill-switch tiers (reject-on-trip, halt auto-lifts, kill switch sticky + persists across restart, SELL never blocked, peak-equity persistence, warning-status flag), per-position ATR stop-pct override (default/persistence/cleared-on-full-close), check_exposure projected (pending-trade-value) exposure — defaults to current-state-only, catches an oversized single BUY, allows one that stays under cap |
| `tests/stock/test_fx_sizing.py` | 14 | USD/CAD sizing fix (2026-07-31): `is_cad_symbol`, `get_usd_cad_rate` (fetch/fallback/cache), StockPaperExecutor mixed-currency `total_value`/`check_exposure`, sector-concentration gate (reject 3rd same-sector position, allow add-on to already-held symbol, allow different sector), check_exposure projected (pending-trade-value) exposure — defaults to current-state-only, catches an oversized single BUY, allows one that stays under cap |
| `tests/shared/test_heartbeat.py` | 8 | Heartbeat pings (bot/alerts/heartbeat.py): URL-off, success/failure never raise, healthy_fn gate |
| `tests/stock/test_tws_monitor.py` | 6 | TwsConnectionMonitor state machine: blip tolerance, alert-once per outage, recovery notice |
| `tests/crypto/test_atr_sizing.py` | 7 | calc_trade_qty_atr_risk: dollar-risk-at-stop == fixed-SL baseline, tight-stop cap, fallbacks |
| `tests/stock/test_stock_atr_sizing.py` | 7 | Stock-bot analog: `StockConfig.calc_shares_atr_risk` (whole-share sizing) — same invariant, opt-in via PAPER_ATR_SIZING_ENABLED (default false) |
| `tests/stock/test_stock_telegram.py` | 7 | Stock→Telegram relay: root-.env credential sourcing, ops_alert/fill forwarding, HIGH-only filter, channel-off no-ops |
| `tests/shared/test_crash_hardening.py` | 9 | atomic_write_json (valid/replace/no-tmp/parents/old-file-preserved), send_now sync + disabled, crash-alert helpers never raise |
| `tests/crypto/test_engine_params.py` | 8 | `engine_kwargs_from_cfg` builder: keys accepted by engine.run, ATR keys sourced from cfg, previously-drifted keys present, macd_enabled + Mode A/B entry params sourced from cfg, generic parity test (every StrategyConfig∩IndicatorConfig field reaches the backtest), both validation scripts use the builder |
| `tests/stock/test_alert_evaluator.py` | 4 | AlertEvaluator EARNINGS_SOON: held-vs-not-held priority/message, live-executor-only held-position source (no static PORTFOLIO tracker) |
| `tests/crypto/test_crypto_telegram.py` | 2 | TelegramAlerter.fill() reason line: included when given, omitted when absent |
| `tests/shared/test_liveness.py` | 7 | LivenessTracker (bot/alerts/liveness.py): touch/is_alive/staleness boundary, simulated hang between touches |
| `tests/stock/test_ai_engine_timeout.py` | 2 | nvidia_nim AI client is constructed with `timeout=_TIMEOUT_S`; empty `completion.choices` degrades to a HOLD verdict instead of raising TypeError |
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
| `tests/shared/test_telegram_retry.py` | 3 | `TelegramAlerter._send()` retry (added 2026-08-17, closes known-gaps #17): healthy send calls `requests.post` once with no retry, a transient failure recovers on retry, a persistent failure still degrades to a warning-only no-raise after exhausting attempts |

Run: `python -m pytest --tb=short -q` — must show **543 passed**.

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

### Two-way Telegram control (crypto — built 2026-08-20, opt-in, off by default)
```
TELEGRAM_CONTROL_ENABLED=false   # NOT set in .env — using the config.py default (false).
                                  # Separate from TELEGRAM_ENABLED (outbound alerts only):
                                  # this one starts an INBOUND getUpdates poller — a real
                                  # control surface, not just notifications — so it ships
                                  # opt-in rather than silently active on upgrade, same
                                  # reasoning as NATIVE_STOP_LOSS_ENABLED. Set true (same
                                  # TELEGRAM_BOT_TOKEN/CHAT_ID already used for alerts) to
                                  # turn it on.
```
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

### How to verify the config is active
Run: `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py`
Expected (current, since the self-referential ATR regime-baseline fix, 2026-08-20):
**31 trades, PF 2.19, 38.7% win rate.** Hash `b30f2f9e769c8d41` unchanged.
If RSI_FILTER_ENABLED=false accidentally: trade count jumps significantly, PF drops below 1.2.

Reproducible pinned-window verification (identical result to rolling run):
```
EXCHANGE=binance SYMBOL=BTC/USDT BACKTEST_SINCE=2024-03-07 BACKTEST_UNTIL=2026-06-20 python backtest.py
```

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

### Current operational status (as of 2026-07-28)
- **Crypto bot:** live on Kraken, BTC/CAD only, $77 slot cap, capital gate at 0/15 fills
  (strategy trades ~every 1–3 weeks; two BUY signals fired since 2026-07-05, both lost to
  execution fragility that's now fixed — see history 2026-07-24 entry). ATR SL 2.0 +
  ATR sizing both live. Telegram (t.me/amaresh_tradebot) + healthchecks.io heartbeat live.
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
  Two-way Telegram control built 2026-08-20 (see "Two-way Telegram control" above) —
  `/status_crypto`, `/pause_crypto`, `/resume_crypto`, `/status_stock`, `/help_crypto` via a
  `getUpdates` long-poller, closing the Freqtrade-comparison gap (Telegram was alert-only).
  Ships **off** by default (`TELEGRAM_CONTROL_ENABLED`) — built and tested this session but
  not yet turned on live; the user opts in via `.env` when ready.
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
  model `meta/llama-3.1-8b-instruct` (swapped 2026-08-07 — see `stock_bot/.env` comment above
  `NVIDIA_MODEL` for the full incident history: this is the **third** model on this account to
  degrade the same way, ~5h of 100% `APITimeoutError`/`DEGRADED` on the prior model
  `mistral-nemotron`, verified provider-side via direct API calls bypassing the bot's own code
  before swapping. Picked deliberately small this time on the theory that congestion here
  tracks model popularity/size, not account quota — the code's own hardcoded fallback default,
  `nvidia/nemotron-3-ultra-550b-a55b`, was re-tested at the same time and is NOT reliable
  either, 1/3 calls timing out at the full 20s). AI is advisory-only throughout — zero trading
  impact during the outage, `RULE_TRADING_ENABLED` signals were unaffected the whole time.
  The dormant `_fallback_openrouter()`/`_fallback_to_openrouter()` in `stock_bot/ai/ai_engine.py`
  found during this investigation is still unwired (zero call sites, no test coverage) — a
  live `OPENROUTER_API_KEY` sits unused in root `.env` that could auto-switch providers on a
  future nvidia_nim outage instead of requiring this same manual model-swap dance again. Not
  acted on yet — flagged, not fixed. Daily-loss breaker now marks open positions to
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

### Watchlist (not yet tradeable — monitored for re-validation)
| Symbol | Status | Reason |
|--------|--------|--------|
| XRP/CAD | WATCHLIST | Walk-forward failed on current Mode A/B strategy: 87% SL-exit rate. Re-entry requires a full 3-window walk-forward pass on current strategy code. |

### Blocked (walk-forward failed)
| Symbol | Status | Reason |
|--------|--------|--------|
| DOGE/CAD | BLOCKED | Walk-forward failed at corrected 0.8% fee on all windows. |
| ETH/CAD | BLOCKED | Walk-forward failed on all windows; no edge on ETH over the full 2024–2026 period. |
| SOL/CAD | BLOCKED | Walk-forward failed — all windows below 1.0. ATR×2.0 OOS validation later showed genuine promise (HOLDS train→validation, see CLAUDE_HISTORY.md) but SOL stays BLOCKED until full USD/new-symbol preconditions below are met. |

### Screened out — liquidity gate
| Symbol | 24h Vol (CAD) | Gate | Reason |
|--------|--------------|------|--------|
| PEPE/CAD | $1,659 | $50,000 | Failed liquidity gate — walk-forward not run |
| XDC/CAD | $10,288 | $50,000 | Failed liquidity gate — walk-forward not run |

### Implementation
- `.env`: `UNIVERSE_WHITELIST=BTC/CAD`
- `regime_monitor.py`: `MONITOR_SYMBOLS=BTC/CAD` (traded), `MONITOR_WATCHLIST=XRP/CAD` (health metrics only, labeled NOT TRADED)
- Screen tooling: `screen_universe.py`, run monthly via the in-bot `rescreen.py` scheduler (never auto-changes whitelists — flags decay/new-qualifiers only).

### Current stock bot RULE_WHITELIST
`MRNA,AMD,RY,PLTR,GLD,TD,CM,CSCO,KO,T,CAT,GOOGL,WMT,MSFT,GM,CVX` — all US-listed/API-tradeable
(no `.TO` symbols — see TSX API block below). Watchlist is a superset including AC.TO,
SHOP.TO, BNS, SU (advisory-only, never rule-buyable). Adding a symbol requires a fresh
`stock_backtest.py` PASS on the current strategy hash — never by hand. Full screen history
(affordable-symbol screen, large-cap screen, metals/currency screen) is in `CLAUDE_HISTORY.md`.
GM,CVX added 2026-07-31 from a 20-candidate batch screen (`logs/stock_backtest_20260731.md`,
2/20 passed — GM PF 1.31-1.94, CVX PF 1.43-inf). The other 18 (JPM, V, MA, PG, JNJ, SBUX,
NKE, ORCL, IBM, QCOM, TXN, PYPL, UPS, PEP, VZ, ABBV, MO, F) failed and are not whitelisted.

---

## Capital Sizing Rules

### Starting capital
$100 CAD per symbol. Each live symbol trades independently with its own capital allocation, trade counter, and sizing tier. Currently: BTC/CAD only ($100 CAD).

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

Later ATR-stop research (2026-07-16/17) showed SYN and SOL both clear the full gate in-sample and hold out-of-sample at ATR×2.0–2.5 — see `CLAUDE_HISTORY.md` "SOL/BTC/SYN ATR OOS validation" entries. These remain conditional candidates, not promotions.

### Preconditions for any USD pair promotion
All of the following must be met before adding any USD pair to UNIVERSE_WHITELIST:
1. A future screen run produces a 3-window PASS (PF ≥ 1.2 all windows + trades ≥ 10 + SL ≤ 70%)
2. BTC/CAD live gates met: ≥ 15 fills + live PF ≥ 1.2
3. Capital ≥ $500 CAD available for the new symbol slot (raised together with
   `MAX_CONCURRENT_POSITIONS` — see Capital Sizing Rules CapitalPool note)
4. Documented decision on CAD→USD conversion cost and ongoing FX exposure (Kraken charges
   ~0.20% conversion; USD P&L requires separate tracking from CAD base)
5. Full 3-window walk-forward pass on the CURRENT strategy code at promotion time (a pass on
   an older hash does not count)
6. SL-distance-based position sizing — already built generically (`calc_trade_qty_atr_risk()`,
   confirmed symbol-generic 2026-07-21), no new code needed

### Re-screen triggers
- Strategy code change (new hash after walk-forward) — re-screen all alts before assuming new results
- New high-volume symbol appears on Kraken USD (run `SCREEN_QUOTE=USD python screen_universe.py`)
- SL-exit rate cap relaxed (would require separate validation that high-SL symbols are genuinely profitable)
- Automated monthly via `rescreen.py` (in-bot scheduler) — flags decay/new-qualifiers, never auto-changes whitelists

---

## Roadmap (open items only)

| # | Item | Status |
|---|------|--------|
| F | VPS logrotate (`/etc/logrotate.d/trade_bot`) | Open — small effort |
| H | Ollama Cloud key revoke | Confirmed unused 2026-07-16; user parked indefinitely — don't re-raise unprompted |
| I | IBKR live go-live | Gate-blocked (30 paper trades + PF ≥ 1.2) |
| J | USD symbol re-screen | Automated monthly via rescreen.py |
| K | ATR SL experiment for SYN/LINK | SYN + SOL + BTC all OOS-validated at ATR×2.0; still gate-blocked on USD/new-symbol preconditions above |
| — | Crypto capital gate | 0/15 live fills on BTC/CAD — strategy trades ~every 1–3 weeks; keep watching, don't force it |
| — | Stock Phase A gate | Position book counting toward 30 completed trades, PF ≥ 1.2, win rate ≥ 30% |

Everything else from the original near-term roadmap (swing book features, IBKR paper
executor, dashboard work, heartbeat/alerting, held-position visibility, rule-based rebuild)
is DONE — see `CLAUDE_HISTORY.md` for how/when.
