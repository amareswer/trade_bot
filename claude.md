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

**Expected total: 864 tests** (`pytest --collect-only -q`). If the count disagrees: a file
has an import error, was deleted, was added without a manifest bump, or was excluded from the
runner — investigate before trusting a green suite. Suite runtime ~9–26s; minutes means a
test is reading live `.env` config. The per-row table sum below lags the header total by ~22
(pre-existing row-vs-total drift; `--collect-only` and this header agree). Full count-delta
history: `CLAUDE_HISTORY.md` → "CLAUDE.md trim, 2026-09-01" → "count-delta history".

Run: `python -m pytest --tb=short -q` — must show **864 passed**.

| File | Tests | What it covers |
|------|-------|----------------|
| `tests/shared/test_indicators.py` | 30 | RSI, EMA, ADX, MACD, ATR; regime-classification self-referential-ATR-baseline regression |
| `tests/crypto/test_live_executor.py` | 65 | LiveExecutor: dry-run, market/limit orders, urgent-exit bypass, fee deduction, state save/load, min-size guard, restart recovery, native static + trailing stop-loss backstop (placement/cancel/resync/failure-alert/restart reconciliation/quantity reconciliation/untracked-order adoption/multi-stop ambiguity), `native_stop_price` property, slippage guard, maker→taker silent-fallback alert, native-stop pre-cancel-on-SELL (2026-08-27 deadlock incident) |
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
| `tests/stock/test_paper_report.py` | 10 | Expectancy math: IBKR commission model, net-of-cost flip, merged paper+IBKR book, IBKR account section, live-cash-snapshot precedence |
| `tests/stock/test_exit_policy.py` | 11 | Stock asymmetric exit bars: single-verdict exit, 2-strike SELL streak, streak resets, AC.TO incident regression |
| `tests/stock/test_stock_backtest_engine.py` | 14 | Stock backtest engine: next-open fills, intra-candle SL/TP, gap handling, slippage/commission math, walk-forward gating, optional ATR(14)×mult stop mode |
| `tests/stock/test_stock_rules.py` | 5 | Rule signals: live==backtest replay parity, drop_last, determinism, validated-parameter pin |
| `tests/crypto/test_audit_scheduler.py` | 14 | REAL `_audit_due()` — daily catch-up, once-per-day, Mon-anchored weekly, monthly 1st-anchored re-screen, missed-run catch-up |
| `tests/crypto/test_limit_chase_recovery.py` | 6 | 2026-07-15 unrecorded-fill regression: market-fallback polling, actual-type amount inference, cancel-race double-fill guard |
| `tests/stock/test_ibkr_executor.py` | 66 | IBKRExecutor (hermetic FakeIB): live-port/paper-account guards, contract mapping, broker-price fills, timeout rejection, cancel-race fill recording, realized-PnL persistence, try_reconnect probe, FX/margin-minimum guard (**checks NET-LIQ, not free cash** — 2026-08-31 fix), sector-concentration gate, weekly/drawdown-halt/kill-switch tiers, per-position ATR stop-pct override, projected-exposure check, LiveTradingGate enforcement, TWS-query resilience (last-good cache), `ibkr_trades.csv` write buffer/retry, Error 10349 slow-resubmit fill (20s grace + `tif="DAY"`), daily-loss calendar-day anchoring |
| `tests/stock/test_fx_sizing.py` | 14 | USD/CAD sizing: `is_cad_symbol`, `get_usd_cad_rate`, mixed-currency `total_value`/`check_exposure`, sector-concentration gate, projected-exposure check |
| `tests/stock/test_screener_in_distribution.py` | 5 | In-distribution ATR%/liquidity filter (`stock_bot/data/screener.py`, replacement safety net after RULE_WHITELIST stopped gating BUYs) |
| `tests/stock/test_accuracy_tracker.py` | 18 | `LiveTradingGate` gates — Gate 1 (`stock_backtest_latest.json` vs `RULE_WHITELIST`), Gate 2 (AI confidence-band edge), Gate 3 (≥30 round-trips/PF≥1.2/win≥30%) |
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
| `tests/stock/test_sl_tp_watcher_audit_log.py` | 12 | `_check_open_positions_sl_tp` behavior + "N/M positions priced" audit log + rejected-SL/TP-exit `else` branch (`logger.error` + `StuckLoopDetector`) |
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
| `tests/stock/test_blocked_rule_buys_alert.py` | 10 | `_evaluate_blocked_rule_buys_alert`: end-of-cycle debounced digest, edge-triggered on the `{symbol: gate}` mapping, `_BLOCKED_BUY_ABSENT_CYCLES_TO_CLEAR=3` debounce, all-clear message, source guard |

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
TAKE_PROFIT_PCT=0.10
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
TAKE_PROFIT_PCT=0.10
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
                                    # PAPER_RISK_PCT=0.20, ~5 full positions = 100% invested. ZERO cash
                                    # buffer accepted, eyes open. Does NOT apply to the real-money crypto bot.
PAPER_MAX_POSITIONS=6              # 4→6 (2026-08-31)
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
- **Stock (2026-08-27):** `stock_bot.main._evaluate_blocked_rule_buys_alert` — end-of-cycle
  debounced `ops_alert` digest listing every symbol whose rule BUY a gate held (MACRO/
  EARNINGS_BLACKOUT, REGIME_SKIP, VIX_CRISIS, MAX_EXPOSURE/MAX_POSITIONS, CORRELATION,
  SIZE_SKIP). Edge-triggered on the `{symbol: gate}` mapping;
  `_BLOCKED_BUY_ABSENT_CYCLES_TO_CLEAR=3` debounce so a symbol flapping near a gate alerts once.

### Settlement date + FX-rate tax record-keeping (stock bot)
`paper_trades.csv` / `ibkr_trades.csv` are UNCHANGED (9-column schema frozen). New data goes
into `paper_trades_settlement.csv` / `ibkr_trades_settlement.csv` (columns `timestamp, symbol,
side, settlement_date, fx_rate_at_trade`), joined by `(timestamp, symbol, side)`. Written on
every fill, best-effort. `settlement_date` is T+1 skipping weekends only (no holiday
calendar). `fx_rate_at_trade` is `1.0` for CAD symbols, live USD/CAD otherwise. Data capture
only — no ACB/gain computation, no CRA report (descoped 2026-08-05; still paper trading).

### IBKR executor readiness hardening (stock bot — 2026-08-27)
`IBKRExecutor._account_value()` / `positions_snapshot()` cache last-good and serve it on a
transient TWS failure (was a fabricated `0.0`/`{}` → every BUY rejected / SL/TP watcher
blind). `_note_sync(ok)` flips `executor.sync_healthy` on the edge → edge-triggered
`ops_alert`. `_record_trade()` CSV append buffers a failed row (`_unwritten_csv_rows`) and
retries on the next fill; `executor.csv_write_healthy` False while buffered. Order-timeout
path left as-is (already alerts + the cancel-race grace window records a beating fill).

### LiveTradingGate — stock bot IBKR readiness check (repaired + code-enforced 2026-08-20)
`stock_bot/analysis/accuracy_tracker.py`. `IBKRExecutor.__init__()` on a live port with
`allow_live=True` calls `LiveTradingGate().evaluate()` and raises `ValueError` naming every
non-PASS gate unless Gates 1-3 are all PASS (before any TWS connection). Paper-mode callers
never reach it.
- **Gate 1** — every current `RULE_WHITELIST` symbol has `verdict: PASS` in
  `logs/stock_backtest_latest.json`. **Status: 16/16 PASS** (re-run 2026-08-28 — AMD now
  passes, small-sample window-boundary effect, was 15/16 on 2026-08-20).
- **Gate 2** — AI confidence-band edge: ≥10 completed MED/HIGH-confidence (80+) round-trips,
  ≥55% win rate. **PENDING.**
- **Gate 3** — position book: ≥30 completed round-trips, PF≥1.2, win≥30%, all three.
  **PENDING (~5/30).**
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
`UNIVERSE_SIZE=30` top-movers scanned per cycle on top of the ~28 `WATCHLIST` symbols (raised
15→30, 2026-08-27, scan breadth only — the rule criteria + in-distribution screener are
unchanged, `interval=1d` untouched). Refreshed on the **first LIVE scan cycle of each day**
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
Expected (rolling, drifts as the window advances): **~32 trades, PF ~2.1, ~37% win
rate**, hash `b30f2f9e769c8d41` (was 31 / 2.19 / 38.7% on 2026-08-20; 32 / 2.10 / 37.5%
on 2026-09-02 — pure new-candle drift, hash identical). If `RSI_FILTER_ENABLED=false`
accidentally: trade count jumps, PF drops below 1.2. **Use the pinned-window check below
for a deterministic pass/fail.**

Reproducible pinned-window check (deterministic — data range fixed):
```
EXCHANGE=binance SYMBOL=BTC/USDT BACKTEST_SINCE=2024-03-07 BACKTEST_UNTIL=2026-06-20 python backtest.py
```
Expected: **30 trades, PF 1.94, 40.0% win rate** (5010 candles), hash `b30f2f9e769c8d41`.
Use the rolling run for the canonical fingerprint, this pinned run for "did my
environment/data change break something".

### Canonical strategy fingerprint (BTC/USDT)
- **Strategy hash:** `b30f2f9e769c8d41`
- **Hashed files (behavior-defining only):** `bot/strategy/indicator_strategy.py`,
  `bot/strategy/threshold_strategy.py`, `bot/indicators/indicators.py`
- **Current result:** rolling ~32 trades, PF ~2.1 (pinned window: 30 / 1.94). Trade-count
  evolution across sessions (58→39→35→32→31) and the 2026-08-20
  self-referential-ATR-regime-baseline fix that produced the current hash are in
  `CLAUDE_HISTORY.md`.
- Stamp after each passing walk-forward: `python stamp_strategy.py` → `logs/validated_strategy_hash`
- If the bot or backtest prints `STRATEGY CODE DIFFERS`, re-run walk-forward before trusting any PF numbers.

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
| BTC/CAD | ACTIVE | Walk-forward re-confirmed on current code: all windows PF > 1.0. Original validated pair. |
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
2. Position sizing — flat notional (`PAPER_RISK_PCT=0.20`). ATR-inverse sizing gated behind
   `PAPER_ATR_SIZING_ENABLED` (still `false` — AMD/KO fail its walk-forward).
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
| I | IBKR live go-live | Gate-blocked. `LiveTradingGate` Gates 1-3 code-enforced in `IBKRExecutor.__init__()`. Gate 1 16/16 PASS; Gates 2-3 PENDING (~5/30 live trades). |
| J | USD symbol re-screen | Automated monthly via `rescreen.py` (now genuinely covers the USD leg as of 2026-08-24). |
| K | ATR SL for SYN/LINK/PUMP | SOL/CAD promoted 2026-08-25. SYN/PUMP/LINK validation-complete but blocked on new capital + an un-built FX-conversion layer (both need a deposit). None promoted. Detail: `.memory/decisions/multi-symbol-validation.md`. |
| — | Crypto capital gate | BTC/CAD 0/15 fills (~3–6 wk/trade — don't force it). SOL/CAD 1/15 fills, 1 completed round-trip. |
| — | Stock Phase A gate | Position book toward 30 completed trades / PF ≥ 1.2 / win ≥ 30% (= `LiveTradingGate` Gate 3). ~5/30. |

Everything else from the original near-term roadmap (swing book, IBKR paper executor,
dashboard work, heartbeat/alerting, held-position visibility, rule-based rebuild) is DONE —
see `CLAUDE_HISTORY.md`.
