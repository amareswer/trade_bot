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
- Python 3.10+
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

Expected total: **168 tests** (as of 2026-07-04). If `pytest --collect-only -q` reports a lower number, a file has an import error, was deleted, or was excluded from the runner. Investigate before trusting any green suite result. Suite runtime is ~5s — if it takes minutes, a test is reading live `.env` config (see hermeticity note under Execution hardening).

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
| `test_drift_escalation.py` | 6 | BUG 2: consecutive drift counter, escalation threshold, resolution reset |
| `test_tsx_validation.py` | 5 | Stock-bot TSX price sanity check |
| `test_stock_breaker.py` | 3 | Stock-bot daily-loss breaker: restart baseline includes position marks |
| `test_candle_watchdog.py` | 5 | Candle watchdog: timing, alert, no double-fire |
| `test_halt_flag.py` | 5 | Manual halt kill-switch: logs/HALT flag file engage/lift, ownership guard |
| `test_universe.py` | 4 | Universe screener: scoring, momentum filter, fallback |
| `test_main_strategy.py` | 2 | Strategy builder: full config wiring |
| `test_fast_validator_exits.py` | 6 | FastValidator exits: MAX_HOLD live-price fallback, corruption guard, SL regression |
| `test_paper_report.py` | 6 | Expectancy math: IBKR commission model, net-of-cost flip, report rendering |

Run: `python -m pytest --tb=short -q` — must show **168 passed**.

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
STOP_LOSS_PCT=0.015
TAKE_PROFIT_PCT=0.10   # was 0.045 — validated 2026-06-19
BACKTEST_LIMIT=5000
BACKTEST_TIMEFRAME=4h
EXCHANGE=binance
SYMBOL=BTC/USDT

### Live trading settings (Kraken — separate from backtest)
EXCHANGE=kraken
SYMBOL=BTC/CAD
CANDLE_MINUTES=240   # 4h — now matches the validation timeframe (was 60; the 1h config was never backtested — roadmap item 2 closed by this change)
RISK_PER_TRADE_PCT=0.10   # intentionally high at $100 capital (Kraken min order ~$4.50 CAD)
STOP_LOSS_PCT=0.015
TAKE_PROFIT_PCT=0.10   # confirmed 2026-07-01: matches backtest and regime monitor (was stale 0.045)
ORDER_TYPE=limit / LIMIT_ORDER_ENABLED=true   # BUY entries limit-chase for maker rate; ALL SL/TP exits forced to market via urgent=True (2026-07-04)

### How to verify the config is active
Run: EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py
Expected: ~39 trades, PF ~1.79 (see note below on trade count evolution)
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
- **Known-inert leftover:** the DOGE/CAD liquidity gate in `bot/main.py` is dead code (DOGE
  is BLOCKED) and hardcodes a symbol — generalize to config or delete when next touched.
- `.env` chmod 600 (was world-readable). A bare un-named secret sits as a comment under
  "── Secrets ──" in `.env` — identify the service, move to a named var, and rotate it.

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
  $564.87 entry → phantom "SL" exit at $163.51 (−71% in 20 min, data corruption not market).
  That row still poisons fast_trades.csv stats (PF 0.07 / −16.85% per signal vs the 3 sane
  trades: +4.0% TP, +1.2% MAX_HOLD, −1.5% SL). Consider resetting the fast book (only 4
  closed signals — cheap now) so post-guard stats start clean. User decision.
- **FastValidator scope widened:** scans watchlist + top FAST_MOVERS_COUNT (5) universe
  movers per cycle (was watchlist-only). Cap bounds yfinance fetch volume — raise carefully,
  rate-limit spirals are a known failure mode.
- **Expectancy report:** `stock_bot/analysis/paper_report.py` now prints per-trade net $ / net %,
  net PF, trades/week pace, and projected $/week — net of the IBKR Pro fixed commission model
  (COMMISSION_PER_SHARE_USD/CAD + COMMISSION_MIN_USD/CAD in stock_bot/.env). Slippage is NOT
  added there: the paper executor already applies PAPER_SLIPPAGE_BPS to every fill. The fast
  book is reported separately as a unit-sized signal book (% stats only — never $ expectancy).
- Phase A gate stays: 30 completed trades, PF ≥ 1.2, win rate ≥ 30% before any IBKR live step.

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
11. Correct ADX default: `config.py:383` change `25.0` → `18.0` (safe only while `.env` exists)
12. ~~Correct RSI levels~~ — DONE: `.env` has `RSI_OVERSOLD=30.0` / `RSI_OVERBOUGHT=70.0`
13. Add logrotate on VPS: `/etc/logrotate.d/trade_bot` — weekly, 4 rotations, compress (log grows unbounded)
    (local log uses RotatingFileHandler 10MB × 5 — VPS journald/logrotate still unconfigured)
14. ~~Add position drift reconciliation~~ — DONE: every 120 ticks per symbol, retry ladder,
    escalation after DRIFT_ALERT_THRESHOLD consecutive detections; compares `total` (2026-07-04)
15. ~~Add candle watchdog~~ — DONE: `_check_candle_watchdog`, per symbol, alert at 2× candle_minutes
16. Set up external uptime monitor (UptimeRobot free tier) — systemd stops after 5 crashes with no external alert
17. Schedule weekly `live_comparison.py`: `0 9 * * 1 python live_comparison.py >> logs/weekly.log`

#### MONTH+ — Revenue unlock gates
| Milestone | Gate | Impact |
|---|---|---|
| 1h backtest PF > 1.0 confirmed | Run this week | Validates live config isn't a gamble |
| 30–50 live trades accumulated | ~2–3 months | Compare live PF vs backtest |
| Kraken maker fee confirmed <0.20% | Test one limit order | Validates limit-order cost model for XRP/CAD |
| Stock bot: 30 paper trades, PF ≥ 1.2, win rate ≥ 30% | ~4–6 weeks | Gate for Phase 7 IBKR live |
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

**Cron invocation (suggested daily):**
```
# Shadow signal fidelity — run daily at 06:00 UTC
0 6 * * *  cd /path/to/trade_bot && python shadow_signal.py >> logs/shadow_signal.log 2>&1
```
Override env for non-standard paths:
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
