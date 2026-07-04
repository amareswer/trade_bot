---
name: progress-current
description: "Current stage and what is in progress"
metadata:
  type: project
---

**Status as of 2026-07-03 (Session 7 complete):**

## Session 2026-07-03 (Session 7) — Full Audit + Crypto Hardening + Multi-Coin Readiness (COMPLETE ✅)

### Full-codebase audit (crypto expert review)
- 139/139 tests passed pre-change; strategy hash verified live == stamped (`659d1c03987b72fd`)
- Most CLAUDE.md roadmap items confirmed already done (fee 0.008, SL/TP bypass, deploy.sh state
  preservation, limit BUY 0.998, alerts, drift check, watchdog, min-order validation)
- Found: stock-bot daily-loss breaker baseline still cash-only after restart (paper.py:88 —
  NOT yet fixed, crypto prioritized); no runtime kill-switch; risk state not persisted;
  daily P&L double-fire; local-time daily reset; unpinned requirements; no CI; Python 3.9
  actually running the suite despite 3.10+ target

### Crypto fixes shipped (all in risk/execution/main — strategy hash UNCHANGED)
- `logs/HALT` flag-file kill-switch (`_check_halt_flag()` in bot/main.py, Telegram on engage/lift)
- RiskManager state persistence → `logs/risk_state.json` (live only; peak/day-open/fill counts)
- RiskManager daily reset now UTC (`_utc_today()`)
- Daily P&L alert: fires exactly once per UTC day (date-change trigger)

### Multi-coin readiness (single-symbol behavior numerically identical)
- `risk.evaluate(..., account_value=, symbol=)`: daily-loss/drawdown measure aggregate account;
  position-size stays per-slot; per-symbol daily trade caps via `record_fill(symbol)`
- Drift check, candle watchdog, price-feed error counter, daily P&L now per-symbol (was active-symbol only)
- Universe refresh guard: cannot switch to a symbol with no executor (would trade cold)
- Gates to actually add a coin: walk-forward pass on current code + capital ≥ $250

### Tests: 139 → **152 passed** (halt flag 5, risk persistence 4, multi-symbol 4). CLAUDE.md manifest updated.

### ATR stop-loss experiment — COMPLETE ✅ (2026-07-04, report: logs/atr_sl_experiment_20260704.md)
Tool: `atr_sl_experiment.py` (repo root; reproduces screen_universe config, sweeps ATR_SL_MULT).
Baseline reproduced screen exactly (SYN 1.80/2.56/2.39, SL 79%) — harness validated.
- **Mechanism confirmed:** ATR×2.0–2.5 stops cut SL-exit rate 76–87% → 9–43% everywhere
  (in-sample + OOS), win rate ~triples, net return improves in every OOS case (less fee bleed).
- **But PF gains do NOT replicate OOS** (parity ±0.1) → ATR SL = variance/fee improvement,
  NOT proven alpha. In-sample lifts (SYN 1.80→1.93, BTC 1.77→2.20) partly window-specific.
- XRP still fails all variants (entries dead — not an overfit rescue). SYN + LINK now clear
  the screen gate in-sample at ATR×2.0–2.5 (fixed SL never did).
- **Decision: BTC/CAD live unchanged** (validated fixed SL; no OOS PF gain; don't reset the
  15-fill comparison). SYN/LINK = conditional candidates via USD-expansion preconditions
  (capital ≥ $500 + BTC gates + per-symbol walk-forward at chosen mult + SL-distance sizing).

---

## Session 2026-07-03 (Session 6) — Uptime + Ledger Reconciliation (COMPLETE ✅)

### Workstream 1 — Uptime (58% downtime diagnosed and mitigated)

**Root cause:** Bot was running on local Mac (caffeinate), not VPS. Mac sleep + manual stops = all downtime.
No systemd on Mac → no automatic restart. 42 distinct gaps > 30 min across 34.9-day log window.

**Fixes applied:**
- `deploy/trade_bot.service`: `Restart=on-failure` → `Restart=always`; `StartLimitIntervalSec=300`+`StartLimitBurst=5` → `StartLimitIntervalSec=0` (never give up)
- `bot/main.py`: `_record_startup_and_check_crash_loop()` added — writes `logs/startup_timestamps.txt`, fires `alerter.error()` on 3+ restarts in 5 min
- Candle watchdog extracted to `_check_candle_watchdog()` module-level function (unit-testable)
- `test_candle_watchdog.py`: 5 tests with mocked clock — all PASS
- `deploy/UPTIME.md`: Operational guide (how to check, what each alert means, systemd commands)

### Workstream 2 — Ledger Reconciliation + Fee Capture

**Fee capture:**
- `executor.py` Order: added `fee_cost: float = 0.0` + `fee_currency: str = ""`
- `live_executor.py`: fills Order with actual fee from Kraken exchange response
- `trade_log.py`: added `fee_cost`/`fee_currency` columns + in-place migration; `log_fill()` takes fee args; `source` kwarg prepends to notes
- All 3 `log_fill()` call sites in `bot/main.py` now pass fee data from order
- `shadow_signal.py`: shows `actual_fee=X.XX%` when fee_cost populated (fallback to `assumed_fee`)

**Reconciliation (reconcile_ledger.py):**
- Fetched 7 Kraken BTC/CAD trades (full history), 0 XRP/CAD
- True Kraken balance: **154.11 CAD** (matches 2 × 77.05 from live_state files)
- True realized P&L: **-2.20 CAD** (including all fees); DB had only recorded -0.02
- 2 phantom rows marked (qty=0 SELLs with no position): id=2, id=3
- 6 Kraken trades backfilled into trades.db with fee data (source='kraken_backfill')
- 1 unexplained SELL (Jun 27, 0.000378 BTC) — no matching BUY in Kraken API window
- Report: `logs/reconciliation_20260703.md`

### Test results
- **114/114 tests pass** (was 109; +5 candle watchdog tests)

---

**Status as of 2026-07-02 (Sessions 3–5 complete):**
- ATR SL drift resolved (Session 3). Walk-forward re-confirmed with BACKTEST_SINCE/UNTIL pinning (Session 4).
- XRP/CAD removed from live trading (Session 5): walk-forward fails on Mode A/B strategy. Moved to watchlist.
  `.env`: UNIVERSE_WHITELIST=BTC/CAD, MAX_CONCURRENT_POSITIONS=1, UNIVERSE_SIZE=1, STARTING_CASH=100.
- Strategy fingerprint guard added (Session 5): SHA-256 over bot/strategy/*.py logged at startup and in backtest header.
  `logs/validated_strategy_hash` stamped with hash `d3c7c383d91d5ef9`.
  Startup warns loudly if code drifts from stamped version.
- `regime_monitor.py`: XRP/CAD moved to MONITOR_WATCHLIST (health metrics, NOT TRADED label).
- CLAUDE.md: XRP→WATCHLIST, canonical fingerprint section added, Validation Discipline section added.
- 109/109 tests pass. Pinned backtest: 39 trades, PF 1.77 (within expected variance range 1.77–1.79).

---

## Session 2026-07-02 — Bot Hardening Tasks 1–6 (COMPLETE ✅)

### Changes applied

**Task 1 — SL/TP bypass of risk gate (bot/main.py)**
- Intra-candle SL/TP path now calls `executor.execute()` directly, bypassing `risk.evaluate()`
- New env var `RISK_HALT_BLOCKS_STOPS` (default false) — when true, manual halt also suppresses stops
- `risk.record_fill()` still called on FILLED for accounting
- Unit test added to `test_risk_manager.py`: `test_sl_tp_bypasses_risk_gate_in_halt()`

**Task 2 — BUY fills to trades.db (ALREADY DONE — no code change)**
- Verified: unified `trade_log.log_fill(side=order.side.value, ...)` at lines 1391–1398 covers both BUY and SELL
- Also verified: partial TP path (lines 859–867) already calls `alerter.fill()` — CLAUDE.md item 9 was stale
- known-gaps item 1 marked RESOLVED

**Task 3 — deploy.sh state preservation**
- Added `--exclude='logs/*.log'` alongside existing `--exclude='logs/trade_bot.log'`
- Added explanatory comment: live_state_*.json and trades.db must survive redeploys

**Task 4 — regime_monitor.py alignment**
- `compute_rolling_pf()`: added `ema_spread_val >= MIN_EMA_SPREAD_PCT` to is_buy condition
- `print_table()` + `append_log()`: trade_count == 0 → PF shows "N/A (no signals in window)",
  excluded from pass/fail; verdict reads "X/3 measurable conditions met"

**Task 5 — Dead candle-close SL block**
- Block was already removed in 2026-06-22 session; added a one-line comment at the
  intra-candle block confirming it is the only SL/TP evaluation path

**Task 6 — Config hygiene (config.py)**
- ADX_THRESHOLD default changed 25.0 → 18.0 (dataclass + _load())
- RSI_OVERSOLD/RSI_OVERBOUGHT confirmed 30.0/70.0 (no change needed)
- live_state.json legacy default: added comment in live_executor.py confirming it's never reached
- ADX missing-env warning downgraded from WARNING to INFO (default is now correct)
- New field `RISK_HALT_BLOCKS_STOPS` added to RiskConfig dataclass and _load()

### Test results
- 109/109 tests pass (108 baseline + 1 new SL/TP bypass test)

### Backtest fingerprint
- Pre-change baseline: 33 trades / PF 2.19 / WR 42.4%
- Post-change: identical (my changes do NOT alter strategy behavior)
- CLAUDE.md fingerprint (58 trades / PF 1.79) is stale — caused by ATR_SL_ENABLED=true
  in .env with default ATR_SL_MULTIPLIER=2.0 (different from validation run). Not caused
  by these changes. See known-gaps.md item 4 for details.

### Open items from this session
- Backtest fingerprint in CLAUDE.md needs updating (separate task — confirm ATR_SL config)
  → RESOLVED in Session 2 — see below

---

## Session 2026-07-02 (Session 2) — ATR SL Config Drift Fix (COMPLETE ✅)

### Root cause
Two independent ATR SL config systems in config.py:
- `StrategyConfig` (live bot): reads `ATR_SL_MULT` — .env `ATR_SL_MULT=0.0` → live was CORRECT
- `BacktestConfig` (redundant): reads `ATR_SL_ENABLED` + `ATR_SL_MULTIPLIER` — .env `ATR_SL_ENABLED=true` → backtest used ATR SL at 2× → 33 trades / PF 2.19 (not validated 58 / 1.79)

### Changes applied

**config.py:**
- `StrategyConfig.atr_sl_mult` default: 2.0 → 0.0 (disabled; convention: 0 = disabled)
- `BacktestConfig`: removed `atr_sl_enabled`/`atr_sl_multiplier`; added `atr_sl_mult` (reads `ATR_SL_MULT`, default 0.0)
- `calc_trade_qty_atr`: updated ref from `backtest.atr_sl_multiplier` → `backtest.atr_sl_mult`
- `_load()`: removed `ATR_SL_ENABLED`/`ATR_SL_MULTIPLIER` reads; `BacktestConfig.atr_sl_mult` reads same key as `StrategyConfig`
- Added `_STRATEGY_CRITICAL_PREFIXES` and `_KNOWN_STRATEGY_ENV_KEYS` constants before `_load()`
- Added startup strategy fingerprint log (SL type, SL%, TP%, ADX, RSI, EMA spread, whitelist)
- Added drift guard: WARN if .env contains unknown key matching ATR_/RSI_/ADX_/STOP_/TAKE_/RISK_/EMA_ prefixes

**bot/backtest/engine.py:**
- Replaced `atr_sl_enabled: bool, atr_sl_multiplier: float` params with `atr_sl_mult: float = 0.0`
- Check changed: `atr_sl_enabled and _entry_atr > 0` → `atr_sl_mult > 0 and _entry_atr > 0`

**backtest.py:**
- Replaced `atr_sl_enabled = cfg.backtest.atr_sl_enabled, atr_sl_multiplier = cfg.backtest.atr_sl_multiplier` with `atr_sl_mult = cfg.backtest.atr_sl_mult`

**swing_backtest.py + swing_walkforward.py:**
- Replaced `atr_sl_enabled=False, atr_sl_multiplier=2.0` with `atr_sl_mult=0.0`

**.env:**
- Removed `ATR_SL_ENABLED=true` (stale key that caused drift)
- `ATR_SL_MULT=0.0` retained

**CLAUDE.md:**
- Updated fingerprint: now ~39 trades / PF 1.79 (count lower due to EMA spread filter added 2026-06-27)
- Added ATR drift incident note with timeline

### Live impact
- 1 fill under ATR config: 2026-06-22 16:36 UTC, SELL BTC/CAD 0.00055556 @ 91433.5, pnl=-0.02, reason='trail_stop'
- Live on fixed SL=1.5% from 2026-06-22 21:24 UTC onwards

### Test results
- 12/12 tests pass

### Backtest verification
- `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` → 39 trades / PF 1.79 / exit SL=27 TP=7 / max DD -4.41%
- PF 1.79 confirmed ✓ (trade count different from original 58 due to EMA spread filter added 2026-06-27)

---

**Status as of 2026-07-01:** Two live symbols (BTC/CAD + XRP/CAD). Capital $77.05/symbol. Bot running on 4h candles. DOGE/CAD blocked. ETH/CAD blocked. Live PF accumulating — no completed round-trips yet on new allocation.

---

## Session 2026-07-01 — Live-Bot Audit + Config Corrections (COMPLETE ✅)

### Fixes applied (no restarts required — config/doc only)
- **Ambient-balance guard** — `live_executor.py`: bot now reads actual Kraken balances on startup instead of assuming cash = STARTING_CASH; prevents ghost positions after restart
- **Maker-order fix** — `live_executor.py`: added `postOnly=True` param to limit BUY call; corrected price offset from `price * 1.001` (taker-side) to `price * 0.998` (bid-side passive) — fix was needed to actually achieve maker 0.40% rate
- **.env corruption fix** — `.env` had duplicate/conflicting EXCHANGE lines; cleaned to single `EXCHANGE=kraken`
- **BACKTEST_FEE_PCT corrected** — `0.0016` → `0.008` in `.env`; all prior PF numbers under old value were optimistic
- **DOGE/CAD → BLOCKED** — walk-forward failed at corrected 0.8% fee (5000c PF 0.44); removed from `UNIVERSE_WHITELIST`
- **MAX_CONCURRENT_POSITIONS / UNIVERSE_SIZE corrected** — both `3` → `2` after DOGE removal; pool is now BTC/CAD + XRP/CAD only
- **Capital confirmed**: $77.05 available per symbol; position sizing at 10% RISK_PER_TRADE_PCT = $7.70 CAD per BUY (0.000092 BTC at $83,300) — passes Kraken minimums (amt_min=0.00005 BTC, cost_min=$1 CAD)
- **TP/SL confirmed consistent** — `.env` TP=10% / SL=1.5% confirmed matching across `.env`, `backtest.py`, and `regime_monitor.py`; CLAUDE.md stale reference (TP=0.045) corrected

### Known gaps logged this session (see decisions/known-gaps.md)
- BUY fills not written to trades.db
- Regime monitor rolling PF missing MIN_EMA_SPREAD_PCT filter
- live_state.json (no symbol suffix) is dead code

---

**Status as of 2026-06-25 Late:** Multi-symbol parallel execution live in paper mode. 5 CAD crypto symbols running simultaneously, each with independent strategy/state machine/position manager. Bot at 22:04 UTC, next candle close ~1h 55m.

---

## Session 2026-06-25 Late — Multi-Symbol Parallel Execution (COMPLETE ✅)

**Phase 6C: symbol_state dict built in bot/main.py only — no other files touched.**

### Architecture
- `symbol_state: dict[str, dict]` — 5 symbols, each holding:
  `strategy`, `sm` (TradingStateMachine), `pm` (PositionManager),
  `last_ts_ms`, `trail_peak`, `partial_done`, `atr_sl`, `atr_tp`, `last_price`
- Shared across all symbols: `RiskManager`, `LiveExecutor` (single cash pool)
- Per-symbol cash cap: `executor.cash / cfg.universe.size` (= $200 each at $1k)
- All 5 symbols warm up sequentially on startup (~7s total)

### Changes made (bot/main.py only)
1. `_warmup_strategy()` — added `symbol: str = None` param; falls back to `cfg.exchange.symbol`
2. `_fetch_completed_candle()` — added `symbol: str = None` param; same fallback pattern
3. `per_symbol_max_pct()` helper added to `config.py` (standalone function before `_load()`)
4. Startup: `symbol_state` dict replaces single `state_machine` / `position_manager` init
5. Restart recovery moved to after `symbol_state` loop; uses `symbol_state[_active_symbol]`
6. Main loop: `for sym, ss in symbol_state.items()` wraps all per-symbol processing
7. Price fetch: `live_exchange.fetch_ticker(sym)['last']` per symbol (was `feed.get_price()`)
8. Intra-candle SL/TP fully migrated — all refs use `ss['trail_peak']`, `ss['atr_sl']` etc.
9. **3 `executor.position` bugs fixed → `ss['pm'].quantity`** (lines 656, 658, 709)
10. Display/tick-log/dashboard: only rendered for `sym == _active_symbol`
11. `live_signals.csv` gains `symbol` column
12. Backward-compat aliases kept: `state_machine`, `position_manager` (used by `_render_dashboard` closure + `display.stopped()`)
13. `last_candle_ts_ms` alias removed (zero remaining uses)

### Active symbols (2026-06-25, Kraken CAD pairs)
| Symbol | Price |
|---|---|
| BTC/CAD | $84,307 |
| ETH/CAD | $2,222 |
| XRP/CAD | $1.48 |
| SOL/CAD | $96.06 |
| DOGE/CAD | $0.11 |

**Bot running:** 5 symbols, PAPER_MODE=true, $1,000 virtual cash ($200/symbol cap)
**Next candle close:** ~1h 55m from 22:04 UTC

---

**Status as of 2026-06-25 (updated):** Crypto bot switched to BTC/CAD paper mode with CryptoUniverse auto-selection. MR_RSI_OVERSOLD tuned to 38 (Config C). Waiting for first paper signal.

---

## Session 2026-06-25 Evening — CryptoUniverse + Paper Mode (IN PROGRESS)

- **MR_RSI_OVERSOLD changed 35 → 38** (Config C validated)
- **CryptoUniverse built** (`bot/data/crypto_universe.py`)
  - Scans 7 CAD crypto pairs on Kraken
  - Ranks by volume × momentum
  - Config-driven quote currency and exclusion list
- **UNIVERSE_ENABLED=true** — auto-selects BTC/CAD today
- **PAPER_MODE=true** — $1,000 virtual cash, dry_run=True
- **PaperConfig added** to config.py
- Both symbol sync issues fixed
- Bot now running on BTC/CAD paper mode

**Active .env flags (crypto bot):**

| Setting | Value |
|---|---|
| SYMBOL | ETH/CAD (overridden by universe to BTC/CAD) |
| UNIVERSE_ENABLED | true |
| UNIVERSE_SIZE | 5 |
| UNIVERSE_QUOTE | CAD |
| UNIVERSE_EXCLUDE | EUR,USD,USDC,USDT,DAI,BUSD |
| PAPER_MODE | true |
| PAPER_STARTING_CASH | 1000.00 |
| MR_RSI_OVERSOLD | 38 |
| REGIME_ENABLED | true |

**Next:** Wait for first BTC/CAD paper signal on candle close. After 15 paper trades — evaluate PF and consider going live.

---

**Status as of 2026-06-24 (updated):** Two bots active. Crypto bot live on Kraken (ETH/CAD, limit orders active). Stock bot in paper trading with AC.TO open position ($24.29). Walk-forward on 1d swing strategy: VALIDATED. Week 2 hardening complete. AI confidence band tracker built. Three strategy fixes applied.

**Paper state RESET 2026-06-23:** Both `stock_bot/paper_trades.csv` and `stock_bot/paper_state.json` deleted and reset to $1,000.00 clean.
- **Reason:** Corrupted data from early development — prices in millions (TSX currency mismatch bug), share counts of 43,984 on a $1k account, self-test data leaked into real CSV before tempfile fix was applied.
- **Self-test isolation:** The `if __name__ == "__main__":` block in paper.py already uses `tempfile.mkdtemp()` to redirect state files — verified working, does not touch real files.
- **Paper trading clock restarts** from $1,000.00 clean as of 2026-06-23. Confidence tracking now active from first real trade.
- **paper_state.json** written fresh: `{"cash": 1000.00, "starting_cash": 1000.00, "positions": {}, "realized_pnl": 0.0, "orders": []}`.
- **paper_trades.csv** not recreated — will be auto-created with correct 9-column header on first real fill.

---

## Session 2026-06-24 (continued) — Crypto Config Updates + Stock Bot Stability (COMPLETE ✅)

### Crypto bot — active config changes
- **SYMBOL changed to ETH/CAD** (not ETH/USD — Kraken balance is CAD)
- **LIMIT_ORDER_ENABLED=true** — post-only limit orders active (maker 0.40% rate, confirmed Jun 14 fill)
- **REGIME_ENABLED=false** — `_ranging_signal()` disabled; unvalidated, not safe to run live
- **ATR_SL_MULT=0.0** — ATR stops disabled; fixed 1.5% SL active
- **ZeroDivisionError fixed** in `position_manager.py` `on_buy()` / `on_sell()` — division by zero guard added
- **One crashed trade 08:00 UTC Jun 23** — no funds lost, cash $99.86 CAD; bot recovered and is live

### Stock bot — AC.TO corruption root cause found and fixed
- **Root cause:** yfinance `fast_info` returned $103 for an actual $24 stock (currency mismatch)
  - Bot bought at corrupted $103 price; SL fired at real ~$24 price → -$300 paper loss
- **Fix A:** Pre-trade sanity check in `paper.py` — rejects BUY if `|candle_close - live_price| / live_price > 0.10` (10% deviation)
- **Fix B:** `raw_live_price` preserved in `main.py` before sanity null — price object wasn't surviving the check
- **Fix C:** Duplicate price tolerance fixed — changed from absolute `$0.01` to relative `0.1%` (was rejecting legitimate same-priced stocks)
- **AC.TO restored** to watchlist (was briefly replaced with TD.TO during investigation)
- **Paper state reset** — clean $1,000 start as of 2026-06-24
- **First clean trade:** AC.TO 10 shares @ $24.29 (open position)
- **Realized P&L:** $0.00 (no completed round-trips yet)

---

## Session 2026-06-24 — AI Confidence Tracker + Strategy Fixes (COMPLETE ✅)

### Task 1 — AI confidence band accuracy tracker (DONE ✓)
New files: `stock_bot/analysis/__init__.py`, `stock_bot/analysis/accuracy_tracker.py`, `stock_analysis.py`
- `ConfidenceBandTracker`: load_trades(), pair_trades(), band_report(), recommendation()
- Confidence bands: LOW 70–79, MED 80–89, HIGH 90–100, PRE <70 (pre-tracker)
- paper.py: `buy()` now accepts `confidence=0`, written to CSV as 9th column
- paper.py: `save_state()` now includes `starting_cash` for paper_report to read
- main.py: `executor.buy()` now passes `confidence=verdict.confidence`

**Stock bot validation framework:**
- Gate: 15+ completed trades → check band_report()
- Live trading gate: 80+ confidence band win% >= 55%, trades >= 10
- Run: `python stock_analysis.py --report`

### Task 2 — Three strategy fixes (DONE ✓)

**FIX A — EMA 2-candle confirmation:**
- `indicators.py: trend()` — added `confirmation_candles=1` (default, preserves prev_trend behavior)
  When `confirmation_candles=2`: computes EMA on prices[:-1] to verify prior candle shows same direction
- `main.py: _fetch_symbol_data()` — now calls `calc_trend(closes, fast_period=9, slow_period=21, confirmation_candles=2)`
- Removed external `_prev_trend` state tracking dict (no longer needed)

**FIX B — Universe composite 1d+5d momentum:**
- `universe.py: _batch_metrics()` — score now uses abs() values and weights 0.4×1d + 0.6×5d
  `composite = (0.40 × abs(change_1d)) + (0.60 × abs(change_5d))`; `score = volume_ratio × composite`
- Stocks just starting to move today rank higher; stocks that moved 5 days ago but stalling rank lower

**FIX C — ATR-based volatility context for AI:**
- `indicators.py: atr(highs, lows, closes, period=14)` — Wilder's smoothing, returns float | None
- `main.py: _fetch_symbol_data()` — computes `atr_val = calc_atr(highs, lows, closes, period=14)`, added to data dict
- `main.py: _run_ai_call()` — passes `"atr": data.get("atr")` in indicators dict
- `prompt_builder.py` — adds `ATR(14): $X.XX (X.X% of price) — {bucket}` to PRICE & TECHNICALS
  bucket: >3% = high volatility, 1-3% = moderate, <1% = low
  AI rule added: high ATR = wider natural swings, may hit 5% SL on noise

### Task 3 — Paper trade report (DONE ✓)
New file: `stock_bot/analysis/paper_report.py`
- `generate_report()`: reads paper_trades.csv + paper_state.json, no network calls
- Shows: ACCOUNT, COMPLETED ROUND-TRIPS, OPEN POSITIONS, SUMMARY STATS
- Status: NEED MORE DATA / TRACKING / VALIDATED based on completed trade count
- Integrated into `stock_analysis.py --report` flag

---

## Session 2026-06-23 (continued x2) — Daily Loss Fix + Stock Backtester (COMPLETE ✅)

### Task 1 — Daily loss circuit breaker fix — paper.py (DONE ✓)
Bug: `_is_daily_loss_tripped()` compared cash-only drawdown, ignoring open position losses.
If 3 positions each down 4%, cash was unchanged — breaker never fired.

Fix applied to `stock_bot/execution/paper.py`:
- Added `self._open_position_value: float = 0.0` in `__init__`
- New method `_update_position_value(prices: dict[str, float])` — called after every fill
  - Uses fresh fill price for the traded symbol, avg_cost proxy for others
- `_is_daily_loss_tripped()` now uses `current_total = self._cash + self._open_position_value`
- Called in `buy()` and `sell()` after FILLED, passing `{sym: fill_px}`
- Self-test: `python stock_bot/execution/paper.py` → ALL PASS (5/5 checks)

### Task 2 — Stock bot backtester — stock_backtest.py (DONE ✓)
New file: `stock_backtest.py` in project root.
- Uses `yf.download(period="5y", interval="1d")` per symbol; 0.5s sleep between
- Indicator-only: RSI<35 + BULLISH EMA trend + ADX≥20 → BUY; SL/TP/strategy SELL
- Shared cash pool, max 4 positions, 25% risk/trade, 0.5% commission, 15 bps slippage
- Saves to `stock_bot/logs/stock_backtest_YYYYMMDD.csv`

**BASELINE RESULTS (2026-06-23, 11 symbols, 5 years):**

| Symbol     | Trades | Win% | PF   | Return% | MaxDD% |
|------------|--------|------|------|---------|--------|
| HOOD       | 0      |  0%  | 0.00 | +0.00%  | -0.00% |
| MRNA       | 0      |  0%  | 0.00 | +0.00%  | -0.00% |
| NCLH       | 0      |  0%  | 0.00 | +0.00%  | -0.00% |
| CCL        | 0      |  0%  | 0.00 | +0.00%  | -0.00% |
| INTC       | 0      |  0%  | 0.00 | +0.00%  | -0.00% |
| AAPL       | 0      |  0%  | 0.00 | +0.00%  | -0.00% |
| NVDA       | 0      |  0%  | 0.00 | +0.00%  | -0.00% |
| AMD        | 1      |100%  | inf  | +14.88% | -0.00% |
| AC.TO      | 0      |  0%  | 0.00 | +0.00%  | -0.00% |
| BMO.TO     | 1      |  0%  | 0.00 | -5.78%  | -0.00% |
| CM.TO      | 0      |  0%  | 0.00 | +0.00%  | -0.00% |

**AGGREGATE:**
- Total trades: 2 | Win rate: 50.0% | PF: 2.45
- Return: +1.87% | Max DD: -1.54% | Sharpe: 3.07
- Commission: $50.53

**BASELINE VERDICT: FAIL** (only 2 trades — too few to be statistically significant)

**Interpretation:** RSI<35 + BULLISH EMA trend + ADX≥20 is an extremely selective combination
that rarely fires without AI as the primary signal generator. The live paper bot uses AI with
indicators as gates, not indicator-only. Paper trading PF cannot meaningfully be compared to
this indicator-only baseline. Paper bot's primary signal is AI confidence ≥ 70.

**What paper trading must beat:** N/A as a direct comparison — the backtester confirms the
pure indicator strategy is too rare to establish a stat-sig baseline. Paper trades should be
compared to each other over time (30+ trades needed for meaningful PF).

---

## Session 2026-06-23 (continued) — Swing Walk-Forward + Week 2 Hardening (COMPLETE ✅)

### 1d Swing Walk-Forward — swing_walkforward.py (DONE ✓)

New file: `swing_walkforward.py` — validates SL=4% TP=25% on 1d BTC/USDT across 3 OOS periods.

**Results (fee=0.8%, ADX≥18, RSI filter ON, cash=$10k):**

| Period           | Candles | Trades | PF   | Return% | MaxDD% | Verdict |
|------------------|---------|--------|------|---------|--------|---------|
| Train 2017–2022  | 1963    | 29     | 2.67 | +8.35%  | -3.76% | PASS    |
| Val_1 2023–mid24 | 547     | 8      | 2.30 | +1.50%  | -1.41% | PASS    |
| Val_2 mid24–now  | 723     | 5      | 1.54 | +0.06%  | -2.21% | PASS    |

**Conclusion: VALIDATED — Edge holds out-of-sample. Safe to paper-trade alongside 4h bot.**

### 1d Swing Strategy — Status: VALIDATED

- Config: SL=4% TP=25% ADX=18 RSI_FILTER=true cooldown=3 fee=0.8%
- All 3 walk-forward periods PASS (PF ≥ 1.3)
- Val_2 (recent regime) shows PF decay to 1.54 — lower trade count (5 trades), still PASS
- **Next step:** paper-trade alongside live 4h bot for 4 weeks; compare signal quality before activating with real capital
- Do NOT add to live .env — research only until paper-trade period complete

### Week 2 Hardening — COMPLETE ✅ (4 items)

**CHANGE A — Candle watchdog (bot/main.py)**
- Added `_last_candle_time = time.time()` to initialization block
- Fires `alerter.error()` when no new candle for `candle_minutes × 2` minutes
- Resets after firing to avoid spam every tick
- Updates `_last_candle_time` each time a new candle arrives

**CHANGE B — Position drift reconciliation (bot/main.py)**
- Runs every 60 ticks in live mode only
- Calls `executor._exchange.fetch_balance()`, compares exchange vs bot position
- Fires `alerter.error()` + `logger.warning()` if drift > 10 satoshi (0.000010)
- Fails silently on exchange error (logs warning only)

**CHANGE C — Logrotate config (deploy/logrotate_trade_bot.conf)**
- Weekly rotation, 4 rotations kept, compressed, copytruncate (no service restart needed)
- Install: `sudo cp deploy/logrotate_trade_bot.conf /etc/logrotate.d/trade_bot`
- Requires replacing `/path/to/your/project` with actual VPS path

**CHANGE D — UptimeRobot setup guide (deploy/UPTIME_MONITOR.md)**
- Full step-by-step: create account, Heartbeat monitor, VPS cron ping, alert contacts
- Explains why Heartbeat (no HTTP server) and systemd restart limit pitfall

---

## Session 2026-06-23 — Alerting + Swing Backtest + DCA Module (COMPLETE ✅)

### Task 1 — Telegram alert wiring in bot/main.py (DONE ✓)
Three changes made to `bot/main.py`:
- **Partial TP alert**: added `trade_log.log_fill()` + `alerter.fill()` immediately after partial TP fills (previously unreported real-money exits)
- **Midnight daily P&L**: added UTC midnight check after `time.sleep()` at bottom of main loop → calls `alerter.daily_pnl()` with realized_pnl, total_value, fills_today
- **Consecutive error counter**: `_consecutive_errors` counter increments on each price fetch failure; calls `alerter.error()` when >= 5 consecutive failures; resets to 0 on success
- Verified: `python -c "from bot.main import run; print('import OK')"` → clean

### Task 2 — 1D swing backtest sweep (DONE ✓)
New file: `swing_backtest.py`
- Fetches 5000 × 1d BTC/USDT from Binance (got 3,233 — full history since Aug 2017)
- Sweeps 6 SL/TP combinations at fee=0.8%, ADX≥18, RSI filter ON, cooldown=3 ticks
- **Results:**

| SL%  | TP%  | Trades | Win%  | PF   | MaxDD%  | Return%  | Verdict  |
|------|------|--------|-------|------|---------|----------|----------|
| 2%   | 10%  | 83     | 20.5% | 1.30 | -10.21% | -9.16%   | MARGINAL |
| 3%   | 15%  | 75     | 21.3% | 1.30 | -10.10% | -6.74%   | MARGINAL |
| 3%   | 20%  | 66     | 21.2% | 1.68 | -4.09%  | -0.05%   | PASS     |
| 4%   | 20%  | 61     | 26.2% | 1.58 | -6.56%  | +0.53%   | PASS     |
| 4%   | 25%  | 59     | 27.1% | 1.85 | -5.17%  | +5.19%   | PASS ⭐  |
| 5%   | 25%  | 54     | 29.6% | 1.67 | -4.83%  | +4.00%   | PASS     |

- **Best config**: SL=4%, TP=25%, PF=1.85, 59 trades, return +5.19%, maxDD -5.17%
- Saved to `logs/swing_backtest_1d_20260623.csv`
- **Decision**: noted as candidate only — 1d candles, different from live 4h config. Do NOT change live .env. Requires forward walk-forward before any promotion.

### Task 3 — DCA module (DONE ✓)
New file: `dca_bot.py`
- Standalone — separate from live bot, separate state: `logs/dca_state.json`
- Config via .env: DCA_AMOUNT_CAD=50, DCA_INTERVAL_DAYS=7, DCA_SYMBOL=BTC/CAD, DCA_EXCHANGE=kraken
- Filters: RSI overbought skip (DCA_SKIP_IF_RSI_ABOVE), daily trend skip (DCA_SKIP_IF_DAILY_BEARISH)
- DCA_DRY_RUN=true (default): updates state as if filled, never places real orders
- DCA_DRY_RUN=false: places real market BUY via ccxt using KRAKEN_API_KEY/SECRET
- `--report` flag: prints buy history table + portfolio summary (no network calls)
- Dry-run test confirmed: filters working (today BEARISH — correctly skipped)
- Full buy summary output confirmed with filters disabled

---

**Status as of 2026-06-19:** Two bots active. Crypto bot live on Kraken. Stock bot stable on paper trading at $1,000.

---

## Stock Bot (stock_bot/)

**Status:** STABLE ✅ — Phase 6 complete

**Running:** `python -m stock_bot.main` — paper trading active

**Last session (2026-06-19):** Stability fixes + 8 signal/execution quality fixes
- Reverted session management from price_feed.py (broke yfinance)
- Reverted ticker.info company name lookup (2-3s penalty per symbol)
- Added price validation in paper.py buy(): type check, 0 < price < 500k, shares < 100k
- Added state corruption guard in _load_state(): rejects cash > $1M or |realized_pnl| > $1M
- Added int(shares) storage in portfolio tracker
- Screener price filter: $5–$200 (universe symbols)
- Max 4 positions enforced in main.py
- Stop loss -5% / take profit +12% using fresh fetch_candles per position
- WATCHLIST changed to HOOD,MRNA,NCLH,AC.TO,CCL,INTC (affordable at $1k)

**This session (2026-06-19 continued):** Signal quality + infrastructure
- Fix 1: Sentiment Laplace smoothing (K=4) + confidence field
- Fix 2: Google Trends None vs 0 — AI no longer sees "zero interest" on rate-limited cycles
- Fix 3: Intraday execution price via get_live_price() — paper no longer buys at yesterday's close
- Fix 4: SL/TP watcher daemon thread (30s) — replaces 120s scan-loop SL check
- Fix 5: Volume ratio (vol / 20d avg) in Candle dataclass + AI prompt
- Fix 6: News ticker collision fix — ≤3-char tickers use word-boundary regex
- Fix 7: Daily loss circuit breaker (3%) in paper executor
- Fix 8: Slippage model (15 bps) on all paper fills
- Fix 9: Dynamic holiday computation — hardcoded 2026 sets removed, works any year
- Fix 10: _get_loop_mode() partial-holiday fix — US-only holidays no longer kill TSX pre-market scan
- Fix 11: _run_news_scan() now covers watchlist + universe_symbols (not watchlist only)

**Active config:**
- `PAPER_STARTING_CASH=1000.00` | `PAPER_RISK_PCT=0.25` | `PAPER_MIN_CONFIDENCE=70`
- `UNIVERSE_SIZE=10` | `WATCHLIST=HOOD,MRNA,NCLH,AC.TO,CCL,INTC`
- `AI_PROVIDER=nvidia_nim` | `NVIDIA_MODEL=openai/gpt-oss-120b`
- `PAPER_STOP_LOSS_PCT=0.05` | `PAPER_TAKE_PROFIT_PCT=0.12`
- `PAPER_DAILY_LOSS_PCT=0.03` | `PAPER_SLIPPAGE_BPS=15`

**Real portfolio (display only, no paper trading):**
- BMO.TO: 5 shares @ $66.10 | CM.TO: 4 @ $41.15 | SPCX: 2 @ $160.00
- EBON: 3 @ $1.95 | IGC: 50 @ $0.2799

## Stock Bot — Next Steps (updated 2026-06-28)

### Completed this session ✅
- [x] Watchlist expanded: 6 → 14 symbols (added NVDA, AMD, TSLA, SHOP.TO, RY.TO, PLTR, META, AMZN)
- [x] PAPER_MIN_CONFIDENCE lowered: 70 → 65
- [x] UNIVERSE_ENABLED confirmed true, UNIVERSE_SIZE set to 15
- [x] PAPER_RISK_PCT lowered: 0.25 → 0.20 (wider watchlist needs smaller per-trade allocation)
- [x] PAPER_TAKE_PROFIT_PCT raised: 0.12 → 0.15 (let winners run on momentum names)
- [x] Screener confirmed no upper bound (removed in prior session)
- [x] Phase 7 IBKR executor skeleton built: stock_bot/execution/ibkr_executor.py
      - 6/6 self-tests PASS
      - Same interface as StockPaperExecutor — one-line swap in main.py
      - All methods fail-safe (no exceptions propagate)
      - Default IBKR_PAPER=true (cannot accidentally go live)

### Active config (stock_bot/.env)
WATCHLIST=HOOD,MRNA,NCLH,AC.TO,CCL,INTC,NVDA,AMD,TSLA,SHOP.TO,RY.TO,PLTR,META,AMZN
PAPER_MIN_CONFIDENCE=65
UNIVERSE_ENABLED=true
UNIVERSE_SIZE=15
PAPER_RISK_PCT=0.20
PAPER_STOP_LOSS_PCT=0.05
PAPER_TAKE_PROFIT_PCT=0.15
PAPER_DAILY_LOSS_PCT=0.03
PAPER_SLIPPAGE_BPS=15

### Paper trading state (as of 2026-06-28)
- Cash: $520.71 | Open: AC.TO (10 @ $24.29), DLTR (2 @ $118.22)
- Completed round-trips: 0
- Target before IBKR activation: 30 completed trades

### Remaining gates before Phase 7 (IBKR live)
1. 30+ completed paper round-trips accumulated
2. python stock_analysis.py --report → PF >= 1.2, win rate >= 30%
3. HIGH confidence band (80+): win% >= 55%, trades >= 10
4. pip install ib_insync → run 10 IBKR paper trades → verify fills

### Next development work (do NOT touch until paper gates met)
- Earnings blackout: 5-day window before earnings → block BUY (add to prompt_builder.py)
- Oversold recovery filter: universe pre_filter rejects RSI > 60 on universe symbols
- IBKR paper mode test: activate IBKRExecutor with IBKR_PAPER=true, 10 trades, verify

---

## Crypto Bot (bot/)

**Status:** LIVE on Kraken BTC/CAD

**Fee status:** Taker 0.80%, maker confirmed 0.40% (Jun 14 real fill). Limit BUY + market SELL = 1.20% round trip.

**Next steps:**
1. Accumulate 30-50 live trades and compare live PF/win rate to backtest
2. Maker fee confirmed 0.40% via Jun 14 live fill (was assumed 0.16% — incorrect)
3. Once fee confirmed <0.20%: consider ETH/CAD expansion
4. When capital grows to $500+: revisit RISK_PER_TRADE_PCT (lower to 2%)

---

## Open Items (Crypto)

1. **Accumulate 15+ live fills per symbol** — then evaluate live PF vs backtest before capital increase
2. **Capital increase gates** — $100→$250 requires 15 trades + live PF ≥ 1.2 + no single loss >3%; see CLAUDE.md Capital Sizing Rules
3. ~~**Wire daily P&L Telegram alert**~~ — DONE (bot/main.py midnight loop at lines 1443-1454)
4. ~~**Wire partial TP alert**~~ — DONE (lines 859-867 already call alerter.fill())
5. ~~**live_state.json (no symbol suffix)**~~ — ADDRESSED (comment added to live_executor.py; file on disk is inert)
6. ~~**BUY fills missing from trades.db**~~ — RESOLVED (unified call covers BUY+SELL; see known-gaps.md item 1)
7. ~~**Update CLAUDE.md backtest fingerprint**~~ — RESOLVED 2026-07-02 (ATR SL drift fixed; now ~39 trades/PF 1.79 confirmed; drift guard added to config.py)

---

## PM Audit — 2026-06-21 (Multi-agent review)

Three agents audited crypto bot, stock bot, and deployment. Findings below by priority.

### TODAY — Active money at risk
- [x] Fix `BACKTEST_FEE_PCT=0.001` → `0.008` in `.env` — DONE (prior session)
- [ ] Run 1h backtest (`BACKTEST_TIMEFRAME=1h`) — live bot on 1h but ALL validation done on 4h; untested
- [x] Fix SL/TP risk gate bypass — DONE 2026-07-02 (Task 1: direct execute, RISK_HALT_BLOCKS_STOPS=false default)
- [x] Fix `deploy.sh` — DONE 2026-07-02 (Task 3: added --exclude='logs/*.log', preserved state files)

### DAY 2
- [x] Enable limit orders for BUY only — DONE (prior session, ORDER_TYPE=limit active)
- [x] Remove dual SL evaluation path — DONE (prior session; confirmed absent 2026-07-02, comment added)

### DAY 3
- [ ] Fix stock bot daily loss breaker (`paper.py:81,114`) — uses cash only, ignores position value
- [x] Wire `alerter.daily_pnl()` in `bot/main.py` midnight loop — ALREADY DONE (lines 1443-1454)
- [x] Wire `alerter.fill()` on partial TP path — ALREADY DONE (lines 859-867)
- [x] Add consecutive error counter → Telegram after 5 failures — DONE (prior session)

### WEEK 2
- [x] ADX default `config.py`: `25.0` → `18.0` — DONE 2026-07-02 (Task 6)
- [x] RSI levels confirmed: `RSI_OVERSOLD=30.0 RSI_OVERBOUGHT=70.0` in both code and .env ✓
- [x] Add logrotate on VPS — `deploy/logrotate_trade_bot.conf` created ✓
- [x] Add position drift reconciliation — wired in `bot/main.py` (every 120 ticks) ✓
- [x] Add candle watchdog alert — wired in `bot/main.py` (2× candle_minutes) ✓
- [x] External uptime monitor — `deploy/UPTIME_MONITOR.md` setup guide created ✓
- [ ] Cron for `live_comparison.py` weekly

### MONTH+ Gates
- Kraken fee <0.20% confirmed → ETH/CAD expansion
- 30-50 paper trades on stock bot → PF ≥ 1.2, win rate ≥ 30% → Phase 7 IBKR live
- Capital $500+ → lower RISK_PER_TRADE_PCT 10% → 2%

---

## Session 2026-06-21 — Tier 1–3 Professional Upgrade (COMPLETE ✅)

**Metrics (Tier 1):**
- `bot/indicators/indicators.py`: ATR (Wilder's), MACD (12/26/9) added
- `bot/backtest/metrics.py`: Sortino, Calmar, annualized return
- `bot/backtest/report.py`: new metric rows in terminal output

**Live hardening (Tier 2):**
- `bot/main.py`: trailing stop (intra-candle tick), partial TP (partial_tp_pct > 0), MTF 1D gate (blocks BUY when daily BEARISH)
- `bot/backtest/engine.py`: trail_stop_pct, partial_tp_pct, partial_tp_size wired (all default 0 — baseline preserved)

**Infrastructure (Tier 3):**
- `bot/signals/external_signals.py`: ExternalSignalGate (Fear & Greed + BTC funding rate, fail-open, 1h TTL)
- `bot/alerts/telegram.py`: TelegramAlerter (daemon threads, fill/daily_pnl/error/startup)
- `bot/data/trade_log.py`: TradeLog (SQLite at logs/trades.db)
- `live_comparison.py`: CLI — loads live fills, computes PF/win rate/Sharpe vs baseline
- `deploy/trade_bot.service` + `deploy/deploy.sh`: systemd + one-shot VPS deploy

**New env vars:**
EXT_FNG_ENABLED, EXT_FUNDING_ENABLED, TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
TRAIL_STOP_PCT, PARTIAL_TP_PCT, PARTIAL_TP_SIZE

**Hardcoded value audit (COMPLETE):** All previously hardcoded exchange/symbol/thresholds replaced with config reads in both bots. See decisions/stock-bot-stability.md for stock bot specifics.

---

---

## Session 2026-06-22 — Critical Fixes + Live Config Hardening (COMPLETE ✅)

**Backtest validation:**
- Corrected BACKTEST_FEE_PCT=0.001 → 0.008 (real Kraken taker rate)
- 4h backtest at 0.8% fee: PF 1.78, 61 trades, return -22.68% (fee drag)
- 1h backtest at 0.8% fee: PF 0.49 — strategy FAILS on 1h (zero TPs fired)

**Decision: locked to 4h candles** — see decisions/timeframe-4h-validated.md

**Code fixes:**
- `bot/main.py`: removed dead candle-close SL/TP block (trail stop always fires first)
- `bot/execution/live_executor.py`: limit BUY at price*0.998 (maker 0.40%, confirmed), SELL always market, poll 9s
- `deploy/deploy.sh`: preserves live_state.json and trades.db on redeploy (was wiping entire logs/)
- Risk gate bypass: confirmed already fixed — risk_manager.py only gates BUY

**Bot status:** Running locally (caffeinate) on Kraken BTC/CAD
- Position: 0.000556 BTC recovered, entry reseeded at $91,466 (actual was $90,611 — minor)
- Cash: $49.47 CAD | Total: $100.29

---

## Active .env — Crypto Bot (bot/.env)

| Setting | Value |
|---|---|
| EXCHANGE | kraken |
| SYMBOL | ETH/CAD |
| CANDLE_MINUTES | 240 |
| ORDER_TYPE | limit |
| LIMIT_ORDER_ENABLED | true |
| REGIME_ENABLED | false |
| ATR_SL_MULT | 0.0 |
| ADX_THRESHOLD | 18 |
| RSI_FILTER_ENABLED | true |
| RSI_OVERSOLD | 30.0 |
| RSI_OVERBOUGHT | 70.0 |
| VOLUME_K | 0 |
| STOP_LOSS_PCT | 0.015 |
| TAKE_PROFIT_PCT | 0.10 |
| RISK_PER_TRADE_PCT | 0.50 |
| BACKTEST_FEE_PCT | 0.008 |
| LIVE_TRADING | true |
| DRY_RUN | false |
