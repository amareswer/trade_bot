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
way they do) lives in **`CLAUDE_HISTORY.md`** (split out 2026-07-25; this file was
trimmed again 2026-09-01 when it re-crossed 150k chars — incident narratives and the
test-count history moved to `CLAUDE_HISTORY.md` under "CLAUDE.md trim, 2026-09-01").
This file holds only current, actionable state. Consult the history file for the full
narrative behind any decision below, and `.memory/decisions/*.md` for the deepest trails.

---

## Test Suite Manifest

**Expected total: 950 tests** (`pytest --collect-only -q`). If the count disagrees: a file
has an import error, was deleted, was added without a manifest bump, or was excluded from the
runner — investigate before trusting a green suite. Suite runtime ~9–26s; minutes means a
test is reading live `.env` config. The per-row table sum below lags the header total by ~22
(pre-existing row-vs-total drift; `--collect-only` and this header agree). Full count-delta
history: `CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-01" → "count-delta history".

Run: `python -m pytest --tb=short -q` — must show **906 passed**.

| File | Tests | What it covers |
|------|-------|----------------|
| `tests/shared/test_indicators.py` | 30 | RSI, EMA, ADX, MACD, ATR; regime-classification self-referential-ATR-baseline regression |
| `tests/crypto/test_live_executor.py` | 70 | LiveExecutor: dry-run, market/limit orders, urgent-exit bypass, fee deduction, state save/load, min-size guard, restart recovery, native static + trailing stop-loss backstop (placement/cancel/resync/failure-alert/restart reconciliation/quantity reconciliation/untracked-order adoption/multi-stop ambiguity), `native_stop_price` property, slippage guard, maker→taker silent-fallback alert, native-stop pre-cancel-on-SELL (2026-08-27 deadlock incident), **duplicate-order guards (2026-09-11)**: submission-exception reconciliation adopts an untracked resting order instead of market-ordering on top of it, cancel-timeout retry blocked unless the post-cancel status is confirmed terminal, **clientOrderId reconciliation (2026-09-12)**: a fresh UUID per attempt lets post-exception recovery check both open AND closed orders, catching the "already fully filled" case fetch_open_orders alone can't see |
| `tests/crypto/test_capital_pool.py` | 37 | CapitalPool: slot allocation, slot cap, per-symbol slot caps (`slot_caps`, `slot_cash_for()`), release, edge cases; `config._slot_caps_by_base()` env scanner; `PortfolioConfig.max_slot_cash_cad_by_base` validation |
| `tests/crypto/test_correlation.py` | 17 | Pearson correlation, pct_returns, fetch_correlation |
| `tests/stock/test_stock_correlation.py` | 5 | `stock_bot/risk/correlation.py`: `fetch_correlation_from_closes` — no-network wrapper reusing the crypto pearson/pct_returns |
| `tests/stock/test_stock_correlation_gate.py` | 8 | `_check_correlation_gate`: blocks on >0.70 correlation with an open position, fails open on missing data, case-insensitive, source guard |
| `tests/stock/test_stock_macro_calendar.py` | 14 | `macro_calendar.py`: `jobs_report_dates`, `parse_user_event_dates`, `is_macro_blackout` (window/boundary/disabled/nearest-event) |
| `tests/stock/test_stock_macro_blackout_gate.py` | 6 | `_is_macro_event_blackout` wrapper: user date / disabled / fail-open / jobs-report-alone / source guard; pinned to a fixed reference date (was contaminated by the live calendar until 2026-08-07) |
| `tests/stock/test_stock_vix_crisis.py` | 6 | `vix_crisis.py`: `is_vix_crisis` — at/above/below threshold, None fails open, zero/negative disables |
| `tests/stock/test_stock_vix_crisis_gate.py` | 2 | Source guard: `run()` fetches `^VIX`, computes crisis mode, gates BUYs via the shared `_regime_ok` flag |
| `tests/stock/test_stock_settlement_csv.py` | 11 | Settlement/FX tax record-keeping: `_next_business_day` T+1, frozen CSV header unchanged, settlement CSV written on BUY/SELL with correct join key, CAD → fx_rate=1.0 |
| `tests/crypto/test_risk_manager.py` | 32 | RiskManager: halt gate, daily loss, position size, SL/TP bypass, state persistence, per-symbol caps, aggregate breakers, kill-switch/drawdown-halt/weekly-loss/drawdown-warning tiers |
| `tests/crypto/test_fill_recording.py` | 8 | qty=0 fill — filled priority, amount fallback, guard, TradeLog guard |
| `tests/crypto/test_external_holdings.py` | 6 | External-holdings guard in `_sync_position` (adopt=false/true) |
| `tests/crypto/test_executor.py` | 6 | PaperExecutor: BUY/SELL, insufficient cash, history |
| `tests/crypto/test_drift_escalation.py` | 17 | REAL `_evaluate_drift()` (escalation, ack, changed-amount re-alert, resolution reset); REAL `_update_auth_health()` (Kraken auth-outage alert-edge/heartbeat flag); REAL `_seed_native_stop_state()` (restart native-stop `ss`-mirroring) |
| `tests/stock/test_tsx_validation.py` | 5 | Stock-bot TSX price sanity check |
| `tests/stock/test_stock_breaker.py` | 18 | Stock breakers (StockPaperExecutor): daily/weekly/drawdown-halt/kill-switch tiers, SELL never blocked, peak-equity persistence, per-position ATR stop-pct override, daily-loss calendar-day anchoring (`_day_open_equity`/`_day_start_iso` persist + UTC-roll) |
| `tests/crypto/test_candle_watchdog.py` | 7 | Candle watchdog circuit breaker: silent/blocked/no-re-alert on stale, alert+unblock on recovery |
| `tests/crypto/test_halt_flag.py` | 5 | Manual halt kill-switch: `logs/HALT` engage/lift, ownership guard |
| `tests/crypto/test_telegram_control.py` | 30 | `TelegramCommandPoller` transport (auth dispatch, unauthorized/unrecognized silent ignore, offset advance, `prime_offset()`, failure handling, error backoff); source guards (no order methods, no direct halt bypass, zero trading imports); `_pause/_resume_crypto_flag`, `_status_crypto_text`, `_status_stock_text`, `_help_crypto_text` |
| `tests/crypto/test_orphaned_positions.py` | 5 | Startup orphan check: open position outside this run's symbol list alerts |
| `tests/crypto/test_universe.py` | 4 | Universe screener: scoring, momentum filter, fallback |
| `tests/crypto/test_main_strategy.py` | 2 | Strategy builder: full config wiring |
| `tests/stock/test_fast_validator_exits.py` | 6 | FastValidator exits: MAX_HOLD live-price fallback, corruption guard, SL regression |
| `tests/stock/test_paper_report.py` | 10 | Expectancy math: IBKR commission model, net-of-cost flip, merged paper+IBKR book, IBKR account section, live-cash-snapshot precedence (row parsing is now `_row_to_trade`, tested separately) |
| `tests/stock/test_exit_policy.py` | 11 | Stock asymmetric exit bars: single-verdict exit, 2-strike SELL streak, streak resets, AC.TO incident regression |
| `tests/stock/test_stock_backtest_engine.py` | 15 | Stock backtest engine: next-open fills, intra-candle SL/TP, gap handling, slippage/commission math, walk-forward gating, optional ATR(14)×mult stop mode, **ATR look-ahead-bias fix (2026-09-12)**: the entry/fill candle's own high/low/close must not feed the ATR sizing its own stop |
| `tests/stock/test_stock_rules.py` | 5 | Rule signals: live==backtest replay parity, drop_last, determinism, validated-parameter pin |
| `tests/crypto/test_audit_scheduler.py` | 14 | REAL `_audit_due()` — daily catch-up, once-per-day, Mon-anchored weekly, monthly 1st-anchored re-screen, missed-run catch-up |
| `tests/crypto/test_limit_chase_recovery.py` | 6 | 2026-07-15 unrecorded-fill regression: market-fallback polling, actual-type amount inference, cancel-race double-fill guard |
| `tests/stock/test_ibkr_executor.py` | 89 | IBKRExecutor (hermetic FakeIB): live-port/paper-account guards, contract mapping, broker-price fills, timeout rejection, cancel-race fill recording, realized-PnL persistence, try_reconnect probe, FX/margin-minimum guard (**checks NET-LIQ, not free cash** — 2026-08-31 fix), sector-concentration gate, weekly/drawdown-halt/kill-switch tiers, per-position ATR stop-pct override, projected-exposure check, LiveTradingGate enforcement (incl. Gate 2 SKIPPED-when-AI-disabled bypass, 2026-09-10), TWS-query resilience (last-good cache, incl. **disconnected-but-no-exception preserves cache** — 2026-09-11 fix), `ibkr_trades.csv` write buffer/retry, Error 10349 slow-resubmit fill (20s grace + `tif="DAY"`), daily-loss calendar-day anchoring, **partial-fill tracking to completion or confirmed cancel** (2026-09-12 fix), **concurrent-sell serialization** (2026-09-12 fix, overlap-counter proof), **native broker-side protective stop** (2026-09-12: place/no-op/replace/adopt-on-restart, cancel-before-sell, broker-triggered-fill detection, multi-stop ambiguity — hardened across two further review passes: ambiguous-lookup sentinel distinct from "confirmed none", cost basis captured once before any cancel/place operation rather than re-queried afterward, `_cancel_trade_and_wait` returns a tri-state cancelled/filled/unconfirmed outcome so a stop that fills during its own cancellation is never mistaken for "safe to replace", shared `_record_native_stop_fill` helper, `sync_protective_stop`/`sell()` share one reentrant per-symbol lock), **currency-aware cash check** (2026-09-12 fix: USD-stock affordability now converted to CAD before comparing against CAD cash) |
| `tests/stock/test_concurrent_sell.py` | 1 | `StockPaperExecutor` concurrent-sell regression (2026-09-12): two threads racing a full-position sell — proves both the overlap invariant (per-symbol lock) and the actual business outcome (one FILLED, one REJECTED, never both filling the same shares) |
| `tests/stock/test_intraday_price_guard.py` | 5 | `get_live_price()`'s previous-close corruption guard (2026-09-12): a genuine crash confirmed by today's own day_high/day_low is no longer discarded; a corrupted read outside that range still is; day-range lookup failure fails toward the conservative reject |
| `tests/stock/test_paper_executor_fill_price.py` | 2 | `StockPaperExecutor.buy()`/`sell()` regression (2026-09-12): `order.price`/`quantity`/`total_value` now reflect the actual slippage-adjusted fill, not the pre-slippage requested price — IBKRExecutor already did this correctly, paper.py did not |
| `tests/stock/test_fx_sizing.py` | 15 | USD/CAD sizing: `is_cad_symbol`, `get_usd_cad_rate`, mixed-currency `total_value`/`check_exposure`, sector-concentration gate, projected-exposure check, **lazy fast_info failure inside get_usd_cad_rate now falls back gracefully instead of raising** (2026-09-12 fix — same class of bug already fixed in `intraday_price.py`, missed here at the time) |
| `tests/shared/test_indicator_strategy_macd_history.py` | 1 | `IndicatorStrategy` MACD-history regression (2026-09-12): an ADX-rejected candle must still update `_last_macd_hist` — reproduces the exact reviewer scenario (histogram 1→5→3, middle candle ADX-rejected) that used to read a real momentum *fall* as "rising" and fire a false pullback BUY |
| `tests/stock/test_screener_in_distribution.py` | 5 | In-distribution ATR%/liquidity filter (`stock_bot/data/screener.py`, replacement safety net after RULE_WHITELIST stopped gating BUYs) |
| `tests/stock/test_accuracy_tracker.py` | 20 | `LiveTradingGate` gates — Gate 1 (`stock_backtest_latest.json` vs `RULE_WHITELIST`), Gate 2 (AI confidence-band edge, incl. SKIPPED when `AI_ENABLED=false` — 2026-09-10), Gate 3 (≥30 round-trips/PF≥1.2/win≥30%) |
| `tests/stock/test_checkpoint_tracker.py` | 14 | Post-whitelist review checkpoint tracker (`checkpoint_tracker.py`, dashboard visibility only): sample floors, win-rate/PF/AI-agreement gap triggers, AI-split sample-size guard |
| `tests/shared/test_heartbeat.py` | 8 | Heartbeat pings: URL-off, success/failure never raise, healthy_fn gate |
| `tests/stock/test_tws_monitor.py` | 6 | TwsConnectionMonitor state machine: blip tolerance, alert-once per outage, recovery notice |
| `tests/crypto/test_atr_sizing.py` | 7 | `calc_trade_qty_atr_risk`: dollar-risk-at-stop == fixed-SL baseline, tight-stop cap, fallbacks |
| `tests/stock/test_stock_atr_sizing.py` | 7 | `StockConfig.calc_shares_atr_risk` (whole-share) — same invariant, opt-in via `PAPER_ATR_SIZING_ENABLED` |
| `tests/stock/test_stock_telegram.py` | 7 | Stock→Telegram relay: root-.env credential sourcing, ops_alert/fill forwarding, HIGH-only filter, channel-off no-ops |
| `tests/shared/test_crash_hardening.py` | 9 | `atomic_write_json`, `send_now` sync + disabled, crash-alert helpers never raise |
| `tests/crypto/test_engine_params.py` | 8 | `engine_kwargs_from_cfg` builder: keys accepted by `engine.run`, ATR keys from cfg, macd/Mode A/B params from cfg, generic parity, validation scripts use the builder (backtest / walkforward / validate_symbol / screen_universe) |
| `tests/crypto/test_overlay_gates.py` | 10 | `engine.run` opt-in live-only BUY overlays (`mtf_daily_closes` / `fng_by_date`, added 2026-09-02 for `mtf_overlay_backtest.py`): None ≡ baseline, MTF BEARISH-daily veto, FNG>threshold veto, `_fng_asof` most-recent-prior / fail-open, MTF-before-FNG precedence, insufficient-daily-history skip |
| `tests/crypto/test_display_broken_pipe.py` | 4 | `bot/display.py` print wrapper swallows `BrokenPipeError`/`OSError` (2026-09-02 regression: a broken-pipe from `display.warmup()` crashed the crypto bot mid-warmup); normal output still reaches stdout |
| `tests/stock/test_tsx_rule_buy_block.py` | 4 | Source guard: `run()` blocks `.TO` symbols from automated BUYs (`TSX_BLOCKED`), clears `_act_buy` before the exec block, records the block for the digest, leaves the SELL path alone (CIRO DMR 3200; implicit guard lost when RULE_WHITELIST stopped gating BUYs 2026-08-23; AC.TO hit it live 2026-09-02) |
| `tests/crypto/test_exit_overrides.py` | 11 | Per-symbol EXIT overrides (`TAKE_PROFIT_PCT_<BASE>` / `TRAILING_STOP_PCT_<BASE>` / `TRAILING_STOP_ACTIVATION_PCT_<BASE>`, added 2026-09-03): `_exit_overrides_by_base()` scanner (bare keys ignored, non-numeric rejected), `BacktestConfig.exit_params_for()` fallback/merge/base-not-quote/missing-symbol, out-of-range validation, `engine_kwargs_from_cfg(cfg, symbol=...)` resolves for the right base, `bot/main.py` exit block reads the per-symbol dict |
| `tests/stock/test_alert_evaluator.py` | 4 | AlertEvaluator EARNINGS_SOON: held-vs-not-held priority, live-executor-only held-position source |
| `tests/crypto/test_crypto_telegram.py` | 2 | `TelegramAlerter.fill()` reason line included/omitted; dup-alert throttle |
| `tests/shared/test_liveness.py` | 7 | LivenessTracker: touch/is_alive/staleness boundary, simulated hang |
| `tests/shared/test_stuck_loop.py` | 10 | `StuckLoopDetector` — generic "same operation keeps failing" watchdog: threshold, success reset, escalation cadence, key independence, TTL prune, alerter-fault-tolerance, `failing_keys()` |
| `tests/stock/test_ai_engine_timeout.py` | 2 | nvidia_nim client built with `timeout=_TIMEOUT_S`; empty `completion.choices` degrades to HOLD not TypeError |
| `tests/stock/test_ai_failover.py` | 16 | Mistral provider + auto-failover: `AI_PROVIDER=mistral`, switch after `_FALLBACK_AFTER`(5) API failures, `nvidia_nim` as failover target, one-way per process, parse-error exemption; recoverable failover (`_revert_to_primary()` on a dead fallback, re-arms) |
| `tests/stock/test_earnings_cache.py` | 4 | Earnings-fetch cache: failures use 1h TTL, successes 24h, `_yf_lock` serialization |
| `tests/stock/test_yf_client_retry.py` | 4 | `fetch_with_retry`: generic exceptions retried with a short delay, max_attempts, rate-limit path unchanged |
| `tests/stock/test_research_aggregator_timeout.py` | 1 | Per-source research-fetch timeout: earnings 45s vs news 15s |
| `tests/crypto/test_kraken_retry.py` | 4 | `bot/exchanges/retry.fetch_with_retry`: no-retry success, retry-and-recover, raise last after exhaustion, custom params |
| `tests/crypto/test_shadow_signal_retry.py` | 3 | `shadow_signal.shadow_replay` Kraken fetch wrapped in `fetch_with_retry` |
| `tests/shared/test_unified_dashboard.py` | 9 | `_read_gate_stats`/`_gate_tracker_section` shadow-match-rate parsing (bounded regex, N/A handling); `_crypto_card` STALE-vs-NO-FILLS badge |
| `tests/stock/test_stock_position_mark_refresh.py` | 4 | REAL `_mark_positions_to_market()` — breaker trips from a price move alone, silent within limit, None-executor no-op, source guard |
| `tests/stock/test_sl_tp_watcher_audit_log.py` | 16 | `_check_open_positions_sl_tp` behavior + "N/M positions priced" audit log + rejected-SL/TP-exit `else` branch (`logger.error` + `StuckLoopDetector`) + native-stop wiring (2026-09-12: `sync_protective_stop` called every cycle at the exact SL price, no-op when the executor lacks it, broker-triggered fills alerted and checked before the price-based decision, **and called independently of get_live_price() succeeding — a yfinance outage must not also disable broker-side protection**) |
| `tests/crypto/test_grid_stress_test.py` | 14 | `grid_stress_test.py` pure helpers (research tooling): crash-period parsing, buy-and-hold P&L, PASS/MARGINAL/FAILED classification |
| `tests/crypto/test_grid_dca_experiment.py` | 12 | `grid_dca_experiment.py` standalone engines (research tooling): grid fills/reopens/floor-stop, capital split, fee math, DCA averaging + cycle restart |
| `tests/stock/test_stock_momentum_experiment.py` | 14 | `stock_momentum_experiment.py` (research tooling — NOT the live pipeline): cross-sectional 6-1 momentum rotation. FAILED (see strategy-search note) |
| `tests/stock/test_stock_mean_reversion_experiment.py` | 20 | `stock_mean_reversion_experiment.py` (research tooling): Bollinger/RSI + short leg on daily stock candles. FAILED |
| `tests/crypto/test_mean_reversion_experiment.py` | 20 | `mean_reversion_experiment.py` (research tooling): Bollinger(20,2σ)/RSI(<35)/ADX(<20) "buy the chop". FAILED on BTC + SOL |
| `tests/crypto/test_rescreen.py` | 13 | `rescreen.py`: `_crypto_usd_whitelist()`, the USD leg (`SCREEN_QUOTE=USD` → `## crypto-usd` section), `RESCREEN_SKIP_USD`, `_alert()` nested-config bugfix, crypto-CAD edge-decay via a separate `SCREEN_SYMBOLS` re-validation run |
| `tests/shared/test_telegram_retry.py` | 3 | `TelegramAlerter._send()` retry: no-retry on healthy, recover on transient, warn-only after exhaustion |
| `tests/stock/test_ai_health.py` | 8 | `_update_ai_health()`: below-threshold silence, trip-at-3, no re-alert, recovery + counter reset, healthy path no-op; source guards (only on a cycle with ≥1 AI attempt; NOT wired into either heartbeat's `healthy_fn`) |
| `tests/crypto/test_dashboard_renderer.py` | 8 | `bot/dashboard/renderer.py` `write_multi()` multi-symbol combine: shared page shell, position-protection panel scoped to the holding symbol, single-symbol `write()` wrapper equivalence, parent-dir creation |
| `tests/stock/test_rules_log_visibility.py` | 2 | Source guard: `run()` `logger.info()`s the per-symbol `📐 RULES:` decision line with the symbol name embedded |
| `tests/stock/test_universe_refresh.py` | 28 | Top-movers universe refresh: `_load/_persist_movers` round-trip, source guards (first-LIVE-cycle-of-day trigger, transient-failure protection), `_prune_dead_movers` (None or <26 candles for 3 cycles), intraday re-rank cadence (`_MOVERS_REFRESH_INTERVAL_S`, `refreshed_at` persistence) |
| `tests/crypto/test_mtf_gate_alert.py` | 2 | Source guards: MTF (1D BEARISH) veto fires **MTF GATE BYPASSED** alert only in the no-cached-closes branch |
| `tests/crypto/test_blocked_buy_alert.py` | 7 | `_evaluate_blocked_buy_alert`: edge-triggered on (symbol, gate), no re-alert while blocked, re-alert on gate change, clears when raw signal stops being BUY, source guard |
| `tests/crypto/test_buy_signal_alert.py` | 6 | `_evaluate_buy_signal_alert`: edge-triggered Telegram heads-up the moment the raw strategy signal turns BUY (before gates/execution), no re-alert while BUY, resets + re-alerts on a fresh BUY episode, missing-price clause, wired into `run()` ahead of the blocked-BUY alert |
| `tests/stock/test_blocked_rule_buys_alert.py` | 10 | `_evaluate_blocked_rule_buys_alert`: end-of-cycle debounced digest, edge-triggered on the `{symbol: gate}` mapping, `_BLOCKED_BUY_ABSENT_CYCLES_TO_CLEAR=3` debounce, all-clear message, source guard |
| `tests/stock/test_trade_csv_parsing.py` | 8 | `paper_report._row_to_trade` (shared by `accuracy_tracker.load_trades`): clean row, header/junk reject, **unquoted-comma-in-`reason` recovery** (>9 cols → rejoin), bad `confidence` never zeroes `price`/`shares` (2026-09-07 RY phantom -$842 regression), missing-confidence default, end-to-end RY round-trip = +$6.32 |
| `tests/stock/test_weekly_monitor.py` | 17 | `stock_bot/analysis/weekly_monitor.py` (report-only): verdict tiers (EARLY/EDGE_FAILING/EDGE_WEAK/THROUGHPUT_STALLED/NEEDS_ATTENTION/ON_TRACK), EARLY suppresses stalled, throughput needs a prior run, severity ordering, log-scan fault-vs-noise bucketing + time window, render sections, `run()` writes report + baseline, `--quiet` suppresses ON_TRACK, `main()` exit code on fault |

---

## Current Live Configuration

For the *why* / incident history behind any feature below, see `CLAUDE_HISTORY.md`
("CLAUDE.md trim, 2026-09-01" and earlier dated entries) and `.memory/decisions/*.md`.

### Active .env — backtest/validation (do not change without re-running validation)
```
ADX_THRESHOLD=18
RSI_FILTER_ENABLED=true
MIN_EMA_SPREAD_PCT=0.004
VOLUME_K=0
STOP_LOSS_PCT=0.015          # fallback only — ATR_SL_MULT takes priority when set
TAKE_PROFIT_PCT=0.10         # SOL/CAD + default
TAKE_PROFIT_PCT_BTC=0.20     # BTC/CAD only — 10% was capping trend winners (exit-logic
                             #  research 2026-09-03; per-symbol via _exit_overrides_by_base())
ATR_SL_MULT=2.0              # adopted live 2026-07-17, walk-forward validated
ATR_SIZING_ENABLED=true      # adopted live 2026-07-17, caps qty at fixed-SL-baseline dollar risk
BACKTEST_LIMIT=5000
BACKTEST_TIMEFRAME=4h
EXCHANGE=binance
SYMBOL=BTC/USDT
```

### Live trading .env (Kraken — separate from backtest)
```
EXCHANGE=kraken
SYMBOL=BTC/CAD
CANDLE_MINUTES=240            # 4h — the only validated live timeframe (1h FAILED walk-forward)
RISK_PER_TRADE_PCT=0.10       # capital-allocation dial, NOT % risked — real dollar risk ~0.15% of cash
STOP_LOSS_PCT=0.015           # fallback only — ATR_SL_MULT=2.0 takes priority when ATR is available
TAKE_PROFIT_PCT=0.10          # SOL/CAD + default
TAKE_PROFIT_PCT_BTC=0.20      # BTC/CAD only (per-symbol exit — see "Per-symbol exit config" below)
ORDER_TYPE=limit / LIMIT_ORDER_ENABLED=true   # BUY entries limit-chase for maker rate (post-only);
                              # ALL SL/TP exits forced to market via urgent=True
UNIVERSE_WHITELIST=BTC/CAD,SOL/CAD
MAX_SLOT_CASH_CAD=77          # BTC/CAD slot cap
MAX_SLOT_CASH_CAD_SOL=376     # SOL/CAD per-symbol slot cap
MAX_CONCURRENT_POSITIONS=2
STARTING_CASH=553.39
NATIVE_STOP_LOSS_ENABLED=true
TELEGRAM_CONTROL_ENABLED=true
MONITOR_SYMBOLS=BTC/CAD,SOL/CAD
```

**Post-only param bug** (live 2026-06-22 → fixed 2026-08-26): `_place_limit_order()` sent
`{"timeInForce": "PO"}` which Kraken rejects, silently falling back to market/taker fees on
every BUY entry + non-urgent SELL for 2+ months. Fixed to `{"postOnly": True}` (ccxt unified
param → Kraken `oflags=post`), verified against real ccxt source + a Kraken `validate=true`
round-trip. Monitoring addendum (2026-08-27): 4 post-only→market fallback paths in
`_place_limit_order()` now set `self._maker_fallback_reason` → `execute()` fires a **MAKER
FALLBACK** `alerter.error()` post-fill. Full detail: `CLAUDE_HISTORY.md`.

### Risk-gate config (crypto RiskManager — `bot/risk/risk_manager.py`)
```
RISK_MAX_POSITION_PCT=0.20    # BUY blocked if it would push position above 20% of slot value (module default 5%)
RISK_DAILY_LOSS_LIMIT=0.01    # halt new BUYs if portfolio down >1% from today's UTC-midnight open
RISK_MAX_DRAWDOWN=0.05        # DRAWDOWN-HALT — down >5% from all-time peak. Not sticky — auto-lifts on recovery
RISK_MAX_TRADES_PER_DAY=5     # hard cap on BUY fills/calendar day (per-symbol; SELL not capped)
COOLDOWN_TICKS=6              # state-machine cooldown between a fill and the next signal eval
RISK_HALT_BLOCKS_STOPS=false  # config.py default. false = SL/TP exits still fire during a manual halt
RISK_WEEKLY_LOSS_LIMIT=0.05   # config.py default. Down >5% from ISO-week UTC-Monday open. Not sticky
RISK_DRAWDOWN_WARNING=0.03    # config.py default. Non-blocking — Telegram alert once per episode
RISK_KILL_SWITCH=0.15         # config.py default. Down >15% from all-time peak. STICKY — persisted to
                              # logs/risk_state.json, does NOT auto-clear (edit kill_switch_tripped=false to resume)
```
SELL is never blocked by any breaker. Check order in `RiskManager.evaluate()` (most severe
first): HALT → KILL_SWITCH → MAX_DRAWDOWN → WEEKLY_LOSS → DAILY_TRADE_CAP → DAILY_LOSS →
POSITION_SIZE. `peak_value`, `week_open_value`, `kill_switch_tripped` persist in
`logs/risk_state.json`. Config validation enforces
`RISK_DRAWDOWN_WARNING < RISK_MAX_DRAWDOWN < RISK_KILL_SWITCH` strictly increasing.
Four-tier breaker upgrade added 2026-08-07 (mirrors the stock bot's 2026-08-05 upgrade).

### Per-symbol exit config (crypto — added 2026-09-03)
`config._exit_overrides_by_base()` scans `.env` for `TAKE_PROFIT_PCT_<BASE>` /
`TRAILING_STOP_PCT_<BASE>` / `TRAILING_STOP_ACTIVATION_PCT_<BASE>` (same `_<BASE>` pattern as
`MAX_SLOT_CASH_CAD_<BASE>`). `BacktestConfig.exit_params_for(symbol)` merges any override for
that base over the shared `TAKE_PROFIT_PCT` / `TRAILING_STOP_PCT` / `TRAILING_STOP_ACTIVATION_PCT`.
**Both `bot/main.py` (live, `_ep` per loop iteration) and `engine_kwargs_from_cfg(cfg, symbol=)`
(validation) route through it**, so a symbol's live exits always match its walk-forward.
- **Live:** `TAKE_PROFIT_PCT_BTC=0.20` (BTC sustains trends — a flat 10% capped its winners;
  exit-logic research 2026-09-03: TP20 beats TP10 in **both** walk-forward windows, BTC/USDT
  TRAIN PF 1.20→1.37, VAL 2.78→3.41, and 5 of 6 rolling windows tested). **SOL keeps the 10% TP**
  — every wider-TP / trailing-stop variant made SOL worse (choppier price action).
- `backtest.py`'s `--stop_loss` / `--take_profit` now default to `None` (only an explicit CLI
  value overrides the per-symbol resolution — the old `default=cfg.backtest.*` always clobbered it).
- `validate_symbol.py` / `screen_universe.py` pass `symbol=` so a screened candidate's exit
  params resolve for ITS base, not the configured symbol's.
- No strategy-hash impact (exit params are `cfg.backtest`, not the hashed strategy files).
- Research: `strategy_exit_sweep.py` + `logs/strategy_exit_sweep_20260902.md`,
  `CLAUDE_HISTORY.md` "Crypto exit-logic research — 2026-09-02".

### Limit-chase duplicate-order guards (crypto — fixed 2026-09-11)
Code review found two real gaps in `_place_limit_order()` (`bot/execution/live_executor.py`)
where an ambiguous exchange response could lead to a duplicate live order:
1. **Submission exception → blind market fallback.** An exception raised by `create_order()`
   means the *response* was lost (network timeout, connection drop) — it does NOT mean Kraken
   never received the *request*. The old code fell straight to a market order regardless,
   risking a double fill if the original limit order had actually gone through. Fixed:
   `_find_untracked_entry_order()` checks `fetch_open_orders()` for a matching resting order
   first (same "adopt, don't duplicate" pattern as `_adopt_untracked_stop()`) — if found, it's
   adopted and polled like a normal placement; only a genuinely empty result falls back to
   market, same as before.
2. **Cancel-timeout retry with no terminal-status check.** After a chase timeout, the old code
   cancelled the order, then only checked whether it had *filled* before allowing a retry — it
   never checked whether the cancel actually reached a terminal state. An order still reading
   back `status="open"` (cancel silently ignored, or eventual consistency) let the loop place a
   **second** live order on top of the still-resting first one. Verified via a regression test
   run against the pre-fix code: this **placed 5 separate live orders** in one chase (one per
   retry attempt, `max_retries=3` → 4 total attempts + retries). Fixed: `_CANCELLED_TERMINAL_
   STATUSES` (`canceled`/`cancelled`/`closed`/`expired`/`rejected`) gates the retry — anything
   else aborts the chase without re-placing, identical to the existing "unverifiable state"
   branch.
+3 tests (each verified to fail against the pre-fix code, reproducing a real duplicate-order
scenario), suite 911→914. Execution-layer only — no `bot/strategy/` change, fingerprint unaffected.

### Native exchange-side stop-loss (crypto — ON since 2026-08-15)
`NATIVE_STOP_LOSS_ENABLED=true` (config.py default false). `sync_protective_stop()` rests a
real Kraken stop order (`params={"stopLossPrice": X}`, market on trigger) after every BUY
fill, at the SL price `bot/main.py` computed. Usually static, no mid-trade repricing.
Cancelled the moment the bot closes the position itself. Order id/price persist in
`logs/live_state_BTC_CAD.json`, reconciled on every restart (still-open kept as-is;
saved-but-gone cleared; naked held position gets a same-startup fallback). Placement/cancel
failures alert but never raise. A native trailing-stop path exists for when `ss['atr_sl']==0`
(dormant — `TRAILING_STOP_PCT=0`, ATR SL always available live). `PARTIAL_TP_PCT` unset.

**Deadlock incident 2026-08-27 (FIXED):** a resting native stop reserves 100% of the base
asset on Kraken, so every SL/TP SELL failed `EOrder:Insufficient funds` in a retry loop.
`LiveExecutor.execute()` now cancels any resting native stop *before* placing a SELL (all
three exit paths); a rejected SELL triggers `_rearm_native_stop_after_failed_sell()` (static
level restored, or a "NAKED POSITION" alert for trailing). Full detail + all the
restart-seeding / quantity-mismatch / untracked-order gap fixes: `CLAUDE_HISTORY.md`,
`.memory/execution_layer.md`.

### Native broker-side protective stop (stock bot — added 2026-09-12, IBKR only)
Code review finding: the stock bot's only stop-loss protection was `_check_open_positions_
sl_tp` (`stock_bot/main.py`) — an in-process thread polling yfinance every 30s. If the bot
process died, hung, or lost its TWS connection, a real open position sat completely
unprotected, unlike the crypto bot's native exchange-side stop (above, live since 2026-08-15).
Fixed with the IBKR analog: `IBKRExecutor.sync_protective_stop(symbol, stop_price)` places a
real resting `StopOrder` (`orderType="STP"`, GTC) via ib_async, confirmed against a real paper
API session (2026-09-12) — the trigger price lives in `Order.auxPrice`, **not**
`Order.stopPrice` (a different, unrelated order shape).

**Scope (v1, deliberately narrower than the crypto version):** static stop only, no trailing —
matches the stock SL/TP watcher, which has no trailing-stop concept either. `StockPaperExecutor`
is untouched (paper trading has no broker to place a real stop with; its own in-process watcher
IS its protection).
- `sync_protective_stop()` is called every `_check_open_positions_sl_tp` cycle (not just at BUY
  time) at the exact same stop price the in-process check itself would trigger on
  (`avg_cost * (1 - effective_stop_pct)`), guarded by `hasattr(executor, "sync_protective_stop")`
  so paper trading takes no code path here at all. A restart with an open, unprotected position
  gets covered within one 30s cycle — no separate startup reconciliation pass needed.
- **The exchange is always the source of truth, never in-memory tracking alone**: before placing
  anything, it queries `openTrades()` live for an existing resting STP-SELL on that symbol and
  adopts it if the price/quantity already match (no-op — avoids cancel/replace churn every
  cycle), replaces it if the level changed, or places fresh if none exists. This is what makes
  the "no separate reconciliation pass" claim above safe: a restart's first cycle naturally
  adopts whatever was already resting rather than duplicating it.
- **Cancel-before-sell** — the exact deadlock class the crypto bot hit 2026-08-27 (a resting
  protective order racing the bot's own exit): `sell()` cancels any resting native stop for that
  symbol *first*, inside the same `_position_lock` that also fixed the concurrent-sell finding,
  before ever placing its own market sell.
- **Broker-triggered fill detection** — `check_native_stop_fills()`, called once per SL/TP-watcher
  cycle *before* the price-based check touches the same positions. Without this, a stop firing
  independently of the bot noticing would self-correct the share count on the next
  `positions_snapshot()` read (always live) but leave a real accounting gap: no CSV row, no
  realized-P&L update, no fill notification — the same failure class as the 2026-09-12
  partial-fill finding, just via a different order. Reason recorded as `NATIVE_STOP_HIT`.
- **Ambiguity is never auto-resolved** — more than one resting STP-SELL found for a symbol logs
  an error and touches neither, mirroring the crypto bot's multi-stop-ambiguity philosophy.
- Not persisted to `ibkr_state.json` — a live `Trade` object isn't JSON-serializable, and a
  fresh `IB()` connection after a restart gets new subscription state regardless; the live
  `openTrades()` query above is the reconciliation mechanism, not a saved order id.

**Companion fix, same finding — price-guard fail-open:** `get_live_price()`
(`stock_bot/data/intraday_price.py`) rejected *any* price deviating >20% from previous close,
including a genuine crash — since this backs the SL/TP watcher, a real sharp fall silently
disabled stop-loss protection exactly when it mattered. Fixed: a deviant price is now checked
against `fast_info`'s `day_high`/`day_low` (a separately-fetched price-history field, not the
same live-quote value as `last_price` — genuine independent corroboration, not re-reading the
same suspect number) with a 2% after-hours tolerance. A real crash's `last_price` falls inside
the day's own low (that feed moved too) and is kept; a corrupted read usually lands nowhere near
the day's actual range and is still rejected. Fails toward the old conservative reject if
`day_high`/`day_low` are unavailable or the lookup itself fails.

+17 tests total (9 `IBKRExecutor` native-stop unit tests, 3 `_check_open_positions_sl_tp`
wiring tests, 5 price-guard tests), suite 918→935. Both require a stock bot restart.

### Currency-aware cash check + accurate fill reporting (stock bot — fixed 2026-09-12)
Two Medium findings from the same code review, same day.

**Currency mismatch in the BUY affordability check:** `IBKRExecutor.buy()` compared
`shares × price` (the security's OWN currency) directly against `self.cash` (always
base-currency CAD) — for a USD stock this understated the real CAD cost needed by the
USD/CAD rate (~1.35-1.40×), so a BUY could pass the cash check yet still be unaffordable in
CAD terms. `_price_in_cad()` already existed and is used everywhere else this comparison
matters (`total_value()`) — this was the one spot still comparing mismatched currencies
directly. Fixed: `est_cost = shares * self._price_in_cad(sym, price)`. +3 tests (USD-short
rejects, USD-sufficient-after-FX passes, CAD-quoted unaffected by the rate).

**Fill notifications reported the request, not the fill:** `stock_bot/main.py`'s BUY/SELL
notifier/print/log calls (all three call sites — the main scan loop's BUY, its SELL, and the
SL/TP watcher's own SELL) used the pre-order signal price and requested share count, not
`order.price`/`order.quantity` — so a partial fill or slippage between the signal price and
the real fill made the Telegram alert, console output, and P&L math disagree with what
actually happened. Root cause ran deeper than main.py: `StockPaperExecutor.buy()`/`sell()`
set `order.status = FILLED` but never updated `order.price`/`order.quantity`/`order.total_value`
away from the values passed into `_new_order()` at construction — `IBKRExecutor` already did
this correctly (`order.quantity = filled_qty; order.price = fill_px`), `paper.py` did not, so
patching only main.py would have "fixed" IBKR while leaving paper trading subtly wrong in a
different way (its own slippage model — `_fill_price()`, `_slippage_bps` — was already being
silently discarded from the returned order). Fixed at the source in both executors (mirroring
IBKR's existing three-line pattern: quantity, price, and `total_value` recomputed together —
the dataclass computes `total_value` once in `__post_init__`, so it goes stale too if only
`price` is updated), then all three `main.py` call sites read `order.quantity`/`order.price`/
`order.total_value` instead of the request. +2 tests proving the paper-executor fix directly
(non-zero slippage bps, confirmed to fail against the pre-fix code: old code returned the
exact pre-slippage request). Suite 935→940. Requires a stock bot restart.

### Second-pass review found real bugs in the SAME-DAY fixes above (2026-09-12)
An independent review of the six 2026-09-12 fixes (native stop, duplicate orders, partial
fills, concurrent sells) found that several had genuine gaps — verified against the actual
code before touching anything, same discipline as the original review. Worth stating plainly:
the first pass introduced new bugs while fixing old ones, most seriously an inverted-sign P&L
that a hermetic test accidentally masked. All confirmed and fixed same day; both bots restarted
after. Full list:

- **Native-stop P&L used a cost basis that was already gone (High).** `check_native_stop_fills()`
  queried `positions_snapshot()` for the avg_cost — but by the time a SELL stop is `isDone()`/
  filled, the position it closed is already gone from the broker's position list, so this
  returned `(0.0, 0.0)` and **inverted the sign of every native-stop P&L**. Reproduced: 10
  shares @ $60 stopped at $54.80 reported **+$548 instead of −$52**. The original test for this
  exact path passed anyway — its hermetic FakeIB never shrinks its static position list after a
  fill (a limitation already known and documented for the concurrent-sell fix, but not applied
  here where it mattered most). Fixed: `avg_cost` is now cached in `self._native_stops[sym]` at
  placement/adoption time, while the position still genuinely exists, and read back from there
  — never re-queried after the close. The regression test was rewritten to explicitly zero out
  the fake's position list before checking the P&L, so this class of bug can't hide again.
- **Ambiguous stop-lookup was read as "nothing exists, place one" (High).** `_find_resting_
  native_stop()` returned `None` for BOTH "confirmed zero resting stops" and "query failed /
  multiple found" — and `sync_protective_stop()` treated any `None` as "safe to place a new
  stop". Reproduced: two existing stops became three. Fixed: a distinct sentinel,
  `_NATIVE_STOP_LOOKUP_AMBIGUOUS`, for the unsafe case — only a confirmed-empty result may ever
  place an order. `_cancel_native_stop()` (used ahead of an executor-initiated sell, where the
  goal is "clear everything, not just the one exactly-matched stop") was split onto its own
  `_all_resting_native_stops()` query so it cancels every match found, not just the single-match
  case `_find_resting_native_stop()` is scoped to.
- **Cancellation wasn't confirmed before replacing or proceeding (High).** `_cancel_trade_and_
  wait()` was fire-and-forget — callers proceeded to place a replacement stop (or, for `sell()`,
  proceed with the sell) whether or not the cancel actually landed, risking two live orders both
  able to sell the same shares. Fixed: it now returns a bool: confirmed only if `trade.isDone()`
  by the end of its wait window. `sync_protective_stop()`'s replace path aborts (retries next
  cycle) rather than placing a second stop on an unconfirmed cancel; `sell()`'s cancel-before-sell
  still proceeds regardless per its existing best-effort contract, but now logs loudly (`logger.
  error`, not `warning`) when the cancel didn't confirm, instead of silently continuing.
- **`sync_protective_stop()` didn't share `sell()`'s per-symbol lock (High).** The whole point of
  the 2026-09-12 concurrent-sell fix was one lock per symbol guarding every position-mutating
  operation — but `sync_protective_stop()` (called independently every SL/TP-watcher cycle, not
  from `sell()`) never acquired it, so it could run concurrently with an executor-initiated sell
  on the very same symbol. Fixed: `sync_protective_stop()`, `_cancel_native_stop()`, and
  `check_native_stop_fills()` (per-symbol) all now hold `_position_lock(sym)`. Since `sell()`
  already holds this lock when it calls `_cancel_native_stop()`, `StockExecutorBase._position_
  lock()` was changed from `threading.Lock` to `threading.RLock` (reentrant) — a plain Lock would
  have deadlocked the instant one method called into another on the same thread. Proven with the
  same overlap-counter technique as the original concurrent-sell fix, across two real threads.
- **`sync_protective_stop()` was gated behind a live yfinance price (Medium/High in practice).**
  In `_check_open_positions_sl_tp`, the call was placed AFTER `if get_live_price(symbol) is None:
  continue` — but it only needs `avg_cost` (from `positions_snapshot()`, the broker's own data),
  not the live price. A yfinance outage disabled broker-side protection at exactly the moment
  it's supposed to compensate for a degraded in-process check. Fixed: moved before the
  `get_live_price()` call, decoupled entirely from yfinance availability.
- **Crypto: an empty open-orders list can't rule out an order that already filled (High).**
  `_find_untracked_entry_order()` only checked `fetch_open_orders()` — an order that fully
  filled and closed between the submission exception and the recovery check is, correctly, no
  longer "open", so the old check read this as "nothing to adopt" and placed a second market
  order on top of an already-filled position. Fixed properly, not just patched: every limit
  placement attempt now carries a fresh `clientOrderId` (a UUID, sent as Kraken's `cl_ord_id` —
  offline-verified against the real installed ccxt that it coexists with `postOnly`). On a
  submission exception, the recovery check searches for that exact id across BOTH open and
  closed orders, resolving the order's fate definitively instead of guessing from order shape.
- **Stock: the 15s cancel-timeout still returns without confirmed cancellation (High, accepted
  as a known residual gap, not fully closed).** After a fill-timeout cancel, `_place_market_
  async()` waits up to 15s then returns regardless of whether `trade.isDone()` ever became true.
  A later fill on a still-live order past that point is not captured. Fully closing this would
  mean tracking unresolved orders across cycles the way `check_native_stop_fills()` already does
  for native stops — a real, understood follow-up, deliberately not built same-day on top of
  everything else above (today's own mistakes were reason enough for caution about rushing
  another new tracking mechanism). What WAS done: the silent return is now a loud `logger.error`
  naming the symbol, so this state is investigable instead of invisible.
- **ATR look-ahead bias in the (currently disabled) backtest sizing path (lower urgency).**
  `stock_bot/backtest/engine.py`: the entry fill (at a candle's OPEN) computed its own ATR stop
  distance using `highs[:i+1]` — including that same candle's own high/low/close, which aren't
  actually known yet at the moment of filling at its open. Confirmed live trading is unaffected
  (`stock_bot/main.py`'s `data.get("atr")` is always from an already-completed prior candle by
  BUY time — this was purely a backtest-simulation gap). Since `PAPER_ATR_SIZING_ENABLED=false`
  today, nothing live changes, but the 2026-08-23 ATR-sizing validation run (AMD/KO failing)
  was run against the biased engine and should be re-run before that result is trusted for any
  future decision to re-enable ATR sizing. Fixed: `highs[:i]` (strictly before the fill candle).
- **Strategy finding, NOT changed:** `Regime.VOLATILE` returns `Signal.HOLD` unconditionally
  (`bot/strategy/indicator_strategy.py`), suppressing the strategy's own SELL signal during a
  volatile regime — existing positions rely entirely on SL/TP/native-stop protection to exit
  during that window, not a trend-reversal signal. Confirmed as designed behavior baked into the
  walk-forward-validated strategy fingerprint, not a bug — changing it would touch `bot/strategy/`
  and invalidate every current fingerprint/ACTIVE status per the Validation Discipline rules
  above. Left alone deliberately; flagged here for visibility, not as an open item.

+8 tests for the execution-layer fixes (2 ambiguous/unconfirmed-cancel, 1 lock-sharing overlap
proof, 1 yfinance-outage sync, 1 crypto already-filled-and-closed adoption, 1 ATR look-ahead,
existing native-stop P&L test strengthened to actually exercise the bug), suite 940→946. Both
bots need a restart.

### Third-pass review found MORE bugs in the SAME native-stop feature (2026-09-12)
A third review pass, checking the fixes above, found two more real bugs in native-stop —
same feature, third round in a row. Worth being direct about the pattern: this feature keeps
producing new high-severity findings each time it's looked at more carefully, because it was
built and self-tested quickly across several same-day passes. Given the user's explicit
choice to keep patching rather than simplify or disable it, both were fixed properly this
time — plus two smaller, unrelated findings from the same review, and one real bug in the
LIVE, real-money crypto strategy.

- **A stop that fills during its own cancellation could be replaced against a closed
  position (High).** `_cancel_trade_and_wait()` returned a plain bool based on `trade.isDone()`
  — true for BOTH "cancelled" and "filled". `sync_protective_stop()`'s replace path read any
  truthy return as "safe to place a new stop", so a stop that filled (closing the whole
  position) while being cancelled — racing the cancel — was replaced with a fresh stop
  against a position that no longer existed. Reproduced: a new 10-share SELL stop placed
  after the original had already closed all 10 shares. Fixed: `_cancel_trade_and_wait` now
  returns a tri-state outcome — `"cancelled"` / `"filled"` / `"unconfirmed"` — and only
  `"cancelled"` allows a replacement. A `"filled"` outcome is handed to a new shared helper,
  `_record_native_stop_fill()`, instead — recording the fill (using the cost basis captured
  before the race, see next item) rather than pretending nothing happened.
- **The round-3 cost-basis fix still had a race, just moved (High).** Round 3 fixed the
  known 0-cost-basis bug by caching `avg_cost` — but it re-queried `positions_snapshot()`
  *after* `placeOrder()` returned, which is still late if the new stop fills immediately.
  Reproduced the identical +$548-instead-of-−$52 sign inversion a second time, via an
  immediate rather than a later fill. Fixed properly this time: `held` and `avg_cost` are
  now captured together, ONCE, at the very top of `sync_protective_stop()` — before any
  cancel or place operation — and that single captured value is reused everywhere in the
  call, never re-queried. `_record_native_stop_fill()` (used by all three fill-recording call
  sites now: routine `check_native_stop_fills()`, a fill discovered mid-cancel by
  `sync_protective_stop()`, and one discovered mid-cancel by `_cancel_native_stop()` ahead of
  a sell) logs loudly and records nothing, rather than guessing, when no cached cost basis is
  available at all (e.g. a stop adopted from a prior session).
- **Native-stop fills during bot downtime are permanently invisible (High, acknowledged, not
  fixed).** `_native_stops` is in-memory only. A stop that fills while the bot is offline is,
  after a restart, neither resting (so the live-query restart adoption won't find it) nor in
  the fresh empty dict — `check_native_stop_fills()` has nothing to inspect. The broker's own
  position still self-corrects (`positions_snapshot()` always reflects live state), but that
  fill's P&L/CSV row is permanently missed, not just delayed. Closing this needs persisted
  order tracking plus startup reconciliation against the broker's execution history (with
  duplicate-record protection against fills the normal `sell()` path already captured) — a
  real feature, deliberately not built same-day on top of everything else here. Documented in
  `check_native_stop_fills()`'s own docstring as a known residual gap.
- **FX rate lookup could raise instead of using its own advertised fallback (Medium).**
  `get_usd_cad_rate()` (`stock_bot/data/price_feed.py`) — the exact same lazy-`fast_info`
  gotcha already fixed in `get_live_price()` (`intraday_price.py`, earlier the same day) was
  present in this second, separate function and missed at the time: `fast_info` was fetched
  inside `fetch_with_retry`, but its lazy `.last_price` access happened outside it, where a
  rate-limit/network failure propagates uncaught past this function's documented graceful
  fallback — able to interrupt sizing/affordability checks. Fixed the same way: both accesses
  now happen inside the same retried lambda.
- **Stock: re-entry on the same daily signal after a stop-out (Medium, a policy question, not
  fixed).** The live scan loop re-evaluates yesterday's still-current daily candle every
  cycle; `executor.position(symbol) == 0` is the only re-entry gate, so a stop-out that closes
  a position intraday lets the SAME unchanged daily BUY signal re-fire later the same day,
  subject to the other risk gates. The daily backtest structurally can't reproduce this (it
  only ever evaluates once per candle), so this behavior has never been backtested either
  way. This is a real design decision — allow same-day re-entry, or gate on "already acted on
  this candle's signal" — not a bug to silently patch; needs an explicit decision and its own
  backtest before either behavior can be called validated. Not addressed.
- **Strategy bug in the LIVE, real-money crypto strategy: falling MACD momentum could be
  classified as rising (High).** `bot/strategy/indicator_strategy.py`'s `_last_macd_hist` was
  only updated inside `_trend_signal()`, *after* its own ADX/regime-EMA rejection checks could
  already return HOLD — so a candle rejected by ADX never updated the tracked value, and the
  next passing candle's "is momentum rising?" check compared against a stale pre-rejection
  value instead of the immediately preceding real one. Reproduced with histogram values
  1→5→3 (middle candle ADX-rejected): the old code read the third candle's 3 > 1 as "rising"
  despite real momentum having fallen 5→3, and would fire a false pullback BUY. Fixed by
  moving the MACD computation and history update into `evaluate()`, unconditionally on every
  completed candle — mirroring how RSI's own `_last_rsi`/`_prev_rsi` history already worked
  correctly (updated before any gate, including the VOLATILE-regime early return). Both bots
  share this exact strategy file, so the fix applies to crypto and stock identically.
  **Re-validated 2026-09-12 per this repo's own Validation Discipline** (any `bot/strategy/`
  change invalidates every fingerprint until walk-forward is re-run): new hash
  `5c6540eccbd2f45f`. BTC/USDT came back byte-identical to the pre-fix baseline on every
  number checked (pinned window 27/1.87/40.7%, walk-forward TRAIN 1.37/VALIDATION 3.41) — the
  bug is real but didn't happen to change any trade decision within BTC's validated windows.
  SOL/USDT shifted slightly (walk-forward TRAIN 1.68 vs the old 1.49, VALIDATION 1.84 vs the
  old 1.98) — the fix did change at least one SOL decision — but both numbers still clear the
  gate comfortably. Stamped via `stamp_strategy.py`. **Both bots need a restart to run the
  fixed strategy code** — unlike the execution-layer fixes above, this changes live BTC/CAD
  and SOL/CAD signal generation itself, not just order handling.

+5 tests (2 native-stop race reproductions — each confirmed to fail against the pre-fix
code — 1 FX-fallback reproduction, 1 dedicated `IndicatorStrategy` unit test reproducing the
exact 1→5→3 scenario with indicator internals mocked to isolate the control-flow bug), suite
946→950.

### Generic stuck-loop detector (crypto + stock — BUILT 2026-08-27)
`bot/alerts/stuck_loop.StuckLoopDetector` — error-string-agnostic "same operation keeps
failing" watchdog. `record(key, ok, detail)`; `threshold`(5) consecutive failures → one
`alerter.error()`, re-alert every `re_alert_every`(20); any success resets; idle keys pruned
after `ttl_s`(1h). Crypto: wired into `bot/main.py`'s primary `execute()` path +
`failing_keys()` feeds the health digest. Stock: `stock_bot/main.py` scan-loop buy/sell +
`_check_open_positions_sl_tp` (which also gained a previously-silent rejected-exit `else`
branch). Still open: remote-reachable dashboards (currently local HTML files).

### Daily health digest (crypto — BUILT 2026-08-27)
`bot/main.py._maybe_send_health_digest()` — once/day at `HEALTH_DIGEST_TIME` (local, default
`08:00`; `off`/`0`/`false` disables), scheduled via `_audit_due()`, tracked under
`"health_digest"` in `logs/audit_state.json`. One `alerter.message()` covering both bots
(crypto status + open Kraken orders + stock snapshot + 24h ERROR counts). `⚠️ NEEDS
ATTENTION` header on: manual halt, tripped kill-switch, any `exit_fail_count > 0`, stale
candle feed, ≥20 errors/24h, or any `stuck_detector.failing_keys()`.

### Slippage guard (crypto — post-fill alert, on by default)
```
MAX_SLIPPAGE_PCT=0.01   # config.py default (1%). 0 = disabled.
```
`LiveExecutor.execute()` compares every live fill against the signal-evaluation price,
direction-aware (only unfavorable counts). Post-fill only, never blocks. Every real fill logs
its delta at INFO; a Telegram alert fires only above the threshold. Complements
`shadow_signal.py`'s daily retrospective fidelity check.

### Candle watchdog — real circuit breaker (crypto — always on)
While the feed is stale (no new candle for 2× `CANDLE_MINUTES`, 8h at 4h), new BUYs are
blocked (`ss['candle_feed_stale']`, in-memory) — SELL/exits untouched (independent live-tick
feed). Alerts fire once on each stale↔fresh transition. No config flag.

### Live-only BUY overlays — audited 2026-09-02
`bot/main.py` layers extra BUY vetoes on top of the validated `IndicatorStrategy`. Three
were reviewed after "why won't the bot trade":
- **Removed:** the independent "regime gate" (old `bot/main.py` section 2e). It re-checked
  ADX ≥ threshold AND EMA spread ≥ `min_ema_spread_pct` using the *same* `strategy.last_adx`
  / same closes the strategy already gates on — so it could never flip a strategy BUY, and
  its `"regime"` blocked-gate label collided with the strategy's own 200-EMA filter (cost
  time in the 2026-08-18 investigation). Deleted as dead code; strategy hash unchanged (not
  a `bot/strategy/` file). "regime" as a blocked-gate label now means only the 200-EMA /
  VOLATILE path.
- **Backtested, kept:** the **MTF 1D-BEARISH veto** (section 2c). `mtf_overlay_backtest.py`
  (engine gained opt-in `mtf_daily_closes` / `fng_by_date` params, default-off, fingerprint
  verified byte-identical) shows it's regime-dependent — helps a little in the 2022 bear
  (BTC PF 1.47→1.50), hurts in the 2024–26 bull/chop (BTC PF 2.10→1.48). Roughly a wash
  over a cycle; kept because it's genuine bear protection and has blocked only 1 live signal
  ever. Report: `logs/mtf_overlay_backtest_20260902.md`.
- **Removed (2026-09-02, user-approved):** the **Fear&Greed > 75 / BTC-funding veto**
  (old `bot/main.py` section 2d, `bot/signals/external_signals.py`, `config.ExternalSignalsConfig`,
  `cfg.signals`, `EXT_FNG_*` / `EXT_FUNDING_*` env keys — all deleted). Net-negative or wash
  in every backtest window, 0 live vetoes ever, and it cost a third-party API dependency
  (alternative.me) + a fail-open bypass-alert path. Funding was already dead (Kraken spot).
  `bot/backtest/engine.py`'s opt-in `fng_by_date` param stays — it replays the old gate for
  research only.
- **Untouched:** the 200-EMA macro regime filter inside the strategy — it IS in the
  validated fingerprint.

### Two-way Telegram control (crypto — built + enabled 2026-08-20)
`TELEGRAM_CONTROL_ENABLED=true` (config.py default false). `bot/alerts/telegram_control.py`
(`TelegramCommandPoller`) long-polls `getUpdates` in its own daemon thread. Commands:
`/status_crypto`, `/pause_crypto`, `/resume_crypto`, `/status_stock` (read-only),
`/help_crypto`. Unauthorized `chat.id` and unrecognized commands are silently ignored.
`/pause_crypto`/`/resume_crypto` only touch `logs/HALT` (the same flag the tick loop polls —
no parallel path). `/status_crypto` is structurally read-only (no `LiveExecutor` import in
any command body).

**Shared-token constraint — read before adding a second poller:** `TELEGRAM_BOT_TOKEN`/
`TELEGRAM_CHAT_ID` are shared with the stock bot's outbound `TelegramAlerter`. Telegram's
`getUpdates` `offset` is server-side per-token — **exactly one process may ever run a
`TelegramCommandPoller` against this token** (today the crypto bot only). Stock-bot two-way
control, if added, must route through this poller or use a second dedicated token. Also in
the module docstring. Full detail: `CLAUDE_HISTORY.md`.

### Risk-gate config (stock bot — `StockPaperExecutor` / `IBKRExecutor`)
Both executors implement the same tiers independently. All tiers block new BUYs only;
SELL/exits are never blocked.
```
PAPER_MAX_EXPOSURE_PCT=1.0         # SET in stock_bot/.env (config.py default 0.25). History 0.25→0.45
                                    # →0.60→0.85 (2026-08-27) →1.0 (2026-08-31, "use all the amount"
                                    # — paper track-record bot, idle cash generates no trades). At
                                    # PAPER_RISK_PCT=0.12 (2026-09-07), ~8 full positions = 100% invested.
                                    # ZERO cash buffer accepted, eyes open. Does NOT apply to the real-money crypto bot.
PAPER_RISK_PCT=0.12               # SET in stock_bot/.env (config.py default 0.10). 0.20→0.12 (2026-09-07)
                                    # — the exposure ceiling × 0.20 saturated the book at ~5 fat positions and
                                    # blocked weekly rule BUYs on MAX_EXPOSURE; smaller positions → ~8 fit →
                                    # ~60% more concurrent trades toward the 30-round-trip live gate. Tail risk
                                    # ~unchanged (8×0.12×5% ≈ 5×0.20×5%). Benefit ramps in ~4wk as fat
                                    # positions recycle. See project_config_tune_2026-08-30 (auto-memory).
PAPER_MAX_POSITIONS=10             # 4→6 (2026-08-31) →10 (2026-09-07, so it doesn't re-bind at ~8 positions)
PAPER_DAILY_LOSS_PCT=0.03          # config.py default. Down >3% from calendar-day open (UTC). Baseline
                                    # (day_open_equity/day_start_iso) persisted + UTC-rolled (unified with
                                    # crypto RiskManager 2026-08-28). Non-sticky, recomputed each call.
PAPER_WEEKLY_LOSS_PCT=0.05         # Down >5% from ISO-week open. Monday-anchored.
PAPER_DRAWDOWN_WARNING_PCT=0.10    # Non-blocking — ops_alert only.
PAPER_DRAWDOWN_HALT_PCT=0.15       # Down >15% from all-time peak. NOT sticky.
PAPER_KILL_SWITCH_PCT=0.20         # Down >20% from all-time peak. Sticky — persisted, edit
                                    # kill_switch_tripped=false in the state file to resume.
```
`peak_equity`, `week_open_equity`, `day_open_equity` all persisted. Config validation
enforces `warning < halt < kill_switch`.

### ATR-based stop distance + risk-capped sizing (stock bot — opt-in, OFF)
```
PAPER_ATR_SIZING_ENABLED=false     # Default OFF — do not enable without a stock_backtest.py walk-forward PASS.
PAPER_ATR_SL_MULT=2.0
```
When enabled: `StockConfig.calc_shares_atr_risk()` caps share count so a stop at `ATR*mult`
never risks more than the flat-5%-baseline; the ATR stop % is stored per-position
(`set_position_stop_pct`, persisted, cleared on full close) and the SL/TP watcher reads it
back. **Validation 2026-08-23 (`validate_atr_sizing.py`): 14/16 RULE_WHITELIST PASS, but AMD
and KO FAIL under ATR×2.0** (AMD full-window PF 1.05 — a regression from its flat-stop PASS).
Flag left OFF. Per-window table + options: `CLAUDE_HISTORY.md`.

### Correlation gate (stock bot — always on)
`stock_bot/risk/correlation.py`, wired into the BUY path (`_check_correlation_gate`). Blocks
a new position whose 30-day daily-return correlation with any open position exceeds
`CORRELATION_THRESHOLD=0.70` (same Pearson math as the crypto gate). Fail-open on missing
data. Zero extra network calls — reuses candle closes the scan cycle already fetched.

### Macro economic event blackout (stock bot — always on)
```
MACRO_BLACKOUT_DAYS=1               # symmetric window (days before AND after). 0/negative disables.
MACRO_EVENT_DATES=<dates>           # user-maintained FOMC/CPI/GDP. Populated 2026-08-30 for rest of 2026:
  # 2026-09-11,2026-09-15,2026-09-16,2026-10-14,2026-10-27,2026-10-28,2026-10-29,2026-11-10,2026-12-08,2026-12-09,2026-12-10
  # CPI Sep11/Oct14/Nov10/Dec10; FOMC Sep15-16/Oct27-28/Dec8-9; GDP advance Q3 Oct29. REFRESH JAN 2027.
```
`macro_calendar.py`. Two date sources: `jobs_report_dates()` (first Friday of month, computed
algorithmically, zero maintenance) + the user-maintained `MACRO_EVENT_DATES` list. Market-wide
(checked before the per-symbol earnings check). Fail-open on any error. Sources:
federalreserve.gov/monetarypolicy/fomccalendars.htm, bls.gov/schedule/news_release/cpi.htm,
bea.gov/news/schedule.

### VIX crisis mode (stock bot — always on)
```
VIX_CRISIS_ENABLED=true          # Default ON.
VIX_CRISIS_THRESHOLD=35.0        # CBOE VIX level. 0/negative disables.
```
`vix_crisis.py` — pure threshold check. `^VIX` fetched once per scan cycle
(`fetch_with_retry`), reuses the same `_regime_ok` flag as the SPY BULL/BEAR/NEUTRAL filter.
Fetch failure fails open. Full BUY block market-wide, not a sizing dial.

### Blocked-BUY alerts (observability)
- **Crypto (2026-08-27):** `bot.main._evaluate_blocked_buy_alert(ss, sym, raw_signal_was_buy,
  block_gate, alerter)` — edge-triggered `alerter.error()` "BUY signal blocked [sym]" when
  the raw strategy signal is BUY but an external gate (state_machine/capital_pool/risk_manager/
  correlation/candle_watchdog/mtf_trend/regime) holds it. One per fresh
  (symbol, gate). Not persisted. Called from `run()` section-7b after the CSV write.
  (`external_signal` label retired 2026-09-02 with the Fear&Greed gate.)
- **Crypto BUY-signal heads-up (2026-09-07):** `bot.main._evaluate_buy_signal_alert(ss, sym,
  raw_signal_was_buy, price, alerter)` — edge-triggered `alerter.message()` "🔔 BUY signal [sym]"
  fired at raw-strategy-signal time, *before* gates/execution, so a BUY is announced even during
  a limit-chase or an "already holding" filter. One per fresh BUY episode (resets when the raw
  signal stops being BUY). Called from `run()` section-2b (ahead of the blocked-BUY alert); the
  fill alert / blocked-BUY alert then report the outcome.
- **Stock (2026-08-27):** `stock_bot.main._evaluate_blocked_rule_buys_alert` — end-of-cycle
  debounced `ops_alert` digest listing every symbol whose rule BUY a gate held (MACRO/
  EARNINGS_BLACKOUT, REGIME_SKIP, VIX_CRISIS, MAX_EXPOSURE/MAX_POSITIONS, CORRELATION,
  SIZE_SKIP). Edge-triggered on the `{symbol: gate}` mapping;
  `_BLOCKED_BUY_ABSENT_CYCLES_TO_CLEAR=3` debounce so a symbol flapping near a gate alerts once.

### Weekly stock-bot progress monitor (BUILT 2026-09-07)
`stock_bot/analysis/weekly_monitor.py` — report-only. Once a week, reads the live
logs/state (no network, no TWS) and returns one verdict: `NEEDS_ATTENTION` /
`EDGE_FAILING` / `EDGE_WEAK` / `THROUGHPUT_STALLED` / `EARLY` / `ON_TRACK`. Tracks
Gate 3 progress (round-trips vs 30, net-of-commission PF, win rate, pace), whether the
2026-09-07 sizing change is recycling the fat positions, drawdown/kill-switch, and a
7-day log scan (faults vs noise, blocked rule-BUYs by gate). Writes
`logs/weekly_monitor_<date>.md` + `logs/weekly_monitor_state.json` (week-over-week
baseline); `--send` relays a summary via the stock `AlertNotifier.ops_alert` (Telegram).
**It never trades, edits config, or commits** — strategy/whitelist/capital stay
human-gated. Schedule: `make install-monitor` (launchd, Mon 17:30 local) — logic is in
the repo, only the schedule is machine-local (one command to re-install after a machine
move). Full doc: `deploy/WEEKLY_MONITOR.md`.

**CSV parser fix, same change:** `paper_report._row_to_trade` is now the shared
trade-CSV row parser (used by `accuracy_tracker.load_trades` too). A hand-backfilled
RY SELL row in `ibkr_trades.csv` had an unquoted comma inside `reason`, over-splitting
it to 10 columns; the old per-reader try/except then zeroed `price`/`shares` when the
shifted `confidence` failed to parse — turning RY's real +$6.32 round-trip into a
phantom −$842.20 / −100% loss feeding `paper_report` and `LiveTradingGate.check_gate3`
(masked only because gate 3 is still PENDING at n<30). Fixed: the row is quoted in the
CSV, and `_row_to_trade` rejoins over-split `reason` + coerces each numeric field
independently. Post-fix book: 7 round-trips, net PF 0.62, −$3.11/trade (was showing
0.04 / −$124).

### Settlement date + FX-rate tax record-keeping (stock bot)
`paper_trades.csv` / `ibkr_trades.csv` are UNCHANGED (9-column schema frozen). New data goes
into `paper_trades_settlement.csv` / `ibkr_trades_settlement.csv` (columns `timestamp, symbol,
side, settlement_date, fx_rate_at_trade`), joined by `(timestamp, symbol, side)`. Written on
every fill, best-effort. `settlement_date` is T+1 skipping weekends only (no holiday
calendar). `fx_rate_at_trade` is `1.0` for CAD symbols, live USD/CAD otherwise. Data capture
only — no ACB/gain computation, no CRA report (descoped 2026-08-05; still paper trading).

### IBKR executor readiness hardening (stock bot — 2026-08-27, gap closed 2026-09-11)
`IBKRExecutor._account_value()` / `positions_snapshot()` cache last-good and serve it on a
transient TWS failure (was a fabricated `0.0`/`{}` → every BUY rejected / SL/TP watcher
blind). `_note_sync(ok)` flips `executor.sync_healthy` on the edge → edge-triggered
`ops_alert`. `_record_trade()` CSV append buffers a failed row (`_unwritten_csv_rows`) and
retries on the next fill; `executor.csv_write_healthy` False while buffered. Order-timeout
path left as-is (already alerts + the cancel-race grace window records a beating fill).

**Live gap found + fixed 2026-09-11:** the last-good cache above only guarded a *raised*
exception. `accountValues()`/`positions()` are local reads of ib_async's own cache, not
network calls — on a stale/disconnected client they return an empty list *without raising*
(ib_async clears its local cache on disconnect), which sailed past the `try` as a "successful"
read and **overwrote the good cache with the empty one**. Live incident: TWS was quit
(testing IB Gateway/IBC, see below) and relaunched; the running bot's own executor reported
`cash=$0.00` / 0 positions for ~26 minutes across the reconnect gap despite this exact cache
existing to prevent that — self-healed the moment the periodic reconnect succeeded, no fills
missed, but the SL/TP watcher was genuinely blind to all 5 real positions for that window.
Fixed: both methods now check `self._ib.isConnected()` inside the same async call and raise
if not, routing a stale-but-non-raising connection through the identical cache-preserving
path as a thrown exception. +2 tests (each verified to fail against the pre-fix code), suite
909→911. Requires a stock bot restart to take effect — running process still has the old code.

**Second gap found + fixed 2026-09-12 (code review):** `_place_market_async()`'s wait loop
exited the instant ANY fill appeared — even a partial one — while the order kept working the
unfilled remainder on the broker. `_execute()` logged "IBKR PARTIAL FILL" and returned that
partial quantity as if the trade were complete: no cancellation of the remainder, no
continued tracking, so a later fill on the same order was never recorded anywhere (a real
accounting gap between `ibkr_trades.csv` and the broker's actual position). Fixed: the loop
now only exits on a genuine terminal state (`trade.isDone()`) or the fill deadline, regardless
of partial-fill amount; a timeout with the order still live — whole or partially filled —
cancels the remainder and waits for its actual fate, same conservative philosophy as the
crypto bot's cancel-race handling. The Error-10349 flicker/resubmit grace window (RY/BNS
incidents) intentionally keeps its original "any fill resolves it" exit — it's narrowly
watching for a known resubmit-then-fill pattern, not a normal working order, and reusing
`isDone()` there would trip on the flicker's own leftover 'Cancelled' status before the
resubmit ever resolved (caught by a regression during this fix — `test_flicker_cancel_
then_fill_is_recorded` failed until the scoping was corrected). +2 tests (each verified to
fail against the pre-fix code — old behavior recorded 2 of 4 shares and never called
`cancelOrder` on the stalled remainder), suite 914→916. Requires a stock bot restart.

### Concurrent-sell race across both stock executors (fixed 2026-09-12, code review)
The background SL/TP watcher (`stock_bot/main.py:_check_open_positions_sl_tp`, its own thread,
~30s poll) and the main strategy scan loop can both decide to exit the same symbol at nearly
the same moment. Neither `StockPaperExecutor.sell()` nor `IBKRExecutor.sell()` had a lock
around the full read-position → validate → submit-order → update-state sequence — `IBKRExecutor
._state_lock` only ever protected the realized-P&L increment, a few lines *after* the
unprotected position check and broker order. Two near-simultaneous exits could both read the
same held-shares figure, both pass the "enough shares to sell" check, and both submit a sell,
overselling the real position.

Fixed with a shared `_position_lock(symbol)` helper on `StockExecutorBase` (per-symbol, lazily
created via `dict.setdefault` — atomic under the GIL, no subclass `__init__` change needed, and
unrelated symbols never serialize against each other). Both `sell()` implementations now wrap
their entire body in it; `IBKRExecutor`'s holds the lock across the real broker round-trip in
`_execute()` on purpose — a second sell on the same symbol must wait for the first to actually
resolve, not just queue behind an in-memory increment. `buy()` was left unchanged — only one
code path (the main scan loop) ever calls it, so it has no concurrent-caller risk today.

Verified deterministically, not by timing luck: both new tests wrap the first read inside the
critical section with an artificial delay and an overlap counter, then run two real threads —
proving directly that the second call never enters the critical section while the first is
still inside it (confirmed to fail against the pre-fix code on both executors, overlap counter
hit 2). The `StockPaperExecutor` test additionally confirms the actual business outcome (one
FILLED closing the position, one REJECTED, never both filling the same shares) since its
fully in-memory book — unlike the hermetic IBKR test's static fake position list — genuinely
updates after a fill. +2 tests, suite 916→918. Requires a stock bot restart.

### LiveTradingGate — stock bot IBKR readiness check (repaired + code-enforced 2026-08-20)
`stock_bot/analysis/accuracy_tracker.py`. `IBKRExecutor.__init__()` on a live port with
`allow_live=True` calls `LiveTradingGate().evaluate()` and raises `ValueError` naming every
gate that is neither PASS nor SKIPPED, unless Gates 1-3 all report PASS/SKIPPED (before any
TWS connection). Paper-mode callers never reach it.
- **Gate 1** — every current `RULE_WHITELIST` symbol has `verdict: PASS` in
  `logs/stock_backtest_latest.json`. **Status: 16/16 PASS** (re-run 2026-08-28 — AMD now
  passes, small-sample window-boundary effect, was 15/16 on 2026-08-20).
- **Gate 2** — AI confidence-band edge: ≥10 completed MED/HIGH-confidence (80+) round-trips,
  ≥55% win rate. **Status: SKIPPED** (2026-09-10 fix — `check_gate2()` now returns SKIPPED,
  not PENDING, whenever `AI_ENABLED=false`. AI was disabled 2026-09-09 (sustained provider
  failures — see "AI provider" below), so a pure-rules bot can never accumulate MED/HIGH AI
  trades again; leaving it PENDING would have permanently code-blocked go-live on a gate the
  user had already accepted dropping. SKIPPED counts as resolved everywhere PASS does
  (`get_gate_status()`, `print_gate_status()`, the `IBKRExecutor` enforcement check) —
  re-enabling AI makes Gate 2 a live requirement again automatically, no code change needed.
- **Gate 3** — position book: ≥30 completed round-trips, PF≥1.2, win≥30%, all three.
  **PENDING (7/30 as of 2026-09-10)** — the only gate actually standing between the stock bot
  and IBKR real-money go-live now.
- Gate 4 (infrastructure importability) deliberately excluded from enforcement.
Full repair trail: `.memory/decisions/livetradinggate-gate-repair-2026-08-20.md`.

### AI provider — stock bot
**Primary: `mistral`** (`AI_PROVIDER=mistral`, `MISTRAL_MODEL=mistral-small-latest`, free
"Experiment" tier, `MISTRAL_API_KEY` in root `.env`, 2s rate-limit spacing). Swapped to
primary 2026-08-27 after `meta/llama-3.1-8b-instruct` hit EOL and the interim nvidia swap
was a slow parse-failing reasoning model.
**Failover target: `nvidia_nim` / `NVIDIA_MODEL=openai/gpt-oss-120b`** (re-probed 2026-09-01;
`deepseek-v4-pro` and others are dead — use `verify_nvidia_models.py` to re-check).
`AI_FALLBACK_PROVIDER=nvidia_nim`.

Auto-failover (`stock_bot/ai/ai_engine.py`): after `_FALLBACK_AFTER=5` consecutive API
failures **or** sustained parse failures, `_switch_to_fallback()`; a fallback that itself
racks up 5 failures triggers `_revert_to_primary()` (2026-09-01 fix — was one-way/one-shot,
stranded on a dead fallback for hours). `_fallback_active` resets on revert. AI is
advisory-only (`RULE_TRADING_ENABLED=true`) — zero trading impact through any of this.

`_update_ai_health()` (`stock_bot/main.py`): at 3 consecutive fully-failed cycles (majority
of attempted calls must succeed) fires an edge-triggered `notifier.ops_alert()`.
**Deliberately NOT wired into either heartbeat's `healthy_fn`** — a degraded advisory
provider must not misreport "the bot is down". `verify_nvidia_models.py` (repo root) is the
standing model-hunt tool. Full saga: `CLAUDE_HISTORY.md`.

### Crypto dashboard — multi-symbol combine (2026-08-26)
`bot/dashboard/renderer.py` rewritten around `write_multi(path, exchange, strategy, tick,
symbols: list[dict], ...)` — one shared page shell wrapping one content block per symbol
(SOL/CAD had zero visibility after its promotion). `bot/main.py`: `tick_log` entries carry a
`"sym"` tag; sticky display values in `symbol_state[sym]['dash_*']`; `_render_dashboard(sym,
...)` re-renders the full page from `_dash_snapshots`. `unified_dashboard.py` unchanged.

### Stock bot RULES-decision log visibility (2026-08-26)
The per-symbol `📐 RULES: BUY/SELL/HOLD` + RSI/ADX/trend/regime line is now
`logger.info("RULES [%s]: ...", symbol, ...)` with the symbol name embedded (was
`print()`-only, no log evidence for "why isn't the bot buying X").

### Stock bot scan universe + top-movers refresh
`UNIVERSE_SIZE=45` top-movers scanned per cycle on top of the ~28 `WATCHLIST` symbols (raised
15→30 on 2026-08-27, 30→45 on 2026-09-07, scan breadth only — the rule criteria + in-distribution
screener are unchanged, `interval=1d` untouched). Refreshed on the **first LIVE scan cycle of each day**
(2026-08-27 fix — the old `hour==16` gate was unreachable), re-ranked every
`UNIVERSE_MOVERS_REFRESH_HOURS` (default 2h) during market hours (2026-08-31), persisted to
`stock_bot/universe_movers.json` (`{date, movers, refreshed_at}`, gitignored) across
restarts. `_prune_dead_movers` drops a mover that comes back unusable (`None` or <26 candles)
for 3 consecutive cycles (watchlist + held positions exempt); `_dead_movers` clears per
refresh. Held positions + WATCHLIST are force-scanned every cycle — SELL is never affected by
the movers universe.

`UNIVERSE_REFRESH_HOURS` (plural, =4) = the raw index-constituent-list cache TTL in
`StockUniverse` — NOT legacy. `UNIVERSE_REFRESH_HOUR` (singular, =16) IS the dead clock-hour
key, kept only so `.env` parses.

### Stock bot `regime()` — live gating
`regime()` in `stock_bot/indicators/indicators.py` is live every scan cycle on fresh SPY
closes and directly gates real BUYs via `_regime_ok` (shared with VIX crisis mode). The same
module's `rsi()`/`trend()`/`adx()`/`macd()` are also called live but feed display only. The
actual rule trade trigger is `IndicatorStrategy` in `bot/strategy/indicator_strategy.py`
(imported by `stock_bot/strategy/rules.py`). Audited read-only 2026-08-20 — all 8 functions
pure/stateless, no bug class, no lookahead. `stock_bot/backtest.py` (module) is DEAD TOOLING;
the load-bearing gate is root `stock_backtest.py` → `stock_bot/backtest/engine.py` (package).

### How to verify the config is active
Run: `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py`
Expected (rolling, drifts as the window advances): **~29 trades, PF ~2.4–2.5, ~38% win
rate**, hash `5c6540eccbd2f45f`. If `RSI_FILTER_ENABLED=false` accidentally: trade count
jumps, PF drops below 1.2. **Use the pinned-window check below for a deterministic pass/fail.**

Reproducible pinned-window check (deterministic — data range fixed):
```
EXCHANGE=binance SYMBOL=BTC/USDT BACKTEST_SINCE=2024-03-07 BACKTEST_UNTIL=2026-06-20 python backtest.py
```
Expected: **27 trades, PF 1.87, 40.7% win rate** (5010 candles), hash `5c6540eccbd2f45f`.
Use the rolling run for the canonical fingerprint, this pinned run for "did my
environment/data change break something".

### Canonical strategy fingerprint (BTC/USDT)
- **Strategy hash:** `5c6540eccbd2f45f` — changed 2026-09-12 (was `b30f2f9e769c8d41`), a
  genuine `bot/strategy/` edit: `IndicatorStrategy` could classify falling MACD momentum as
  "rising" (`_last_macd_hist` was only updated after ADX/regime gates that could already
  return HOLD, so an ADX-rejected candle left it stale — an independent review reproduced
  it with histogram values 1→5→3, the ADX-rejected middle candle causing a false "rising"
  read on the third). Fixed by moving the MACD computation + history update to run
  unconditionally on every completed candle (matching how RSI's own history update already
  worked), before any gate can return early. Full detail: "Second-pass review" section above.
- **Hashed files (behavior-defining only):** `bot/strategy/indicator_strategy.py`,
  `bot/strategy/threshold_strategy.py`, `bot/indicators/indicators.py`
- **Re-validated 2026-09-12, both live symbols pass:**
  - **BTC/USDT — byte-identical to the pre-fix baseline** on every number checked: pinned
    window 27 trades / PF 1.87 / 40.7% win rate; walk-forward TRAIN PF 1.37 / VALIDATION PF
    3.41. The bug is real (proven in the dedicated unit test) but didn't happen to change
    any trade decision within BTC's validated windows.
  - **SOL/USDT — numbers shifted slightly, still solidly passing:** rolling 43 trades / PF
    1.78 / 41.9% win rate (unchanged from before). Walk-forward: TRAIN PF **1.68** (was
    1.49), VALIDATION PF **1.84** (was 1.98) — the fix did change at least one SOL decision
    within the walk-forward window; both numbers still clear the >1.0 gate comfortably
    ("Strong: PF holds above 1.2 out-of-sample").
- Stamped: `python stamp_strategy.py` → `logs/validated_strategy_hash` (done 2026-09-12).
- If the bot or backtest prints `STRATEGY CODE DIFFERS`, re-run walk-forward before trusting any PF numbers.
- **Both bots need a restart** to run the fixed strategy code — this is a `bot/strategy/`
  change, so it affects live BTC/CAD and SOL/CAD signal generation, not just execution.

### Current operational status
- **Crypto bot:** live on Kraken. BTC/CAD ($77 slot) capital gate 0/15 fills (strategy trades
  ~every 3–6 weeks; 65+ days elapsed with zero progress toward fill #1 as of 2026-08-24 —
  genuine variance + ranging regime, strategy faithful, keep waiting). SOL/CAD ($376 slot)
  1/15 fills (BUY 0.080808 @ $134.02 on 2026-08-26 — the fill that surfaced the post-only
  bug; then TP-closed +$1.27/+10.9% on 2026-08-27, first completed round-trip). ATR SL 2.0 +
  ATR sizing live. Telegram (t.me/amaresh_tradebot) + healthchecks.io heartbeat + two-way
  Telegram control live. Native stop-loss ON. All four items from the 2026-08-07 crypto-bot
  gap review closed (native stop, risk tiering, slippage guard, candle-watchdog breaker).
  - **Kraken auth incident 2026-08-15:** every authenticated Kraken call failed
    `EGeneral:Permission denied` for ~4 days (IP restriction / key reset, resolved outside
    the repo). Was invisible to all monitoring. Fixed: `_update_auth_health()` — edge alert +
    heartbeat `healthy_fn` wiring. Full detail: `CLAUDE_HISTORY.md`.
- **Stock bot:** live on IBKR paper (DUQ273338, reset to $5,000 CAD 2026-07-20). Swing book
  retired (`FAST_ENABLED=false`). Position book (rule-based, Mode A/B) is the only active
  book. TSX symbols permanently advisory-only (CIRO — never re-add `.TO` to `RULE_WHITELIST`).
  AI provider mistral (advisory-only). Trades ~1/week — not starved. Circuit breakers (4
  tiers), correlation gate, macro blackout, VIX crisis mode, sector-concentration gate,
  settlement CSV, StuckLoopDetector all live. Restarts often (config changes, incident
  recovery). **User must restart** to pick up the 2026-09-01 AI-failover fix.
- **Both bots:** crash-alert + atomic state writes + SIGTERM graceful shutdown + liveness
  tracking all live.

---

## Live Symbol Universe

### Approved for live trading
| Symbol | Status | Basis |
|--------|--------|-------|
| BTC/CAD | ACTIVE | Walk-forward re-confirmed 2026-09-03 on current code with `TAKE_PROFIT_PCT_BTC=0.20` (per-symbol exit): TRAIN PF 1.37 / VALIDATION PF 3.41, all windows PF > 1.0. Original validated pair. |
| SOL/CAD | ACTIVE (2026-08-25) | Fresh 3-window walk-forward PASS on current strategy (TRAIN PF 1.32 / VALIDATION PF 1.46, ATR×2.0 stop + dollar-risk-capped sizing — `logs/atr_oos_SOL_2.0_sized_20260825.md`); capital verified live ($553.39 CAD, `check_kraken_balance.py`); FX precondition N/A (direct CAD-quoted market). `.env`: `MAX_SLOT_CASH_CAD_SOL=376`, `MAX_CONCURRENT_POSITIONS=2`, `STARTING_CASH=553.39`. Trail: `.memory/decisions/multi-symbol-validation.md`. |

### Watchlist (not yet tradeable)
| Symbol | Status | Reason |
|--------|--------|--------|
| XRP/CAD | WATCHLIST | Walk-forward fails on current Mode A/B strategy (87% SL-exit rate). Re-verified 2026-08-26: still fails (5000c PF 0.99, 3000c PF 0.50); Kraken liquidity also now narrowly fails on spread (0.18% vs 0.15% max). Re-entry: full 3-window pass on current code. |

### Blocked (walk-forward failed)
| Symbol | Status | Reason |
|--------|--------|--------|
| ETH/CAD | BLOCKED | Walk-forward fails all windows; no edge over 2024–2026. Re-verified 2026-08-26 (5000c PF 0.67); Kraken liquidity itself is clean — pure strategy-edge failure. |

### Screened out — liquidity gate ($50,000/day)
| Symbol | 24h Vol (CAD) | Note |
|--------|--------------|------|
| DOGE/CAD | $6,228 | Liquidity fails hard (spread 1.01%). Walk-forward actually PASSES the two reliable windows (5000c PF 1.41, 3000c PF 1.42) — blocker is market structure, not edge. Re-check liquidity if Kraken volume recovers. |
| PEPE/CAD | $941 | Liquidity + spread (1.52%) fail. Walk-forward also fails (5000c PF 0.82). |
| XDC/CAD | $25,709 | Under the gate; spread (0.34%) fails. Walk-forward can't be run (no XDC/USDT on Binance). |

### Implementation
- `.env`: `UNIVERSE_WHITELIST=BTC/CAD,SOL/CAD`
- `regime_monitor.py`: `MONITOR_SYMBOLS=BTC/CAD,SOL/CAD` (traded), `MONITOR_WATCHLIST=XRP/CAD` (health metrics only)
- Screen tooling: `screen_universe.py`, run monthly via the in-bot `rescreen.py` scheduler (never auto-changes whitelists — flags decay/new-qualifiers only).

### Current stock bot RULE_WHITELIST
`MRNA,AMD,RY,PLTR,GLD,TD,CM,CSCO,KO,T,CAT,GOOGL,WMT,MSFT,GM,CVX` — all US-listed/API-tradeable
(no `.TO`). Watchlist is a superset including AC.TO, SHOP.TO, BNS, SU (advisory-only, never
rule-buyable — TSX regulatory block). Full screen history: `CLAUDE_HISTORY.md`.

**RULE_WHITELIST no longer gates rule-based BUY entry (removed 2026-08-23, user request —
full-universe trading).** `stock_bot/main.py`'s `_rule_buy` now fires on `rule_v.signal ==
"BUY" and rule_v.warmed_up` alone, for ANY symbol in that cycle's scan universe (watchlist +
top-movers + held positions). `RULE_WHITELIST` still feeds `LiveTradingGate.check_gate1()`
(the code-enforced IBKR readiness gate), but it is no longer the day-to-day paper-bot safety
net. Full detail: `CLAUDE_HISTORY.md`, `.memory/decisions/stock-whitelist-gate-removed-2026-08-23.md`.

**Remaining safety net (post-2026-08-23):**
1. In-distribution ATR%/liquidity filter (`stock_bot/data/screener.py`) — rejects a
   non-watchlist symbol with ATR% > 3× the reference range (~30.8%) or avg $ volume < $50M/day.
   Rejections visible on the dashboard. Held + watchlist symbols exempt.
2. Position sizing — flat notional (`PAPER_RISK_PCT=0.12`, was 0.20 until 2026-09-07). ATR-inverse
   sizing gated behind `PAPER_ATR_SIZING_ENABLED` (still `false` — AMD/KO fail its walk-forward).
3. Risk-gate tiers (see "Risk-gate config (stock bot)").
4. Sector-concentration + correlation gates — generic (live yfinance sector lookups, Pearson
   over fetched candles), no hardcoded mapping.
5. AI shadow-vote review criteria — a documented, not-yet-met trigger for revisiting a lighter
   validation gate (≥15 round-trips outside {MRNA, AMD, RY.TO, PLTR} + a material PF/win-rate/
   AI-agreement gap). Thresholds: `.memory/decisions/stock-whitelist-gate-removed-2026-08-23.md`.

---

## Capital Sizing Rules

### Starting capital
$100 CAD per symbol (general rule). Each live symbol trades independently with its own capital
allocation, trade counter, and sizing tier. Currently: BTC/CAD ($77) + SOL/CAD ($376 — a
documented SOL-specific exception since SOL's volatility × the ATR-risk sizer needs a larger
slot to clear Kraken's order minimum; see `.memory/decisions/multi-symbol-validation.md`).
SOL/CAD's own promotion gate starts from zero live fills, independent of BTC/CAD's.

### First increase — $100 → $250 CAD per symbol (ALL required, on live fills)
- Minimum 15 completed trades on that symbol
- Live profit factor ≥ 1.2
- No single trade loss exceeding 3% of account
- Regime monitor PASS on all metrics for ≥2 consecutive readings before the increase

### Second increase — $250 → $500 CAD per symbol (ALL required)
- Minimum 30 completed live trades on that symbol
- Live profit factor ≥ 1.3 sustained over the last 20 trades
- Maximum drawdown on the live account ≤ 5% at any point

### Hard rules that override everything
- **Never increase capital after a winning streak** — only after the trade-count threshold is met
- **Never increase capital on both symbols simultaneously** — increase one, wait 10 trades, then evaluate the second
- **If live PF drops below 1.0 over any 10-trade window**, reduce to the previous tier immediately
- **Symbol removal must never implicitly increase surviving symbol allocation.** Hard-cap per-slot cash with `MAX_SLOT_CASH_CAD` (`CapitalPool(slot_cap=...)`, `bot/portfolio/capital_pool.py`).
- **`CapitalPool` is a single shared pool split N ways** (`slot_cash = min(total_capital / max_concurrent, slot_cap)`). When adding a symbol, raise `STARTING_CASH` AND `MAX_CONCURRENT_POSITIONS` together — concurrency alone shrinks every slot; capital alone leaves the new symbol no slot.
- **Personal holdings in the same Kraken account are invisible by default.** `ADOPT_EXTERNAL_HOLDINGS=false` (default). Never set true unless you want the bot to trade all account assets.

---

## Exchange Setup
- Backtesting: EXCHANGE=binance, SYMBOL=BTC/USDT (Kraken OHLCV history ~720 candles, Binance 5000+; price diff 0.048% — negligible)
- Live trading: EXCHANGE=kraken, SYMBOL=BTC/CAD
- Kraken API key (Security → API): enable Query Funds / Query Orders / Create Orders / Cancel Orders; disable Withdrawals (never on a bot key); restrict to your IP.

---

## Validation Discipline

**Any commit that touches `bot/strategy/` invalidates all fingerprints and symbol ACTIVE
statuses until walk-forward is re-run and the hash re-stamped.** Changes to config,
execution, risk, data, or tests do NOT invalidate the hash.

### Workflow after a strategy change
1. Edit `bot/strategy/*.py`
2. `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` — confirm PF ≥ 1.72 (current fingerprint floor)
3. `python walkforward.py` — confirm all windows PF > 1.0
4. `python stamp_strategy.py`
5. Update "Canonical strategy fingerprint" above with the new hash + result
6. Re-run walk-forward on every symbol in UNIVERSE_WHITELIST before assuming ACTIVE still holds
   (a symbol validated on strategy version X is NOT automatically valid on version Y)

### Re-entry gate for watchlisted / blocked symbols
Full 3-window walk-forward pass (all windows PF > 1.0) on the CURRENT strategy code. A pass on
an older version does not count.

### Capital gate evaluation (15-fill threshold) — ALL THREE, not just PF
1. **Live PF ≥ 1.2** over ≥15 completed round-trips (net of fees)
2. **Shadow match rate ≥ 95%** — `python shadow_signal.py` (runs daily via the in-bot scheduler, `SHADOW_AUDIT_TIME`)
3. **Fee/slippage within assumptions** — fills within 0.5% of signal-candle close; round-trip cost ≈ 1.20% (0.40% maker BUY + 0.80% taker SELL)

At 15-trade sample sizes: a **failing PF with clean fidelity** = variance, extend to 25–30
trades (don't demote). A **passing PF with poor fidelity** = the live bot may not be executing
the validated strategy — investigate before promoting.

### Why this matters (incident log)
- XRP/CAD: validated on the old RSI < 30 strategy. Mode A/B entry logic was added without
  re-running XRP walk-forward. XRP traded live with real money for weeks on a stale,
  passing-but-now-failed validation before being caught and removed 2026-07-02.

### Deflated Sharpe / CSCV — deferred, deferral stands (re-checked 2026-08-20)
DSR / CSCV-PBO are multiple-testing corrections. Deferred: single/few-symbol scale, no active
multi-parameter grid search. `screen_universe.py` (≤15 symbols monthly vs one fixed strategy
config) IS structurally the same selection-bias mechanism, but stays low-value to formalize —
trial count far below where DSR's correction bites, the pass bar is a real 3-window
walk-forward, and the 15-fill live capital gate is an empirical version of the same idea.
Revisit trigger: multi-parameter grid search *combined with* multi-symbol screening. Full
writeup: `.memory/decisions/expert-practices-benchmark.md`.

---

## Standing Policies

### No day-trading (1h or faster)
1h walk-forward FAILED 2026-07-10 (63% SL-exit rate, PF < 1.0 on the two largest windows).
`CANDLE_MINUTES=240` (4h) stays the only validated live timeframe. Don't revisit without a
new/modified strategy (its own fresh walk-forward + hash stamp) or materially more 1h history.

### TSX symbols — permanently API-blocked
CIRO rule DMR 3200 A.1.(b)(i) prohibits IBKR Canada clients from placing orders on Canadian
exchanges via ANY automated system. Regulatory, not a settings fix. Never re-add a `.TO`
symbol to `RULE_WHITELIST` — TSX names may only be watch-listed (advisory) or traded manually in TWS.
**Explicit code guard since 2026-09-02** (`stock_bot/main.py` `run()`): a `.TO` symbol that
produces a rule/AI BUY has `_act_buy` cleared and logs `TSX_BLOCKED` — it never reaches an
IBKR order. This was implicit in `RULE_WHITELIST` (no `.TO` members) until the whitelist
stopped gating BUYs on 2026-08-23; AC.TO then reached IBKR live on 2026-09-02 and bounced off
the broker's 'Inactive' rejection with a false "Order rejected" ops alert. `.TO` names stay
watch-list / top-mover scannable for advisory + AI-training purposes.

### No automated IPO trading
The bots never trade IPOs or recent listings via any special path. New listings earn entry
exactly like every other symbol: accumulate history → screener eligibility → full
`stock_backtest.py` walk-forward PASS → whitelist. No exceptions for famous names. SPCX case
study in `CLAUDE_HISTORY.md`.

### Investment philosophy — two-bucket policy
- **Bucket 1 — wealth building (personal, outside the bots):** low-cost broad index fund,
  regular contributions, hold for decades. The bots are NOT the wealth engine.
- **Bucket 2 — trading system (this repo):** capped, gate-controlled experiment. Capital
  grows only through the documented fill-count / net-PF gates — never through conviction,
  streaks, or excitement.
- Buffett rule mapping enforced in code: capital protection = risk engine + breakers + slot
  caps · circle of competence = default-deny whitelists + walk-forward gates · patience =
  HOLD through weak regimes (ADX gate) · margin of safety = PF ≥ 1.2 net-of-fee gates + small sizing.

### Sizing-visibility rule (stock bot)
Never "fix" a `SIZE_SKIP` (signal valid but rounds to 0 shares) by raising `PAPER_RISK_PCT`,
adding fractional shares, or special-casing a minimum share count. That bypasses the
margin-of-safety sizing rule. The correct lever is letting the account grow through the Phase
A gate, or not whitelisting symbols unaffordable at the current account size.

### Strategy search — CONCLUDED (2026-08-28/29)
Three candidate second strategies tested (mean-reversion, grid/DCA, cross-sectional
momentum). **None cleared the bar.** Momentum was closest (validation CAGR +43.8% vs SPY
+21.3%, beats SPY on Sharpe 1.42 vs 1.28) but loses to the trivial "hold all 54
equal-weight" benchmark on Sharpe (1.42 vs 1.49) with deeper drawdowns. Consistent finding:
beating a passive diversified hold net of costs is hard — the premise of the two-bucket
policy. Don't re-propose a strategy without beating these baselines. Full record:
`.memory/decisions/strategy-search-2026-08-28.md`, `CLAUDE_HISTORY.md`.

---

## USD Expansion (contingent)

**Status: no qualifying symbols promoted.** Last full USD screen 2026-07-03 (strategy hash
`659d1c03987b72fd`): 603 Kraken USD pairs → 178 cleared liquidity → top 15 walk-forwarded →
zero passed (79–90% SL-exit rate — no edge for the Mode A/B pullback entry on those alts).
Later ATR-stop research (2026-07-16/17) showed SYN and SOL clear the full gate at ATR×2.0–2.5;
SOL was subsequently promoted (CAD pair). SYN/USD, LINK/USD, PUMP/USD remain conditional
candidates. Full tables: `CLAUDE_HISTORY.md`, `.memory/decisions/multi-symbol-validation.md`.

### Preconditions for any USD pair promotion (ALL required)
1. A future screen run produces a 3-window PASS (PF ≥ 1.2 all windows + trades ≥ 10 + SL ≤ 70%)
2. Capital ≥ ~$100 CAD (general Stage-1) for a new slot, without reducing BTC/CAD's
   `MAX_SLOT_CASH_CAD=77` (raise `STARTING_CASH` + `MAX_CONCURRENT_POSITIONS` together).
   **Symbol-specific in practice** — the ATR-risk sizer × exchange unit-minimum interaction
   drives the real number well above $100 for volatile/low-priced pairs: SOL needed
   ~$110–$334 (volatility-dependent), SYN ~$250–$690, PUMP ~$786–$1,618. Re-check per symbol
   against current volatility; `.memory/decisions/multi-symbol-validation.md` has the working.
3. Documented decision on CAD→USD conversion cost + ongoing FX exposure (Kraken ~0.20%/leg;
   USD P&L needs separate tracking from the CAD base). SOL/CAD was exempt (direct CAD-quoted).
4. Full 3-window walk-forward pass on the CURRENT strategy code at promotion time
5. SL-distance-based sizing — built generically (`calc_trade_qty_atr_risk()`, symbol-generic,
   live for BTC/CAD since 2026-07-17). Specifically exercised against SOL 2026-08-24 (still
   HOLDS with the dollar-risk cap applied — `logs/atr_oos_SOL_2.0_sized_20260824.md`).

The old "BTC/CAD live gates met: ≥15 fills" precondition was removed 2026-08-24 — it coupled
a new symbol's promotion to BTC/CAD's unrelated trade frequency (a realistic 1+ year wait)
and was never derived from anything; the evidentiary bar (PF ≥ 1.2, full walk-forward) is
unchanged. Full reasoning: `.memory/decisions/multi-symbol-validation.md`.

### Automated USD re-screen (added 2026-08-24)
`rescreen.py` now runs `screen_universe.py` a second time with `SCREEN_QUOTE=USD`, producing
its own `## crypto-usd` report section (`RESCREEN_SKIP_USD=true` skips it). No USD pair is
whitelisted, so every USD PASS surfaces as a NEW QUALIFIER for a human to look at — same
"never auto-changes a whitelist" rule. crypto-CAD edge decay for the live bases comes from a
separate explicit-symbol `SCREEN_SYMBOLS=<whitelist>` re-validation run (auto-discovery
excludes already-decided bases). `screen_universe.py` now uses the shared
`engine_kwargs_from_cfg()` builder (was hand-listing stale kwargs → validated a more
permissive strategy shape; caught + fixed 2026-08-26, no past promotion was decided on a
false result). Current fresh USD candidate: **PUMP/USD** (PF 1.83–2.04 all 3 windows, clean
liquidity) — informational only. Full trail: `CLAUDE_HISTORY.md`.

### Re-screen triggers
- Strategy code change (new hash after walk-forward) — re-screen all alts
- SL-exit rate cap relaxed
- Automated monthly via `rescreen.py` (both CAD + USD legs; flags decay/new-qualifiers, never auto-changes whitelists)
- Out-of-cycle manual check: `SCREEN_QUOTE=USD python screen_universe.py`

---

## Roadmap (open items only)

| # | Item | Status |
|---|------|--------|
| F | VPS logrotate | Config ready (`deploy/logrotate_trade_bot.conf`, `/opt/trade_bot` path). Nothing left until a VPS exists — migration deferred. |
| G | Stock-bot headless deploy (IB Gateway + IBC) | Scoped + written 2026-08-27 (`deploy/IBKR_GATEWAY_SETUP.md`, `deploy/stock_bot.service`). No bot code change needed (only `IBKR_PORT=7497→4002`). ~4h + a day's observation. Not started — deferred with the VPS migration; the crypto bot moves first. |
| H | Ollama Cloud key revoke | Confirmed unused 2026-07-16; user parked indefinitely — don't re-raise unprompted. |
| I | IBKR live go-live | Gate-blocked. `LiveTradingGate` Gates 1-3 code-enforced in `IBKRExecutor.__init__()`. Gate 1 16/16 PASS; Gate 2 SKIPPED (AI disabled, 2026-09-10 fix — no longer a permanent blocker); Gate 3 PENDING (7/30 live trades) — the only remaining blocker. |
| J | USD symbol re-screen | Automated monthly via `rescreen.py` (now genuinely covers the USD leg as of 2026-08-24). |
| K | ATR SL for SYN/LINK/PUMP | SOL/CAD promoted 2026-08-25. SYN/PUMP/LINK validation-complete but blocked on new capital + an un-built FX-conversion layer (both need a deposit). None promoted. Detail: `.memory/decisions/multi-symbol-validation.md`. |
| — | Crypto capital gate | BTC/CAD 0/15 fills (~3–6 wk/trade — don't force it). SOL/CAD 1/15 fills, 1 completed round-trip. |
| — | Stock Phase A gate | Position book toward 30 completed trades / PF ≥ 1.2 / win ≥ 30% (= `LiveTradingGate` Gate 3). ~5/30. |

Everything else from the original near-term roadmap (swing book, IBKR paper executor,
dashboard work, heartbeat/alerting, held-position visibility, rule-based rebuild) is DONE —
see `CLAUDE_HISTORY.md`.
