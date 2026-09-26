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
trimmed again 2026-09-01, 2026-09-15 and 2026-09-25 when it re-crossed 150k chars — incident
narratives and multi-pass review write-ups moved to `CLAUDE_HISTORY.md` under "CLAUDE.md
trim, 2026-09-01", "CLAUDE.md trim, 2026-09-15" and "CLAUDE.md trim, 2026-09-25").
This file holds only current, actionable state. Consult the history file for the full
narrative behind any decision below, and `.memory/decisions/*.md` for the deepest trails.

---

## Test Suite Manifest

**Expected total: 1775 tests** (`pytest --collect-only -q`, re-counted 2026-09-26 after the 22635de review fixes). If the count
disagrees: a file has an import error, was deleted, was added without a manifest bump, or was
excluded from the runner — investigate before trusting a green suite. Suite runtime ~85s; many
minutes means a test is reading live `.env` config. **The per-row counts in the table below are
stale** (last reconciled at 1051; the ~666 tests added since — mostly the dynamic-universe,
accounting and ledger-observer work of 2026-09-13 → 09-24 — were not bumped row-by-row). Treat the
table as a map of what each file covers, and `--collect-only` as the source of truth for counts.
Count-delta history: `CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-01" → "count-delta history".

Run: `python -m pytest --tb=short -q` — must show **1775 passed**.

| File | Tests | What it covers |
|------|-------|----------------|
| `tests/shared/test_indicators.py` | 30 | RSI, EMA, ADX, MACD, ATR; regime-classification self-referential-ATR-baseline regression |
| `tests/crypto/test_live_executor.py` | 70 | LiveExecutor: dry-run (incl. simulated maker/taker fees + affordability reject), market/limit orders, urgent-exit bypass, fees, state save/load, min-size guard, restart recovery, native static/trailing stop backstop, slippage guard, maker-fallback alert, stop pre-cancel-on-SELL, duplicate-order guards + clientOrderId reconciliation |
| `tests/crypto/test_capital_pool.py` | 37 | CapitalPool: slot allocation, slot cap, per-symbol slot caps (`slot_caps`, `slot_cash_for()`), release, edge cases; `config._slot_caps_by_base()` env scanner; `PortfolioConfig.max_slot_cash_cad_by_base` validation |
| `tests/crypto/test_correlation.py` | 17 | Pearson correlation, pct_returns, fetch_correlation |
| `tests/stock/test_stock_correlation.py` | 5 | `stock_bot/risk/correlation.py`: `fetch_correlation_from_closes` — no-network wrapper reusing the crypto pearson/pct_returns |
| `tests/stock/test_stock_correlation_gate.py` | 8 | `_check_correlation_gate`: blocks on >0.70 correlation with an open position, fails open on missing data, case-insensitive, source guard |
| `tests/stock/test_stock_macro_calendar.py` | 14 | `macro_calendar.py`: `jobs_report_dates`, `parse_user_event_dates`, `is_macro_blackout` (window/boundary/disabled/nearest-event) |
| `tests/stock/test_stock_macro_blackout_gate.py` | 6 | `_is_macro_event_blackout` wrapper: user date / disabled / fail-open / jobs-report-alone / source guard; pinned to a fixed reference date (was contaminated by the live calendar until 2026-08-07) |
| `tests/stock/test_stock_vix_crisis.py` | 6 | `vix_crisis.py`: `is_vix_crisis` — at/above/below threshold, None fails open, zero/negative disables |
| `tests/stock/test_stock_vix_crisis_gate.py` | 2 | Source guard: `run()` fetches `^VIX`, computes crisis mode, gates BUYs via the shared `_regime_ok` flag |
| `tests/stock/test_stock_settlement_csv.py` | 11 | Settlement/FX tax record-keeping: `_next_business_day` T+1, frozen CSV header unchanged, settlement CSV written on BUY/SELL with correct join key, CAD → fx_rate=1.0 |
| `tests/crypto/test_risk_manager.py` | 35 | RiskManager: halt gate, daily loss, position size, SL/TP bypass, state persistence, per-symbol caps, aggregate breakers, kill-switch/drawdown-halt/weekly-loss/drawdown-warning tiers, **kill-switch trip evaluation on every tick regardless of signal (2026-09-14)** — a HOLD-only (or SELL-only) drawdown through the threshold now trips the sticky flag even if it fully recovers before the next BUY, **`mark_valuation()` standalone entry point (2026-09-15)** — proves the kill switch trips from a fresh valuation alone, with zero calls to `evaluate()` |
| `tests/crypto/test_no_new_candle_valuation.py` | 2 | Source guard (2026-09-15): `run()`'s "no new candle" branch — most ticks on a 4h timeframe — must call `risk.mark_valuation(_account_value())` before its `continue`, or a drawdown-and-recovery entirely between two candle closes still never reaches the kill switch even after it checks on every `evaluate()` call, because `evaluate()` itself was never being called on that path |
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
| `tests/crypto/test_main_strategy.py` | 2 | Strategy builder: full config wiring, incl. **`atr_volatile_multiplier` (2026-09-14)** — live `build_strategy()` was omitting it entirely, silently trading `IndicatorConfig`'s hardcoded 1.5 default regardless of `ATR_VOLATILE_MULTIPLIER` in `.env`, while the backtest config builder already passed it correctly |
| `tests/crypto/test_backtest_engine_execution_model.py` | 3 | `bot/backtest/engine.run()` execution model: threshold-mode BUY crash, gap-through SL fills at the open, trailing stop can't activate-and-trigger on the same candle |
| `tests/stock/test_fast_validator_exits.py` | 6 | FastValidator exits: MAX_HOLD live-price fallback, corruption guard, SL regression |
| `tests/stock/test_paper_report.py` | 10 | Expectancy math: IBKR commission model, net-of-cost flip, merged paper+IBKR book, IBKR account section, live-cash-snapshot precedence (row parsing is now `_row_to_trade`, tested separately) |
| `tests/stock/test_exit_policy.py` | 11 | Stock asymmetric exit bars: single-verdict exit, 2-strike SELL streak, streak resets, AC.TO incident regression |
| `tests/stock/test_stock_backtest_engine.py` | 15 | Stock backtest engine: next-open fills, intra-candle SL/TP, gap handling, slippage/commission math, walk-forward gating, optional ATR(14)×mult stop mode, **ATR look-ahead-bias fix (2026-09-12)**: the entry/fill candle's own high/low/close must not feed the ATR sizing its own stop |
| `tests/stock/test_stock_rules.py` | 5 | Rule signals: live==backtest replay parity, drop_last, determinism, validated-parameter pin |
| `tests/crypto/test_audit_scheduler.py` | 14 | REAL `_audit_due()` — daily catch-up, once-per-day, Mon-anchored weekly, monthly 1st-anchored re-screen, missed-run catch-up |
| `tests/crypto/test_limit_chase_recovery.py` | 6 | 2026-07-15 unrecorded-fill regression: market-fallback polling, actual-type amount inference, cancel-race double-fill guard |
| `tests/stock/test_ibkr_executor.py` | 90 | IBKRExecutor (hermetic FakeIB): live-port/paper guards, contract mapping, fills, timeouts, cancel-race, realized PnL, reconnect, FX/NET-LIQ margin guard, sector gate, breaker tiers, ATR stop override, LiveTradingGate enforcement, last-good cache (incl. disconnected-no-exception), CSV retry buffer, Error 10349 grace, partial-fill tracking, concurrent-sell lock, native protective stop (place/replace/adopt/cancel-before-sell/fill detection/ambiguity/tri-state cancel), currency-aware cash check |
| `tests/stock/test_concurrent_sell.py` | 1 | `StockPaperExecutor` concurrent-sell regression (2026-09-12): two threads racing a full-position sell — proves both the overlap invariant (per-symbol lock) and the actual business outcome (one FILLED, one REJECTED, never both filling the same shares) |
| `tests/stock/test_intraday_price_guard.py` | 5 | `get_live_price()`'s previous-close corruption guard (2026-09-12): a genuine crash confirmed by today's own day_high/day_low is no longer discarded; a corrupted read outside that range still is; day-range lookup failure fails toward the conservative reject |
| `tests/stock/test_paper_executor_fill_price.py` | 2 | `StockPaperExecutor.buy()`/`sell()` regression (2026-09-12): `order.price`/`quantity`/`total_value` now reflect the actual slippage-adjusted fill, not the pre-slippage requested price — IBKRExecutor already did this correctly, paper.py did not |
| `tests/stock/test_fx_sizing.py` | 15 | USD/CAD sizing: `is_cad_symbol`, `get_usd_cad_rate`, mixed-currency `total_value`/`check_exposure`, sector-concentration gate, projected-exposure check, **lazy fast_info failure inside get_usd_cad_rate now falls back gracefully instead of raising** (2026-09-12 fix — same class of bug already fixed in `intraday_price.py`, missed here at the time) |
| `tests/shared/test_indicator_strategy_macd_history.py` | 1 | `IndicatorStrategy` MACD-history regression (2026-09-12): an ADX-rejected candle must still update `_last_macd_hist` — reproduces the exact reviewer scenario (histogram 1→5→3, middle candle ADX-rejected) that used to read a real momentum *fall* as "rising" and fire a false pullback BUY |
| `tests/shared/test_backtest_metrics_fees.py` | 3 | `bot/backtest/metrics.compute()` fee-accounting regression (2026-09-12): a $1 gain eaten by $1.608 in fees must count as a net LOSS, not a win — includes a from-scratch recomputation cross-check against the real saved 2026-09-12 BTC/USDT pinned-window CSV confirming net PF ≈0.82 (a documented loss on that window); **partial-exit fee allocation (2026-09-13)**: a position closed via two partial SELLs must split the entry fee proportionally by quantity, not dump 100% onto whichever SELL closes first (dormant against today's live full-exit-only config, real bug if `partial_tp_pct` is ever enabled) |
| `tests/stock/test_screener_in_distribution.py` | 5 | In-distribution ATR%/liquidity filter (`stock_bot/data/screener.py`, replacement safety net after RULE_WHITELIST stopped gating BUYs) |
| `tests/stock/test_accuracy_tracker.py` | 23 | `LiveTradingGate` gates — Gate 1 (`stock_backtest_latest.json` vs `RULE_WHITELIST`, **plus a `strategy_hash` staleness check added 2026-09-13** — a report computed on an old `bot/strategy/` version now hard-FAILs Gate 1 outright, not just a human noticing an old `run_at` date; a report with no hash field at all, pre-dating this fix, falls back to the original symbol-verdict check), Gate 2 (AI confidence-band edge, incl. SKIPPED when `AI_ENABLED=false` — 2026-09-10), Gate 3 (≥30 round-trips/PF≥1.2/win≥30%) |
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
| `tests/shared/test_unified_dashboard.py` | 13 | `_read_gate_stats`/`_gate_tracker_section` shadow-match-rate parsing (bounded regex, N/A handling); `_crypto_card` STALE-vs-NO-FILLS badge; **`_dynamic_universe_card()` (2026-09-13)** — absent-file returns empty, renders eligible/rejected/blocked, stale-scan badge |
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
| `tests/crypto/test_dynamic_eligibility.py` | 20 | `DynamicUniverseScreener` (dynamic-universe, paper-only — see "Dynamic Crypto Universe"): every filter (inactive/stablecoin/leveraged/volume/min-order/spread/depth/history), `max_candidates` cap, duplicate-base dedup across quotes, discovery-failure fail-safe (cache fallback, expired-cache rejection, never-arbitrary empty result), real-ccxt order-book-shape regression (`[price, amount, timestamp]`, not 2 elements) |
| `tests/crypto/test_dynamic_lifecycle.py` | 12 | `DynamicSymbolManager`: admit/warmup wiring, idempotent re-admit, manifest persistence, retire-if-flat vs. keep-if-holding, `sync_to_candidates` retires only flat+dropped symbols, restart recovery from manifest + defensive orphaned-open-position state-file scan |
| `tests/crypto/test_dynamic_ranking.py` | 4 | `rank_buy_signals`: ADX-descending, volume tiebreak, missing-ADX sorts-last-not-dropped, empty list |
| `tests/crypto/test_dynamic_paper_isolation.py` | 5 | Source guards on the retired `dynamic_universe_bot.py` paper runner: `dry_run=True` hardcoded (not config-derived), no `logs/HALT` reference in code, no live `live_state_BTC`/`live_state_SOL` reference, isolated state directory, empty API credentials |
| `tests/crypto/test_dynamic_live_integration.py` | 41 | Live-engine dynamic-universe integration: admission/retirement/sync, `_execute_approved_signal`, ranked BUYs, equity conservation on restart (live fold + paper replay), replay trust/NaN guards, eligibility gate, shadow isolation, dry-run fee lifecycle, construction-site wiring — see "Review passes 4–15" |

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

### Limit-chase duplicate-order guards (crypto — fixed 2026-09-11/12)
`_place_limit_order()` (`bot/execution/live_executor.py`): (1) a `create_order()` exception no
longer blind-falls-back to market — `_find_untracked_entry_order()` adopts a resting order first,
and a fresh `clientOrderId` per attempt lets recovery check open AND closed orders; (2) a
chase-timeout retry is only allowed once the cancel reads back a status in
`_CANCELLED_TERMINAL_STATUSES`, otherwise the chase aborts (pre-fix code placed 5 live orders in
one chase in a regression test). Full detail: `CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-25".

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

### External review fixes (2026-09-26)
- **Secret redaction (P0):** the live Telegram token was in local logs ~106k times —
  `requests` errors embed the URL (`.../bot<token>/getUpdates`), incl. an Aug-21 DNS-failure
  hot loop (~21k lines/min) and a 09:20 2026-09-26 network drop. `bot/alerts/redact.py`:
  `RedactingFormatter` on every root handler of BOTH bots + `redact()` at the Telegram/retry
  call sites; also scrubs any `*_TOKEN/*_SECRET/*_API_KEY/*_PASSWORD` env value. Existing local
  logs scrubbed in place. `logs/` was never committed. **Rotate the token** (BotFather) anyway.
- **Health digest (P1):** a failed open-orders fetch is now "UNKNOWN" + an attention item (was
  silently "none"); `_digest_accounting_attention()` flags missing/in-progress/unreconciled/
  stale (>2×interval+grace) accounting status when `ACCOUNTING_ENABLED`.
- **Accounting mislabel (P1):** a four-way failure was folded into `account_cash_blocked`
  and the report (sharing that state) explained itself after the mutation → every cycle logged
  "account cash unreconciled (… account cash unreconciled ())" although cash had PASSED.
  Now `BlockState.four_way_blocked/four_way_reason` via `_apply_four_way_result()`; BUY blocking
  unchanged. **Real remaining diffs (diagnosed, not "fixed"):** BTC/CAD = the 2026-06-26 external
  deposit of 0.00037766 BTC (see `asset_movement_analysis.py`); SOL/CAD = Kraken **staking
  rewards** (0.0000035775 SOL dust — Aug-28 reward on the bot's 20h hold + weekly accruals,
  proven from the ledger observer's `staking` entries). Both are non-trade ledger movements the
  trade-only position fold can't see — folding them in is a deliberate design decision, pending.
- **Backtest execution model (P1):** `engine.run(fill_model=...)`, default now `"next_open"`
  (strategy orders fill at the next candle's open; SL/TP stay intra-candle, gap-aware).
  Measured: 4h crypto candles open at the prior close (max gap 0.008% over 5000 BTC candles) —
  BTC pinned/rolling results IDENTICAL (27 trades, net PF 0.82 / 29, 1.18), SOL within $0.01.
  `"close"` reproduces the old engine byte-for-byte. Reports now print fill model/fee/slippage.
- **Follow-up review of 22635de (same day):** (1) a `next_open` fill across UTC midnight was
  recorded before RiskManager rolled to the execution date, so the reset erased it from the
  daily trade cap — now `risk.mark_valuation(open value, exec date)` runs first; (2) a pending
  BUY now re-runs `risk.evaluate()` at the actual (slipped-once) fill price + a notional+fee
  affordability check — failing orders are REJECTED, never resized; pending SELLs are never
  re-gated; (3) digest and BUY gate share `_accounting_max_age_ms_for()` = interval + grace
  (digest had 2×interval + grace). Re-ran BTC/SOL pinned+rolling: fills identical to 22635de.
- **Stale reports (P2):** 49 pre-2026-09-12 crypto research reports (42 `logs/`, 7
  `.memory/decisions/`) carry an "OBSOLETE NUMBERS" banner (gross PF, close fills).
- **Not done (P2 refactor of main.py/live_executor.py):** deliberately skipped — restructuring
  money-handling code during a trial period is its own risk; revisit at the 2027-03-12 review.

### TWS-restart duplicate stop + CVX short (stock bot — fixed 2026-09-25)
A mid-session TWS restart (11:58) exposed two IBKR-executor bugs, both "acted on a
disconnected/cached view":
- **Duplicate BNS stop:** `openTrades()` on a dropped socket returns `[]` without raising →
  read as "no stop resting" → the placement step reconnected on its own and placed a 2nd
  stop. Fixed: `_all_resting_native_stops()` raises when disconnected (→ ambiguous → skip),
  and `sync_protective_stop()`'s `_place()` never reconnects.
- **CVX short −3:** `sell()` sized from `positions_snapshot()`'s last-good cache while
  disconnected, then `_execute()` reconnected and sold shares a broker stop had already sold.
  Fixed: new `_live_positions()` (None when unconfirmed, never cached) used by `sell()` and
  `sync_protective_stop()`; `sell()` reconnects first, and rejects unless
  `_cancel_native_stop()` returns `"clear"` (now also rejects on `"filled"` / `"unconfirmed"` —
  previously it proceeded "best-effort"). `positions_snapshot()` keeps its cache for
  display/risk gates only.
- **Missed broker fills — `IBKRExecutor.reconcile_missed_fills()`:** called from the SL/TP
  watcher (self-throttled to 5 min). Compares IBKR's execution report (~1-day window) for
  THIS client's orders against shares already recorded per orderId (`recorded_fills`,
  persisted in `ibkr_state.json`) and writes only the unrecorded remainder as
  `BROKER_FILL_RECONCILED` (P&L via `last_known_cost`). Skips manual/other-client trades,
  still-working orders, tracked native stops, and fills <120s old. First run only baselines.
  The historical CVX + AMZN exits predate it — they need a manual CSV backfill.
+15 tests (4 of the first 6 fail on the pre-fix code). Needs a stock-bot restart.

### Currency-aware cash check + accurate fill reporting (stock bot — fixed 2026-09-12)
- `IBKRExecutor.buy()` affordability now uses `shares * self._price_in_cad(sym, price)` (was
  comparing USD cost against CAD cash).
- Both executors now set `order.price`/`quantity`/`total_value` to the actual fill (paper.py
  previously discarded its own slippage); all three `stock_bot/main.py` notifier call sites read
  `order.*`, not the request. Full detail: `CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-25".

### Review passes 2 through 7 (2026-09-12 → 2026-09-15) — native-stop, fee-accounting, kill-switch
Five more same-week review passes after the initial 2026-09-12 fixes above, each checking the
previous pass's own work — a genuine pattern (native-stop alone got 4 rounds of new bugs found
on each closer look), documented in full in `CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-15".
Current state of everything found:

- **Native-stop (stock, IBKR) — robust after 4 rounds**: cost-basis cached once before any
  cancel/place (not re-queried mid-race), ambiguous-lookup sentinel distinct from
  confirmed-empty, tri-state cancel outcome (cancelled/filled/unconfirmed) gates replacement,
  `sync_protective_stop()`/`_cancel_native_stop()`/`check_native_stop_fills()` share the same
  per-symbol `RLock` as `sell()`, decoupled from yfinance availability. **One acknowledged open
  gap, not fixed**: a stop that fills while the bot is offline is permanently invisible to P&L/
  CSV (in-memory tracking only, no persisted reconciliation against broker execution history) —
  documented in `check_native_stop_fills()`'s own docstring.
- **Crypto duplicate-order guard**: fresh `clientOrderId` per limit-placement attempt lets a
  post-submission-exception recovery check resolve the order across BOTH open and closed orders
  (an already-filled-and-closed order no longer causes a second market order on top of it).
- **Stock 15s cancel-timeout**: still returns without confirmed cancellation on a rare race —
  accepted residual gap, now at least logs loudly (`logger.error`) instead of silently continuing.
- **Fee-accounting fix (`bot/backtest/metrics.py`, round 4)**: `profit_factor`/`win_rate`/etc.
  are now NET of fees, not gross — see "⚠️ PF/win-rate are NET of fees" above for the full
  consequence (BTC/USDT's training window flipped from a gross win to a net loss). Partial-exit
  fee allocation (round 5) now splits the entry fee proportionally by quantity across multiple
  SELLs instead of dumping it all on the first — confirmed dormant (no live position closes via
  partial exits today).
- **Stock Gate 1 staleness (round 5)**: `stock_backtest_latest.json` now carries a
  `strategy_hash` field; `check_gate1()` hard-FAILs on a mismatch instead of silently validating
  stale code. Re-run post-fix: 15/16 PASS (T fails on a low-sample window, same shape as the
  2026-08-20 AMD false-FAIL — left as a real FAIL, not special-cased).
- **Halt-scope correction (round 5)**: `logs/HALT` blocks BUY AND strategy-driven SELL (only
  SL/TP exits are exempt, via `RISK_HALT_BLOCKS_STOPS=false`) — this was always the code's
  actual behavior; only my own prior description of it here was wrong. User's explicit call:
  keep it as the existing full-stop rather than build a narrower BUY-only pause.
- **Kill switch (rounds 6+7) — now evaluates every tick, not just on a BUY/SELL signal**:
  round 6 made the trip check run on every `RiskManager.evaluate()` call regardless of signal;
  round 7 found `evaluate()` itself is only reached on a new-candle tick, so a drawdown-and-
  recovery entirely between two 4h candle closes still escaped it — fixed with a new standalone
  `RiskManager.mark_valuation()`, called from `bot/main.py`'s "no new candle" branch on every
  tick using the already-fetched live price. **Crypto bot restarted 2026-09-14 13:21 for the
  round-6 fix; the round-7 `mark_valuation()` fix (2026-09-15) needs its own restart to take
  effect — confirm this happened before relying on it.**
- **Backtest-engine execution-model fixes (rounds 6+7, validation tooling only, no live-trading
  impact)**: SL/TP fills now respect gap-through risk (`min`/`max` against the candle's open,
  mirroring the stock engine's existing pattern) instead of always filling at the theoretical
  level; `strategy_mode="threshold"` no longer crashes on its first BUY (dead/unused mode); a
  trailing stop can no longer fire on the exact candle that activated it, at a stale
  pre-activation price. Re-ran pinned/rolling BTC and rolling SOL after both fixes together —
  numbers unchanged (no gap-through or same-candle-activation event occurred in the validated
  windows), strategy hash unaffected (`5c6540eccbd2f45f`).
- **MACD-history strategy bug (round 3, LIVE-affecting)**: `_last_macd_hist` wasn't updated on
  an ADX-rejected candle, letting falling momentum read as rising. Fixed in
  `bot/strategy/indicator_strategy.py` — new hash `5c6540eccbd2f45f`, re-validated (BTC
  byte-identical, SOL shifted slightly, both still clear their gates). Both bots restarted.

Suite grew 940→964 across these five passes (+8, +5, +3, +4, +4). Full bug-by-bug detail
(reproduction steps, before/after numbers): `CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-15".

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

### IBKR executor readiness hardening (stock bot — 2026-08-27, gaps closed 2026-09-11/12)
- `_account_value()`/`positions_snapshot()` serve a last-good cache on transient TWS failure; they
  also check `self._ib.isConnected()` and raise if not, since ib_async returns empty lists
  *without raising* on a dead connection (2026-09-11 live incident: ~26 min of `cash=$0.00`).
- `_note_sync(ok)` → `executor.sync_healthy` edge alert; failed `ibkr_trades.csv` appends buffer
  in `_unwritten_csv_rows` and retry (`csv_write_healthy`).
- `_place_market_async()` waits for `trade.isDone()` or the deadline, not the first partial fill;
  a timed-out live remainder is cancelled and its fate awaited. The Error-10349 resubmit grace
  window intentionally keeps "any fill resolves it". Full detail: `CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-25".

### Concurrent-sell race across both stock executors (fixed 2026-09-12)
The SL/TP watcher thread and the main scan loop could both sell the same shares. Fixed with a
per-symbol `StockExecutorBase._position_lock(symbol)` (RLock, `dict.setdefault`) wrapping the
whole `sell()` body in both executors — IBKR's holds it across the broker round-trip. `buy()`
unchanged (single caller). Verified with overlap-counter thread tests. Full detail: `CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-25".

### LiveTradingGate — stock bot IBKR readiness check (repaired + code-enforced 2026-08-20)
`stock_bot/analysis/accuracy_tracker.py`. `IBKRExecutor.__init__()` on a live port with
`allow_live=True` calls `LiveTradingGate().evaluate()` and raises `ValueError` naming every
gate that is neither PASS nor SKIPPED, unless Gates 1-3 all report PASS/SKIPPED (before any
TWS connection). Paper-mode callers never reach it.
- **Gate 1** — every current `RULE_WHITELIST` symbol has `verdict: PASS` in
  `logs/stock_backtest_latest.json`. **Status: 15/16 FAIL — T now fails** (re-run
  2026-09-12, replacing the stale 2026-09-01 report per the fourth-pass review below).
  T's overall verdict is FAIL only because its 250-day window is `low_sample: true` (2
  trades, PF 0.0) — the other three windows all pass (full 1.79, 750d 2.49, 500d 1.13) —
  the same small-sample window-boundary shape as AMD's 2026-08-20 false-FAIL, not
  evidence T's edge actually broke. Left as a real FAIL rather than special-cased away:
  Gate 1 is meant to be a mechanical, unmassaged check. This blocks IBKR go-live (below)
  until either T clears a re-run with more history, or a human decides to drop T from
  `RULE_WHITELIST`. (Was 16/16 PASS 2026-08-28 — AMD.)
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

### ⚠️ PF/win-rate are NET of fees as of 2026-09-12 — every number below changed
`bot/backtest/metrics.py` computed `profit_factor`/`win_rate`/`avg_win`/`avg_loss`/
`best_trade`/`worst_trade` from `FillRecord.pnl`, which is **gross** price-difference P&L —
`position_manager.on_sell()` never subtracted fees, and `engine.py` only deducted them from
cash, never from this per-trade figure. A trade gaining $1 before $1.608 in combined
entry+exit fees was counted as a **winning trade**, and every validation gate that reads
`m.profit_factor` (`walkforward.py`, `screen_universe.py`, `validate_symbol.py`, the
`stamp_strategy.py` floor, ...) was approving strategies on gross trading profit, not real
edge. This had been true since the metrics module's inception — a review pass caught it
2026-09-12, independently reproduced against the real saved CSVs (BTC/USDT's pinned-window
PF was 1.87 gross, **0.82 net — a real loss**), and it is now fixed: `profit_factor` etc. are
NET of fees; `gross_profit_factor`/`gross_win_rate` are new fields kept alongside for
comparison only, never used to gate anything. The report/walk-forward printers show both,
gross dimmed. **Every PF number anywhere in this file from before 2026-09-12 is a gross
number and should not be trusted as "the edge" — treat this section as the current truth.**

### How to verify the config is active
Run: `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py`
Expected (rolling, drifts as the window advances): **~29 trades, net PF ~1.1–1.2, ~35% win
rate** (gross ~2.4–2.5) — hash `5c6540eccbd2f45f`. If `RSI_FILTER_ENABLED=false` accidentally:
trade count jumps, PF drops further. **Use the pinned-window check below for a deterministic
pass/fail.**

Reproducible pinned-window check (deterministic — data range fixed):
```
EXCHANGE=binance SYMBOL=BTC/USDT BACKTEST_SINCE=2024-03-07 BACKTEST_UNTIL=2026-06-20 python backtest.py
```
Expected: **27 trades, net PF 0.82 (gross 1.87), 33.3% win rate** (5010 candles), hash
`5c6540eccbd2f45f`. **This pinned window is a net LOSS once fees are correctly attributed —
this is the documented, expected result, not a regression.**

### Canonical strategy fingerprint (BTC/USDT) — profitability status downgraded 2026-09-12
- **Strategy hash:** `5c6540eccbd2f45f` — changed 2026-09-12 for the MACD-history fix
  (`_last_macd_hist` wasn't updated on an ADX-rejected candle, letting falling momentum read
  as rising; see the "second-pass review" section above for detail). The fee-accounting fix
  above does NOT change this hash — `metrics.py` isn't in the hashed file list, it only
  changes how the SAME trades are scored.
- **Hashed files (behavior-defining only):** `bot/strategy/indicator_strategy.py`,
  `bot/strategy/threshold_strategy.py`, `bot/indicators/indicators.py`
- **Re-validated 2026-09-12 net of fees — neither symbol clears the documented floors
  (PF≥1.72 backtest, walk-forward PF≥1.2) anymore; both round-trip on paper about thin/none:**
  - **BTC/USDT:** pinned window 27 trades, **net PF 0.82** (gross 1.87, real loss on this
    historical window). Rolling: net PF 1.17 (gross 2.46). Walk-forward: **TRAINING net PF
    0.67** (a loss, gross was 1.37) / **VALIDATION net PF 1.54** (gross 3.41). The
    walk-forward "✓ holds" verdict only checks validation-vs-training degradation, not
    whether either window is actually profitable in absolute terms — a training PF of 0.67
    technically "holds" against a validation PF of 1.54, which is a strategy that lost money
    in-sample and made some out-of-sample, not a demonstrated edge.
  - **SOL/USDT:** rolling net PF 1.05 (gross 1.78). Walk-forward: TRAINING net PF 1.06 /
    VALIDATION net PF 1.05 (gross 1.68/1.84) — barely above breakeven in both windows, no
    real margin.
  - The MACD-history fix itself is unrelated to this and remains correctly stamped (BTC was
    byte-identical pre/post that fix on every gross number; SOL shifted slightly). The
    profitability picture above is a pre-existing condition the MACD fix didn't cause and
    doesn't affect — it was simply never visible until fees were accounted for correctly.
- **This is not a "re-run and see if it still passes" situation — it may not have ever
  cleared the documented floors net of fees.** No further strategy code change has been made
  in response to this; it needs a decision (tighten entries, filter low-edge trades, accept a
  smaller/no live allocation, or something else) before the PF≥1.72 / walk-forward-PF≥1.2
  floors can be called met again. See "Current operational status" below.
- Stamped: `python stamp_strategy.py` → `logs/validated_strategy_hash` (done 2026-09-12, for
  the MACD fix — stamping only certifies the code matches what was tested, not profitability).
- If the bot or backtest prints `STRATEGY CODE DIFFERS`, re-run walk-forward before trusting any PF numbers.
- **Both bots need a restart** to run the fixed MACD-history strategy code.

### Review deadline & keep/retire criteria (set 2026-09-12)
Both strategies are on trial, not accepted indefinitely. One review date, explicit numeric
bars, decided now so evaluation happens on a schedule instead of being deferred again each
time a review pass finds another thing to fix.

**Review date: 2027-03-12** (6 months out). This is a backstop — each bot below can also
trigger review earlier, but neither can be pushed out past this date by "not enough trades yet."

- **Stock bot (Gate 3):** review at 30 completed round-trips (the existing gate) OR
  2027-03-12, whichever comes first. At review: PF≥1.2 net-of-commission AND win rate≥30% on
  whatever trade count exists at that point — the existing Gate 3 bar, this only adds a date
  backstop so a slow trade pace can't defer judgment forever.
- **Crypto bot (BTC/CAD + SOL/CAD, HALTed since 2026-09-12):** resume BUYs only after a
  walk-forward on data strictly AFTER 2026-09-12 — genuinely out-of-sample, not one of the
  windows already reviewed across 7 passes — clears PF≥1.2 net on ALL windows for BOTH
  BTC/USDT and SOL/USDT, evaluated against a simple buy-and-hold benchmark over the same
  window (net return AND max drawdown, not PF alone). Check this at the 2027-03-12 review
  date at the latest.
- **If either bot fails its bar at review:** retire or redesign that strategy rather than
  keep patching or waiting further. Don't relitigate this deadline itself without genuinely
  new evidence (e.g., a materially different market regime) — a fixed deadline that gets
  pushed back on request isn't a deadline.
- **Feature/process work pauses:** no more proactive multi-pass code reviews on either bot
  between now and the review date unless a specific bug is suspected — user's explicit call,
  2026-09-12 (7 review passes already ran on this exact code this month).

### Execution-accounting reconciliation (crypto — built 2026-09-19/20, opt-in, off by default)
`bot/accounting/` — a real SQLite-backed reconciliation layer that observes actual Kraken
trade history, links it to the bot's own fill records, runs four-way verification (exchange
trades vs. local ledger vs. account cash vs. position), and blocks new BUYs
(`ACCOUNTING_ENABLED=true` + `block_buys_on_unreconciled=true`, both required) while anything
is unreconciled or stale. `cfg.accounting.enabled` defaults `false` — with it off, `bot/main.py`
behaves exactly as before this subsystem existed. Built across 11+ same-week review passes
(2026-09-13 → 2026-09-20); design + full pass-by-pass findings live in the standalone
`CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_2026-09-19.md` and `CRYPTO_BOT_MONEY_READINESS_REVIEW_*.md`
files at repo root, not duplicated here. A gated paper/shadow + security readiness review
(`CRYPTO_BOT_GATED_READINESS_REPORT_2026-09-20.md`, `CRYPTO_BOT_SECURITY_REVIEW_2026-09-20.md`,
`deploy/PAPER_SHADOW_RUNBOOK.md`) found and fixed one real gap (the dynamic-universe ranked-BUY
path didn't consult the accounting block state — fixed, unreachable in production either way
since dynamic mode is off) and confirmed the Kraken key's actual permission scope has never
been manually verified against the checklist that already existed in "Exchange Setup" below.
None of this changes HALT status — see the review-deadline section above; profitability is the
independent, still-unmet reason the bot stays halted regardless of accounting readiness.

**`migrate_legacy_fills.py` applied to the live DB 2026-09-20** (dry-run reviewed first, per
the readiness report's own "review every proposed match before applying" instruction —
`--apply-to-live` took an unconditional backup first: `logs/trades_pre_migration_backup_
20260920T150248Z.db`, restore by copying it back over `logs/trades.db`). Result: 8 fills
linked to their real Kraken trade ids, 3 orphan trades backfilled, 10 pre-existing unlinked
legacy fills → 2 remaining, both confirmed as legacy-bug artifacts rather than real
unaccounted trades and left permanently blocked (exact-conservation matching refuses to guess):
- `fills.id=1` — BTC/CAD SELL 2026-06-22 (the day the post-only bug went live), fee recorded
  locally as $0.00; the real Kraken trade almost certainly charged a nonzero fee, so it can't
  conserve. Unresolved — would need the real trade's fee pulled from Kraken to correct the
  local row, which hasn't been done (a data-correction write, not a review).
- `fills.id=2` — BTC/CAD SELL 2026-06-27, recorded with **quantity=0.0**; `scripts/
  accounting_shadow_report.py` labels it `[phantom] zero-qty row`. `fills.id=9` has the same
  second/side/symbol with the correct quantity/price/fee and is already linked to the real
  trade — `fills.id=2` is a duplicate artifact from the old qty=0 recording bug, not a second
  real trade. Safe to leave blocked.
`scripts/accounting_shadow_report.py` re-run post-migration: BTC/CAD 2 residuals (both above,
expected), SOL/CAD 0 residuals.

**Kraken key permission check completed 2026-09-20** (manual UI check, `trade_bot_local` key,
created 2026-09-04): Withdraw Funds **disabled** (confirmed — the security-critical item).
Query/Query orders/Create & modify orders/Cancel & close orders all enabled as expected. IP
address restriction is **Off** — a known, deliberate prior tradeoff (see
`.memory/project_kraken_auth_outage_2026-09-04.md`: IP restriction previously caused real auth
outages against a dynamic IP), not an oversight, but worth revisiting if a static IP is ever
set up. Security-review Gate 2's blocking item is now closed.

### Current operational status
- **Crypto bot:** live on Kraken, **but its profitability basis is now in question
  (2026-09-12)** — see "Canonical strategy fingerprint" above. The walk-forward/backtest
  floors this deployment rested on (PF≥1.72, walk-forward all windows PF>1.0) were measured
  on gross P&L; net of real trading fees, BTC/USDT's training window is a loss (PF 0.67) and
  SOL/USDT is barely above breakeven everywhere (PF ~1.05). Actual live financial exposure to
  date is small — BTC/CAD 0/15 fills, SOL/CAD exactly one completed round-trip (+$1.27) — but
  the strategy has NOT been shown to have a demonstrated edge net of costs, and continuing to
  trade it live is a decision to make deliberately, not a default. **User decision 2026-09-12:
  new BUYs paused** — `logs/HALT` engaged via the existing manual kill-switch (same mechanism
  `/pause_crypto` uses). **Correction (2026-09-13):** this was first described here as "SELL/
  exit safety mechanisms fully active" — true only for SL/TP exits (`RiskManager.evaluate()`
  isn't in that code path at all, gated only by `RISK_HALT_BLOCKS_STOPS=false`); an ordinary
  strategy-driven SELL signal DOES still route through `risk.evaluate()` and IS blocked by
  `config.halt`, same as BUY (`bot/risk/risk_manager.py` Check 1 — the one breaker in that
  file that isn't BUY-only, unlike every other tier). Confirmed as intentional, documented
  full-stop kill-switch behavior, not a bug — user's explicit call (2026-09-13) is to keep it
  that way rather than build a narrower BUY-only pause. No open position is at risk either way
  (BTC/CAD has never filled, SOL/CAD is flat); if one opens while halted, SL/TP alone protects
  it (which is the existing safety net, not a gap this halt creates). Lift with `rm logs/HALT`
  or `/resume_crypto` once the net-of-fees edge question is resolved one way or the other.
  BTC/CAD ($77 slot) capital
  gate 0/15 fills (strategy trades ~every 3–6 weeks; 65+ days elapsed with zero progress
  toward fill #1 as of 2026-08-24). SOL/CAD ($376 slot) 1/15 fills (BUY 0.080808 @ $134.02 on
  2026-08-26 — the fill that surfaced the post-only bug; TP-closed +$1.27/+10.9% on
  2026-08-27). ATR SL 2.0 + ATR sizing live. Telegram (t.me/amaresh_tradebot) +
  healthchecks.io heartbeat + two-way Telegram control live. Native stop-loss ON. All four
  items from the 2026-08-07 crypto-bot gap review closed (native stop, risk tiering, slippage
  guard, candle-watchdog breaker) — execution-layer hardening was never the question here;
  the underlying edge is. **Independent second-opinion review, 2026-09-14:** an assessment
  requested separately from these code-review passes reached the same conclusion from the same
  numbers (BTC pinned 0.82, BTC rolling 1.17, SOL rolling 1.05, net of fees) and explicitly
  recommended keeping crypto paused and prioritizing out-of-sample/realistic-cost validation
  over new features before any capital increase — every figure and status claim in it was
  independently re-verified against the live system (HALT still engaged, Gate 1 15/28 total
  symbols scanned pass, Gate 3 still 7/30 round-trips, net PF 0.62 unchanged since the
  2026-09-07 report) and checked out exactly. Restarted 2026-09-14, 13:21 (new PID) — running
  the sixth-pass kill-switch and volatility-multiplier fixes.
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

## Dynamic Crypto Universe (LIVE-ENGINE INTEGRATION — 2026-09-13, activation still pending)

User-authorized redesign, explicitly overriding the fixed BTC/CAD+SOL/CAD whitelist so the
**existing live Kraken bot itself** (`bot/main.py`) can discover and trade qualifying coins
beyond BTC/SOL, using its own established execution/risk paths — not a separate trading engine.
Gated entirely behind `DYNAMIC_UNIVERSE_ENABLED` (default `false`): fixed mode is byte-identical
to before this feature existed. **`logs/HALT` stayed engaged throughout development and testing;
no real order, fund conversion, or live-trading enablement occurred at any point — see
"Activation & rollback" below for what a human needs to do to actually turn this on.**

### History — two builds, one superseded
- **2026-09-13, first pass:** a standalone, paper-only runner (`dynamic_universe_bot.py`) with
  its own simplified tick loop. Working and tested at the time, but a follow-up review found it
  re-implemented (imperfectly) logic the live bot already had right — **superseded the same day**;
  see "Retired standalone runner" below for the specific bugs found and why none were carried
  into the integration that replaced it.
- **2026-09-13, second pass (current):** direct integration into `bot/main.py`'s existing
  `run()` — the only trading engine now involved. This section describes the current state.

### What it is
- `bot/dynamic/eligibility.py` (`DynamicUniverseScreener`) and `bot/dynamic/ranking.py`
  (`rank_buy_signals`) — **reused unchanged** from the first pass; both were already
  self-contained, hermetically-tested pure logic with no execution-loop involvement, so nothing
  about them needed to change for the live integration. Screener filters: active+spot,
  stablecoin/leveraged-token base exclusion, 24h quote volume, exchange minimum order size vs.
  slot cash, top-of-book spread, order-book depth, OHLCV history length, duplicate-base dedup
  across quote currencies. Fail-safe: a discovery failure serves the last bounded-age-validated
  cache (`logs/dynamic_universe_cache.json`, stale past `DYNAMIC_CACHE_MAX_AGE_HOURS`) or, with
  no usable cache, an **empty** eligible list — never an arbitrary fallback symbol; existing
  positions are always still managed regardless. Ranking: ADX (the strategy's own trend-strength
  gate) then 24h volume — deliberately not a new/untested scoring model.
- `bot/dynamic/lifecycle.py` (`DynamicSymbolManager`) — **not used by the live integration.**
  It predates a settled design decision to produce `bot/main.py`'s own richer `symbol_state[sym]`
  dict shape directly (trail_peak, atr_sl, native_stop_price, dash_* fields, etc.) rather than a
  more generic `SymbolHandle`, so admission/retirement/restart-recovery for the live bot are new,
  purpose-built functions in `bot/main.py` itself (below) that produce that exact shape. Left in
  place, unused by the live path, in case a future generic (non-`bot.main`-shaped) consumer needs
  it — not deleted, not documented as load-bearing for live trading.
- **New in `bot/main.py`** (all module-level, explicitly-parameterized functions — same
  "extract for testability" convention as `_evaluate_drift`/`_check_candle_watchdog`/
  `_seed_native_stop_state`, chosen specifically so each is independently unit-testable without
  invoking the ~1800-line `run()` loop):
  - `_new_symbol_state_dict()` — the one authoritative `symbol_state[sym]` shape, used by both
    the original static-roster startup init and dynamic admission (previously a literal dict
    typed out once at startup with no second call site at all).
  - `_make_dynamic_executor(sym)` — builds a `LiveExecutor` for a newly-admitted symbol using
    the EXACT SAME parameters (exchange, order type, adopt-external-holdings, native-stop,
    slippage guard, `dry_run` rule) as the fixed roster's own construction — a dynamically
    admitted symbol's fee accounting, slippage guard, and native-stop handling are identical to
    BTC/CAD's or SOL/CAD's the moment this is ever activated. `starting_cash` is always 0.0;
    the caller funds it via `capital_pool.slot_cash_for(sym)` right after construction — scanning
    more coins never changes any single position's sizing basis.
  - `_admit_dynamic_symbol(sym, live_exchange, timeframe, capital_pool)` — initializes strategy,
    historical warmup, candle timestamp, executor, position manager, and trading state for a
    new symbol WITHOUT a restart, reusing `build_strategy()`/`_warmup_strategy()` unchanged (so
    behavior is byte-identical to the fixed roster's). If the executor's own persisted state
    already shows a position (a restart re-admitting a symbol that was already trading, not a
    fresh candidate), applies the SAME recovery seeding the fixed roster's restart-recovery
    block gets (`pm.seed`/`sm.recover_long`/native-stop mirror) **plus
    `capital_pool.allocate(sym)`** — see "Confirmed bugs addressed" below. Never raises: returns
    `(None, error_str)` on failure so one bad candidate can never block management of every
    OTHER symbol's existing positions this cycle.
  - `_retire_dynamic_symbol_if_eligible(sym, ss, capital_pool)` — retires ONLY once genuinely
    flat AND with no resting protective order left outstanding; releases the capital-pool slot
    (no-op if never allocated).
  - `_sync_dynamic_universe(...)` — one full refresh cycle: discover → admit new eligible
    symbols not already in `symbol_state` → retire flat-and-dropped dynamically-admitted symbols.
    Only ever retires a symbol IT previously admitted (tracked in a separate `dynamic_admitted`
    set) — the original fixed roster is never touched here no matter what the screener says.
  - `_execute_approved_signal(...)` — a **verbatim extraction** of what was previously the inline
    "9. Execute" block: fee-aware fill recording, PnL, native-stop sync, ATR-SL computation,
    trailing-stop seeding, `capital_pool.allocate()`/`release()`, trade log, Telegram fill/reject
    alert, and the stuck-loop watchdog. Both the fixed-roster immediate call site and the new
    ranked-dynamic-BUY call site invoke this SAME function — one code path, not two copies that
    could silently drift apart.
  - `_execute_ranked_dynamic_buys(...)` — ranks every BUY signal gathered this tick and executes
    in that order, re-checking `capital_pool.can_open_position()` and `risk.evaluate()` FRESH
    per candidate (not a stale gather-time snapshot) — see "Capital allocation" below for why
    this is sufficient without a separate reservation mechanism.
  - `_resync_native_stop()` — hoisted from a `run()`-local closure to module level (pure
    relocation, zero behavior change) purely so it too is independently testable.
- Config: `DynamicUniverseConfig` in `config.py` (`cfg.dynamic`), entirely separate dataclass
  and env namespace (`DYNAMIC_*`) from the live `UniverseConfig`/`PortfolioConfig` — no shared
  keys, no way for a `.env` edit here to affect fixed-mode behavior. `cfg.dynamic.enabled` IS
  the fixed-vs-dynamic switch.
- Dashboard: `unified_dashboard.py`'s `_dynamic_universe_card()` (unchanged) reads
  `logs/dynamic_universe_dashboard.json`, now written by `bot/main.py` itself
  (`_write_dynamic_universe_dashboard()`) every refresh cycle — same JSON shape as before, so
  the card works against the live integration's output with no changes needed. Shows
  discovered/eligible counts, eligible symbols, rejection reasons, admitted symbols, open
  positions, per-cycle blocked-gate reasons, and pool cash — labeled "PAPER — not live" (the
  label is accurate today since `DYNAMIC_UNIVERSE_ENABLED=false`; it stops being paper-only the
  moment a human flips that on with real `dry_run=False` execution, per "Activation" below).

### Fixed vs. dynamic universe selection — the actual switch
```
DYNAMIC_UNIVERSE_ENABLED=false   # default — fixed mode, byte-identical to before this feature existed
DYNAMIC_UNIVERSE_ENABLED=true    # dynamic mode — bot/main.py additionally discovers/admits/retires
                                  # symbols at runtime; the ORIGINAL UNIVERSE_WHITELIST/registry
                                  # roster still seeds the initial symbol set either way
```
When `false`: `_dynamic_screener` is never constructed, `_sync_dynamic_universe`/
`_execute_ranked_dynamic_buys` are never called, and section 9's BUY path takes the exact
`elif approval:` immediate-execute branch it always has — proven by
`test_fixed_mode_buy_still_executes_immediately_not_queued` and
`test_run_source_gates_every_dynamic_addition_behind_cfg_dynamic_enabled`.

### Discovery, ranking, sizing, and position limits
- **Discovery**: CAD-quoted pairs only today (`DYNAMIC_QUOTE_CURRENCIES=CAD`) — matches current
  funding; the config is a list specifically so a future USD leg gets its OWN capital pool,
  never silently drawing CAD cash for a USD order.
- **Filters**: active+spot, not a stablecoin/leveraged-token base, 24h quote volume floor,
  spread ceiling, order-book depth floor, minimum OHLCV history, exchange minimum order size
  checked against the SLOT cash (a cheap pre-filter) — the AUTHORITATIVE check against the
  ACTUAL proposed order size happens where it always has, inside `LiveExecutor.execute()`'s own
  min-size guard (unchanged, already covered by its 70 existing tests) — reused, not duplicated.
- **Ranking**: ADX then 24h volume, computed at decision time only, no future data.
- **Sizing**: unchanged — `calc_trade_qty()` / `calc_trade_qty_atr_risk()`, exactly as the fixed
  roster uses, off of `capital_pool.slot_cash_for(sym)` — a slot's cash is always
  `total_capital / max_concurrent_positions` (or a per-symbol cap), independent of how many
  symbols are merely being SCANNED. Scanning more coins never divides any position's sizing
  basis further.
- **Position limits**: **one shared limit for everything** — `MAX_CONCURRENT_POSITIONS`
  (`cfg.portfolio.max_concurrent_positions`, the SAME existing live config, currently 2 for
  BTC/CAD+SOL/CAD) via the SAME `CapitalPool.can_open_position()` — the fixed roster and every
  dynamically-admitted symbol compete for this ONE pool of slots, per "use a shared capital
  budget." **`DYNAMIC_MAX_CONCURRENT_POSITIONS` does NOT control this for the live integration**
  (a real inconsistency caught and fixed 2026-09-13 — it's used only by the retired standalone
  paper runner's own separate, isolated pool; see config.py's field comment). Practically: with
  `MAX_CONCURRENT_POSITIONS=2` unchanged, enabling dynamic mode means BTC/CAD, SOL/CAD, and every
  dynamic candidate all compete for those same 2 slots — a human wanting dynamic symbols to have
  room WITHOUT displacing the fixed roster's existing capacity must raise
  `MAX_CONCURRENT_POSITIONS` (and `STARTING_CASH` together, per the existing documented capital
  rule) BEFORE activating. Scanning is bounded separately by `DYNAMIC_MAX_CANDIDATES` (default
  40), a cost/rate-limit control on the SCREENER, not a position limit.
- **Capital allocation / no double allocation**: `capital_pool.allocate()` still fires only on a
  CONFIRMED fill (unchanged from the fixed-roster design) — this is sufficient to prevent double
  allocation WITHOUT a separate pre-reservation mechanism because (a) `execute()` is
  synchronous/blocking (a limit-chase fully resolves before returning) and (b)
  `_execute_ranked_dynamic_buys` processes ranked candidates strictly sequentially, one at a
  time, never concurrently — so the next candidate's `can_open_position()` check always sees the
  true post-fill slot count. Account exposure and the crypto correlation gate (`bot/risk/
  correlation.py`, already generic over `symbol_state.items()`) needed zero changes — both were
  already written without any BTC/SOL-specific assumption.

### Confirmed bugs from the retired standalone runner — NOT carried into this integration
| # | Bug in `dynamic_universe_bot.py`'s `run_cycle()` | Fixed how, in the live integration |
|---|---|---|
| 1 | A fill never called `sm.on_fill()` — state stuck IDLE, later SELLs suppressed | `_execute_approved_signal` calls it on every confirmed fill (same line the fixed roster always used) — `test_execute_buy_fill_updates_all_state_and_fees` proves `sm.state == LONG` after a BUY fill |
| 2 | No stop-loss/take-profit execution path at all | Not applicable — the live integration adds NO separate exit path; it reuses `run()`'s own intra-candle SL/TP block (section 2) unchanged for every symbol in `symbol_state`, dynamic or fixed |
| 3 | `LiveExecutor(dry_run=True)` modeled zero fees/slippage | `_make_dynamic_executor` builds the SAME `LiveExecutor` the fixed roster uses, with the SAME `dry_run` rule (`paper_mode or cfg.exchange.dry_run`) — no dynamic-specific executor variant exists |
| 4 | Restart recovery restored positions but not capital-pool allocations | `_admit_dynamic_symbol` calls `capital_pool.allocate(sym)` when the recovered executor already holds a position; the equivalent FIXED-roster gap (latent, never reachable with only 2 always-present symbols) was fixed too, in `run()`'s own restart-recovery block |
| 5 | `risk.record_fill()` never called | `_execute_approved_signal` calls it on every confirmed fill — `test_execute_buy_fill_updates_all_state_and_fees` asserts `risk.record_fill_calls == ["ETH/CAD"]` |

`dynamic_universe_bot.py` itself is marked deprecated-for-evaluation in its own module
docstring (not deleted — its recorded logs/state/backtest report are left in place) rather than
removed, since it's still safe to read/run for inspection (dry_run stays hardcoded) — it just
proves nothing about performance.

### Tests
`tests/crypto/test_dynamic_live_integration.py` — behavioral tests against real
`TradingStateMachine`/`PositionManager`/`CapitalPool` + fake executors/risk (no network, no
Telegram, no production writes), plus a few source guards for the parts irreducibly inline in
`run()`. Per-test inventory: see the manifest row above and `CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-25". **No real-network smoke test
of `run()` with dynamic mode on** — deliberately not run, since `run()` is the same entry point
as the live bot and shares its keys/state files.

### Evaluation (`dynamic_universe_backtest.py`) — result: does NOT support expanding today
**Still describes independent single-symbol backtests only — it does NOT evaluate the dynamic
shared-capital selection/ranking process this integration adds.** No new performance claim is
made for the live-integration's actual behavior (ranking + shared slots + capital reservation);
building a true multi-symbol, shared-capital, timestamp-interleaved portfolio replay was not
attempted this pass (a real gap, stated plainly — see "Remaining limitations" below). The table
below is unchanged from the first pass and answers a narrower, still-relevant question: is there
ANY edge in the extra candidate coins at all, independent of how capital would be shared.
Compares the current BTC/SOL whitelist against BTC+SOL+ETH+XRP (the only other CAD pairs that
clear Kraken's liquidity/spread/depth bar today — PEPE/DOGE/XDC all fail volume, confirmed by
the live screener run above) using the SAME unmodified strategy, on both the deterministic
pinned window and the current rolling window. Report: `logs/dynamic_universe_backtest_<date>.md`.

**Result (2026-09-13, hash `5c6540eccbd2f45f`, net of real fees):**
| Symbol | Pinned net PF | Pinned return | Rolling net PF | Rolling return |
|---|---|---|---|---|
| BTC/USDT | 0.82 | -0.72% | 1.17 | +0.71% |
| SOL/USDT | 0.94 | -0.33% | 1.05 | +0.25% |
| ETH/USDT | **0.42** | -2.93% | **0.31** | -3.83% |
| XRP/USDT | **0.55** | -2.47% | **0.57** | -2.26% |

Both candidate expansion symbols are worse than EITHER currently-live symbol on BOTH windows,
under the identical unmodified strategy. Blended (naive equal-weight, no shared-slot contention
modeled): expanding to 4 symbols is worse than staying at 2 on both windows (-1.61% vs -0.52%
pinned; -1.28% vs +0.48% rolling). **This independently reconfirms, with freshly-generated
numbers post the 2026-09-12 fee-accounting and kill-switch fixes, the same conclusion already
on record in memory (`project_crypto_usd_expansion_closed_2026-09-09`) from the prior
exhaustive CAD/USD screening — on the actual available Kraken CAD universe, there is currently
nothing to expand into, not because the infrastructure doesn't work, but because the only real
candidates don't have a demonstrated edge under this strategy.**

**Explicit limitations (see the script's own docstring for full detail):**
- **Not genuinely fresh data.** All market data here predates 2026-09-12 and has been examined
  before (BTC/SOL) or is a new RUN on old data (ETH/XRP). This does NOT satisfy the "fresh
  post-2026-09-12 walk-forward" bar the 2026-09-12 review-deadline decision requires before
  resuming live BUYs — only the forward paper run above (`dynamic_universe_bot.py`, ~0 days of
  history as of this writing) can eventually produce that.
- **Proxy-market data.** ETH/CAD and XRP/CAD are backtested via their Binance USDT pair (same
  methodology as the existing BTC/SOL backtests) — not independently re-verified per-symbol
  for price-difference drift the way BTC's ~0.048% was.
- **No point-in-time historical universe reconstruction.** Exchanges don't expose "which pairs
  were liquid on date X" historically; this evaluates today's real eligible set across the
  whole historical window — it cannot suffer from picking today's winners in hindsight (the
  screen doesn't know the outcome), but also can't prove what a genuinely time-varying universe
  would have looked like further back.
- **No shared-slot-contention model.** The "expanded universe" numbers are each symbol's own
  independent single-symbol backtest, not a true multi-symbol engine sharing one capital pool
  and competing for capped slots (that engine doesn't exist and wasn't built this pass) — a
  simplification disclosed, not hidden.

### Config reference (`DYNAMIC_*`, all in `config.py`'s `DynamicUniverseConfig`)
```
DYNAMIC_UNIVERSE_ENABLED=false        # THE fixed-vs-dynamic switch for the LIVE bot/main.py integration
DYNAMIC_QUOTE_CURRENCIES=CAD          # comma-separated; each quote WOULD get its own capital pool if ever
                                        # added — CAD is the only funded/live one today
DYNAMIC_EXCLUDE_BASES=EUR,USD,USDC,USDT,DAI,BUSD,TUSD,PYUSD,FDUSD,GUSD,USDP
DYNAMIC_MIN_QUOTE_VOLUME=50000        # matches the existing $50k/day liquidity gate
DYNAMIC_MAX_SPREAD_PCT=0.0015         # matches the existing 0.15% spread gate
DYNAMIC_MIN_DEPTH_QUOTE=500           # min resting notional within DYNAMIC_DEPTH_BAND_PCT of mid, each side
DYNAMIC_DEPTH_BAND_PCT=0.01
DYNAMIC_MIN_HISTORY_CANDLES=200
DYNAMIC_MAX_CANDIDATES=40             # scan cap only (cost/rate-limit control) — used by the live integration
DYNAMIC_MAX_CONCURRENT_POSITIONS=3    # ⚠️ NOT used by the live integration — see "Position limits" above.
                                        # Real position limit is the EXISTING MAX_CONCURRENT_POSITIONS,
                                        # shared with the fixed roster. This field only matters to the
                                        # retired standalone dynamic_universe_bot.py paper runner.
DYNAMIC_STARTING_CASH_CAD=1000        # ⚠️ NOT used by the live integration either — same reason. The live
                                        # integration's capital comes from the real shared capital_pool
                                        # (cfg.portfolio.starting_cash / the real Kraken balance), not this.
DYNAMIC_REFRESH_HOURS=4
DYNAMIC_CACHE_MAX_AGE_HOURS=48
```

### Remaining limitations (stated plainly)
- **No shared-capital portfolio replay exists yet.** Point 8 of the live-integration request is
  explicit that the independent BTC/SOL/ETH/XRP backtests above do NOT evaluate the actual
  dynamic ranking + shared-slot-contention process this integration adds — building a true
  multi-symbol, timestamp-interleaved, shared-capital backtest engine was not attempted this
  pass. **No profitability claim is made for the live integration's actual behavior** — only for
  the narrower, already-answered question of whether the extra candidate coins have any edge at
  all in isolation (they don't, on the evidence above).
- **Zero live-adjacent track record.** Even with a positive backtest, this system has never run
  against live data at all (the standalone runner's own output is explicitly disqualified — see
  "Confirmed bugs" above). A real evaluation needs elapsed time accumulating genuine forward
  trades, the same way the BTC/CAD+SOL/CAD walk-forward gates required real fills, not just a
  backtest, before being trusted.
- **No real-network smoke test of `run()` itself with dynamic mode on** — see "Not attempted,
  honestly" under Tests above; verification is hermetic tests + source guards only.
- Discovery/proxy-data/point-in-time-universe limitations from the first pass (see the
  Evaluation section below) are unchanged and still apply to the backtest table.

### Whether the evidence supports going live with this
**No, not yet, on two independent grounds, unchanged in substance from the first pass:**
(1) the only real expansion candidates (ETH, XRP) have a worse net-of-fee edge than the
currently-live pair on every window checked (an isolated-symbol result, not yet a portfolio
one — see limitation above), and (2) this system — now correctly wired into the live
engine — has zero live-adjacent track record of its own; the forward-running BTC/CAD+SOL/CAD
positions inside it are the same real fixed-mode positions as always, but no dynamic symbol has
ever actually traded. The infrastructure itself (screening, lifecycle, ranking, capital
allocation, extraction-for-testability of the fill-processing path) is real, tested, and reused
from — not duplicated alongside — the bot's existing execution engine. "We can now let the live
bot discover more coins safely" and "there is more money to be made" remain two separate
questions; only the first is currently answered yes.

### Second + third review passes, same day (2026-09-13) — 2 Critical + 4 High, plus 2 more gaps, all fixed
Same-day self-review of the just-built integration (HALT engaged throughout, no activation).
Found and fixed: account-value inflation from counting cash on flat/unallocated dynamic
candidates (`_compute_account_value()` now counts pool cash once + only truly allocated slots
contribute); `_make_dynamic_executor()` could build a live-capable executor without checking
`LIVE_TRADING` (new `_dynamic_mode_active` gate replaces the bare `cfg.dynamic.enabled` check
everywhere); orphaned positions on restart/rollback are now unconditionally re-admitted via
`_admit_dynamic_symbol` regardless of current screener eligibility; ranked BUY candidates now
recheck correlation against positions filled earlier in the SAME batch, not just gather-time
state; an ambiguous (`None`) BUY outcome now conservatively reserves its capital slot instead
of freeing it for a different candidate; any `DYNAMIC_QUOTE_CURRENCIES` other than `CAD` now
disables dynamic mode outright (no real multi-currency accounting exists). Third pass closed
the two remaining gaps: discovery/screening now runs AFTER position management each tick (never
delays checking an existing position's SL/TP), and each ranked candidate's price is refreshed
immediately before execution, skipping the candidate if it's moved beyond `MAX_SLIPPAGE_PCT`
since gather time. +11 tests (suite 1040→1051). Full bug table + reproduction detail:
`CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-15".

### Review passes 4–15 (2026-09-22 → 2026-09-24) — current-state summary
Full pass-by-pass write-ups (reproductions, numbers, test lists): `CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-25". **Ten consecutive
same-day passes each found a real gap in the previous pass's own fix — treat any single-pass
fix in this subsystem as provisional until a pass finds nothing.** Current state:
- **Restart equity, live mode:** `_admit_dynamic_symbol`'s recovery branch folds only
  `position × avg_entry` into `total_capital` (the fresh exchange balance already includes the
  symbol's cash), matching `_initialize_capital_pool`'s fixed-roster rule. This path runs for
  orphan recovery **regardless of `cfg.dynamic.enabled`** whenever `live_trading` is on.
- **Restart equity, paper/dry-run mode:** account-level `_replay_paper_realized_pnl(state_dir)`
  sums `realized_pnl - fees_paid` over EVERY `live_state_*.json` (roster, open, flat, retired);
  `pool_total = starting_cash + sum`. The per-symbol paper bump was removed (would double-count).
  Flat symbols still get `slot_cash_for()` from the corrected total (shared-pool design).
- **Replay trust:** returns `(total, ok)`; `ok=False` on unreadable/non-dict/missing-field/
  non-numeric/non-finite files → `paper_accounting_ok=False`, fresh slots not funded, alert, and
  gate "7a1" blocks all new BUYs (`BlockReason.PAPER_ACCOUNTING_INCOMPLETE`). `CapitalPool`
  rejects non-finite totals; `release()` validates before mutating (a rejected call changes nothing).
- **BUY eligibility in dynamic mode:** gate "7a2" `_dynamic_buy_eligible()` blocks a new BUY on
  ANY symbol (fixed roster too) unless the latest discovery (`_dynamic_last_screen`) is fresh
  (≤ 2 × `DYNAMIC_REFRESH_HOURS`) and lists it eligible (`BlockReason.DYNAMIC_INELIGIBLE`). Exits unaffected.
- **Dry-run fee simulation:** `SIMULATED_MAKER_FEE_PCT`/`SIMULATED_TAKER_FEE_PCT` (0.40%/0.80%)
  flow through the normal `fee_cost` path; `SIMULATE_MAKER_FILLS=false` default → every dry-run
  fill pays taker. Unaffordable dry-run BUYs (notional + fee > cash) are REJECTED. `LiveExecutor`'s
  own constructor defaults the rates to 0.0 (old behavior for direct construction). All 4
  production construction sites pass all three settings (tested via the real `_make_dynamic_executor`).
- **Shadow isolation:** `_compute_shadow_mode()` = `live_trading and not paper_mode and dry_run`.
  State, dashboard, `risk_state.json`, `trades.db` AND `_HALT_FLAG_PATH` all route via
  `_STATE_LOG_DIR` → a shadow run uses `logs/shadow/HALT`, independent of the real `logs/HALT`.
  `PAPER_MODE=true` is NOT isolated — never use it for the shadow run.
- **Shadow Telegram control:** `_resolve_telegram_control_credentials()` refuses the two-way
  poller in shadow mode unless `SHADOW_TELEGRAM_CONTROL_BOT_TOKEN`/`_CHAT_ID` are both set AND the
  (whitespace-normalized) token differs from `TELEGRAM_BOT_TOKEN` — one token = one poller.
- **Multi-coin one-bankroll lifecycle test** (discover → rank → allocate → execute → restart →
  exit, three coins, exact equity conservation) is the regression net for all of the above.

### Bounded paper/shadow acceptance criteria (set 2026-09-22/23 — corrected multiple times by the fifth through ninth review passes; criterion 1's fee caveat closed by the tenth pass, corrected by the eleventh, wiring gap closed by the twelfth; replaces the vague pointer that used to sit here as "Activation step 6")
Mirrors the discipline already applied to the BTC/SOL profitability question in the 2026-09-12
review-deadline decision: a fixed bar and a fixed date, decided now, so this doesn't get
deferred indefinitely every time a review pass finds one more thing to fix (fifteen passes have,
so far — including several that found the acceptance criteria's OWN text, or the prior pass's
OWN fix, were themselves wrong). This does NOT authorize activation by itself — it defines what
evidence WOULD have to exist before activation is even a live question.

**Before `DYNAMIC_UNIVERSE_ENABLED` is ever set true in the live `.env` with real capital, ALL
of the following must hold:**
1. **A genuine forward paper/shadow run, not a backtest substitute, on an ACTUALLY runnable AND
   storage-isolated configuration.** This section originally named `LIVE_TRADING=false` as the
   harness — WRONG (third round, P2): `_dynamic_mode_active` requires `live_trading=True`, so
   that combo never runs any dynamic-universe code at all. It was then corrected to name
   `PAPER_MODE=true` as an interchangeable alternative to `DRY_RUN=true` — ALSO WRONG (sixth
   round, P1): `bot/main.py`'s own shadow-mode isolation (`_compute_shadow_mode`) is
   `live_trading and not paper_mode and dry_run` — `paper_mode` is deliberately EXCLUDED, so a
   `PAPER_MODE=true` run's executors/`risk_state.json`/`trades.db` would resolve to the ordinary
   PRODUCTION `logs/` directory, at real risk of colliding with (or overwriting) genuine
   production state. **The only supported, storage-isolated combination is `LIVE_TRADING=true`
   WITH `DRY_RUN=true` AND `PAPER_MODE=false`.** Never launch `PAPER_MODE=true` for this purpose,
   "wired or not." `_make_dynamic_executor` builds every dynamic-universe executor with
   `dry_run=cfg.paper.paper_mode or cfg.exchange.dry_run`, and `LiveExecutor.execute()`'s own
   dry_run branch simulates every fill locally, never calling `create_order`/`cancel_order`/
   `fetch_balance`. Both the pipeline AND the storage isolation are proven end-to-end (not just
   asserted in prose), in `tests/crypto/test_dynamic_live_integration.py`:
   `test_paper_shadow_harness_exercises_full_pipeline_with_zero_real_orders` (discover → filter →
   rank → allocate → execute → restart-recover, then asserting the mocked exchange's
   `create_order`/`cancel_order`/`fetch_balance` were never called) and the `_compute_shadow_mode`
   tests (proving `PAPER_MODE=true` genuinely resolves outside the isolated directory, not just
   claiming it). Run continuously for at least **60 calendar days**, observing real discovery/
   admission/retirement/ranking behavior against live market data with zero simulated capital
   risk.
   **Fee caveat — closed by the tenth pass (2026-09-22/23), corrected by the eleventh (same day),
   still not independently reviewed beyond that:** `LiveExecutor`'s dry_run fills used to always
   use `fee_cost=0.0`, making criterion 3 (net-of-fee PF) unmeasurable from this harness. Fixed:
   `simulated_maker_fee_pct`/`simulated_taker_fee_pct` (new `ExchangeConfig` fields, defaulting to
   Kraken's documented real 0.40%/0.80%) now compute a fee on every dry-run fill, flowing through
   the same `fee_cost`/`trade_log.log_fill()` path a real fill already uses. The eleventh pass
   then found and fixed two real gaps in that same-day code: a BUY could be filled without
   checking it could actually afford the notional plus the fee (now rejected, not silently
   overspent); and every non-urgent limit BUY was assumed to guarantee a maker fill (now gated
   behind `simulate_maker_fills`, defaulting **False** — every fill pays the conservative taker
   rate unless explicitly opted into the more optimistic assumption). Verified with a full
   shared-bankroll lifecycle test (BUY → partial SELL → restart → full exit, two coins, exact
   conservation with fees included at every stage, using the conservative default) — see
   "Eleventh pass" above for detail. `PaperExecutor` (the classic single-symbol paper mode,
   unrelated to the dynamic-universe harness) still models no fees at all — irrelevant to this
   harness, since `_make_dynamic_executor` never constructs one. A twelfth pass then found
   `simulate_maker_fills` itself was never wired to any of the 4 production construction sites
   (parsed by config, zero effect when set) — fixed, with a construction-level test that calls
   the real `_make_dynamic_executor` rather than a monkeypatched stand-in, since that's the only
   way this class of bug can be caught at all. **This is now SEVEN consecutive same-day passes
   where a real, previously-uncaught gap was found in the immediately-prior pass's own work in
   this subsystem — none of the tenth/eleventh/twelfth passes has itself been independently
   reviewed. Do not assume this is finally the last one.**
2. **At least 15 completed round-trips across the DYNAMICALLY-ADMITTED symbols combined** (not
   the fixed roster) during that window — the same sample-size floor already used for BTC/CAD's
   and SOL/CAD's own capital-tier gates, applied here to a NEW population of symbols rather than
   assumed to transfer from BTC/SOL's track record.
3. **Net-of-fee PF ≥ 1.2 on those round-trips**, using the SAME fee-accounting fix already
   applied fleet-wide (see "⚠️ PF/win-rate are NET of fees" above) — no gross-PF shortcut. The
   harness's own fills now carry a conservative simulated fee (criterion 1's caveat, closed by
   the tenth pass, corrected by the eleventh) rather than zero, so this number is at least
   MEASURABLE now — still needs the fee-simulation code (both passes) to survive independent
   review before trusting it as accurate, not just present.
4. **A genuine multi-symbol, shared-capital, chronological portfolio replay** — not the
   independent single-symbol backtests `dynamic_universe_backtest.py` already produces (that
   tool answers "does ANY candidate coin have edge in isolation," not "does the ranking +
   shared-slot-contention process itself perform," and was never built to). This is a real,
   currently-missing piece of tooling — building it is itself a prerequisite, not optional.
5. **The independent, already-answered profitability question for BTC/USDT and SOL/USDT (the
   2026-09-12 review-deadline decision) must ALSO have been resolved by then** — dynamic-universe
   activation is not a way to route around that gate; if the FIXED roster's strategy itself
   hasn't cleared its own bar by the time this section's criteria are otherwise met, dynamic mode
   stays off regardless.
6. **Decide the position-limit tradeoff and tune `DYNAMIC_*` thresholds** (Activation steps 1-2
   below) explicitly, in writing, before the paper run starts — not adjusted mid-run to chase a
   result.

**Review date: 2027-03-12** (same date as the 2026-09-12 review-deadline decision, so both
questions get judged together rather than on staggered clocks). If the 60-day paper run hasn't
even STARTED by some meaningfully earlier point, that's its own signal this isn't a current
priority — no obligation to rush it just because a deadline exists.

**Failure handling:** if the paper run's own criteria (2-4 above) aren't met by the review date,
dynamic-universe mode is retired (code and tests may stay, matching the "leave it, don't delete"
convention elsewhere in this file) rather than re-extended on request, mirroring the exact
"retire, don't keep patching" rule the 2026-09-12 decision already set for the fixed roster.

### Shadow-acceptance run — recorded configuration, launch command, and pass/fail checklist (prepared 2026-09-23/24, NOT started)
Written so that whenever a human actually decides to start the 60-day clock, there's one
concrete reference — not a re-derivation from scattered prose — for exactly what to set, how to
launch it, and exactly how the result will be judged. **Writing this down does not start the
clock.** Starting it is a separate, deliberate action a human takes.

**Launch command — explicit, no separate `.env` file needed:** `config.py` calls plain
`load_dotenv()` (no path argument), which reads the working directory's existing `.env` but —
critically — **never overrides a variable already present in the process environment.** Exporting
the shadow-specific keys inline on the command itself therefore reliably wins over whatever the
ambient `.env` says for those SAME keys, with zero risk of editing (or forgetting to revert) a
second file:
```bash
LIVE_TRADING=true \
DRY_RUN=true \
PAPER_MODE=false \
DYNAMIC_UNIVERSE_ENABLED=true \
SIMULATED_MAKER_FEE_PCT=0.0040 \
SIMULATED_TAKER_FEE_PCT=0.0080 \
SIMULATE_MAKER_FILLS=false \
TELEGRAM_CONTROL_ENABLED=false \
.venv/bin/python -m bot.main
```
`TELEGRAM_CONTROL_ENABLED=false` (fourteenth pass) is belt-and-suspenders here — `run()` itself
now REFUSES to start the two-way control poller for a shadow process unless a dedicated
`SHADOW_TELEGRAM_CONTROL_BOT_TOKEN`/`SHADOW_TELEGRAM_CONTROL_CHAT_ID` pair is separately
configured, so this inherited-from-ambient-`.env` value can't actually corrupt the real bot's own
Telegram control channel either way — but setting it explicitly here removes any ambiguity about
intent. Run from the same directory/venv as always. Every OTHER key (`EXCHANGE`, `SYMBOL`, API
credentials, `UNIVERSE_WHITELIST`, `MAX_CONCURRENT_POSITIONS`, `STARTING_CASH`, `DYNAMIC_*`
thresholds, ...) is inherited unchanged from whatever `.env` already has — `DRY_RUN=true` is
what makes this safe regardless of those, not a separate credential set. Because of the
thirteenth pass's fix above, this process's own `logs/shadow/HALT` is completely independent
of the real bot's `logs/HALT` — the shadow run can trade, and can be independently halted
(`touch logs/shadow/HALT`), while the real bot's own halt (or lack of one) is untouched either
way. It can run concurrently with the real (halted) bot process without file collisions —
every state/dashboard/risk-state/trade-log/halt path shadow-isolates.

Before starting: also decide and record (per criterion 6/"Activation step 6") the position-limit
tradeoff (`MAX_CONCURRENT_POSITIONS`/`STARTING_CASH`) and any `DYNAMIC_*` threshold tuning — in
writing, in this file, not adjusted mid-run.

**What gets recorded, and from where:** every fill's `fee_cost` (now real, per the tenth/eleventh
passes) flows into `trade_log`'s CSV rows automatically — no separate reporting step needed. At
the end of the 60 days, read directly from that log: total completed round-trips (dynamically-
admitted symbols only, not the fixed roster), net-of-fee PF and win rate over those round-trips,
and the raw discovery/admission/retirement event history (for a qualitative read on whether the
screener behaved sensibly, independent of the PF number).

**Pass/fail checklist (restates criteria 1-5 above as literal go/no-go items — ALL required):**
- [ ] Ran continuously for ≥60 calendar days on the exact configuration/command above, unmodified mid-run
- [ ] ≥15 completed round-trips across dynamically-admitted symbols combined (only possible at all
      because of the thirteenth pass's halt-isolation fix — confirm that fix is still in place)
- [ ] Net-of-fee PF ≥ 1.2 on those round-trips
- [ ] The multi-symbol shared-capital chronological portfolio replay tool exists and has been run
      against this run's own data (criterion 4 — not yet built as of 2026-09-24)
- [ ] The independent BTC/USDT + SOL/USDT profitability question (2026-09-12 review-deadline
      decision) has ALSO been separately resolved — a passing shadow run does not substitute for it
- [ ] The fee-simulation AND shadow-isolation code (tenth through fifteenth passes) has survived
      at least one independent review pass with zero new findings, so its numbers and its ability
      to run at all — and to not interfere with the real bot's own controls — can be trusted, not
      merely present

**Any unchecked box at the review date (2027-03-12) or whenever the run concludes → retire, per
the failure-handling rule above — do not extend or re-scope the checklist after the fact to fit
whatever the run actually produced.**

### Activation & rollback (for later review — not done as part of this build)
**Activation steps** (a human decision, deliberately not taken here — see the bounded acceptance
criteria immediately above for what must be true FIRST):
1. Decide the real position-limit tradeoff: raise `MAX_CONCURRENT_POSITIONS` (and `STARTING_CASH`
   together, per the existing capital-sizing rule) if dynamic symbols should get room WITHOUT
   displacing BTC/CAD+SOL/CAD's current 2 slots; leave it at 2 if dynamic symbols should simply
   compete for the existing pool.
2. Review/tune the `DYNAMIC_*` filter thresholds (volume/spread/depth/history) for the account's
   actual real size — the defaults mirror the existing $50k/0.15% liquidity gate but haven't been
   walk-forward-validated as a promotion bar the way BTC/SOL's whitelist was.
3. Set `DYNAMIC_UNIVERSE_ENABLED=true` in the LIVE `.env` (not the backtest/validation one).
4. Restart the crypto bot — `cfg.dynamic` is read once at startup like every other config value.
5. `logs/HALT` should be LIFTED only as its own separate, explicit decision — dynamic mode being
   enabled and HALT being engaged are fully independent; leaving HALT engaged after step 4 is a
   safe way to first confirm (via logs / the dashboard card) that discovery/admission is behaving
   as expected with zero risk of a real order, before ever lifting it.
6. Real capital only follows once the bounded acceptance criteria above are actually met — not
   before.

**Rollback steps** (fast, low-risk, always available):
1. `DYNAMIC_UNIVERSE_ENABLED=false` in `.env`, restart the crypto bot — fixed mode resumes
   exactly as before this feature existed; no migration, no state cleanup needed (a
   dynamically-admitted symbol's own `logs/live_state_<SYM>.json` file is simply no longer read
   by anything once dynamic mode is off, and is harmless left in place).
2. If a dynamic symbol is HOLDING a position at the moment of rollback: it keeps trading —
   turning `DYNAMIC_UNIVERSE_ENABLED` off does not itself close any position; a human must
   manage that position manually (or re-enable dynamic mode briefly to let the bot's own SL/TP
   continue managing it) exactly as they would for any other held position.
3. `logs/HALT` remains the immediate, independent full-stop for anything going wrong regardless
   of fixed/dynamic mode — untouched by any of the above.

---

## Roadmap (open items only)

| # | Item | Status |
|---|------|--------|
| F | VPS logrotate | Config ready (`deploy/logrotate_trade_bot.conf`, `/opt/trade_bot` path). Nothing left until a VPS exists — migration deferred. |
| G | Stock-bot headless deploy (IB Gateway + IBC) | Scoped + written 2026-08-27 (`deploy/IBKR_GATEWAY_SETUP.md`, `deploy/stock_bot.service`). No bot code change needed (only `IBKR_PORT=7497→4002`). ~4h + a day's observation. Not started — deferred with the VPS migration; the crypto bot moves first. |
| H | Ollama Cloud key revoke | Confirmed unused 2026-07-16; user parked indefinitely — don't re-raise unprompted. |
| I | IBKR live go-live | Gate-blocked. `LiveTradingGate` Gates 1-3 code-enforced in `IBKRExecutor.__init__()`. Gate 1 now 15/16 FAIL (T, small-sample window effect — 2026-09-12 re-run); Gate 2 SKIPPED (AI disabled, 2026-09-10 fix — no longer a permanent blocker); Gate 3 PENDING (7/30 live trades). Two blockers now, not one. |
| J | USD symbol re-screen | Automated monthly via `rescreen.py` (now genuinely covers the USD leg as of 2026-08-24). |
| K | ATR SL for SYN/LINK/PUMP | SOL/CAD promoted 2026-08-25. SYN/PUMP/LINK validation-complete but blocked on new capital + an un-built FX-conversion layer (both need a deposit). None promoted. Detail: `.memory/decisions/multi-symbol-validation.md`. |
| — | Crypto capital gate | BTC/CAD 0/15 fills (~3–6 wk/trade — don't force it). SOL/CAD 1/15 fills, 1 completed round-trip. |
| — | Stock Phase A gate | Position book toward 30 completed trades / PF ≥ 1.2 / win ≥ 30% (= `LiveTradingGate` Gate 3). ~5/30. |

Everything else from the original near-term roadmap (swing book, IBKR paper executor,
dashboard work, heartbeat/alerting, held-position visibility, rule-based rebuild) is DONE —
see `CLAUDE_HISTORY.md`.
