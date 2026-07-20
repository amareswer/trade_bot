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
  then upgraded 1.2.0 → 1.5.1 as its own change (2026-07-05): 168 tests green and all
  four live data paths smoke-tested (daily US/TSX candles, guarded live price,
  FastValidator 1h fetch, multi-ticker batch). pandas stays 2.3.3 — pip wanted 3.0.x
  on 3.11; upgrade it deliberately, not as a side effect.
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

## Test Suite Manifest (as of 2026-07-03)

Expected total: **289 tests** (as of 2026-07-18). If `pytest --collect-only -q` reports a lower number, a file has an import error, was deleted, or was excluded from the runner. Investigate before trusting any green suite result. Suite runtime is ~5s — if it takes minutes, a test is reading live `.env` config (see hermeticity note under Execution hardening).

| File | Tests | What it covers |
|------|-------|----------------|
| `test_indicators.py` | 28 | RSI, EMA, ADX, MACD, ATR calculations |
| `test_live_executor.py` | 22 | LiveExecutor: dry-run, market/limit orders, urgent-exit bypass, fee deduction, state save/load |
| `test_capital_pool.py` | 19 | CapitalPool: slot allocation, slot cap, release, edge cases |
| `test_correlation.py` | 17 | Pearson correlation, pct_returns, fetch_correlation |
| `test_risk_manager.py` | 20 | RiskManager: halt gate, daily loss, position size, SL/TP bypass, state persistence, per-symbol caps, aggregate account breakers |
| `test_fill_recording.py` | 8 | BUG 1: qty=0 fill — filled priority, amount fallback, guard, TradeLog guard |
| `test_external_holdings.py` | 6 | External-holdings guard in _sync_position (adopt=false/true) |
| `test_executor.py` | 6 | PaperExecutor: BUY/SELL, insufficient cash, history |
| `test_drift_escalation.py` | 8 | Drift: tests REAL `_evaluate_drift()` from bot.main — escalation, ack (no re-alert on unchanged drift), changed-amount re-alert, resolution reset |
| `test_tsx_validation.py` | 5 | Stock-bot TSX price sanity check |
| `test_stock_breaker.py` | 3 | Stock-bot daily-loss breaker: restart baseline includes position marks |
| `test_candle_watchdog.py` | 5 | Candle watchdog: timing, alert, no double-fire |
| `test_halt_flag.py` | 5 | Manual halt kill-switch: logs/HALT flag file engage/lift, ownership guard |
| `test_orphaned_positions.py` | 5 | Startup orphan check: open position outside this run's symbol list alerts (removed-from-whitelist safety) |
| `test_universe.py` | 4 | Universe screener: scoring, momentum filter, fallback |
| `test_main_strategy.py` | 2 | Strategy builder: full config wiring |
| `test_fast_validator_exits.py` | 6 | FastValidator exits: MAX_HOLD live-price fallback, corruption guard, SL regression |
| `test_paper_report.py` | 9 | Expectancy math: IBKR commission model, net-of-cost flip, report rendering, merged paper+IBKR position book, IBKR account section, active-book state synthesis |
| `test_exit_policy.py` | 11 | Stock-bot asymmetric exit bars: single-verdict exit, 2-strike SELL streak, streak resets, AC.TO incident regression |
| `test_stock_backtest_engine.py` | 11 | Stock backtest engine: next-open fills, intra-candle SL/TP, gap handling, slippage/commission math, walk-forward gating |
| `test_stock_rules.py` | 5 | Rule signals: live==backtest replay parity, drop_last (forming candle), determinism, validated-parameter pin |
| `test_audit_scheduler.py` | 14 | In-bot audit scheduler: tests REAL `_audit_due()` — daily catch-up, once-per-day, Mon-anchored weekly, monthly 1st-anchored (re-screen), missed-run catch-up |
| `test_limit_chase_recovery.py` | 6 | 2026-07-15 unrecorded-fill regression: market-fallback polling, actual-type amount inference, cancel-race double-fill guard |
| `test_ibkr_executor.py` | 21 | IBKRExecutor (hermetic FakeIB): live-port/paper-account guards, contract mapping (.TO↔TSE/CAD, bare NYSE cross-listings→NYSE), broker-price fills, timeout rejection, cancel-race fill recording, realized-PnL persistence, try_reconnect probe (redial/never-raise/no-op) |
| `test_heartbeat.py` | 8 | Heartbeat pings (bot/alerts/heartbeat.py): URL-off, success/failure never raise, healthy_fn gate |
| `test_tws_monitor.py` | 6 | TwsConnectionMonitor state machine: blip tolerance, alert-once per outage, recovery notice |
| `test_atr_sizing.py` | 7 | calc_trade_qty_atr_risk: dollar-risk-at-stop == fixed-SL baseline, tight-stop cap, fallbacks |
| `test_stock_telegram.py` | 7 | Stock→Telegram relay: root-.env credential sourcing, ops_alert/fill forwarding, HIGH-only filter, channel-off no-ops |
| `test_crash_hardening.py` | 9 | atomic_write_json (valid/replace/no-tmp/parents/old-file-preserved), send_now sync + disabled, crash-alert helpers never raise |
| `test_engine_params.py` | 6 | `engine_kwargs_from_cfg` builder: keys accepted by engine.run, ATR keys sourced from cfg, previously-drifted keys present, macd_enabled sourced from cfg, both validation scripts use the builder |

Run: `python -m pytest --tb=short -q` — must show **289 passed**.

---

## VALIDATED TRADING CONFIG

As of 2026-06-19, the following configuration has passed:
- 5000-candle backtest (BTC/USDT 4h, Mar 2024–Jun 2026): PF 1.79, 58 trades, win rate 32.8%, max DD -5.12%, return -4.70% (at 0.8% fee)
  NOTE: This fingerprint was produced on an older strategy (simple RSI < 30 BUY gate).
  The current code uses Mode A/B (pullback RSI 38–58 + breakout); see "trade count evolution" below.
- SL/TP sweep: SL=1.5% / TP=10% validated 2026-06-19 (was TP=4.5%)
- Walk-forward as of 2026-06-19 (OLD strategy — RSI < 30 BUY gate, no EMA spread filter):

  | Window | Candles | PF   | Return  |
  |--------|---------|------|---------|
  | Full   | 5000    | 1.79 | -4.70%  |
  | 4000   | Sep24   | 1.83 | -4.06%  |
  | 3000   | Feb25   | 2.02 | -2.16%  |
  | 2000   | Aug25   | 1.37 | -2.17%  |
  | 1000   | Jan26   | 1.25 | -1.32%  |

  All 5 windows PF > 1.0 — walk-forward passed.

- Walk-forward re-run 2026-07-02 (CURRENT code — Mode A/B + EMA spread filter + MACD):

  | Window | Candles | Period        | Trades | PF   | Return  |
  |--------|---------|---------------|--------|------|---------|
  | Full   | 5000    | Mar24–Jul26   | 39     | 1.79 | -3.00%  |
  | 4000   | Sep24   | Sep24–Jul26   | 30     | 2.00 | -1.75%  |
  | 3000   | Feb25   | Feb25–Jul26   | 20     | 2.99 | +0.17%  |
  | 2000   | Aug25   | Aug25–Jul26   | 8      | 3.12 | +0.08%  |
  | 1000   | Jan26   | Jan26–Jul26   | 4      | 3.38 | +0.08%  |

  All 5 windows PF > 1.0. 2000c and 1000c have very few trades (8/4) — PF is directionally
  valid but not statistically reliable at these sample sizes. The 5000c (39 trades) and
  4000c (30 trades) windows are the most meaningful and both show PF ≥ 1.79.
- ADX sweep (18 / 25 / 30 / 35): ADX=18 is best on both full history and recent window
- RSI filter confirmed ON: RSI_FILTER_ENABLED=false drops PF from 1.38 → 1.19 and return from +1.51% → -0.10%
- Volume filter tested (VOLUME_K=1.2) and disabled: hurt PF (1.38→1.00), added noise not quality
- EMA spread filter validated 2026-06-27: MIN_EMA_SPREAD_PCT=0.004 (≥0.4%) confirmed real edge:

  | Window                    | Baseline PF | Filtered PF | ΔPF   | Trades filtered |
  |---------------------------|-------------|-------------|-------|-----------------|
  | In-sample  Mar24–Jun26    | 1.61        | 1.78        | +0.17 | 9               |
  | Out-of-sample 2019–2021   | 1.85        | 2.00        | +0.15 | 8               |

  ΔPF nearly identical across periods → not curve fitting. Ranging mode also deleted 2026-06-27
  (25% ranging win rate = trend win rate → no alpha). These two changes together bring in-sample PF from 1.21→1.78.

### Active .env settings (do not change without re-running validation)
ADX_THRESHOLD=18
RSI_FILTER_ENABLED=true
MIN_EMA_SPREAD_PCT=0.004   # validated 2026-06-27: improves PF +0.15–0.17 in both in-sample and OOS
VOLUME_K=0
STOP_LOSS_PCT=0.015   # fallback only as of 2026-07-17 — see ATR_SL_MULT below, takes priority when set
TAKE_PROFIT_PCT=0.10   # was 0.045 — validated 2026-06-19
ATR_SL_MULT=2.0   # ADOPTED 2026-07-17 — see "ATR SL adopted live" entry below. backtest.py reads this too (cfg.backtest.atr_sl_mult, same key) — the canonical fingerprint command below now reflects it.
ATR_SIZING_ENABLED=true   # ADOPTED 2026-07-17 (same evening): caps BUY qty so an ATR stop-out never risks more $ than the fixed-SL baseline (cash × RISK_PER_TRADE_PCT × STOP_LOSS_PCT). Validated: 5-window walk-forward with sizing ON all PF > 1.0 (1.90/2.24/3.46/3.29/2.03). One key, read by BOTH StrategyConfig and BacktestConfig.
BACKTEST_LIMIT=5000
BACKTEST_TIMEFRAME=4h
EXCHANGE=binance
SYMBOL=BTC/USDT

### Live trading settings (Kraken — separate from backtest)
EXCHANGE=kraken
SYMBOL=BTC/CAD
CANDLE_MINUTES=240   # 4h — now matches the validation timeframe (was 60; the 1h config was never backtested — roadmap item 2 closed by this change)
RISK_PER_TRADE_PCT=0.10   # intentionally high at $100 capital (Kraken min order ~$4.50 CAD)
STOP_LOSS_PCT=0.015   # fallback only — ATR_SL_MULT=2.0 takes priority whenever ATR is available at entry (bot/main.py:1813); falls back to this fixed % and logs "ATR SL disabled or unavailable" if ATR can't be computed
TAKE_PROFIT_PCT=0.10   # confirmed 2026-07-01: matches backtest and regime monitor (was stale 0.045)
ORDER_TYPE=limit / LIMIT_ORDER_ENABLED=true   # BUY entries limit-chase for maker rate; ALL SL/TP exits forced to market via urgent=True (2026-07-04)

### How to verify the config is active
Run: EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py
Expected (as of macd_enabled added to engine_kwargs_from_cfg, 2026-07-20): **32 trades, PF 1.72**
(hash `659d1c03987b72fd` unchanged — see "MACD live/backtest divergence resolved" entry below).
This superseded the prior "35 trades, PF ~1.90" figure, which was produced with MACD silently
OFF in the backtest while live always ran with MACD ON — the two disagreed. Sizing-OFF and the
older ATR-only ("35/1.90", "35/1.98") and fixed-SL-only ("39/1.79") figures elsewhere in this
file are historical record of earlier config states, not what a fresh run reproduces today.
If RSI_FILTER_ENABLED=false accidentally: trade count jumps significantly, PF drops below 1.2

Reproducible pinned-window verification (identical result to rolling run):
  EXCHANGE=binance SYMBOL=BTC/USDT BACKTEST_SINCE=2024-03-07 BACKTEST_UNTIL=2026-06-20 python backtest.py
  Expected: 39 trades, PF 1.79 (confirmed 2026-07-02 — matches rolling window exactly)
  BACKTEST_SINCE/BACKTEST_UNTIL change the FETCH start, not just post-fetch filtering.

Note — trade count evolution:
- 2026-06-19 fingerprint (TP=10% validated):      ~58 trades, PF 1.79
  Strategy at this point: simple RSI < 30 oversold BUY gate, no EMA spread filter
- 2026-06-27 (MIN_EMA_SPREAD_PCT=0.004 added):    change reduced trade count further
- Post-commit c94d297 (Mode A/B dual entry):       strategy redesigned — pullback RSI 38–58
  and breakout mode replace simple RSI < 30. This is the primary cause of 58→39.
- 2026-07-02 current expected fingerprint:         39 trades, PF 1.79
  Proven: pinned window (2024-03-07→2026-06-19) gives IDENTICAL result to rolling window.
  Window drift is NOT a factor. Trade count difference is entirely from strategy redesign.
  PF 1.79 is the stable invariant across both old and new strategy versions.

### Canonical strategy fingerprint (BTC/USDT, 2026-07-03)
- **Strategy hash:** `659d1c03987b72fd`
- **Hashed files (behavior-defining only):**
  - `bot/strategy/indicator_strategy.py`
  - `bot/strategy/threshold_strategy.py`
  - `bot/indicators/indicators.py`
  - *(fingerprint.py and __init__.py excluded — non-behavioral)*
- **Window:** BACKTEST_SINCE=2024-03-07 BACKTEST_UNTIL=2026-06-20 (pinned) or rolling 5000 × 4h (same trade count)
- **Result:** 39 trades, PF 1.77 (range 1.77–1.79 depending on rolling-window end date; all > 1.0)
  — this was the fixed-SL-only fingerprint. As of 2026-07-17, `ATR_SL_MULT=2.0` is live (see
  "ATR SL adopted live" entry below) — the hash itself is unchanged (SL is config, not a
  strategy file) but a fresh `backtest.py` run returned 35 trades / PF ~1.98 at that point, not
  this figure. As of 2026-07-20, `macd_enabled` was added to `engine_kwargs_from_cfg()` (see
  "MACD live/backtest divergence resolved" below) — a fresh run now returns **32 trades, PF
  1.72**. Hash still unchanged throughout (none of these were `bot/strategy/*.py` edits).
- **Stamped:** run `python stamp_strategy.py` after each passing walk-forward to write `logs/validated_strategy_hash`
- If the bot or backtest prints `STRATEGY CODE DIFFERS`, re-run walk-forward before trusting any PF numbers
- Prior hash `d3c7c383d91d5ef9` (2026-07-02) was computed over all `bot/strategy/*.py` including fingerprint.py — that scope was wrong. Hash value changed when scope was corrected to behavior-only files. No strategy logic changed.

ATR SL drift incident (resolved 2026-07-02):
- Root cause: .env contained ATR_SL_ENABLED=true (a stale key from a second config system in BacktestConfig)
  while live bot correctly used ATR_SL_MULT=0.0. Backtest was running ATR SL at 2× multiplier
  → 33 trades / PF 2.19 (vs expected 58/1.79). Now resolved: BacktestConfig uses ATR_SL_MULT
  (same key as StrategyConfig), convention mult=0.0 means disabled. No separate _ENABLED key.
- 1 live fill occurred under unvalidated ATR config (2026-06-22 16:36 UTC, pnl=-0.02 CAD, reason='trail_stop')
- Live bot was on validated fixed SL=1.5% from 2026-06-22 21:24 UTC onwards.

### 1h day-trading walk-forward — FAILED (2026-07-10)
Tested whether the current strategy (hash `659d1c03987b72fd`, unchanged) has edge on 1h candles
instead of the validated 4h timeframe — i.e. whether day-trading is viable. Same 5-window
walk-forward discipline used for the 4h re-run on 2026-07-02, run on `EXCHANGE=binance
SYMBOL=BTC/USDT BACKTEST_TIMEFRAME=1h`.

| Window | Period | Trades | Win rate | PF | Return | SL-exit rate |
|--------|--------|--------|----------|-----|--------|--------------|
| 5000c (full) | 2025-12-13 → 2026-07-10 (~7mo) | 19 | 26.3% | **1.04** | -2.89% | 63% |
| 4000c | 2026-01-24 → 2026-07-10 | 16 | 31.2% | 1.25 | -2.10% | 63% |
| 3000c | 2026-03-07 → 2026-07-10 | 11 | 36.4% | **0.99** | -1.75% | 64% |
| 2000c | 2026-04-17 → 2026-07-10 | 4 | 75.0% | 5.20 | -0.01% | 25% |
| 1000c | 2026-05-29 → 2026-07-10 | 3 | 66.7% | 3.20 | -0.15% | 33% |

**Verdict: FAILED.** The two windows with meaningful sample size — the full 7-month history and
the 3000-candle window — come in at PF 1.04 and 0.99 (a straight fail). Compare to the 4h
re-run where all 5 windows passed at PF 1.79–3.38. The last two "passing" windows have only
3-4 trades each — the same small-sample caveat already noted for 4h's 1000c/2000c windows,
not enough to act on alone.

Failure mode matches every rejected altcoin (XRP, ETH, SOL, DOGE, the full USD screen): ~63%
of trades exit via stop-loss at 1h vs a much healthier exit mix on 4h. The Mode A/B entry
logic (pullback RSI 38-58 / breakout) does not have edge at hourly frequency — it is mostly
noise that gets stopped out. Binance also only has ~7 months of 1h history via pagination vs
2.3 years at 4h, so the full-window sample is inherently weaker too.

**Decision: no live day-trading (1h or faster) on this strategy.** `CANDLE_MINUTES=240` (4h)
stays the only validated live timeframe. Do not revisit without either a new/modified
strategy (which would need its own fresh walk-forward and hash stamp) or materially more 1h
history becoming available.

### Config change log (2026-06-19)
Previous validated config: TP=4.5% (PF 1.38 at zero fee)
New validated config: TP=10% (PF 1.79 at zero fee, 1.79 at 0.8% fee)
Reason: fee resilience — TP=10% exit mix is 37 SL / 9 TP / 12 strategy
vs TP=4.5% which was 56 SL / 25 TP / 3 strategy. Higher TP lets strategy
SELL signals do meaningful work, reducing fee sensitivity.

### New code added 2026-06-15
- `calc_trade_qty_sl(cash, entry_price, stop_loss_price)` on AppConfig — SL-based position sizing
  (risks exactly risk_per_trade_pct of cash per trade; falls back to calc_trade_qty if SL=0)
- `volume_k` field wired through IndicatorConfig → StrategyConfig → AppConfig → engine.py → backtest.py → main.py
  (set VOLUME_K=0 to disable; VOLUME_K=1.2 requires current candle volume ≥ 1.2× avg of prior 3)

### Crypto bot hardening (2026-07-03)
- **Manual kill-switch:** `touch logs/HALT` engages the risk manager's manual halt without a
  restart (blocks BUY + strategy SELL; SL/TP exits still fire). `rm logs/HALT` resumes.
  Telegram alert on engage/lift. Helper: `_check_halt_flag()` in `bot/main.py`.
- **Risk breaker state persists across restarts:** `logs/risk_state.json` stores the all-time
  drawdown peak, day-open value, and daily fill count (live mode only — backtests stay
  stateless). Previously a crash/restart silently reset the max-drawdown breaker and the
  daily trade cap. Daily counters only restore if saved on the same UTC day; peak always restores.
- **RiskManager daily reset now uses UTC** (`_utc_today()`), matching candle timestamps and
  the daily P&L alert — was local `date.today()`, resetting counters at local midnight.
- **Daily P&L Telegram alert fires exactly once per UTC day** — date-change trigger replaced
  the `hour==0 and minute==0` window, which double-fired on a 30s loop and could skip entirely.

### Stock bot + unified dashboard (2026-07-04)
- **Stock daily-loss breaker fixed** (roadmap item 7 closed — see strikethrough above).
- **`unified_dashboard.py` rewritten for the multi-symbol bot:**
  - Reads `logs/live_state_*.json` per-symbol files (was reading the legacy
    `logs/live_state.json`, stale since Jun 27 — it still showed the phantom external
    0.000378 BTC position). Legacy path is fallback-only now.
  - Symbols split by UNIVERSE_WHITELIST: active slots get cards (with STALE badge if
    state > 48h old); retired slots (XRP/DOGE leftovers) listed but not counted.
  - New ops strip: kill-switch status (`logs/HALT`), fills today per symbol and breaker
    peak/day-open from `logs/risk_state.json`.
  - Stock positions table shows live price / market value / unrealized P&L via the stock
    bot's own guarded `latest_price()` (falls back to cost marks when yfinance rate-limits).
  - **Now auto-refreshed by the crypto bot** (2026-07-04): `bot/main.py` runs
    `_unified_dashboard_loop()` as a daemon thread — regenerates every 60s via subprocess
    (same isolation pattern as the regime monitor). `UNIFIED_DASHBOARD_INTERVAL=0` disables.
    No separate `--watch` terminal needed. Because each refresh is a fresh subprocess, it
    always runs current code (a long-lived `--watch` from Jun 26 once held stale code in
    memory and overwrote new output every 30s — that failure mode is gone).
  - Stock prices are TTL-cached 15 min in `logs/stock_price_cache.json` (cross-process) —
    without it, per-cycle yfinance calls got rate-limited within minutes.

### Execution hardening (2026-07-04) — audit fixes, strategy files untouched (hash `659d1c03987b72fd` still valid)
- **SL/TP exits are always market orders.** `LiveExecutor.execute(..., urgent=True)` bypasses
  the limit-chase and the ORDER_TYPE=limit path entirely; the SL/TP block in `bot/main.py`
  passes it. Before this, a stop exit could sit in the post-only chase (placed ABOVE the ask,
  up to 4 × LIMIT_CHASE_TIMEOUT_S=120s repricing) while price ran away — with SL=1.5% and
  stops being the majority exit, chase slippage was directly eating the validated edge.
  BUY entries keep the limit-chase (0.40% maker saving, confirmed Jun 14 fill).
  Test: `test_urgent_sell_bypasses_limit_chase`.
- **Partial TP reclassified as an exit** — bypasses the risk gate like SL/TP (a manual HALT
  no longer freezes profit-taking; `RISK_HALT_BLOCKS_STOPS=true` still suppresses it).
- **MTF gate no longer runs on stale data.** Daily closes are fetched per symbol at decision
  time (gate 2c) with a per-symbol cache fallback. Previously loaded once at startup and only
  refreshed AFTER a BUY had been judged — the veto could run on weeks-old daily candles, and
  multi-symbol mode applied the active symbol's daily trend to every symbol.
- **Trailing-stop peak seeding respects TRAILING_STOP_ACTIVATION_PCT** — on BUY the peak is
  seeded only when activation is 0; otherwise the intra-candle block arms it at
  entry × (1 + activation). (Latent — trailing disabled in .env.)
- **Drift reconciliation compares Kraken `total`** (matching `_sync_position`) — `free`
  produced false drift alerts during the settlement window after a fill.
- **Dashboard capital-gate PF is NET of fees and window-scoped.** `unified_dashboard.py`
  `_read_gate_stats`: each SELL's pnl minus its fee minus an equal share of window BUY fees;
  only fills since 2026-06-22 21:24 UTC (validated-config go-live) count; `kraken_backfill`
  and phantom rows excluded; `partial_tp` fills count toward PF but not the 15-round-trip
  gate. Rationale: trades.db `pnl` is gross — at $77 capital, fees dwarf gross P&L (book was
  gross −$0.02 but net −$1.13 when this was fixed). A gross-PF gate could promote capital on
  a losing book.
- **Test suite hermeticity:** `test_fill_recording.py` now forces `limit_order_enabled=False`
  (autouse fixture). Since LIMIT_ORDER_ENABLED=true landed in `.env`, its never-closing-order
  test was rerouted into the limit-chase and busy-spun ~8 minutes at 100% CPU (time.sleep
  mocked + 120s wall-clock poll deadline). Suite runtime is back to ~5s; if it ever takes
  minutes again, suspect a test reading live `.env` config.
- ~~**Known-inert leftover:** the DOGE/CAD liquidity gate in `bot/main.py` is dead code (DOGE
  is BLOCKED) and hardcodes a symbol~~ — DELETED 2026-07-16 (gate block in `bot/main.py` plus
  the `doge_vol_min_cad` PortfolioConfig field/validation/loader). `DOGE_VOL_MIN_CAD` stays
  in `.env` — `regime_monitor.py` still reads it directly for watchlist health reporting.
- `.env` chmod 600 (was world-readable). ~~A bare un-named secret sits as a comment under
  "── Secrets ──" in `.env`~~ — RESOLVED 2026-07-13: identified as an exact duplicate of
  `OLLAMA_CLOUD_API_KEY` (already properly named in `stock_bot/.env`); stray comment copy
  deleted from root `.env`. 2026-07-16: confirmed the key is UNUSED — `AI_PROVIDER=nvidia_nim`
  is the active provider; Ollama Cloud is a dormant fallback. Action is revoke (delete at
  ollama.com) + strip the line from stock_bot/.env, not rotate. **User parked this
  2026-07-16 — deferred indefinitely, low urgency (local machine only; exposure was the
  pre-2026-07-04 world-readable perms).**

### Stock bot Phase A — expectancy measurement (2026-07-04)
Goal: get to 30 completed paper round-trips and measure per-trade expectancy net of costs.
Income targets ($/day) are NOT a bot setting — income = expectancy × capital × frequency, and
expectancy is unmeasured until trades complete. The report's expectancy number is the product.
- **Exit audit result:** main paper book SL/TP watcher healthy (30s cadence, market-hours
  gated); AC.TO/DLTR correctly held inside the −5%/+15% band. No missed exits.
- **FastValidator MAX_HOLD starvation fixed:** feed gaps (rate limit / holiday) skipped ALL
  exit checks, so positions could outlive the 48h design (observed AMZN/HOOD 2026-07-04).
  MAX_HOLD now falls back to the guarded `get_live_price()`; SL/TP still wait for real candles.
- **Price-corruption guard on the fast book:** candle close deviating > FAST_PRICE_SANITY_PCT
  (20%) from the prior close is rejected for entries AND exits. Incident: 2026-06-29 META
  $564.87 entry → phantom "SL" exit at $163.51 (−71% in 20 min, data corruption not market),
  which dragged the signal book to PF 0.07 / −16.85% per signal vs the 3 sane trades
  (+4.0% TP, +1.2% MAX_HOLD, −1.5% SL).
  **Fast book RESET 2026-07-05:** pre-guard history archived to
  `stock_bot/archive/fast_trades_pre_guard_20260705.csv` + `..._state_...json`.
  Every signal from Monday 2026-07-06 onward is post-guard — stats clean from trade one.
- **FastValidator scope widened:** scans watchlist + top FAST_MOVERS_COUNT (5) universe
  movers per cycle (was watchlist-only). Cap bounds yfinance fetch volume — raise carefully,
  rate-limit spirals are a known failure mode.
- **Expectancy report:** `stock_bot/analysis/paper_report.py` now prints per-trade net $ / net %,
  net PF, trades/week pace, and projected $/week — net of the IBKR Pro fixed commission model
  (COMMISSION_PER_SHARE_USD/CAD + COMMISSION_MIN_USD/CAD in stock_bot/.env). Slippage is NOT
  added there: the paper executor already applies PAPER_SLIPPAGE_BPS to every fill. The fast
  book is reported separately as a unit-sized signal book (% stats only — never $ expectancy).
- Phase A gate stays: 30 completed trades, PF ≥ 1.2, win rate ≥ 30% before any IBKR live step.

### Ops changes (2026-07-05)
- **Runtime interpreter is the `.venv` (Python 3.11.15)** — see CODING STYLE for launch/test
  commands and the pandas-2.3.3 hold. System python3 (3.9) must not run the bots.
- **yfinance upgraded 1.2.0 → 1.5.1** (own change, after the venv switch): 168 tests green,
  all four live data paths smoke-tested (daily US/TSX candles, guarded live price,
  FastValidator 1h fetch, multi-ticker batch). Better rate-limit resilience via curl_cffi.
- **Runtime data untracked from git:** `stock_bot/fast_trades.csv` was accidentally tracked —
  removed from the index (`git rm --cached`). Root `.gitignore` now covers all stock-bot
  runtime files (`fast_trades.csv`, `paper_state.json`, `universe_cache.json`, `archive/`).
  Rule: code and config templates go in git; anything the bots write at runtime does not.
- **Startup banner shows the executor's restored cash**, not `.env` PAPER_STARTING_CASH
  (banner printed $1,000.00 while the restored book held $520.71).
- **Dashboard stock-bot heartbeat amber window 65h → 80h** — TSX holiday Mondays no longer
  show a false "down?" on Tuesday morning.
- ~~Pending: restart stock bot under venv~~ — DONE 2026-07-05 10:42 (verified via ps: running
  on 3.11 with caffeinate).
- **Crypto bot log pollution fixed:** `bot/main.py` installed the root RotatingFileHandler at
  IMPORT time, so every pytest run wrote test noise into `logs/trade_bot.log` — it faked the
  dashboard heartbeat (log mtime = "alive"), buried real forensics, and one pytest run rotated
  the live log at 10MB. Handler setup now lives in `_setup_logging()`, called only from
  `run()`. Verified: importing bot.main installs zero handlers; pytest no longer touches the
  log. (`stock_bot/main.py` had the same import-time pattern — FIXED 2026-07-16: handlers
  now install in `_setup_logging()`, called only from `run()`; verified importing
  stock_bot.main installs zero handlers.)
- **Crypto bot found DOWN during this session** (no process; last real log 09:23 ET after
  ~3 min of Kraken network errors, no traceback — consistent with closed terminal or Mac
  sleep; it was launched WITHOUT caffeinate). Position was 0 — no exposure while down.
  It picked up the 2026-07-04 execution fixes at its 22:09 ET restart.
  **Launch it like the stock bot from now on:** `caffeinate -i .venv/bin/python -m bot.main`

### Ops changes (2026-07-06)
- **Scheduling is now repo-tracked:** `ops/crontab.txt` is the source of truth for cron on any
  machine — install with `crontab ops/crontab.txt`, never edit the live crontab by hand.
  Installed locally 2026-07-06: daily `shadow_signal.py` (02:00 local ≈ 06:00 UTC) and weekly
  Monday `live_comparison.py` (roadmap item 17 closed). VPS migration note in the file:
  convert to systemd timers (`Persistent=true`) via deploy.sh.
- **Shadow signal 2026-07-06 run: 100% match rate (PASS ≥95%).** Report:
  `logs/shadow_report_20260706.md`.
- **pytest log-pollution fix verified:** full suite (168 passed, 3.6s) leaves
  `logs/trade_bot.log` mtime/size untouched. The test noise stamped 2026-07-05 10:42 in the
  log predates the fix — historical, not a regression.
- **`.env` secret rotation deferred by user** (2026-07-06, "will do it later") — still the
  only open security item. UPDATE 2026-07-13: secret identified (Ollama Cloud key duplicate,
  removed from root `.env`). UPDATE 2026-07-16: key confirmed unused (provider is
  nvidia_nim) — action is revoke, not rotate; user parked it indefinitely.

### Held-position visibility fixes (2026-07-10) — both bots, 173 tests pass
Root cause class: a held position whose symbol leaves the scanned universe becomes invisible
to exit logic. Found via DLTR (stock bot): bought 2026-06-25 from a universe pick, then rotated
out of the movers list — no price refresh, no AI verdict, could never get a strategy SELL, and
its missing price also produced a phantom -$227.80 unrealized P&L (missing-symbol fallback was
$0 instead of avg_cost in `unrealized_pnl()`/`total_value()` — fixed 2026-07-09).
- **Stock bot (`stock_bot/main.py`):** each scan cycle now builds `cycle_symbols =
  watchlist + movers + held positions` and adds held symbols to the screener-bypass set.
  A held symbol can no longer be screened out of its own exit evaluation. The SL/TP watcher
  was never affected (iterates `positions_snapshot()` directly).
- **Crypto bot (`bot/main.py`):** startup orphan guard `_check_orphaned_positions()` — scans
  all `logs/live_state_*.json`; any `position > 0` for a symbol not initialized this run
  (e.g. removed from UNIVERSE_WHITELIST while holding) fires logger.error + Telegram alert.
  Alert-only by design: auto-trading an orphan with a cold strategy would be worse. No live
  orphans existed at ship time (all slots flat). Tests: `test_orphaned_positions.py` (5).
- **Both bots restarted on the fixed code 2026-07-10** (usual caffeinate + .venv launch).
- **Kraken balance is now $146.31 CAD; slot stays capped at $77** (`MAX_SLOT_CASH_CAD=77`).
  Deliberate: capital increases go through the 15-fill / net-PF ≥ 1.2 gate, not deposits.
  Current gate progress: 0 fills. Raise the cap in `.env` only after the gate passes.

### Crypto bot audit (2026-07-10 late) — applying the stock-bot rebuild lessons, 202 tests pass
The crypto bot was the template for the stock rebuild, so "do the same thing" = audit, not
rebuild. Findings:
- **Backtest execution model verified honest for crypto:** intra-candle SL/TP vs low/high,
  SL-before-TP pessimism; strategy fills at signal-candle close, which is FAITHFUL here
  (live bot decides at candle close and fires a market order seconds later in a gapless
  24/7 market — unlike daily stock bars, where next-open fills are required). The daily
  shadow-signal check is the standing proof of that assumption.
- **Shadow fidelity re-verified manually 2026-07-11 02:04 UTC: 96.6% match, PASS** (1
  mismatch = known indicator-warmup boundary, no position at stake).
  `logs/shadow_report_20260711.md`.
- **Drift-alert spam fixed:** 0.000085 BTC (~$8) appeared in the Kraken account ~Jul 6
  (confirmed by user 2026-07-10: manual purchase, not bot activity). The reconciliation re-alerted
  every ~3h for 4 days because the counter reset after each escalation. `_evaluate_drift()`
  extracted from the inline loop in `bot/main.py` (now unit-tested directly, not via a
  hand-mirrored copy) and gained `drift_acked`: an escalated drift amount is acknowledged —
  no re-alert while unchanged; a CHANGED amount re-arms; resolution clears the ack.
  The 0.000085 BTC itself is safe by design (ADOPT_EXTERNAL_HOLDINGS=false — never traded).
  **Crypto bot needs a restart to pick this up.**
- **Cron jobs moved 02:00 → 12:05 local (weekly 09:00 Mon → 12:10 Mon):** macOS cron
  doesn't fire while the lid is closed and never catches up — the shadow job silently
  missed Jul 7–10. `caffeinate -i` prevents idle sleep, NOT lid-close sleep.
  `ops/crontab.txt` updated + reinstalled.

### Stock bot rule-based rebuild (2026-07-10) — AI demoted to advisory, 200 tests pass
The position book's trade trigger is now the backtested rule strategy, not AI verdicts.
Rationale: AI confidence numbers are uncalibrated, verdicts flip on unchanged data (AMD:
BUY 58 → SELL 60 → HOLD 58 → BUY 68 → SELL 62 in ~10 min), and AI opinions cannot be
backtested. This restores the project charter: "AI gives advisory signals only."

**Walk-forward result (2026-07-10, `stock_backtest.py`, report `logs/stock_backtest_20260710.md`):**
Crypto IndicatorStrategy (Mode A/B, unmodified — hash `659d1c03987b72fd` untouched) on
daily candles, 4 windows (full ~6y / 750d / 500d / 250d), 15 bps slippage per fill + IBKR
commissions, SL 5% / TP 15%. Parameters came from BTC — genuinely out-of-sample on stocks.
- **PASS (4): MRNA (PF 1.52–2.22), AMD (1.36–3.18), RY.TO (3.90–7.95, WR 67–75%), PLTR (1.76–2.29)**
- FAIL (10): HOOD, NCLH, AC.TO (PF 0.71!), CCL, INTC, NVDA, TSLA, SHOP.TO, META, AMZN
- Gate: full-window trades ≥ 10, PF ≥ 1.2 every window with ≥ 3 trades, SL-exit ≤ 70%.

**New pieces:**
- `stock_bot/strategy/rules.py` — `build_indicator_config()` is THE single parameter source
  (backtest imports it, live imports it — no drift possible; pinned by test). `rule_signal()`
  statelessly replays candles through a fresh IndicatorStrategy each cycle → live signal is
  identical to backtest by construction. `drop_last=True` excludes today's still-forming
  candle while the market is open (backtest only saw completed candles).
- `stock_bot/backtest/engine.py` — honest daily-bar engine: signal fills at NEXT open,
  intra-candle SL/TP vs low/high with gap-through fills at the open, SL-before-TP pessimism,
  slippage both ways + IBKR round-trip commission (same `_round_trip_commission` the paper
  report uses). Replaced the 2026-06-23 one-off `stock_backtest.py` (look-ahead fills,
  close-only SL checks, 0.5% notional commission, symbol-order-dependent shared cash).
- `main.py` wiring: `RULE_TRADING_ENABLED=true` + `RULE_WHITELIST=MRNA,AMD,RY.TO,PLTR` in
  stock_bot/.env. Rules may BUY only whitelist symbols; rule SELL exits anything held; AI
  exit policy (below) stays as an extra risk-reducing exit; SL/TP watcher unchanged. AI can
  never OPEN a position. `LOOKBACK_DAYS` 200 → 300 (200-day regime EMA needs ~204 warmup).
  Per-symbol "📐 RULES:" line printed each scan; dashboard BUY card notes AI is advisory.
- **Adding a symbol to RULE_WHITELIST requires a fresh `stock_backtest.py` PASS — never by hand.**
- **Dashboard shows the decider (2026-07-10):** `renderer.py` — new "📐 Rule Signals" strip at
  the top (per-symbol rule verdict with "→ buying" / "not whitelisted — no entry" / "→ exiting"
  annotations); AI strip relabeled "🤖 AI Advisory — opinions only, cannot open positions";
  each stock card carries a rule tag next to the AI verdict. `ScanResult` gained
  `rule_verdict` + `rule_whitelisted` (default None/False — backward compatible).
  Invariant restored twice today: what the dashboard shows is what the bot will do.
- **AI shadow votes (2026-07-10):** every rule trade's CSV reason records the AI's opinion at
  fill time (`RULE BUY ... | ai=SELL60`, `| ai=NONE` if unavailable) — frozen schema unchanged,
  content only. After ~30 rule trades, compare outcomes where AI agreed vs disagreed; the AI
  earns entry-veto power only if agreement proves predictive. Do not grant veto before that.
- Held FAIL-symbol positions (AC.TO, DLTR at ship time) are managed by: rule SELL, AI exit
  policy, SL/TP watcher. No new entries on FAIL symbols.
- Re-run trigger: any change to `bot/strategy/*` or `build_indicator_config()` invalidates
  RULE_WHITELIST until the stock walk-forward is re-run (same discipline as crypto).

### Stock bot asymmetric exit policy (2026-07-10) — 184 tests pass
Incident: AC.TO held while the AI issued SELL 60% then SELL 58% on consecutive cycles —
both silently ignored because ONE confidence bar (PAPER_MIN_CONFIDENCE=65) gated both BUY
and SELL. The dashboard showed "🔴 SELL — AC.TO" with no hint the bot wouldn't act.
Design fix: entries and exits are not symmetric — a BUY adds risk (high bar stays), a SELL
on a HELD position removes risk (lower bar). Mirrors the crypto risk manager's "SELL always
allowed" philosophy.
- **New module `stock_bot/execution/exit_policy.py` (`ExitPolicy`)** — pure logic, no I/O.
  A held position exits on: single SELL verdict ≥ `PAPER_MIN_CONFIDENCE_SELL` (55), OR
  `PAPER_SELL_STREAK_CYCLES` (2) consecutive SELL cycles each ≥ `PAPER_SELL_STREAK_MIN_CONF`
  (50). HOLD/BUY verdicts and sub-50 SELLs break the streak; streak is in-memory (restart
  resets it — SL/TP watcher is the crash-safe backstop and is untouched).
- **`main.py`:** `exit_policy.decide()` runs on EVERY verdict (so HOLD breaks streaks);
  BUY keeps the 65 bar; SELL path uses the policy. A held symbol with a below-bar SELL now
  logs + prints "HELD, not exiting: <reason>" instead of silence. Streak-triggered fills
  get " [streak]" appended to the CSV reason (schema unchanged — content only).
- **Dashboard (`renderer.py` `_summary_html`):** signal cards now show per-symbol
  confidence and action status — "AC.TO 60% → exiting" / "AMD 62% (not held)" /
  "(held · below exit bar)" — plus a caption of the act thresholds. Advice and action are
  no longer visually identical. Thresholds threaded via `render(exit_bars=...)`.
- **Phase A note:** this changes the strategy being measured — position-book trades closed
  before 2026-07-10 used the old single-bar exits. Only 8 completed trades existed; the
  30-trade gate continues counting without reset, but any pre/post expectancy split should
  use this date.
- **Known limitation (bigger fix planned):** AI verdicts remain the trade trigger and are
  noisy/unstable (AMD flipped BUY 58 → SELL 60 → HOLD 58 → BUY 68 → SELL 62 within ~10 min
  on 2026-07-10) and unbacktestable. Direction agreed with user 2026-07-10: move the stock
  bot to deterministic rule-based signals (backtested + walk-forwarded like the crypto bot),
  demote AI to advisory — per the project's original "AI cannot execute trades" principle.
  This exit policy is the stopgap that manages open positions sanely until that lands.

### Rule pipeline first live session + sizing-visibility fix (2026-07-13) — 202 tests pass
Monday 2026-07-13 was the first live session of the rule-based stock pipeline (plan queue
item 1 from 2026-07-11). Result: clean — `rule_signal()` fired correctly all day (AMD BUY,
13 HOLDs, 1 no-op SELL on INTC), no crashes, only routine NVIDIA NIM (AI advisory) timeouts
which don't affect trading since AI cannot open positions.

**Found while verifying: AMD's BUY signal was silently unfillable.** Root cause —
`PAPER_RISK_PCT=0.20` on the ~$1,014 paper account targets a ~$203 allocation per trade;
AMD trades at ~$538/share, so `int(alloc / price)` rounds to 0 shares and the old code had
no branch for that case — no log, no print, nothing. The dashboard's "AMD → buying" line
was technically true (an entry would be attempted) but gave no hint it could never clear.
RY.TO (~$298/share) and the pending GLD add (~$300+/share) hit the identical wall.

**Fix — visibility, not a forced fill (`stock_bot/main.py`, sizing block ~line 1081):**
when `shares == 0` the bot now logs `SIZE_SKIP` and prints the target allocation, risk %,
and price so the skip is diagnosable instead of silent.

**Deliberately did NOT fix this by forcing a fill** (bumping `PAPER_RISK_PCT`, allowing
fractional shares, or rounding up to a minimum of 1 share regardless of cost). Buying 1
AMD share would commit ~53% of account cash to one position — a 2.65x blowout over the
intended 20%-per-trade risk cap. That is exactly the failure mode the Buffett rule mapping
in the Investment philosophy section below calls "margin of safety": small, capped sizing
over conviction-sized bets. **Standing warning:** do not "fix" a SIZE_SKIP by raising
`PAPER_RISK_PCT`, adding fractional-share support, or special-casing a minimum share count
for expensive symbols — any of those bypasses the margin-of-safety sizing rule the same way
a crypto capital-gate bypass would. The correct lever is the documented one: let the paper
account grow through the Phase A gate, or don't whitelist single-share-unaffordable symbols
at the current account size. AMD, RY.TO, and (once added) GLD will keep skipping — that is
correct behavior, not a bug, until account size catches up.

### Dashboard updates (2026-07-14) — 202 tests pass
Three changes, no strategy files touched (hash `659d1c03987b72fd` still valid):
- **Rule strip no longer says "→ buying" for unfillable signals.** The 2026-07-13 SIZE_SKIP
  fix covered logs/stdout only; the dashboard still promised fills that would round to 0
  shares. `renderer.py` `_rule_summary_html()` now takes `buy_alloc` (threaded through
  `render()` from `main.py`, computed as `(cash + position value) × PAPER_RISK_PCT` at
  render time); a whitelisted BUY whose share price exceeds it renders
  "⚠ signal valid, can't fill — 1 share $538 > $203 allocation". AMD/RY.TO/GLD show this
  today — the "dashboard shows what the bot will do" invariant holds again. Omitting
  `buy_alloc` preserves old behavior (backward compatible).
- **"Gates at a Glance" strip (roadmap item C closed):** `unified_dashboard.py`
  `_book_gates_section()` — all three books side by side (crypto 0/15 · position book n/30 ·
  swing book n/30, each with net PF + win rate + progress bar). Position/swing numbers come
  from `stock_bot.analysis.paper_report`'s own `_pair_trades`/`_expectancy_stats` (imported,
  not duplicated) — the strip can never disagree with the report.
- **Retired slot state files archived:** `logs/live_state_XRP_CAD.json` and
  `logs/live_state_DOGE_CAD.json` (both flat, position 0.0, untouched since Jul 1) moved to
  `logs/archive/`. Dashboard "retired slots" note now empty; orphan guard unaffected.
- Note: position book gate shows 1/30 — the current `paper_trades.csv` holds exactly one
  round trip (AC.TO: BUY 2026-06-24, SL hit 2026-07-14 10:38, −$12.51 net) + open DLTR.
  The pre-Jun-24 $10k-era trades were deleted from the CSV (last tracked at git fb1751a).
  The "8 completed trades" cited in the 2026-07-10 exit-policy entry does not match the
  current CSV (1 pair) — that count predates the reset or counted differently; the gate
  counts what `paper_report.py` counts, and the dashboard imports the same functions.
- **Stock bot needs a restart** to pick up the renderer change (crypto bot's unified refresh
  is a fresh subprocess each minute — already live).

### Cron retired → in-bot audit scheduler (2026-07-14) — 210 tests pass
**Discovery: the cron jobs NEVER worked.** `/var/mail/nishita` shows every run since the
2026-07-06 install — at 02:00 AND at the moved 12:05 slot — failed with
`/bin/sh: logs/shadow_signal.log: Operation not permitted`. macOS TCC denies `/usr/sbin/cron`
access to `~/Desktop` where the repo lives; errors went to local mail nobody reads. The
2026-07-10 "lid-close sleep" diagnosis was wrong (cron fired fine — it just couldn't write).
The Jul 6 and Jul 11 shadow reports were both manual runs. Consequence: Gate 3 of the capital
gate (shadow ≥ 95%) was silently running on stale data.
- **Replacement:** `_scheduled_audits_loop()` daemon thread in `bot/main.py` — runs
  `shadow_signal.py` daily (SHADOW_AUDIT_TIME, default 12:05 local) and `live_comparison.py`
  Mon-anchored weekly (WEEKLY_AUDIT_TIME, default 12:10). Fresh subprocess per run (same
  isolation pattern as the unified-dashboard thread), output appends to the same
  `logs/shadow_signal.log` / `logs/weekly.log`, last-run dates persist in
  `logs/audit_state.json`. **Catch-up by design:** a bot started after the scheduled time
  runs the missed audit immediately (fixes both cron failure modes). A failed script is
  logged and retried next period, not every minute. `AUDIT_SCHEDULER_ENABLED=false` disables.
  Pure due-logic in `_audit_due()` — tests: `test_audit_scheduler.py` (8).
- **`ops/crontab.txt` rewritten as a tombstone** (full failure history inside) and
  reinstalled — live crontab now has zero active jobs. VPS note: systemd timers or just
  keep the in-bot scheduler.
- **Dashboard: Gate 3 shows shadow-report age** — fresh (≤1d) plain, 2–3d amber "⚠ Nd old",
  >3d red + "STALE" subtitle. A stale report can no longer impersonate a fresh one.
- Stale labels fixed: P&L-by-day caption no longer claims "stock has no closed trades yet"
  (AC.TO closed 2026-07-14; chart is crypto-only and now says so); "Stocks paper ($1,000
  account)" header now computes the real account value (cash + positions at cost).
- **Crypto bot needs a restart** to start the scheduler thread. On first start after 12:05
  it will immediately catch up today's missed shadow audit.

### Stock bot SWITCHED to IBKR paper executor (2026-07-17 11:13 ET) — 241 tests pass
The deliberate switch decision was made and executed the same day the executor shipped.
No strategy files touched (hash `659d1c03987b72fd` still valid).
- **Preconditions verified first:** account reset landed ($1M → $995.30 CAD net_liq,
  no positions); ibkr_trades.csv contained no smoke-test rows (header only);
  ibkr_state.json re-seeded starting_cash=$995.30.
- **Sim book closed flat before the flip** (positions would otherwise become exit-less
  orphans under the new executor — the 2026-07-10 visibility failure mode). DLTR sold
  @ $130.39 (+$24.35) and CM.TO @ $171.03 (+$2.68), reason `EXECUTOR_SWITCH_TO_IBKR`,
  via the normal executor path (slippage applied, CSV rows written). Sim book final:
  $1,014.53 cash, +$14.52 realized (+1.45%), 3 completed round-trips. paper_state.json /
  paper_trades.csv stay in place as the frozen sim-era record — do not delete.
- **`STOCK_EXECUTOR=ibkr` set in stock_bot/.env; bot restarted 11:12 ET** — connected to
  DUQ273338 (PAPER), cash $995.30, positions []. TWS must now be running + logged in
  whenever the stock bot runs (connection failure stops startup, never falls back to sim).
- **Phase A gate counts ACROSS the switch:** `paper_report.read_position_book()` merges
  paper_trades.csv + ibkr_trades.csv in timestamp order (pairs never straddle executors
  because the sim book closed flat). Gate at switch: 3/30 trades, net PF 1.59, WR 67%.
  The report shows the IBKR account section when ibkr_state.json exists (current cash =
  last fill's cash_remaining — the report makes no network calls); realized P&L shown is
  sim + IBKR combined.
- **Dashboard follows the active executor:** `_load_stock_state()` reads STOCK_EXECUTOR
  from stock_bot/.env; ibkr mode synthesizes the card/table state from ibkr_state.json +
  ibkr_trades.csv (badge "IBKR PAPER · DUQ273338"); gate strip uses the merged book.
- Tests: test_paper_report.py 6 → 8 (merge + IBKR account section; existing report test
  made hermetic against real ibkr files). Suite 239 → 241.
- On connect TWS replays the day's account executions (execDetails) — fills from other
  client IDs (e.g. manual TWS orders) are ignored by the executor, by design.
- **Dashboard labels follow the active executor (same day, post-switch):** unified gate
  strip "Position Book · IBKR" (dynamic via `_stock_executor_type()`), positions table
  "Stock Positions — IBKR paper", and `stock_bot/dashboard/renderer.py`'s Paper Trading
  section badge "IBKR PAPER · real order routing, simulated money" (reads STOCK_EXECUTOR
  env; renderer change appears at the stock bot's next restart — cosmetic, no restart
  needed for it alone). All numbers were already executor-correct; only labels lagged.
- **Dashboard roles (agreed with user 2026-07-17):** `unified_dashboard.html` is the
  daily-glance dashboard (both bots, gates, ops, P&L — auto-refreshed every 60s by the
  crypto bot). `stock_dashboard.html` stays as the stock bot's per-symbol drill-down
  (rule-signal strip, AI advisory, exit-bar status — the "why did/didn't it act" view).
  Both are kept; do not fold one into the other without a user decision.
- **Post-switch audit (same day) — remaining sim-file readers fixed, 242 tests pass:**
  `paper_report.load_active_book_state()` (new) returns the ACTIVE executor's book in
  the paper_state.json shape (env-driven; used by the daily Telegram/email summary in
  `alerts/notifier.py`, which previously read the frozen sim state); readiness Gate 3
  (`accuracy_tracker.check_gate3`) and the `stock_analysis.py` accuracy CLI default now
  count the merged paper+IBKR book. Verified non-issues: order placement runs
  `_ensure_connected_async()` (auto-reconnect after a TWS restart); read paths degrade
  to logged warnings when TWS is away; ibkr_state.json/ibkr_trades.csv are gitignored;
  fast_validator/tracker references were comment-only.
- **OPS: TWS auto-logoff.** Classic TWS logs itself off daily by default — set
  Global Configuration → Lock and Exit → "Auto restart" so it stays up (weekly re-login
  Sundays is still required by IBKR). Until that's set, a nightly logoff means the bot's
  orders fail (visibly, with logged errors) until TWS is logged back in.

### TSX API orders BLOCKED by regulation → whitelist moved to NYSE listings (2026-07-17 pm)
First live IBKR-era rule signal (CM.TO BUY, 12:37 + 13:41 ET retries) was rejected by IBKR:
**Error 201 "API/CTCI orders for Canadian products are not allowed."** Root cause is
regulatory, NOT a permission or account setting: CIRO rule DMR 3200 A.1.(b)(i) prohibits
IBKR Canada clients from placing orders on Canadian exchanges via ANY automated system
(API/third-party apps). US products via API are unaffected (KO smoke trade proved it).
Canada trading permission was already enabled (verified in Client Portal) — there is NO
fix; manual TWS orders are the only way to trade TSX. Do not re-attempt .TO via API.
Error 10349 (TIF=DAY) in the same log is the known benign warning. The executor handled
both rejections cleanly: no fill recorded, no state change.
- **Consequence:** 5 of 12 whitelisted symbols (.TO) could never fill. Their dashboards
  said "→ buying" — the visibility invariant was violated by forces outside the code.
- **Response (same day):** ran `STOCK_BT_SYMBOLS=RY,TD,BNS,CM,SU stock_backtest.py` on
  the NYSE cross-listings (USD — .TO validation does NOT transfer across listings).
  Report `logs/stock_backtest_20260717.md`:
  **PASS: RY (PF 1.60–5.77), TD (1.46–7.47), CM (1.87–6.28)** → whitelisted.
  **BNS: gate-letter FAIL** (9 full-window trades < 10; PF 3.35–11.29 everywhere) —
  held out, re-eligible on a future re-screen. **SU: real FAIL** (full PF 0.97, 500d
  0.22) — proof the CAD/USD listings genuinely differ; SU.TO's pass did not carry over.
- **stock_bot/.env updated:** RULE_WHITELIST=MRNA,AMD,RY,PLTR,GLD,TD,CM,CSCO,KO,T
  (all US-listed, all API-tradeable). WATCHLIST swapped .TO→US 1:1 (BNS, SU stay
  watched). AC.TO/SHOP.TO remain watch-only (advisory, never rule-buyable).
- **Known FX sizing quirk (accepted for paper):** account is CAD; `PAPER_RISK_PCT`
  allocation is computed in CAD but US share prices are USD, so USD positions can run
  ~35% over the 20% target (e.g. 3 TD shares ≈ $267 CAD ≈ 27%). Exposure/sector caps
  still bound it. Fix deliberately deferred — sizing change = measurement change;
  revisit before live.
- **Stock bot needs a restart** to load the new .env (also picks up the notifier fix).

### Contract-mapping bug found + fixed same day — bare NYSE cross-listings were routing to TSX (2026-07-17 evening) — 243 tests pass
The whitelist swap above (RY/TD/BNS/CM/SU as bare US tickers) didn't fully close the Error 201
hole. `IBKRExecutor.to_contract()` built bare symbols as `Stock(sym, "SMART", "USD")` with no
`primaryExchange` — for names whose *primary* listing is Toronto, IBKR's SMART/USD
qualification resolved the ambiguous symbol back to the TSX/CAD contract despite the USD
request. Live symptom: CM's rule-BUY signal fired and was rejected by the same CIRO Error 201
**8 times over ~4 hours** (12:13–16:13 ET) before this was caught — a live no-op each time
(no fill, no state change) but silently repeating.
- **Fix:** `to_contract()` now forces `primaryExchange="NYSE"` for a known cross-listed set
  (`_NYSE_CROSS_LISTED = {RY, TD, BNS, CM, SU}`) so the USD/NYSE contract wins over the
  CAD/TSE one. Symbols with only a US listing (MRNA, AMD, PLTR, GLD, CSCO, KO, T) are
  unaffected — `primaryExchange` stays unset for those, as before.
- Tests: `test_contract_mapping_nyse_cross_listed` (new) + `test_contract_roundtrip` extended
  to cover bare RY/CM. Suite 242 → 243.
- **Stock bot needs a restart** to pick up the fix — next CM/RY/TD signal should route to
  NYSE and fill instead of rejecting.

### IBKR paper executor built + verified (2026-07-17) — roadmap item D, 239 tests pass
IBKR paper environment set up end-to-end and the executor written the same day. No strategy
files touched (hash `659d1c03987b72fd` still valid); stock bot NOT yet switched — sim paper
book remains the Phase A measurement account until a deliberate switch decision.
- **Accounts:** live U26459664 (approved, deliberately UNFUNDED — paper needs no funding);
  paper **DUQ273338** (CAD-denominated). Paper page is HIDDEN in the portal menu on unfunded
  accounts — deep link: `interactivebrokers.com/sso/resolver?action=AccountSettings&config=PaperTrading`.
  Paper login = usual credentials with the Live/Paper toggle set to Paper.
- **Mac setup:** classic TWS at `~/Applications/Trader Workstation/` (paper mode, API port
  7497, localhost-only, Read-Only off, "Bypass Order Precautions for API Orders" checked).
  **`/Applications/IBKR Desktop` is the WRONG app** (no API support, installed by mistake
  first, kept for manual use) — never point the bot at it. TWS must be running + logged in
  for the executor to work.
- **`stock_bot/execution/ibkr.py` — `IBKRExecutor(StockExecutorBase)`:** full drop-in for
  StockPaperExecutor (same extra methods main.py calls, same pre-trade sanity gates, same
  daily-loss breaker + sector cap semantics — risk behavior unchanged, only fills are real).
  Dedicated event-loop daemon thread + `run_coroutine_threadsafe` (scan loop AND SL/TP
  watcher call it concurrently). Guards: live ports 7496/4001 raise without
  `IBKR_ALLOW_LIVE=true`; connected account must start "DU". Market orders wait for fill;
  on timeout cancel-then-recheck (a fill racing the cancel is recorded — Jul-15 lesson,
  and it actually happened in the smoke test: TWS reported `filled=0` on a cancelled BUY
  that later filled server-side, leaving an orphan share we cleaned up).
  Contract mapping `RY.TO` ↔ `Stock('RY', SMART, CAD, primaryExchange=TSE)`; realized P&L
  + starting_cash persist in `stock_bot/ibkr_state.json`; fills append to
  `stock_bot/ibkr_trades.csv` (frozen 9-col schema; both gitignored).
- **Library: `ib_async` 2.1.0** (maintained successor of the archived ib_insync — same
  API; earlier roadmap notes saying "ib_insync" mean this). Side effect: pip downgraded
  tzdata 2026.2→2025.3.
- **Wiring:** `STOCK_EXECUTOR=paper|ibkr` in stock_bot/.env (+ IBKR_HOST/PORT/CLIENT_ID/
  ALLOW_LIVE, defaults documented there). `main.py` instantiates by type; a failed IBKR
  connection raises at startup — never a silent fallback to sim fills.
- **Verified against live TWS:** read paths PASS; 1-share KO round trip PASS (BUY $83.52 /
  SELL $83.49). TWS error 10349 ("TIF set to DAY") is a WARNING, not a rejection.
  Smoke CLI: `.venv/bin/python ibkr_smoke.py [--trade SYMBOL]`.
- **Account reset $1M → $1,000 CAD requested 2026-07-17** (processes overnight; portal
  only offers preset $250k or "Other Amount" free entry). Local ibkr_state.json deleted so
  starting_cash re-seeds from the reset account. **Do not set STOCK_EXECUTOR=ibkr before
  confirming the reset landed** — sizing against $1M would produce ~$200k allocations.
- Tests: `test_ibkr_executor.py` (17, hermetic FakeIB — no network/TWS). Suite 222 → 239.

### Monthly automated re-screen + crypto re-research (2026-07-16) — 222 tests pass
User direction: "make the crypto bot more powerful" — interpreted as research freely +
automate evidence refresh; live-money gates unchanged (validation before capital, caps stay).
- **`rescreen.py` (new):** monthly orchestrator — runs `screen_universe.py` (Kraken CAD
  auto-discovery, so no hardcoded symbol list; includes BTC/CAD → live-symbol edge decay
  is caught) and `stock_backtest.py` (full WATCHLIST → re-validates every RULE_WHITELIST
  symbol). Compares PASS lists to live whitelists; flags 🔻 EDGE DECAY (whitelisted but
  failed) and 🆕 NEW QUALIFIERS (passed but not whitelisted). Writes
  `logs/rescreen_<date>.md` + Telegram alert on any flag. **Never changes a whitelist**
  — additions/removals stay manual per Validation Discipline.
- **Scheduler:** third job in `_scheduled_audits_loop` — monthly, 1st-of-month anchored
  at RESCREEN_AUDIT_TIME (default 12:20 local), catch-up mid-month if the bot was down,
  90-min subprocess timeout (jobs now carry per-job timeouts). `RESCREEN_ENABLED=false`
  disables. `_audit_due()` gained `monthly_first`; tests in `test_audit_scheduler.py` (14).
- **Fresh crypto re-screens (2026-07-16), all still FAIL but the picture moved:**
  - SOL/CAD: PF now 1.48/1.35/1.40 (all ≥ 1.2 — was all < 1.0 on 2026-07-02!) — fails
    ONLY the 79% SL-exit gate. ETH 1.05/1.43/0.73 + SL 79%; XRP 0.94/0.98/1.11 + SL 88%.
  - SYN/USD still PF-strong (1.80/2.56/2.39) + SL 79%; LINK regressed (1000c PF 0.77);
    PAXG/USD failed on a 1-trade recent window (2.36/3.65/0.00 — noise, but a fail).
  - **ATR experiment on SOL:** ATR×2.0 PASSES the full gate in-sample (PF 1.49/1.56/1.20,
    SL 79%→46%) — but single-multiplier pass with both neighbors failing (×1.5 and ×2.5
    fail) = curve-fit risk. Report `logs/atr_sl_experiment_20260716.md`. BTC control:
    ATR×2.0–3.0 beat fixed 1.5% SL in this window (×2.0: PF 2.07 vs 1.65). **Next research
    step if crypto resumes:** OOS split (walkforward.py train/validation) for SOL@ATR×2.0
    and a proper BTC ATR study; promotion would additionally need SL-distance position
    sizing + all capital-gate preconditions. No config changed.
- Crypto HALT LIFTED 2026-07-15 ~23:20 local — user chose "Resume buying" when asked
  explicitly (the Jul 15 halt entry below is superseded). Bot logged "HALT lifted";
  the fixed fill-recorder code was already running (22:45 restart), so the resume
  precondition was met. BTC/CAD live again: $77 slot cap, capital gate 0/15, all risk
  gates active.

### SOL ATR×2.0 OOS validation — HOLDS (2026-07-17) — 243 tests pass, no config changed
Closed the "next research step" from the entry above: `atr_oos_validation.py` (new) splits
one 5000-candle SOL/USDT fetch by DATE into two genuinely non-overlapping halves — unlike
`atr_sl_experiment.py`'s nested 5000c/3000c/1000c windows (1000c sits entirely inside 5000c),
these share zero candles. Same cfg-from-.env strategy params, same PF≥1.2/trades≥10/SL≤70%
gate as the screen.

| Period | Window | Trades | Win% | PF | SL rate | Return |
|--------|--------|--------|------|-----|---------|--------|
| TRAIN (2024-04-05→2025-05-26) | fixed 1.5% | 38 | 16% | 1.25 | 84% | -4.84% |
| TRAIN | ATRx2.0 | 28 | 39% | **1.40** | 46% | -1.38% |
| VALIDATION (2025-05-27→2026-07-17) | fixed 1.5% | 30 | 23% | 1.79 | 73% | -2.13% |
| VALIDATION | ATRx2.0 | 24 | 38% | **1.64** | 46% | -0.38% |

**Verdict: HOLDS.** ATRx2.0's PF and 46% SL-exit rate reproduce on validation-half data the
strategy never saw during the original screen — PF didn't collapse, it held (even ticked up
1.40→1.64). Both halves clear the 10-trade floor. This resolves the curve-fit concern flagged
2026-07-16 (single multiplier passing with both neighbors failing on nested windows).
Report: `logs/atr_oos_SOL_2.0_20260717.md`. Reusable for future symbols/multipliers via
`SYMBOL=`/`ATR_MULT=` env vars (e.g. the still-pending "proper BTC ATR study").

**This is evidence, not a promotion.** SOL/CAD stays BLOCKED — adding it still requires,
per the existing roadmap note: SL-distance-based position sizing (a wider ATR stop must not
raise dollar risk per trade beyond the standard cap) + the capital-gate preconditions in the
"Preconditions for any USD pair promotion" section (BTC/CAD ≥15 fills + PF≥1.2, capital ≥$500,
documented FX handling) + a fresh full walk-forward pass on the CURRENT strategy hash at
promotion time. No .env or whitelist change made.

### BTC ATR×2.0 OOS validation — HOLDS, and this one is live-relevant (2026-07-17)
Same `atr_oos_validation.py`, `SYMBOL=BTC/USDT ATR_MULT=2.0` — the "proper BTC ATR study"
the 2026-07-16 entry flagged as still pending. Same non-overlapping date split as the SOL run.

| Period | Window | Trades | Win% | PF | SL rate | Return |
|--------|--------|--------|------|-----|---------|--------|
| TRAIN (2024-04-05→2025-05-26) | fixed 1.5% | 26 | 19% | 1.34 | 81% | -2.69% |
| TRAIN | ATRx2.0 | 21 | 33% | **1.79** | 52% | -0.39% |
| VALIDATION (2025-05-27→2026-07-17) | fixed 1.5% | 12 | 33% | 1.88 | 58% | -0.94% |
| VALIDATION | ATRx2.0 | 11 | 45% | **2.04** | 45% | -0.68% |

**Verdict: HOLDS,** and PF improved out-of-sample (1.79→2.04) rather than merely surviving —
win rate improved too (33%→45%). Fixed 1.5% SL fails the full gate outright on the training
half (81% SL-exit rate, over the 70% cap); ATRx2.0 roughly halves it in both halves (52%→45%).
Caveat: validation trade count is thin (11–12), just over the 10-trade floor — worth more
months of confirmation before treating as fully settled. Report: `logs/atr_oos_BTC_2.0_20260717.md`.

**Why this one is different from the SOL result: `ATR_SL_MULT` is a live-wired config knob,
not a new-symbol question.** `bot/main.py:1813` already reads `cfg.strategy.atr_sl_mult`
(env `ATR_SL_MULT`, currently `0.0` = disabled, fixed `STOP_LOSS_PCT=0.015` active) to compute
the live SL level. Switching it on is a config change, NOT a `bot/strategy/*.py` change — it
does not invalidate strategy hash `659d1c03987b72fd` per the Validation Discipline rule. This
means, unlike SOL, adopting this doesn't require a new-symbol promotion path — only a full
walk-forward re-run confirming PF≥1.79 with ATR_SL_MULT=2.0 (same discipline as the SL=1.5%/
TP=10% sweep already done 2026-06-19) before it could go live. **Not yet decided or adopted —
research only, no .env change.** Given the 2026-07-02 ATR SL drift incident (a stale .env key
silently ran ATR SL on backtest at the wrong multiplier), any future adoption must double-check
BacktestConfig and StrategyConfig read the identical `ATR_SL_MULT` key before trusting a result.

**Follow-up same day — full 5-window walk-forward requested above, now run** (`atr_walkforward.py`,
new; same nested-trailing-window shape — 5000/4000/3000/2000/1000 candles ending at present —
used for every prior BTC strategy sweep in this file):

| Window | fixed 1.5% PF | ATRx2.0 PF | fixed SL rate | ATRx2.0 SL rate |
|--------|---------------|------------|----------------|-------------------|
| 5000c | 1.65 | **1.98** | 71% | 49% |
| 4000c | 2.09 | **2.33** | 66% | 42% |
| 3000c | 3.03 | **3.76** | 55% | 35% |
| 2000c | 2.56 | **2.88** | 56% | 38% |
| 1000c | 1.70 | **2.39** | 67% | 40% |

**Both variants pass PF > 1.0 on all 5 windows, but ATRx2.0 beats fixed 1.5% on every single
window** — higher PF and 20–30 points lower SL-exit rate everywhere, not a marginal edge.
Combined with the non-overlapping OOS split above (train 1.79 → validation 2.04, held), this
is now validated by both methods this project uses (recency-robustness sweep + temporal
holdout) and both are clean. Report: `logs/atr_walkforward_BTC_2.0_20260717.md`.
**Still not adopted — this is the last research gate before a go/no-go decision on
`ATR_SL_MULT=2.0` in live `.env`, which was deliberately left to the user, not auto-applied.**

### ATR SL adopted live — ATR_SL_MULT 0.0 → 2.0 (2026-07-17) — 243 tests pass, strategy hash unchanged
User approved after reviewing both validation results above. `.env`: `ATR_SL_MULT=0.0` → `2.0`.
`STOP_LOSS_PCT=0.015` stays in place as the documented fallback — `bot/main.py:1813` already
falls back to it (logging "ATR SL disabled or unavailable — using fixed SL/TP") on any entry
where ATR can't be computed; this is pre-existing, tested behavior, not new code.
- **This is a config change, not a strategy change** — `bot/strategy/*.py` untouched, hash
  `659d1c03987b72fd` stays valid, no `stamp_strategy.py` re-run needed (Validation Discipline:
  "Changes to config, execution, risk, data, or tests do NOT invalidate the hash").
- **Verified same day:** `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` now returns
  35 trades / PF 1.98, exactly matching the 5000c row of the walk-forward report — confirms
  `.env` and the backtest/live code paths agree (the 2026-07-02 ATR SL drift incident was
  exactly this kind of mismatch going undetected, hence checking explicitly here).
- **Full suite (243) re-run after the `.env` edit — unaffected** (no test forces a specific
  SL variant at the unit level; `walkforward.py`'s own canonical run stays fixed-SL-only,
  since it never passes `atr_sl_mult` to `engine.run()` — only `backtest.py` and the live bot
  read `ATR_SL_MULT` from `.env`). SUPERSEDED 2026-07-18: walkforward.py now builds its
  engine kwargs from the shared `engine_kwargs_from_cfg()` and runs the full live config,
  ATR SL + sizing included — see "Validation-script config drift fixed" entry below.
- Two new reusable research scripts added this session: `atr_oos_validation.py` (non-overlapping
  train/validation split, any symbol/multiplier via `SYMBOL=`/`ATR_MULT=`) and
  `atr_walkforward.py` (canonical 5000/4000/3000/2000/1000 trailing-window sweep, same env vars).
- **Crypto bot needs a restart** to pick up the new `.env` — live SL exits will compute against
  `entry_price - ATR(14)×2.0` instead of `entry_price × (1 - 0.015)` from the next BUY onward;
  any position already open at restart keeps whatever SL was set at its own entry.
- **Watch for going forward:** expect a lower stop-out rate on the next live SL exits (walk-forward
  showed 71%→49% on the fullest window) and possibly wider-than-1.5%-stop losses on any trade that
  does hit stop, since ATR-based stops are usually looser than the old fixed 1.5%. This trades
  fewer-but-larger occasional losses for meaningfully fewer stop-outs overall — consistent with
  the validated PF improvement, but worth knowing before the first live ATR stop fires.

### Ops + research build session (2026-07-17 evening) — 264 tests pass, strategy hash unchanged
Four builds in one session ("build all" from the post-adoption roadmap discussion). No
`bot/strategy/*` files touched — hash `659d1c03987b72fd` still valid.

**1. DISCOVERY: no outbound alert channel has EVER worked.** `TELEGRAM_*` keys are absent
from `.env` (TelegramAlerter constructs disabled, silently) and the stock bot's
`ALERT_EMAIL_FROM/TO/PASSWORD` are empty. Every "Telegram alert" described in this file —
drift escalation, HALT engage/lift, daily P&L, fill alerts, candle watchdog — only ever
reached the log files. The code paths exist and are tested; the delivery channel was never
configured. Response is the heartbeat inversion below (no credentials needed in repo)
plus — same evening — actual BotFather setup: **Telegram configured and verified
2026-07-17** (bot t.me/amaresh_tradebot, TELEGRAM_* keys in root `.env`, test message
delivered). Crypto-bot alerts go live at its next restart. **Stock bot wired to the
SAME Telegram channel (same evening):** `_make_telegram()` in
`stock_bot/alerts/notifier.py` sources TELEGRAM_* from the ROOT `.env` (one token, one
revoke point; process env overrides); `AlertNotifier` gains `fill()` (BUY/SELL fills
with P&L + reason, tagged sim/IBKR-paper), `ops_alert()` forwards (TWS disconnects,
Sunday reminder), and `notify()` relays HIGH-priority scan alerts only (MEDIUM chatter
excluded by design — same filter as desktop). `bot/alerts/telegram.py` gained a generic
`message()` method. Fill sites wired in `stock_bot/main.py`: scan-loop BUY/SELL + both
SL/TP-watcher exits. Tests: `test_stock_telegram.py` (7, hermetic — injected
credentials, mock alerter).

**2. Heartbeat dead-man's switch (roadmap G closed in code):** `bot/alerts/heartbeat.py`
(shared by both bots, same cross-package import pattern as stock rules). Bot pings a
healthchecks.io check URL every 60s; the service emails when pings STOP — covers process
death, Mac sleep, and network loss with zero secrets in the repo. Env (all empty = off):
`HEARTBEAT_URL` in `.env` (crypto) and in `stock_bot/.env` (stock process), plus
`HEARTBEAT_TWS_URL` (stock; pinged only while `executor.is_connected()` — separates "TWS
logged off" from "bot died"). Fail-silent by design; a broken healthy_fn counts as
unhealthy (no ping) so monitoring can't mask an outage. Tests: `test_heartbeat.py` (8).
**healthchecks.io LIVE (2026-07-17 21:19, same night — user provided an API key and
setup was done programmatically):** 3 checks (crypto-bot / stock-bot / stock-tws),
period 5 min, grace 10 min, all attached to the account email channel (API-created
checks get NO notification channel by default — had to be assigned explicitly).
Ping URLs in the three env keys; bots restarted 21:19; all checks verified "up" with
pings received. Dead-bot detection now exists: bot death, Mac sleep, network loss →
email within ~10 min; TWS logoff → stock-tws email + Telegram ops alert. The account
also has an unused auto-created "My First Check" (status new, never alerts) — user can
delete it in the dashboard for tidiness.**

**3. TWS disconnect alert + Sunday re-login reminder:** `stock_bot/alerts/tws_monitor.py`
(`TwsConnectionMonitor`, pure state machine — blip-tolerant, alert-once per outage,
recovery notice only after an alert; tests `test_tws_monitor.py` 6). Wired as a 60s
monitor thread in `stock_bot/main.py` (IBKR executor only): 10+ min disconnect →
`notifier.ops_alert()` = log WARNING + terminal + desktop notification (plyer). The
Sunday-18:00 weekly-summary timer now also fires a TWS re-login reminder when
`STOCK_EXECUTOR=ibkr` (IBKR forces weekly Sunday re-auth; without it every Monday order
fails). `TWS_DISCONNECT_ALERT_MIN=10` to tune. Local-only delivery by design — the remote
email leg is HEARTBEAT_TWS_URL.

**4. ATR-aware position sizing — built, validated, NOT enabled (flag off).** Gap: with
`ATR_SL_MULT=2.0` live, stop distance varies per entry but sizing was plain notional
(`calc_trade_qty`), so a wide-ATR entry risks MORE dollars at its stop than the validated
fixed-SL baseline (cash × 10% × 1.5% = 0.15% of cash). New
`AppConfig.calc_trade_qty_atr_risk()` caps qty so a stop-out never exceeds that baseline;
a tight ATR stop does NOT size up past standard notional (min, not equality). Wired:
engine (`atr_risk_sizing` + `atr_sizing_baseline_sl_pct` params, default off),
`backtest.py`, live BUY path in `bot/main.py` (behind `ATR_SIZING_ENABLED`, same one key
read by BacktestConfig AND StrategyConfig — drift-incident rule), `atr_walkforward.py`
pass-through. Tests: `test_atr_sizing.py` (7). **Validation run (sizing ON, BTC ATRx2.0,
5-window):** PF 1.90/2.24/3.46/3.29/2.03 — all 5 windows > 1.0 and every window still
beats fixed-SL. Canonical fingerprint with flag OFF re-verified same session: 35 trades /
PF 1.98, unchanged. Report `logs/atr_walkforward_BTC_2.0_20260718.md` (UTC date).
**ADOPTED same night (user approved "proceed with 1"): `ATR_SIZING_ENABLED=true` in
`.env`.** Fresh `backtest.py` now returns 35 trades / PF 1.90 (matches the sizing-ON
5000c walk-forward row exactly — env/code agreement verified at adoption). Rationale for
flipping BEFORE gate fills accumulate: all 15 capital-gate trades get measured under one
consistent sizing config. Note: at $77 slot cash the cap rarely binds (the existing
98%-affordability clamp dominates); this matters as capital grows.

**5. SYN/LINK ATR OOS validations run (roadmap K progress):** same
`atr_oos_validation.py` non-overlapping split as the SOL/BTC runs.
- **SYN/USDT: HOLDS cleanly at both mults** — ATRx2.0 train PF 1.57 / validation 1.99
  (SL 25%/21%); ATRx2.5 train 2.04 / validation 1.86 (SL 11%/14%). Trades 19–28 per half.
- **LINK/USDT: mixed — not candidate-grade.** ATRx2.0 FAILS the train half outright
  (PF 0.85); ATRx2.5 passes both halves but thin margins (1.29 train / 1.61 validation).
  A multiplier that only works at one setting and fails at its neighbor on half the data
  is the same curve-fit profile the OOS script exists to catch. LINK stays out.
- Reports: `logs/atr_oos_{SYN,LINK}_{2.0,2.5}_20260717.md`. SYN remains blocked on the
  unchanged USD-pair preconditions (BTC/CAD 15-fill gate at 0/15, capital ≥ $500, FX
  decision, fresh walk-forward at promotion time) — this closes the "SYN/LINK OOS not yet
  run" gap in roadmap item K, it does not promote anything.

**Restarts DONE same evening (crypto 20:31, stock 20:35 after two live-caught bugs
below). VPS migration (the 5th "build all" item) is blocked on provisioning a server —
nothing to build locally.**

**Two bugs caught during the restart cycle — both mine, both fixed, both carry a lesson:**
- **`stock_bot/main.py` had no `import os`** — the new heartbeat block used `os.getenv`,
  so the first restart (20:30) died with NameError right after "Weekly summary timer
  armed" (traceback to terminal only; the bot was down flat with TWS untouched).
  `import stock_bot.main` had passed pre-restart — an import check does NOT execute
  `run()`; runtime-path code needs runtime-path verification.
- **`IBKRExecutor.is_connected` is a PROPERTY, not a method** — the TWS monitor called
  the returned bool ("'bool' object is not callable" warning every 60s) and the TWS
  heartbeat's `healthy_fn=executor.is_connected` had evaluated the property ONCE at
  wiring time (frozen True — would never have detected a disconnect once a URL was set).
  Fixed with `lambda: bool(executor.is_connected)` / `bool(executor.is_connected)`;
  wiring pattern now exercised against a property-based fake. Same trap exists on
  `cash` (property) vs `positions_snapshot()` (method) — signatures verified before the
  startup-notification call was added.
- **Stock bot startup Telegram message added** (`notifier.startup()` — executor type,
  cash, position count, called once the executor is ready): a silent boot is
  indistinguishable from a broken channel; the crypto bot already had `alerter.startup`.
  Appears from the stock bot's next natural restart (the 20:35 instance predates it and
  is otherwise fully healthy: TWS monitor clean, all threads up).

**Crash-hardening audit follow-up (same night, items 1–3 of the resilience audit) —
280 tests pass:**
- **Fatal-crash Telegram alert, both bots:** `__main__` now wraps `run()` — any unhandled
  exception logs the full traceback (logger.critical) and fires a synchronous
  "💀 ... CRASHED" Telegram via new `TelegramAlerter.send_now()` (the usual daemon-thread
  send races process teardown), then re-raises. Helpers `_send_crash_alert` in both mains;
  never raise, no-op when Telegram is off. Until healthchecks.io is set up this is the
  ONLY way a crash reaches the user.
- **Atomic state writes:** new `bot/atomic_json.py` (tmp + `os.replace`) now used by
  `live_executor._save_state`, `ibkr._save_state_json`, and the audit-state write in
  `bot/main.py`. RiskManager already wrote atomically — pattern now shared. A crash or
  power loss mid-write can no longer truncate live position/cash state (the 2026-06-27
  incident class). Failed writes preserve the previous good file (tested).
- **SIGTERM = graceful shutdown:** crypto registers its SIGINT handler for SIGTERM too;
  stock bot routes SIGTERM into its KeyboardInterrupt path (registered in `run()`).
  launchd/systemd/`kill` now save state instead of hard-killing.
- Audit items NOT built (deliberate): launchd auto-start (medium, later), requirements
  lockfile (small, later), lid-close sleep (ops habit / future VPS), FX sizing (deferred
  pre-live), TWS auto-restart setting (user side).
- **Both bots need a restart** to run the new crash paths — fine to fold into the next
  natural restart; nothing about live trading behavior changes.

**Post-restart verified state (2026-07-17 ~20:40):** crypto bot live with Telegram
(startup message delivered 20:31); stock bot live on IBKR (DUQ273338, $995.30, flat),
TWS monitor running clean; Telegram end-to-end confirmed by user ("🔧 Direct send-path
test" received). Watch Monday: CM's first rule signal through the fixed NYSE contract
mapping should FILL — arriving as a 🟢 BUY Telegram message.

### Validation-script config drift fixed — shared engine-kwargs builder (2026-07-18) — 286 tests pass
Follow-up to the ATR adoptions: audit found `walkforward.py` hand-listed a PARTIAL, stale
engine.run() arg set — it was validating a config that differed from live in at least six
ways: `volume_k` missing (engine default 1.2 vs validated 0 = filter OFF), `min_ema_spread_pct`
missing (0.002 vs validated 0.004), a stale hardcoded 0.5% `max_ema_spread_pct` ceiling
(live runs 0.0 = disabled), `adx_max`/regime/partial-TP params missing, and NO ATR keys
(`ATR_SL_MULT`, `ATR_SIZING_ENABLED`) — so the Validation Discipline workflow's step 3 ran a
config nobody trades. Same failure class as the 2026-07-02 ATR SL drift incident.
- **Fix: `bot/backtest/params.py` — `engine_kwargs_from_cfg(cfg)`** is now THE single source
  of engine kwargs (same pattern as the stock bot's `build_indicator_config()`). Both
  `backtest.py` (CLI flags override on top) and `walkforward.py` use it; a test pins that
  they keep doing so and that every emitted key is accepted by `engine.run()`.
  Tests: `test_engine_params.py` (6, hermetic — fake cfg, never reads live `.env`).
- **Fingerprint parity verified:** refactored `backtest.py` reproduces 35 trades / PF 1.90,
  hash `659d1c03987b72fd` — behavior byte-identical to pre-refactor baseline run same day.
- **New walkforward.py reference numbers (full live config: ATR SL 2.0 + sizing ON,
  VolK=0, EMA≥0.4%, run 2026-07-18):** training (2024-04-05→2025-02-21, 17 trades)
  PF 1.16 / −1.11%; validation (2025-02-22→2026-07-18, 18 trades) PF 3.00 / +0.22% —
  PASS (PF holds ≥1.2 out-of-sample). Prior walkforward numbers in this file were produced
  under the old drifted arg set and are not comparable.
- **False-positive CONFIG DRIFT warning fixed:** `ATR_SIZING_ENABLED` was missing from
  `_KNOWN_STRATEGY_ENV_KEYS` in `config.py` (the adoption added the reader but not the
  drift-guard whitelist), so EVERY config load — including live bot startups — logged
  "unrecognised strategy keys: ATR_SIZING_ENABLED". The key was always read correctly
  (PF 1.90 = sizing ON confirms); only the warning was wrong. Whitelist updated.
- **macd_enabled live-vs-backtest divergence — RESOLVED 2026-07-20 (option a):** live
  (`bot/main.py`) and `shadow_signal.py` had always run `macd_enabled=cfg.strategy.macd_enabled`
  (True — MACD gates both entry modes), but `backtest.py` never passed it, so every canonical
  fingerprint through 2026-07-18 was produced with MACD OFF — a fidelity gap, not a safety
  hole (live was the stricter, subset strategy). User chose to validate what's actually live
  rather than turn MACD off to match the old numbers. See "MACD live/backtest divergence
  resolved" entry below for the full re-validation.

### TWS "restored" notice actually fires now — reconnect probe (2026-07-18) — 289 tests pass
First real outage (TWS daily logoff, Sat 08:00 ET) proved the down-alert works end-to-end
(user received it), and exposed the recovery half's gap: `is_connected` only REPORTS the
socket state, ib_async never redials on its own, and `_ensure_connected_async()` ran only
at order placement — so after logging back into TWS the socket stayed dead, the monitor's
"restored" notice never fired, and the stock-tws heartbeat stayed red until the next order
(possibly days later on a weekend).
- **`IBKRExecutor.try_reconnect()` (new):** non-raising redial probe (non-blocking lock
  guards concurrent probes; DEBUG-logs failures). The TWS monitor thread calls it every
  5th tick (~5 min) while disconnected — a relogged-in TWS now produces the
  "TWS connection restored" ops alert within ~5 min, and the TWS heartbeat resumes
  pinging healthchecks.io on its own.
- Tests: `test_ibkr_executor.py` 18 → 21 (redial after socket drop, never-raises while
  TWS still down, no redial while healthy). Suite 286 → 289.
- **Stock bot needs a restart** to run the probe — the pre-fix instance will NOT send the
  restored notice when TWS comes back; restart it before (or when) logging into TWS.

### MACD live/backtest divergence resolved — option (a), validate what's live (2026-07-20) — 289 tests pass, strategy hash unchanged
Closed the OPEN DECISION flagged during the 2026-07-18 config-drift audit (see the superseded
entry above). `bot/main.py` and `shadow_signal.py` have always run with `macd_enabled=True`
(MACD gates both Mode A/B entries), but `engine_kwargs_from_cfg()` never passed it through, so
`backtest.py` and `walkforward.py` silently validated a MACD-OFF variant — live was trading a
stricter, unvalidated subset of what was on record. User chose (a): validate the strategy
actually running, not turn MACD off to preserve the old numbers.
- **Fix:** `bot/backtest/params.py` — `macd_enabled = cfg.strategy.macd_enabled` added to
  `engine_kwargs_from_cfg()`. No `bot/strategy/*.py` edit, so strategy hash `659d1c03987b72fd`
  is unchanged and `stamp_strategy.py` did not need a re-run.
  `test_engine_params.py`'s `test_macd_enabled_is_deliberately_excluded` replaced with
  `test_macd_enabled_is_sourced_from_cfg` (asserts both True and False flow through). Suite
  stays at 289 (one test replaced, not added).
- **New canonical fingerprint:** `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` →
  **32 trades, PF 1.72, 37.5% win rate** (was 35 trades / PF 1.90 / 40.0% under the old
  MACD-OFF backtest). This is now what a fresh run reproduces — see "How to verify the config
  is active" and "Canonical strategy fingerprint" sections above, both updated.
- **Walk-forward (`walkforward.py`, train 2024-04-08→2025-02-21 / validation 2025-02-22→2026-07-20):**
  training 15 trades, PF 1.09; validation 17 trades, PF **2.30**, 35.3% win. PF holds well
  out-of-sample (validation ≥ 1.2, and improves on training) — interpretation printed by the
  script: "Strong: PF holds above 1.2 out-of-sample. This config may have a genuine edge worth
  pursuing." Same pass bar used for every prior walk-forward in this file.
- **Nothing else changes:** ADX/EMA/RSI/ATR thresholds, sizing, SL/TP — all untouched. This
  was purely closing a validation-script fidelity gap, not a strategy or risk change.
- **Crypto bot does not need a restart** — `bot/main.py` was already running with
  `macd_enabled=True` before this fix; only the backtest/walk-forward tooling changed.

### Affordable-symbol screen (2026-07-15) — 7 new RULE_WHITELIST symbols, 216 tests pass
Goal: widen the funnel of symbols that can actually FILL at the ~$197 target allocation
(0.20 × ~$987 account) — 3 of 5 whitelisted symbols were stuck in SIZE_SKIP, so the
30-trade Phase A gate was fed by MRNA+PLTR alone. Ran `stock_backtest.py` (same 4-window
gate, strategy hash `659d1c03987b72fd` unchanged) on 18 untested liquid candidates chosen
for affordability. Report: `logs/stock_backtest_20260715.md`.
- **PASS + whitelisted (7): TD.TO, BNS.TO, CM.TO, SU.TO, CSCO, KO, T** — all fill 1–9
  shares at current prices. TSX banks echo RY.TO's strong pass (TD full PF 2.41,
  CM 2.09, BNS 1.89, all SL ≤ 45%); CSCO 2.46/KO 2.00/T 2.20 with SL ≤ 33%.
- **PASS but held out (1): UBER** — gate-letter pass (full PF 1.32, SL 66.7%) but
  0-for-2 in the last 500d: decayed-edge profile, margin-of-safety skip. Re-eligible
  on a future re-screen if recent windows recover.
- FAIL (9): ENB.TO, CNQ.TO, PFE, BAC, DIS, TGT (9 trades < 10, PF was fine), XOM, GDX,
  XLF. MU skipped (thin yfinance history).
- Concentration note: whitelist now holds 4 Canadian banks (RY + TD + BNS + CM). Each
  position risks ~1% of account (20% alloc × 5% SL) — acceptable for paper-book data
  collection; revisit before any live capital.
- Watchlist grew 15 → 22 symbols — watch for yfinance rate-limit pressure (known
  failure mode; 15-min price cache mitigates).
- **Stock bot needs a restart** to pick up the new .env.

### IPO policy — no automated IPO trading (2026-07-11, agreed with user)
Trigger: SpaceX IPO'd 2026-06-12 as NASDAQ:SPCX — largest IPO in history (offer $135,
raised ~$75B, ~$1.8T valuation). Pop-and-fade played out in 3 sessions: peak $225.64 on
Jun 16, then multi-week decline to ~$145 by Jul 10. Day-1 open-market buyers ($150 open)
were underwater within a month; only offer-price allocations (institutions) kept the pop.

**Policy (standing, applies to every future IPO):**
- The bots never trade IPOs or recent listings via any special path. New listings earn
  entry exactly like every other symbol: accumulate history → screener eligibility
  (needs ~36 daily candles for MACD scoring) → full `stock_backtest.py` walk-forward
  PASS → RULE_WHITELIST. No exceptions for famous names.
- Rationale: (1) the IPO pop belongs to offer-price allocations, not open-market buyers —
  pooled research shows day-1 open→close averages ~zero for public buyers; (2) per-symbol
  backtesting is impossible by definition on day 1; (3) fast trading already failed our own
  validation (1h walk-forward FAILED 2026-07-10).
- Hand-trades on hype names are the user's personal decision, outside bot capital. The bot
  never touches holdings it didn't open.

**SPCX timeline:**
- ~Early Aug 2026: ~36 trading days accumulated → screener can score it; may appear in
  universe movers / AI advisory / paper-only swing-book signals automatically. No code change.
- ~Mid-2027: enough daily history for a meaningful walk-forward (250d window needs ~a year).
  Run `stock_backtest.py` on SPCX then; whitelist only on a PASS — same gate as MRNA/AMD/RY.TO/PLTR.
- User personally holds 2 SPCX shares (visible in portfolio tracker; never bot-traded).

### Investment philosophy — two-bucket policy + plan queue (2026-07-11, agreed with user)
User defers strategy decisions ("follow the rules from great leaders and experienced persons,
not what I set"). Standing interpretation: follow *evidence* and established-investor
discipline, not hype or pasted "success rules" content.

**Two buckets:**
- **Bucket 1 — wealth building (personal, outside the bots):** Buffett's explicit guidance for
  non-professionals — low-cost broad index fund (e.g., S&P 500 ETF), regular automatic
  contributions, hold for decades (his 90/10 will instruction). The bots are NOT the wealth
  engine and must never be treated as one; studies show 70–97% of active day traders lose money.
- **Bucket 2 — trading system (this repo):** capped, gate-controlled experiment. Capital grows
  only through the documented fill-count / net-PF gates — never through conviction, streaks,
  or excitement (see Capital Sizing Rules).

**Buffett rule mapping (already enforced in code, documented 2026-07-11):**
capital protection = risk engine + breakers + slot caps · circle of competence = default-deny
whitelists + walk-forward gates · patience = HOLD through weak regimes (ADX gate) · margin of
safety = PF ≥ 1.2 net-of-fee gates + small sizing. Honest difference: the bots trade price
patterns, not businesses — momentum trading, labeled as such, not Buffett-style investing.
Permanently out of scope (unbacktestable macro plays): raw gold as store-of-value, forex
speculation, commodity supply-deficit bets, IPO flips (see IPO policy above).

**Plan queue (as of 2026-07-11, items 1–2 done 2026-07-13):**
1. ~~**Mon 2026-07-13 market open:** verify first live session of the rule-based stock pipeline~~
   — DONE: clean session (rule signals fired correctly, no errors); see sizing-visibility
   fix entry above. Found AMD's BUY signal was silently unfillable at current account size —
   fixed to log/print visibly, deliberately not fixed by forcing an oversized fill.
2. ~~**Before adding GLD**~~ — DONE 2026-07-13 (after item 1 verified clean): GLD added to
   WATCHLIST + RULE_WHITELIST in `stock_bot/.env`. GLD trades ~$300+/share — the same
   `PAPER_RISK_PCT=0.20` sizing wall that blocks AMD/RY.TO (target alloc ~$203 < 1 share)
   means it will sit as a visible SIZE_SKIP rather than fill until the paper account grows.
   That is correct behavior. Stock bot needs a restart to pick up the new .env.
3. **Keep filling gates:** crypto 0/15 live fills (BTC/CAD, $77 slot) · stock Phase A 30-trade
   counter · swing book 30-signal counter. No capital changes before gates.
4. **Open ops items:** Ollama Cloud key revoke (H — confirmed unused 2026-07-16, user
   parked it indefinitely; don't re-raise) · VPS logrotate (F) · UptimeRobot (G).
5. **Next big build after gate progress:** IBKR paper executor (D).
No new asset classes, strategies, or business pivots before these complete.

### Metals & currency screen (2026-07-11) — GLD passes, currencies ruled out
User asked whether metals/currencies could join the system. Answer: they go through the same
gates as everything else — so we ran them. Zero new code: `STOCK_BT_SYMBOLS=GLD,SLV,FXE,UUP
stock_backtest.py` (daily engine, same gate as the stock rebuild) + `EXCHANGE=binance
SYMBOL=PAXG/USDT walkforward.py` (crypto 4h engine). Report: `logs/stock_backtest_20260711.md`.

| Symbol | What | Result | Verdict |
|--------|------|--------|---------|
| GLD | Gold ETF (daily) | PF 2.18/4.66/6.07/3.60 across 4 windows, 12 full-window trades, SL ≤ 42% | **PASS** |
| SLV | Silver ETF | PF ok (1.35–2.68) but only 8 full-window trades (< 10) + SL 62–67% | FAIL |
| FXE | Euro ETF | 5 trades in ~6y, 0 in last 250d — FX volatility too low to trigger entries | FAIL |
| UUP | USD ETF | 750d PF 0.99, 500d PF 0.84 | FAIL |
| PAXG/USDT | Tokenized gold (4h crypto engine) | Train PF 1.82 (9 trades) / validation PF 5.13 (12 trades) — holds OOS | Promising, parked |

**Decisions:**
- **Currencies: closed.** The strategy structurally can't trade FX — daily moves are too small
  to trigger Mode A/B entries (FXE: zero trades in the last year). Do not revisit with this
  strategy. Buffett's 2002 forex short was a macro conviction trade — unbacktestable, out of scope.
- **SLV: FAIL** — standard re-screen triggers apply (strategy change → re-run).
- **GLD: legitimate PASS of the documented RULE_WHITELIST gate.** Added to WATCHLIST +
  RULE_WHITELIST 2026-07-13 after the rule pipeline's first live session ran clean —
  one-change-at-a-time discipline held. Paper book only, like all stock trading.
- **PAXG/USDT: parked as conditional candidate.** Small samples (9/12 trades) and all USD-pair
  preconditions still apply (BTC/CAD 15-fill live gate at 0/15, capital ≥ $500, FX-cost decision).
  Re-evaluate only when those gates open.
- Note: a GLD PASS does not contradict the Buffett discussion — the bot would trade gold's
  price trend on daily candles, not hold gold as a store of value. Different game, honestly labeled.

### Dual-strategy formalization (2026-07-06)
Stock bot now has two formally separated, named strategy books. 168 tests pass.

**Strategy architecture:**
| Book | Label | Candle | Max hold | Capital | Stats |
|------|-------|--------|----------|---------|-------|
| Main paper executor | **Position book** | `1d` daily | Days–weeks (no forced exit) | Real $ tracked | $ expectancy, net PF |
| FastValidator | **Swing book** | `1h` hourly | 48h forced exit | Unit-sized (1.0 share) | % stats only |

**Bidirectional symbol conflict guard (no more silent double exposure):**
- `fast_validator.py`: `FastValidator.__init__()` accepts `blocked_symbols_fn: Callable[[], set[str]] | None`. In `_try_enter()`, calls it and skips if the symbol is held in the position book.
- `main.py`: FastValidator created with `blocked_symbols_fn=lambda: set(executor.positions_snapshot().keys())`. Before position book BUY, checks `fast_validator.state.open_symbols()` — skips with log + print if swing book holds it.
- Both directions enforced; neither book can silently build double exposure.

**Label change in `paper_report.py`:**
- "ACCOUNT" section → "POSITION BOOK (daily candles · multi-day holds · sized in $)"
- "FAST VALIDATOR — SIGNAL BOOK" → "SWING BOOK (1h candles · 48h max hold · % stats only)"

### Broker platform research (2026-07-06)
**Decision: Interactive Brokers (IBKR) is the target live broker for the stock bot.**

Alpaca ruled out — no Canadian TSX support (SHOP.TO, RY.TO etc.), which would require splitting into two brokers and violates modularity.

**IBKR facts:**
- Python: `ib_insync` library (clean async wrapper over TWS API)
- Paper trading: built-in (TWS paper port 7497, live port 7496)
- Supports: US stocks (SMART routing) + TSX stocks (TSX exchange contract)
- Commission: US $0/trade, TSX CAD $1/trade minimum
- Monthly: $10/month small account (waived at >$125k monthly volume)
- Account minimum: $1 live; PDT rule ($25k) applies for >3 day-trades in 5 days

**Integration point:** `StockExecutorBase` in `stock_bot/execution/base.py` is the abstraction. A future `IBKRExecutor(StockExecutorBase)` in `stock_bot/execution/ibkr.py` implements all abstract methods (buy, sell, cash, position, avg_cost, positions_snapshot, etc.). No signal or strategy code changes.

**Gate before live IBKR:** same as current Phase A — 30 completed paper trades, PF ≥ 1.2, win rate ≥ 30%.

### Swing book hardening (2026-07-06 continued)
Features A, B, E from the roadmap completed. 168 tests pass.

**A — Swing book cash tracking:**
- `FastValidatorConfig` gains `starting_cash` (env: `FAST_STARTING_CASH=1000.00`) and `risk_pct` (env: `FAST_RISK_PCT=0.25`).
- `FastValidatorState` gains `cash`, `starting_cash`, `realized_pnl` — persisted in `fast_validator_state.json` and restored on restart.
- First run seeds cash from config (state file has no cash yet — `starting_cash == 0.0` guard).
- `_try_enter()` sizes real share count: `int(cash × risk_pct / entry_price)`; skips if result is 0 (not enough cash). Deducts cost on entry, adds proceeds on exit.
- `check_exits()` updates `state.cash` and `state.realized_pnl` on each close; `_write_trade()` writes real `cash_remaining` to CSV (frozen 9-column schema unchanged).
- `paper_report.py` swing book section now shows: starting cash, current cash, realized P&L + %, and net expectancy in $.
- `unified_dashboard.py`: `_fast_validator_card()` rewritten with cash stats + Phase A progress bar; `_combined_stats()` includes swing cash + open position value in total.

**B — Swing book Phase A gate (formal status):**
- Bottom of `paper_report.py` replaced: two independent `Phase A Gate Status` lines replace the single confusing `Status:` line that used position-book counts but appeared after the swing-book section.
- Format: `Position book: TRACKING (8/30 trades · PF 0.95 < 1.2)` and `Swing book: NEED DATA (0 / 30 trades)` — each uses its own count/PF/win-rate against the 30-trade / PF ≥ 1.2 / WR ≥ 30% gate. Shows `✓ PASS` when all three criteria are met independently.
- `win_rate` and `pf` initialized to `0.0` before the `if n_complete > 0:` block so the gate check has values when the book is empty.

**E — Earnings blackout for swing book:**
- `FastValidatorConfig` gains `earnings_blackout_days: int = 7` (env: `FAST_EARNINGS_BLACKOUT_DAYS=7`).
- `FastValidator.__init__()` gains `earnings_blocked_fn: Callable[[], set[str]] | None = None`. Stored as `self._earnings_blocked_fn`.
- `_try_enter()` checks the callback (after `blocked_symbols_fn`, before corruption guard) — skips with log if symbol is in the earnings blackout set.
- `main.py` wiring: `_fv_earnings_blocked: set[str]` is reset to `set()` at the top of each scan cycle; when the position-book loop detects earnings blackout for a symbol, it adds the symbol to this set (same point where `continue` fires). FastValidator receives `earnings_blocked_fn=lambda: _fv_earnings_blocked` — the lambda reads the current binding at call time, so the swing book sees the freshly populated set for each cycle's evaluation.
- Both books now share the same earnings gate with zero duplication of the yfinance earnings fetch.

### Next feature roadmap (2026-07-06)

#### Stock bot — near term
| # | Feature | Why | Effort |
|---|---------|-----|--------|
| ~~A~~ | ~~**Swing book cash tracking**~~ | ~~Add `FAST_STARTING_CASH` + real cash in `FastValidatorState` — unlocks $ expectancy for swing book, same Phase A gate as position book~~ | ~~Medium~~ |
| ~~B~~ | ~~**Swing book Phase A gate**~~ | ~~Separate 30-trade / PF ≥ 1.2 counter for swing book, independent of position book~~ | ~~Small~~ |
| ~~C~~ | ~~**Combined dashboard section**~~ | ~~Total capital across both books, per-book PF side by side, combined exposure~~ — DONE 2026-07-14 ("Gates at a Glance" strip, see Dashboard updates 2026-07-14) | ~~Medium~~ |
| ~~D~~ | ~~**IBKR paper executor**~~ | DONE 2026-07-17 — built, tested (17 hermetic tests), verified vs live TWS paper (see "IBKR paper executor built"). Remaining: confirm $1k reset landed, then decide when to flip `STOCK_EXECUTOR=ibkr` | ~~Large (~10h)~~ |
| ~~E~~ | ~~**Earnings blackout for swing book**~~ | ~~Block swing BUY within N days of next earnings — mirrors position-book gate, shares same yfinance fetch~~ | ~~Small~~ |

#### Crypto bot — near term
| # | Feature | Why | Effort |
|---|---------|-----|--------|
| F | **VPS logrotate** | `/etc/logrotate.d/trade_bot` — local log uses RotatingFileHandler, VPS has no rotation yet | Small |
| ~~G~~ | ~~**UptimeRobot monitor**~~ | DONE in code 2026-07-17 as healthchecks.io heartbeat (bot/alerts/heartbeat.py, both bots + TWS leg) — user still needs to create the 3 checks and paste URLs into env | ~~Trivial~~ |
| H | **Ollama Cloud key revoke** | ~~Identify service~~ (done 2026-07-13); key confirmed UNUSED 2026-07-16 (provider is nvidia_nim) — revoke at ollama.com + strip from stock_bot/.env. **Parked by user 2026-07-16** | Small |

#### Both bots — longer term
| # | Feature | Why | Effort |
|---|---------|-----|--------|
| I | **IBKR live go-live** | After 30 paper trades + PF ≥ 1.2 on stock bot | Gate-blocked |
| J | **USD symbol re-screen** | Re-run `screen_universe.py` after any strategy hash change | ~2h |
| K | **ATR SL experiment for SYN/LINK** | ATR×2.0–2.5 cleared in-sample — needs OOS + per-symbol walk-forward before adding. SOL@ATR×2.0 and BTC@ATR×2.0 OOS both HOLD (2026-07-17). SOL still needs SL-distance sizing + capital-gate preconditions before promotion (new symbol). BTC is live-wired (`ATR_SL_MULT`) — adopting would need a full walk-forward re-run first, not yet decided. SYN/LINK OOS run 2026-07-17: SYN HOLDS at x2.0 and x2.5 (val PF 1.99/1.86, SL ≤ 21%); LINK mixed (x2.0 train FAIL 0.85; x2.5 thin pass) — LINK out, SYN still gate-blocked. ATR-aware sizing built + validated 2026-07-17 (ATR_SIZING_ENABLED, off) | Large |

### Multi-coin readiness (2026-07-03)
The live loop is now safe to run with >1 symbol in UNIVERSE_WHITELIST. Single-symbol behavior
is numerically identical; strategy files untouched (hash `659d1c03987b72fd` still valid).
- **Aggregate account breakers:** `risk.evaluate(..., account_value=..., symbol=...)` — daily-loss
  and max-drawdown now measure the whole account (sum of all slots), not whichever slot happens
  to evaluate that tick. Position-size check stays per-slot. Backtests use the old positional
  signature and are unchanged.
- **Per-symbol daily trade cap:** `record_fill(symbol)` + `fills_today_for(symbol)` — each symbol
  gets its own RISK_MAX_TRADES_PER_DAY budget, persisted in `logs/risk_state.json`.
- **Monitoring covers every symbol** (was active-symbol only): drift reconciliation, candle
  watchdog, price-feed error counter, and daily P&L alert all run per symbol.
- **Universe refresh guard:** the 24h refresh can no longer switch to a symbol that was not
  initialized at startup (no executor / cold strategy) — it logs and keeps the current symbol.
- Adding a second coin still requires: walk-forward pass on current strategy code, capital
  ≥ $250, and the capital sizing rules above. The code is ready; the edge and capital are the gates.

### Bug fixes applied 2026-06-20
All critical bugs resolved:

**Crypto bot (bot/):**
- `bot/risk/risk_manager.py`: daily-loss, max-drawdown, and trade-cap checks block BUY only — SELL always allowed. Manual HALT blocks BUY and strategy SELL, but SL/TP exits bypass the risk gate entirely (unless `RISK_HALT_BLOCKS_STOPS=true`), so stops always fire during a halt.
- `bot/backtest/engine.py`: Added `forced_exit` flag — SL/TP triggers bypass cooldown state machine (stop-losses were being suppressed)
- `walkforward.py` + `montecarlo.py`: ADX threshold corrected 15.0 → 18.0 (was testing wrong strategy vs live)
- `config.py`: Defaults corrected — fee 0.001→0.008, SL 0.02→0.015, TP 0.04→0.10 (both dataclass and _load())
- `bot/backtest/report.py`: Added Buy-and-Hold benchmark section with alpha comparison

**Stock bot (stock_bot/):**
- `stock_bot/data/screener.py`: Removed $200 price cap (was blocking NVDA, AAPL, MSFT); RSI_OVERBOUGHT 70→75; price filter long-only (abs→positive)
- `stock_bot/main.py`: Added 5% sanity check on live_price vs candle_close — TSX fast_info currency mismatch caused impossible P&L like +921%
- `stock_bot/research/sentiment_scraper.py`: Replaced 12-word flat keyword list with phrase-pattern rules + negation detection window (3 tokens)

### Next steps — prioritized roadmap (audited 2026-06-21)

#### TODAY — Active money at risk (ALL DONE — verified in code 2026-07-04)
1. ~~**Fix backtest fee**~~ — DONE: `.env` has `BACKTEST_FEE_PCT=0.008`
2. ~~**Run 1h backtest**~~ — CLOSED by moving live to `CANDLE_MINUTES=240` (4h), which is the
   validated timeframe. No 1h validation exists; do not move back to 60 without running it.
3. ~~**Fix SL/TP risk gate bypass**~~ — DONE: SL/TP exits bypass `risk.evaluate()` entirely
   (only `RISK_HALT_BLOCKS_STOPS=true` suppresses them). Tests in `test_risk_manager.py`.
4. ~~**Fix `deploy.sh` before any VPS push**~~ — DONE: rsync preserves `live_state_*.json` and
   `trades.db`, excludes only `*.log`.

#### DAY 2 — Fee savings + silent failures (ALL DONE)
5. ~~**Enable limit orders for BUY**~~ — DONE: `LIMIT_ORDER_ENABLED=true` with limit-chase;
   2026-07-04: SL/TP exits forced to market via `urgent=True` (the chase must never hold a stop).
6. ~~**Fix dual SL evaluation paths**~~ — DONE: candle-close SL block removed; the intra-candle
   block is the only SL/TP path (comment at `bot/main.py` section 2).

#### DAY 3 — Stock bot circuit breaker + alerting (ALL DONE)
7. ~~**Fix stock bot daily loss breaker**~~ — DONE 2026-07-04: session baseline now includes
   restored position marks (avg_cost) and `_open_position_value` is seeded consistently at
   restore. Previously a restart with open positions silently disabled the breaker
   (cash-only baseline vs cash+positions current). Tests: `test_stock_breaker.py` (3).
8. ~~**Wire daily P&L Telegram alert**~~ — DONE: fires once per UTC day via date-change check.
9. ~~**Wire partial TP Telegram alert**~~ — DONE: partial TP calls `alerter.fill()`.
10. ~~**Add consecutive error counter**~~ — DONE: per-symbol `err_count`, alert at 5 consecutive failures.

#### WEEK 2 — Hardening
11. ~~Correct ADX default~~ — RESOLVED 2026-07-06: `config.py` already defaults 18.0 (dataclass
    + `_load()`). The remaining `25.0` in `bot/strategy/indicator_strategy.py:59` is dead at
    runtime (both `bot/main.py` and `backtest.py` always pass `cfg.strategy.adx_threshold`)
    and was deliberately left: editing that file invalidates strategy hash `659d1c03987b72fd`
    and forces a full walk-forward for zero behavior change. Fix it inside the next real
    strategy change.
12. ~~Correct RSI levels~~ — DONE: `.env` has `RSI_OVERSOLD=30.0` / `RSI_OVERBOUGHT=70.0`
13. Add logrotate on VPS: `/etc/logrotate.d/trade_bot` — weekly, 4 rotations, compress (log grows unbounded)
    (local log uses RotatingFileHandler 10MB × 5 — VPS journald/logrotate still unconfigured)
14. ~~Add position drift reconciliation~~ — DONE: every 120 ticks per symbol, retry ladder,
    escalation after DRIFT_ALERT_THRESHOLD consecutive detections; compares `total` (2026-07-04)
15. ~~Add candle watchdog~~ — DONE: `_check_candle_watchdog`, per symbol, alert at 2× candle_minutes
16. Set up external uptime monitor (UptimeRobot free tier) — systemd stops after 5 crashes with no external alert
17. ~~Schedule weekly `live_comparison.py`~~ — DONE 2026-07-06: installed via `ops/crontab.txt`
    (see Ops changes 2026-07-06)

#### MONTH+ — Revenue unlock gates
| Milestone | Gate | Impact |
|---|---|---|
| ~~1h backtest PF > 1.0 confirmed~~ | FAILED 2026-07-10 — see below | Day-trading (1h) ruled out on current strategy; stay on validated 4h |
| 30–50 live trades accumulated | ~2–3 months | Compare live PF vs backtest |
| Kraken maker fee confirmed <0.20% | Test one limit order | Validates limit-order cost model for XRP/CAD |
| Stock bot: 30 paper trades, PF ≥ 1.2, win rate ≥ 30% | ~4–6 weeks | Gate for Phase 7 IBKR live |
| Swing book: 30 paper signals, PF ≥ 1.2 (separate gate) | ~4–6 weeks | Gate for swing book cash tracking + sizing |
| IBKR paper executor implemented + tested | After paper gate | Enables live go-live without code rewrite |
| Capital grows to $500+ | Organic | Lower RISK_PER_TRADE_PCT from 10% → 2% |
| Add 5-day earnings blackout to stock_bot BUY | After paper validated | Avoid pre-earnings gap risk |
| Add oversold recovery to universe pre_filter | After paper validated | AI rejects overbought momentum leaders |

- Can evolve into a professional-grade trading platform
## Live Symbol Universe (updated 2026-07-02)

### Approved for live trading
| Symbol | Status | Basis |
|--------|--------|-------|
| BTC/CAD | ACTIVE | Walk-forward re-confirmed 2026-07-02 on current code: all 5 windows PF > 1.0 (1.79→2.00→2.99→3.12→3.38). Original validated pair. |

### Watchlist (not yet tradeable — monitored for re-validation)
| Symbol | Status | Reason |
|--------|--------|--------|
| XRP/CAD | WATCHLIST | Walk-forward failed on current Mode A/B strategy (2026-07-02): 5000c PF 0.99, 3000c PF 0.98, win rate 12.9%, 87% SL-exit rate. Prior ACTIVE status was validated on the retired pre-c94d297 RSI<30 strategy. Re-entry condition: full 3-window walk-forward pass on current strategy code. |

### Blocked (walk-forward failed)
| Symbol | Status | Reason |
|--------|--------|--------|
| DOGE/CAD | BLOCKED | Walk-forward failed at corrected 0.8% fee: 5000c PF 0.44, 3000c PF 0.71, 1000c PF 0.44 — all three windows below 1.0. Prior WATCHLIST entry (PF 1.43 on 1000c) was produced at wrong 0.16% fee. Volume gate ($32k vs $50k CAD/day) and wide spread (0.60%) are secondary; walk-forward failure is the deciding factor regardless of volume. |
| ETH/CAD | BLOCKED | Walk-forward failed on all windows (5000c PF 0.90, 3000c PF 1.44, 1000c PF 1.34 — full-history window fails); strategy has no edge on ETH over the full 2024–2026 period |
| SOL/CAD | BLOCKED | Walk-forward failed — all three windows below 1.0 (5000c PF 0.88, 3000c PF 0.75, 1000c PF 0.83) |

### Screened out — liquidity gate (checked 2026-07-02)
| Symbol | 24h Vol (CAD) | Gate | Reason |
|--------|--------------|------|--------|
| PEPE/CAD | $1,659 | $50,000 | Failed liquidity gate — walk-forward not run |
| XDC/CAD | $10,288 | $50,000 | Failed liquidity gate — walk-forward not run |

These are the only remaining Kraken CAD spot pairs not already decided. Re-screen when volume grows.
Screen run: `python screen_universe.py` — report at `logs/screen_results_20260702.md`.

### Implementation
- `.env`: `UNIVERSE_WHITELIST=BTC/CAD` — bot uses fixed whitelist (XRP/CAD removed 2026-07-02)
- `regime_monitor.py`: `MONITOR_SYMBOLS=BTC/CAD` (traded), `MONITOR_WATCHLIST=XRP/CAD` (health metrics only, labeled NOT TRADED)

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
- **Symbol removal must never implicitly increase surviving symbol allocation.** Use `MAX_SLOT_CASH_CAD` in `.env` to hard-cap per-slot cash. Current value: `MAX_SLOT_CASH_CAD=77`. Implemented via `CapitalPool(slot_cap=...)` in `bot/portfolio/capital_pool.py`. When a new symbol is added to the whitelist, update this value deliberately — not as a side effect of adding a slot.
- **Personal holdings in the same Kraken account are invisible to the bot by default.** `ADOPT_EXTERNAL_HOLDINGS=false` (default) ensures `LiveExecutor` only manages positions it opened itself. If Kraken balance exceeds the state-file recorded position, the excess is logged as "EXTERNAL HOLDINGS DETECTED" and is never traded. Incident: Jun 27 2026 — bot adopted and sold 0.000378 BTC deposit + 218 DOGE deposit because state file had stale `bot_opened=True` flag and no guard existed. Fixed in `live_executor._sync_position()`. Never set `ADOPT_EXTERNAL_HOLDINGS=true` unless you explicitly want the bot to trade all assets in the account.

---

## Exchange Setup
- Backtesting: EXCHANGE=binance, SYMBOL=BTC/USDT
- Live trading: EXCHANGE=kraken, SYMBOL=BTC/CAD (XRP/CAD removed from UNIVERSE_WHITELIST 2026-07-02 — walk-forward failed)
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
2. Run full backtest: `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` — confirm PF ≥ 1.79
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

The unified dashboard tracks all three live ("Capital Gate" strip). Its PF is **net of fees**
and only counts fills after 2026-06-22 21:24 UTC (validated-config go-live) — `trades.db`
`pnl` is gross, and at $100 capital fees dwarf gross P&L. Live PF below always means net PF.

1. **Live PF ≥ 1.2** over ≥15 completed round-trips
2. **Shadow match rate ≥ 95%** — run `python shadow_signal.py` to verify the live bot's
   candle-close decisions match a fresh strategy replay. Confirms the live execution is
   faithfully running the validated backtest strategy, not a diverged state.
3. **Fee and slippage within assumptions** — fill prices within 0.5% of signal-candle close;
   round-trip cost consistent with 0.40% maker BUY + 0.80% taker SELL = 1.20%.

**PF alone is insufficient at 15-trade sample sizes:**
- A **failing PF with clean fidelity** (≥95% match, slippage on-spec) means **variance, not
  strategy failure**. Extend the window to 25–30 trades rather than demoting or scaling back.
- A **passing PF with poor fidelity** (<95% match or large slippage) means the live bot may not
  be executing the validated strategy — investigate before promoting capital.
- A **failing PF with poor fidelity** requires investigation of execution problems before any
  capital decision.

**Shadow signal tool:** `python shadow_signal.py`
- Run daily (see cron note below) or before any capital gate evaluation.
- Report: `logs/shadow_report_<date>.md`. Includes strategy hash, match rate, fill slippage.
- First run (2026-07-03): 97.6% match (41/42 comparable candles), 1 MACD-state boundary
  mismatch (identical RSI/ADX on both sides — expected fresh-init vs live-accumulated difference).

**Scheduling:** runs automatically — the crypto bot's in-bot scheduler executes it daily
(SHADOW_AUDIT_TIME, default 12:05 local; see "Cron retired → in-bot audit scheduler
2026-07-14" — do NOT use macOS cron, it cannot write into ~/Desktop).
Override env for non-standard paths (manual runs):
```
SHADOW_LOOKBACK=100 SHADOW_LOG=logs/trade_bot.log SHADOW_DB=logs/trades.db python shadow_signal.py
```

### Why this matters (incident log)
- XRP/CAD: validated on old RSI < 30 strategy (pre-commit c94d297). Mode A/B entry logic
  (pullback RSI 38–58) was added without re-running XRP walk-forward. XRP traded live with real
  money for weeks on a stale, passing-but-now-failed validation. 2026-07-02: 5000c PF 0.99,
  3000c PF 0.98 — removed from live trading.

---

## USD Expansion (contingent)

**Status: no qualifying symbols as of 2026-07-03.** Screen run with strategy hash `659d1c03987b72fd`.

### Screen results (2026-07-03)
603 Kraken USD spot pairs → 178 cleared $50,000/day liquidity gate → top 15 by volume walk-forwarded.

| Symbol | Vol (USD/day) | 5000c PF | 3000c PF | 1000c PF | SL rate | Verdict |
|--------|-------------|--------|--------|--------|---------|---------|
| HYPE/USD | $13,411,669 | — | — | — | N/A | SKIP (no Binance proxy) |
| ZEC/USD | $6,964,638 | 0.99 | 1.43 | 2.65 | 87% | FAIL — full PF < 1.0 + SL 87% |
| ADA/USD | $6,669,035 | 0.60 | 0.53 | 0.00 | 90% | FAIL — PF + SL |
| SUI/USD | $5,784,470 | 1.32 | 0.98 | 2.50 | 82% | FAIL — 3000c PF 0.98 + SL 82% |
| TAO/USD | $4,137,799 | 0.93 | 1.38 | 1.64 | 86% | FAIL — full PF + SL |
| M/USD | $3,942,638 | — | — | — | N/A | SKIP (no Binance proxy) |
| SYN/USD | $3,546,542 | 1.80 | 2.56 | 2.39 | 79% | FAIL — SL 79% > 70% cap |
| XLM/USD | $2,970,677 | 0.96 | 1.14 | 0.66 | 87% | FAIL — full PF + SL |
| UNI/USD | $2,879,284 | 0.91 | 0.95 | 0.59 | 88% | FAIL — PF + SL |
| NEAR/USD | $2,573,455 | 1.05 | 1.30 | 1.18 | 86% | FAIL — full PF + SL |
| LINK/USD | $2,261,609 | 1.54 | 2.19 | 1.28 | 79% | FAIL — SL 79% > 70% cap |
| LTC/USD | $2,194,392 | 0.90 | 0.92 | 0.00 | 88% | FAIL — PF + SL |
| AAVE/USD | $1,962,514 | 0.89 | 0.79 | 1.32 | 88% | FAIL — PF + SL |
| XMR/USD | $1,891,217 | — | — | — | N/A | SKIP (Binance delisted) |
| BASED/USD | $1,879,930 | — | — | — | N/A | SKIP (no Binance proxy) |

**Dominant failure mode:** SL-exit rate 79–90% on every alt tested. The Mode A/B pullback entry
(RSI 38–58) has no edge on these assets — same pathology as XRP/CAD (87% SL rate).

**Closest near-misses on PF alone** (would still fail SL gate):
- SYN/USD: PF 1.80/2.56/2.39 but SL rate 79%
- LINK/USD: PF 1.54/2.19/1.28 but SL rate 79%

### Preconditions for any USD pair promotion
All of the following must be met before adding any USD pair to UNIVERSE_WHITELIST:
1. A future screen run produces a 3-window PASS (PF ≥ 1.2 all windows + trades ≥ 10 + SL ≤ 70%)
2. BTC/CAD live gates met: ≥ 15 fills + live PF ≥ 1.2
3. Capital ≥ $500 CAD available for the new symbol slot
4. Documented decision on CAD→USD conversion cost and ongoing FX exposure (Kraken charges
   ~0.20% conversion; USD P&L requires separate tracking from CAD base)
5. Full 3-window walk-forward pass on the CURRENT strategy code at promotion time (a pass on
   an older hash does not count)

### ATR stop-loss experiment (2026-07-04) — near-miss follow-up
`atr_sl_experiment.py` tested ATR-scaled stops (1.5–3.0 × ATR14) vs the fixed 1.5% SL on
SYN, LINK, XRP, BTC. Report: `logs/atr_sl_experiment_20260704.md`.
- SL-exit rates drop 76–87% → 9–43% everywhere; SYN and LINK clear the full screen gate
  in-sample at ATR×2.0–2.5 (PF ≥ 1.2 all windows). XRP still fails (entries have no edge).
- **OOS shows PF parity, not improvement** — ATR SL is a variance/fee improvement, not alpha.
- BTC/CAD live stays on validated fixed SL. SYN/LINK are conditional candidates: all USD
  preconditions above + fresh per-symbol walk-forward at the chosen mult + SL-distance-based
  position sizing (wider stop must not raise dollar risk per trade).

### Re-screen triggers
- Strategy code change (new hash after walk-forward) — re-screen all alts before assuming new results
- New high-volume symbol appears on Kraken USD (run `SCREEN_QUOTE=USD python screen_universe.py`)
- SL-exit rate cap relaxed (would require separate validation that high-SL symbols are genuinely profitable)
