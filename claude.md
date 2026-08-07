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

## Test Suite Manifest (reconciled 2026-08-05)

Expected total: **486 tests** (verified via `pytest --collect-only -q`; table sum below checked to match exactly). If `pytest --collect-only -q` reports a different number, a file has an import error, was deleted, was added without a manifest update, or was excluded from the runner. Investigate before trusting any green suite result. Suite runtime is ~11s — if it takes minutes, a test is reading live `.env` config.

| File | Tests | What it covers |
|------|-------|----------------|
| `test_indicators.py` | 28 | RSI, EMA, ADX, MACD, ATR calculations |
| `test_live_executor.py` | 27 | LiveExecutor: dry-run, market/limit orders, urgent-exit bypass, fee deduction, state save/load, pre-trade min-size guard, restart recovery (seeds position manager + state machine) |
| `test_capital_pool.py` | 19 | CapitalPool: slot allocation, slot cap, release, edge cases |
| `test_correlation.py` | 17 | Pearson correlation, pct_returns, fetch_correlation |
| `test_stock_correlation.py` | 5 | `stock_bot/risk/correlation.py`: `fetch_correlation_from_closes` — no-network wrapper reusing bot/risk/correlation.py's pearson/pct_returns unchanged |
| `test_stock_correlation_gate.py` | 8 | `stock_bot.main._check_correlation_gate`: blocks on >0.70 correlation with an open position, allows when uncorrelated/no positions/adding to self-held symbol, fails open on missing candle data (candidate or peer), case-insensitive symbol matching, source-inspection guard confirms `run()` still calls it and blocks on a hit |
| `test_stock_macro_calendar.py` | 14 | `stock_bot/risk/macro_calendar.py`: `jobs_report_dates` (12 Fridays/year, first week, invariant-checked not hardcoded), `parse_user_event_dates` (valid/empty/invalid-skipped/whitespace), `is_macro_blackout` (exact-date/before/after/boundary-inclusive, disabled at 0 or negative, jobs-report-alone triggers, nearest-event-wins when multiple in window) |
| `test_stock_macro_blackout_gate.py` | 5 | `stock_bot.main._is_macro_event_blackout` config-reading wrapper: blocks on user-supplied date, disabled at 0, fails open on bad config value, jobs-report-alone still checked with empty user dates, source-inspection guard confirms `run()` calls it market-wide before the per-symbol earnings check |
| `test_stock_vix_crisis.py` | 6 | `stock_bot/risk/vix_crisis.py`: `is_vix_crisis` — at/above/below threshold, None fails open, zero/negative threshold disables |
| `test_stock_vix_crisis_gate.py` | 2 | Source-inspection guard: `run()` fetches `^VIX`, computes crisis mode via `is_vix_crisis`, and gates new BUYs on it (reuses the same `_regime_ok` flag as the SPY regime filter) |
| `test_stock_settlement_csv.py` | 11 | Settlement/FX tax record-keeping (Canadian ACB/FX, minimal scope — data capture only, no gain computation): `_next_business_day` T+1-skip-weekends (both paper.py and ibkr.py copies), frozen `paper_trades.csv`/`ibkr_trades.csv` header proven UNCHANGED, new settlement CSV written on BUY and SELL with correct join key (timestamp/symbol/side) back to the frozen CSV, CAD symbol records fx_rate=1.0 |
| `test_risk_manager.py` | 20 | RiskManager: halt gate, daily loss, position size, SL/TP bypass, state persistence, per-symbol caps, aggregate account breakers |
| `test_fill_recording.py` | 8 | BUG 1: qty=0 fill — filled priority, amount fallback, guard, TradeLog guard |
| `test_external_holdings.py` | 6 | External-holdings guard in _sync_position (adopt=false/true) |
| `test_executor.py` | 6 | PaperExecutor: BUY/SELL, insufficient cash, history |
| `test_drift_escalation.py` | 8 | Drift: tests REAL `_evaluate_drift()` from bot.main — escalation, ack (no re-alert on unchanged drift), changed-amount re-alert, resolution reset |
| `test_tsx_validation.py` | 5 | Stock-bot TSX price sanity check |
| `test_stock_breaker.py` | 14 | Stock-bot circuit breakers (StockPaperExecutor): daily-loss restart baseline includes position marks; weekly-loss/drawdown-halt/kill-switch tiers — reject-on-trip, halt auto-lifts on recovery, kill switch stays sticky through recovery and across restart, SELL never blocked, peak-equity persistence, drawdown_status() warning flag; per-position ATR stop-pct override — defaults to baseline, persists across restart, clears on full close, survives a partial close |
| `test_candle_watchdog.py` | 5 | Candle watchdog: timing, alert, no double-fire |
| `test_halt_flag.py` | 5 | Manual halt kill-switch: logs/HALT flag file engage/lift, ownership guard |
| `test_orphaned_positions.py` | 5 | Startup orphan check: open position outside this run's symbol list alerts (removed-from-whitelist safety) |
| `test_universe.py` | 4 | Universe screener: scoring, momentum filter, fallback |
| `test_main_strategy.py` | 2 | Strategy builder: full config wiring |
| `test_fast_validator_exits.py` | 6 | FastValidator exits: MAX_HOLD live-price fallback, corruption guard, SL regression |
| `test_paper_report.py` | 10 | Expectancy math: IBKR commission model, net-of-cost flip, report rendering, merged paper+IBKR position book, IBKR account section, active-book state synthesis, live-cash-snapshot precedence over stale fill CSV |
| `test_exit_policy.py` | 11 | Stock-bot asymmetric exit bars: single-verdict exit, 2-strike SELL streak, streak resets, AC.TO incident regression |
| `test_stock_backtest_engine.py` | 11 | Stock backtest engine: next-open fills, intra-candle SL/TP, gap handling, slippage/commission math, walk-forward gating |
| `test_stock_rules.py` | 5 | Rule signals: live==backtest replay parity, drop_last (forming candle), determinism, validated-parameter pin |
| `test_audit_scheduler.py` | 14 | In-bot audit scheduler: tests REAL `_audit_due()` — daily catch-up, once-per-day, Mon-anchored weekly, monthly 1st-anchored (re-screen), missed-run catch-up |
| `test_limit_chase_recovery.py` | 6 | 2026-07-15 unrecorded-fill regression: market-fallback polling, actual-type amount inference, cancel-race double-fill guard |
| `test_ibkr_executor.py` | 48 | IBKRExecutor (hermetic FakeIB): live-port/paper-account guards, contract mapping (.TO↔TSE/CAD, bare NYSE cross-listings→NYSE), broker-price fills, timeout rejection, cancel-race fill recording, realized-PnL persistence, try_reconnect probe (redial/never-raise/no-op), low-equity FX/margin-minimum guard (CAD exempt), starting_cash auto-rebaseline on external reset/deposit, live-cash snapshot persisted + preserved across disconnect, sector-concentration gate (reject 3rd same-sector position, allow add-on to already-held symbol, allow different sector), weekly-loss/drawdown-halt/kill-switch tiers (reject-on-trip, halt auto-lifts, kill switch sticky + persists across restart, SELL never blocked, peak-equity persistence, warning-status flag), per-position ATR stop-pct override (default/persistence/cleared-on-full-close), check_exposure projected (pending-trade-value) exposure — defaults to current-state-only, catches an oversized single BUY, allows one that stays under cap |
| `test_fx_sizing.py` | 14 | USD/CAD sizing fix (2026-07-31): `is_cad_symbol`, `get_usd_cad_rate` (fetch/fallback/cache), StockPaperExecutor mixed-currency `total_value`/`check_exposure`, sector-concentration gate (reject 3rd same-sector position, allow add-on to already-held symbol, allow different sector), check_exposure projected (pending-trade-value) exposure — defaults to current-state-only, catches an oversized single BUY, allows one that stays under cap |
| `test_heartbeat.py` | 8 | Heartbeat pings (bot/alerts/heartbeat.py): URL-off, success/failure never raise, healthy_fn gate |
| `test_tws_monitor.py` | 6 | TwsConnectionMonitor state machine: blip tolerance, alert-once per outage, recovery notice |
| `test_atr_sizing.py` | 7 | calc_trade_qty_atr_risk: dollar-risk-at-stop == fixed-SL baseline, tight-stop cap, fallbacks |
| `test_stock_atr_sizing.py` | 7 | Stock-bot analog: `StockConfig.calc_shares_atr_risk` (whole-share sizing) — same invariant, opt-in via PAPER_ATR_SIZING_ENABLED (default false) |
| `test_stock_telegram.py` | 7 | Stock→Telegram relay: root-.env credential sourcing, ops_alert/fill forwarding, HIGH-only filter, channel-off no-ops |
| `test_crash_hardening.py` | 9 | atomic_write_json (valid/replace/no-tmp/parents/old-file-preserved), send_now sync + disabled, crash-alert helpers never raise |
| `test_engine_params.py` | 8 | `engine_kwargs_from_cfg` builder: keys accepted by engine.run, ATR keys sourced from cfg, previously-drifted keys present, macd_enabled + Mode A/B entry params sourced from cfg, generic parity test (every StrategyConfig∩IndicatorConfig field reaches the backtest), both validation scripts use the builder |
| `test_alert_evaluator.py` | 4 | AlertEvaluator EARNINGS_SOON: held-vs-not-held priority/message, live-executor-only held-position source (no static PORTFOLIO tracker) |
| `test_crypto_telegram.py` | 2 | TelegramAlerter.fill() reason line: included when given, omitted when absent |
| `test_liveness.py` | 7 | LivenessTracker (bot/alerts/liveness.py): touch/is_alive/staleness boundary, simulated hang between touches |
| `test_ai_engine_timeout.py` | 2 | nvidia_nim AI client is constructed with `timeout=_TIMEOUT_S`; empty `completion.choices` degrades to a HOLD verdict instead of raising TypeError |
| `test_earnings_cache.py` | 4 | Earnings-fetch cache: failures use a short 1h TTL (retry soon) vs successes using the full 24h TTL, boundary behavior for both, concurrent fetches serialized by `_yf_lock` |
| `test_yf_client_retry.py` | 4 | `fetch_with_retry`: generic exceptions now retried with a short delay (not zero retries), give up after max_attempts, short delay ≠ rate-limit backoff, rate-limit path unchanged |
| `test_research_aggregator_timeout.py` | 1 | Per-source research-fetch timeout: earnings gets a wider budget (45s) than news (15s) |
| `test_kraken_retry.py` | 4 | `bot/exchanges/retry.fetch_with_retry`: succeeds without retrying, retries on failure and can recover, raises the last exception after exhausting attempts, custom attempts/delay respected |
| `test_shadow_signal_retry.py` | 3 | `shadow_signal.shadow_replay` Kraken fetch now wrapped in `fetch_with_retry` (2026-08-05 fix — a single fetch hiccup used to waste the whole day's shadow audit): transient failure recovers, persistent failure still returns `[]` but only after retrying (not a silent single miss), first-try success doesn't retry |
| `test_unified_dashboard.py` | 9 | `unified_dashboard._read_gate_stats`/`_gate_tracker_section` shadow-match-rate parsing (2026-08-05 fix — an unbounded regex let an "N/A" match-rate row fall through to a fabricated reading pulled from an unrelated number later in the report): passing/failing real percentages still parse correctly, N/A no longer bleeds into the unrelated BACKTEST_FEE_PCT number, N/A renders a distinct message from "never run", latest-by-filename report selection; `_crypto_card` STALE-vs-NO-FILLS badge (2026-08-06 fix — state-file age alone flagged a healthy week-quiet bot as STALE): old state + fresh trade_bot.log → "NO FILLS · Nd — bot alive", old state + stale log → still "STALE ... check the bot", fresh state → "LIVE" regardless of log age |
| `test_stock_position_mark_refresh.py` | 4 | Stock-bot daily-loss breaker staleness fix: tests REAL `_mark_positions_to_market()` from stock_bot.main via a mocked `_fetch_symbol_data` — breaker trips from a price move alone (no fill), stays silent within limit, no-ops when executor is None, source-inspection guard confirms `run()` still calls it |
| `test_sl_tp_watcher_audit_log.py` | 9 | First behavioral coverage of `_check_open_positions_sl_tp` (previously untested) plus the 2026-08-06 INFO-level "N/M positions priced" audit log — added after a yfinance outage broke the main scan loop for a full day with no direct evidence either way on whether this separate `get_live_price()` path (fast_info, independent thread) was also blind. Covers full/partial/total pricing failure counts, no-log-when-no-positions, zero-share exclusion, None-executor no-op, and basic STOP_LOSS/TAKE_PROFIT trigger sanity |
| `test_grid_stress_test.py` | 14 | `grid_stress_test.py` pure helpers (crypto research tooling, not the live pipeline): crash-period date parsing, buy-and-hold P&L calc, PASS/MARGINAL/FAILED classification. Hermetic — the actual stress run against Binance is a separate manual step |
| `test_grid_dca_experiment.py` | 12 | `grid_dca_experiment.py` standalone backtest engines (crypto research tooling, not the live pipeline): grid strategy fills/reopens/floor-stop, capital split across slots, fee math on both legs, DCA safety-order averaging + cycle restart, empty-candle edge cases |

Run: `python -m pytest --tb=short -q` — must show **486 passed**.

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
RISK_MAX_DRAWDOWN=0.05        # halt new BUYs if portfolio down >5% from all-time peak
                              # (SELL always allowed — breaker never blocks exits)
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
```

### Native exchange-side stop-loss (crypto — opt-in, off by default)
```
NATIVE_STOP_LOSS_ENABLED=false   # NOT set in .env — using the config.py default (false).
                                  # Added 2026-08-07 from a gap review against external crypto-
                                  # bot best-practice research: the software SL/TP path only
                                  # works while the bot process is alive and polling — a crash
                                  # loop, VPS outage, or extended network partition left an open
                                  # position with zero protection until the bot came back.
```
When enabled, `bot/execution/live_executor.py`'s `sync_protective_stop()` rests a real
Kraken stop order (`create_order(..., params={"stopLossPrice": X})`, executes as market on
trigger — same "never sit in a limit book during a stop" reasoning as `urgent=True`
elsewhere in this file) after every BUY fill, at whatever SL price `bot/main.py` already
computed (ATR if available, else flat `STOP_LOSS_PCT`). **Deliberately static** — it does
NOT track the trailing stop as it rises or reprice mid-trade; it's pure insurance for "the
bot itself is unreachable," not a second copy of the live SL/TP logic. Cancelled the moment
the bot closes the position itself (strategy SELL, software SL/TP, partial TP), so under
normal operation it never fires — Kraken's own trigger only matters when the bot can't get
there first. Order id/price persist in `logs/live_state_BTC_CAD.json` and are reconciled on
every restart: a still-open saved order is kept as-is (level never touches down); a
saved-but-now-gone order (filled while the bot was down — this feature working exactly as
intended) is cleared; a held position with no resting stop at all (feature just enabled, or
the bot crashed before it could place one) gets a same-startup fallback at flat
`STOP_LOSS_PCT` off cost_basis, replaced with the more precise ATR-based level on the next
real BUY for that symbol. Currently trades one symbol with no trailing-stop/partial-TP
config active (`TRAILING_STOP_PCT`/`PARTIAL_TP_PCT` both unset → 0 = disabled), so the
quantity-tracking half of `sync_protective_stop` is defensive/future-proofing rather than
exercised today. Ships **off** — validate on live with the real $77 slot before flipping on;
placement/cancel failures alert to Telegram but never raise, so a Kraken-side rejection
degrades to "no backstop, software SL/TP still fully works while the bot is up," never a
crashed trading loop. Tests: `test_live_executor.py`, 11 new cases (placement, cancel,
resync-on-quantity-change, failure alerting, dry-run/flag-off no-ops, restart reconciliation
for all three states above).

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
Expected (current, since macd_enabled was wired into `engine_kwargs_from_cfg`, 2026-07-20):
**32 trades, PF 1.72, 37.5% win rate.** Hash `659d1c03987b72fd` unchanged.
If RSI_FILTER_ENABLED=false accidentally: trade count jumps significantly, PF drops below 1.2.

Reproducible pinned-window verification (identical result to rolling run):
```
EXCHANGE=binance SYMBOL=BTC/USDT BACKTEST_SINCE=2024-03-07 BACKTEST_UNTIL=2026-06-20 python backtest.py
```

### Canonical strategy fingerprint (BTC/USDT)
- **Strategy hash:** `659d1c03987b72fd`
- **Hashed files (behavior-defining only):** `bot/strategy/indicator_strategy.py`,
  `bot/strategy/threshold_strategy.py`, `bot/indicators/indicators.py`
  (fingerprint.py and __init__.py excluded — non-behavioral)
- **Current result:** 32 trades, PF 1.72 (see "How to verify" above). Full research
  trail (why the trade count moved 58→39→35→32 across sessions) is in `CLAUDE_HISTORY.md`.
- Stamp after each passing walk-forward: `python stamp_strategy.py` → `logs/validated_strategy_hash`
- If the bot or backtest prints `STRATEGY CODE DIFFERS`, re-run walk-forward before trusting any PF numbers

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
  research: software SL/TP only protects a position while the bot process is alive. Ships
  **off** (`NATIVE_STOP_LOSS_ENABLED=false`) pending live validation before enabling on the
  real $77 slot. Same research pass flagged three more gaps not yet acted on: crypto's risk
  engine still has the old single-tier drawdown/daily-loss shape (no weekly-loss tier, no
  sticky kill switch — the stock bot got that upgrade 2026-08-05, crypto didn't), no
  real-time slippage guard on fills, and the candle watchdog alerts on a stale feed but never
  halts trading on one.
- **Stock bot:** live on IBKR paper (DUQ273338, reset to $5,000 CAD 2026-07-20). Swing book
  retired (`FAST_ENABLED=false`) — position book (rule-based, Mode A/B) is the only active
  book. TSX symbols are **permanently** advisory-only — CIRO regulation blocks API orders on
  Canadian exchanges (never re-add `.TO` symbols to RULE_WHITELIST). AI provider `nvidia_nim`,
  model `mistralai/mistral-small-4-119b-2603`. Daily-loss breaker now marks open positions to
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
