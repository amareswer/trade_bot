---
name: feature-plan
description: "Planned features, decisions made, and what was deferred — updated after every discussion or build session"
metadata: 
  node_type: memory
  type: project
  originSessionId: 561ac2ba-311f-4840-9ce1-8792408e3e37
---

Running log of feature decisions. Most recent first.

**Why:** User requested that every change or feature plan be noted here so nothing is forgotten across sessions.

**How to apply:** Check this before suggesting new features — don't re-propose deferred items without context. Use this to pick up exactly where we left off.

---

## 2026-06-09 — Logging, Live-Event Visibility, Banner Fix (BUILT ✓)

**Root cause discovered:** `main.py` called `logging.basicConfig(level=WARNING)` on the root logger, then attached a file handler at INFO. The root-level WARNING filtered all INFO records *before* they reached any handler — so INFO lines never reached `trade_bot.log`. Evidence: log file showed only WARNING entries, newest from Jun 8 despite bot running Jun 9.

**Files changed:**

| File | Change |
|---|---|
| `bot/main.py` | Fixed logging setup: root logger set to INFO; console StreamHandler at WARNING (terminal stays clean); file handler at INFO (everything recorded); uses explicit `addHandler` instead of `basicConfig` to avoid no-op-if-already-configured trap |
| `bot/execution/live_executor.py` | `_sync_cash()` now returns `tuple[float, str \| None]` (cash + error_msg) so caller knows if fallback was used; live-critical events upgraded from INFO→WARNING: balance sync, markets loaded, state restored, state saved, fee deducted, LiveExecutor ready; added unmissable `print()` startup line after balance sync |
| `bot/display.py` | `header()` now accepts `live_trading: bool` and `dry_run: bool`; shows `LIVE $XX` (red+bold) in live mode, `DRY RUN $XX` (yellow) in dry-run, `paper $XX` otherwise |
| `bot/main.py` | Passes `live_trading` and `dry_run` flags to `display.header()` |

**Startup print format (live mode, balance fetch succeeded):**
```
  LIVE BALANCE: $100.42 CAD | position: 0.000000 BTC | source: kraken fetch_balance
```
**Startup print format (balance fetch failed):**
```
  LIVE BALANCE: $100.00 CAD (FALLBACK — fetch_balance FAILED: <error>) | position: 0.000000 BTC
```
This is `print()` not `logging` — appears regardless of log level.

**Why live events are now WARNING:** With a broken root level, any INFO-level trade event (fill, fee, balance sync) would have been silently lost from the log file. Real-money events must survive misconfiguration. WARNING is the correct semantic level for "something happened that a human should be able to audit".

**Test result:** 30/30 pytest + 21/21 test_indicators.py. Logging fix verified by emitting test INFO/WARNING records and confirming both appear in the file.

---

## 2026-06-09 — LiveExecutor Hardening (BUILT ✓)

**What:** All hardening items from the 2026-06-07 known-gaps list implemented and test-covered.

**Files changed:**

| File | Change |
|---|---|
| `bot/execution/live_executor.py` | Full rewrite: added `_sync_cash()`, `_save_state()`, `_load_state()`, `_validate_order()`; real `_fills`/`_rejects` lists; fee deduction with wrong-currency guard; fetch_order polling (3 polls, partial-fill fallback); `reset()` fixed to restore `starting_cash`; imports `Order/OrderSide/OrderStatus/Portfolio` from `executor.py` |
| `test_live_executor.py` | NEW — 11 mocked-exchange tests: dry-run fill, min-amount reject, min-cost reject, live BUY/SELL portfolio update, fetch_order polling (close + timeout), fee deduction (quote + wrong currency), state roundtrip, balance sync (success + error), reset() |
| `test_risk_manager.py` | Fixed `test_daily_loss_limit_blocks_when_exceeded`: added `max_drawdown_pct=0.50` so drawdown check doesn't mask the daily-loss check being tested |

**What each new method does:**

| Method | Description |
|---|---|
| `_sync_cash()` | Calls `fetch_balance()`, reads `free[quote_currency]`; warns if currency absent (wrong symbol/API key); falls back to `starting_cash` on any error |
| `_save_state()` | Writes `{cash, position, cost_basis, realized_pnl, saved_at}` to `logs/live_state.json` after every fill |
| `_load_state()` | Reads state file on init; validates symbol matches; restores portfolio fields; returns False if file missing/wrong symbol |
| `_validate_order()` | Checks `limits.amount.min` and `limits.cost.min` from `load_markets()`; rejection message states both requested and minimum with quote-currency amounts |
| fee deduction | Reads `raw['fee']['cost']` from final polled response; skips if fee currency ≠ quote (logs warning); deducts from cash if quote currency |

**Startup init order:**
1. `load_markets()` (public endpoint — required; fails fast in live mode if unavailable)
2. `_load_state()` — restore position/cost_basis from disk if file exists
3. `_sync_cash()` — override cash with real exchange balance (live mode only); logs mismatch warning if saved cash differs from exchange balance by > $0.50

**state_path** is parameterizable (constructor arg, default `logs/live_state.json`) — allows tests to use temp files.

**Activation:** Set `LIVE_TRADING=true` in `.env`. Use `DRY_RUN=true` for dry-run cycles (validation fires but `create_order` is skipped). Set `DRY_RUN=false` only after verifying dry-run behavior over multiple candle cycles.

**Test coverage:** 30/30 pytest passing (test_executor.py + test_risk_manager.py + test_live_executor.py); test_indicators.py 21/21 via its own runner.

---

## 2026-06-07 — LiveExecutor (BUILT — superseded by 2026-06-09 hardening above)

**Note:** The 2026-06-07 version was the initial build. The 2026-06-09 hardening session completed the remaining items. Do not reference this entry for current feature status — see above.

---

## 2026-05-30 — Candle Aggregator for Live Mode (BUILT ✓)

**Problem:** Fundamental mismatch between backtest (real 4h OHLCV candles) and live mode (single price tick every 30s → fake candle with open=high=low=close=price). ADX needs real high/low to detect trend strength; RSI gets confused by hundreds of near-identical prices. Result: bot never traded in live mode.

**Fix:** `CandleAggregator` class added to `bot/main.py`. Accumulates live ticks over a configurable window before emitting one real OHLCV candle to the strategy.

**Files changed:**

| File | Change |
|---|---|
| `bot/main.py` | Added `CandleAggregator` class (tracks open/high/low/close/volume across ticks, emits `Candle` when period elapses); wired into `run()` — live+indicator path feeds aggregator, only calls `strategy.evaluate()` when candle is ready; simulated path keeps existing flat-candle approach |
| `bot/display.py` | Added `building_candle(elapsed_m, total_m, price, tick_n)` — shows progress bar `building candle [████░░░░░░] 60/240min` during accumulation |
| `config.py` | Added `candle_minutes: int = 240` to `ExchangeConfig`; validation `>= 1`; reads `CANDLE_MINUTES` env var; logged on startup |
| `.env` | Added `CANDLE_MINUTES=240` (4h = matches backtest timeframe) |

**Aggregator design:**
- First tick of a window: sets `open = high = low = price`, records `start_ts`
- Each subsequent tick: updates `high`/`low`, updates `close`, increments tick count
- When `time.time() - start_ts >= period_s`: emits `Candle`, resets with current tick as new open
- `elapsed_minutes` and `period_minutes` properties drive the progress bar

**Activation logic:** `candle_agg` is `None` when `FEED_MODE=simulated` — building a 4h window in real time is impractical during simulation testing. Simulated mode keeps flat fake candles.

**Startup banner shows:**
```
  Candle aggregator: 240min windows  (~480 ticks/candle)
```

**To use 1h candles instead** (4× more signals, still much better than 30s fake candles):
```
CANDLE_MINUTES=60
```

**Exchange also switched this session:** `EXCHANGE=binance`, `SYMBOL=BTC/USDT` (user updated `.env` directly).

---

## 2026-05-30 — Exchange Switch: ZebPay → Binance (ACTIVE)

User switched `.env` to `EXCHANGE=binance`, `SYMBOL=BTC/USDT`. No code changes required.

---

## 2026-05-30 — Exchange Switch: ZebPay + BTC/INR (SUPERSEDED by Binance switch above)

User switched `.env` to:
```
EXCHANGE=zebpay
SYMBOL=BTC/INR
ADX_THRESHOLD=30.0   # raised from 25.0 — stricter trend filter
BACKTEST_LIMIT=500   # lowered from 5000 — faster iteration
```

ZebPay is an Indian crypto exchange supported by ccxt. BTC/INR is the native pair (no stablecoin intermediary). ADX threshold raised to 30 for a stricter trending-only filter. Backtest limit reduced to 500 candles for quick runs.

**No code changes required** — exchange, symbol, and all strategy params are purely config-driven.

---

## 2026-05-30 — ADX Market Regime Filter (BUILT ✓)

**Problem:** Strategy was entering trades in choppy/sideways markets and getting whipsawed. 115 trades with profit factor 1.05 on 4h BTC/USDT — barely breaking even before fees.

**Solution:** ADX (Average Directional Index) filter — measures trend strength regardless of direction. ADX < threshold = ranging market → HOLD. ADX ≥ threshold = trending → allow signal through.

**Files changed:**

| File | Change |
|---|---|
| `bot/indicators/indicators.py` | Added `adx(highs, lows, closes, period)` — Wilder smoothing, returns `float\|None`, needs `2*period+1` min data points |
| `bot/strategy/indicator_strategy.py` | Added `adx_period: int = 14`, `adx_threshold: float = 25.0` to `IndicatorConfig`; `evaluate()` signature changed from `(price)` to `(candle: Candle)`; `_prices` renamed to `_closes`; added `_highs` / `_lows` deques; ADX computed each tick; `last_adx` property added |
| `bot/backtest/engine.py` | Added `adx_period`, `adx_threshold` params; calls `strategy.evaluate(candle)` instead of `strategy.evaluate(price)` |
| `bot/main.py` | Imports `Candle`; wraps live price in minimal `Candle(high=price, low=price, close=price)` to satisfy interface; `strategy._prices` ref → `strategy._closes` |
| `backtest.py` | Passes `adx_period=cfg.strategy.adx_period`, `adx_threshold=cfg.strategy.adx_threshold` to `engine.run()` |
| `config.py` | Added `adx_period: int = 14`, `adx_threshold: float = 25.0` to `StrategyConfig`; validation: `adx_period >= 2`, `0 < adx_threshold <= 100`; reads `ADX_PERIOD` and `ADX_THRESHOLD` env vars |
| `.env` / `.env.example` | Added `ADX_PERIOD=14`, `ADX_THRESHOLD=25.0` (later raised to 30.0 by user) |

**ADX filter proven working — side-by-side comparison (BTC/USDT 4h, 5000 candles):**

| Metric | ADX OFF | ADX ON (25) |
|---|---|---|
| Total trades | 117 | 60 |
| Win rate | 35.9% | 40.0% |
| Profit factor | 1.00 | **1.25** |
| Final value | $9,954 (loss) | **$10,020 (gain)** |
| Sharpe ratio | −0.41 | **+0.25** |

ADX blocked 57 choppy-market trades (48.7% reduction). Profit factor and Sharpe both improved significantly.

**Note on live mode:** Live feed is single-price (no real H/L per tick). Candle is created with `high=low=close=price`. ADX still computes but with flat candles — less effective than backtest. Full H/L live feed is a future improvement.

---

## 2026-05-30 — Stop-Loss / Take-Profit in Backtest (BUILT ✓)

**Files changed:** `bot/backtest/engine.py`, `bot/backtest/report.py`, `backtest.py`, `config.py`, `.env`

**Logic (in engine.py, before state machine filter):**
```python
if executor.position > 0 and entry_price > 0:
    if stop_loss_pct > 0 and price <= entry_price * (1 - stop_loss_pct):
        raw_signal = Signal.SELL; exit_reason = "stop_loss"
    elif take_profit_pct > 0 and price >= entry_price * (1 + take_profit_pct):
        raw_signal = Signal.SELL; exit_reason = "take_profit"
```

`FillRecord` now has `reason: str = "strategy"` field. Exit breakdown shown in report: `SL=34  TP=23  strategy=3`.

Config: `STOP_LOSS_PCT=0.02`, `TAKE_PROFIT_PCT=0.04` in `.env` and `BacktestConfig`.

---

## 2026-05-30 — Paginated Historical Data Fetch (BUILT ✓)

**Problem:** ccxt hard cap of ~1000 candles per API call. `BACKTEST_LIMIT=5000` needed 5× more data.

**Solution:** `fetch_candles_paginated()` in `bot/data/historical_feed.py`
- Pages through history using `since` parameter (milliseconds)
- `_PAGE_SIZE = 1000` — one page per call
- Deduplicates via `dict[timestamp_ms → row]`
- 0.5s sleep between pages (stays inside exchange rate limits)
- Progress: prints `page 1/5  (1000 candles so far)`

`backtest.py` import changed from `fetch_candles` to `fetch_candles_paginated`. `BACKTEST_LIMIT=5000` fetches 27 months of 4h data in 5 API calls.

---

## 2026-05-30 — Loss Protection + Tighter Risk Limits (BUILT ✓)

**Root problem found during code verification:** `DAILY_LOSS` and `MAX_DRAWDOWN` circuit breakers were blocking SELL as well as BUY. If you're in a losing position when a limit fires, the bot freezes and can't exit — making losses worse.

**Fix 1: SELL bypasses DAILY_LOSS and MAX_DRAWDOWN**
- File: `bot/risk/risk_manager.py` checks 2 and 4
- Added `if signal == Signal.BUY` guard to both — same pattern check 3 already had for daily trade cap
- Now: only manual HALT (check 1) can block a SELL. All other checks are BUY-only.

**Fix 2: AI engine hardcoded "BTC" in portfolio prompt**
- File: `bot/ai/ai_engine.py:174`
- Old: `position: {portfolio.position:.4f} BTC` (wrong for ETH, SOL, etc.)
- New: `position: {portfolio.position:.4f} {symbol.split('/')[0]}`

**Fix 3: Tighter risk defaults across the board**

| Setting | Before | After |
|---|---|---|
| `RISK_PER_TRADE_PCT` | 1% | 0.5% |
| `RISK_MAX_POSITION_PCT` | 5% | 3% |
| `RISK_DAILY_LOSS_LIMIT` | 2% | 1% |
| `RISK_MAX_DRAWDOWN` | 10% | 5% |
| `RISK_MAX_TRADES_PER_DAY` | 5 | 3 |

Files changed: `config.py`, `.env`, `.env.example`, `README.md`, `claude.md`

**Backtest comparison (BTC/USDT 1d, 721 candles):**

| Metric | Old limits | New limits |
|---|---|---|
| Total return | +0.28% | +0.14% |
| Total fees | −$3.44 | −$1.72 |
| Max drawdown | −0.34% | −0.17% |
| Profit factor | 1.58 | 1.58 |
| Sharpe | 0.44 | 0.44 |

Same strategy quality (profit factor, Sharpe unchanged). Dollar exposure halved across the board.

---

## 2026-05-29 — Live Bot Bug Fixes During Paper Trading (BUILT ✓)

**Bug 1: Daily trade cap blocking SELL — critical**
- File: `bot/risk/risk_manager.py` — check 3 in `evaluate()`
- Root cause: daily fill cap applied to both BUY and SELL. Bot went LONG and couldn't exit because 5 fills already used that day.
- Fix: cap now checks `signal == Signal.BUY` — SELL always passes regardless of daily fill count. You must always be able to close an open position.

**Bug 2: AI 429 rate-limit errors flooding terminal**
- File: `bot/ai/ai_engine.py` line 127
- Root cause: free DeepSeek model has 50 requests/day limit; bot was hitting it and logging WARNING every tick.
- Fix: downgraded from `logger.warning` to `logger.debug` — errors still captured in log file at DEBUG level but no longer shown in terminal. Bot already correctly fell back to strategy signal.

---

## 2026-05-29 — Terminal Time Display Fix (BUILT ✓)

**File changed:** `bot/display.py` line 170

**Change:** `datetime.now(timezone.utc)` → `datetime.now()` in `_now()`

**Why:** Terminal was showing UTC time, which didn't match user's local clock. Log file (`logs/trade_bot.log`) intentionally kept in UTC — only terminal display changed.

---

## 2026-05-29 — Backtest Tuning + 3 Bug Fixes (COMPLETED ✓)

**Bugs found and fixed:**

| Bug | Root cause | Fix |
|---|---|---|
| SELL orders rejected ("Insufficient position") | Dynamic sizing recalculated qty at sell-time using current price, which differed from buy-time qty | `engine.py` + `main.py`: SELL always uses `executor.position` (full close); BUY uses dynamic sizing |
| Daily trade cap blocked all trades after 5 fills | `_maybe_reset_day()` used `date.today()` (real wall clock) — all backtest candles run on the same real day, counter never reset | Added `candle_date: Optional[date]` param to `RiskManager.evaluate()` and `_maybe_reset_day()`; backtest passes `candle.timestamp.date()` |
| Only 2 trades reported (misleading) | Both bugs above silently killed most fills; combined effect showed 2 trades when 18+ were possible | Fixed by above two changes |

**Validated strategy settings (final):**

```
BACKTEST_TIMEFRAME=1d
FAST_EMA_PERIOD=9
SLOW_EMA_PERIOD=21
RSI_OVERBOUGHT=70.0
RSI_OVERSOLD=30.0
COOLDOWN_TICKS=3
```

**Backtest results on these settings:**

| Metric | BTC/USDT | ETH/USDT | Target | Pass? |
|---|---|---|---|---|
| Profit factor | 1.58 | 1.66 | > 1.5 | ✓ |
| Avg win / avg loss | 5.1× | 4.1× | wins > losses | ✓ |
| Total return | +0.28% | +0.47% | positive | ✓ |
| Max drawdown | -0.34% | -0.65% | < -10% | ✓ |
| Sharpe ratio | 0.44 | 0.44 | > 1.0 | ✗ |

Sharpe < 1.0 is expected for daily swing trading (17 trades / 2 years = most days are zero-return). Profit factor is the primary metric for this strategy type. Both symbols pass.

**Period tested:** Jun 2024 → May 2026 (2 years, 721 daily candles, Kraken).

**Next step:** Paper trading with live prices (`python -m bot.main`) for 2–4 weeks. Watch that signals look sensible. Do not change settings until after the paper trading run.

---

## 2026-05-29 — Central Config + Dynamic Position Sizing (BUILT ✓)

**Files created/changed:**
- `config.py` (NEW) — single source of truth for all settings; reads `.env` overrides; validates on startup; fails fast with clear errors
- `.env.example` (UPDATED) — documents every available env variable with inline comments
- `bot/risk/risk_manager.py` — added `max_drawdown_pct` field + all-time peak tracking + `MAX_DRAWDOWN` circuit breaker (check #2 in evaluate)
- `bot/execution/executor.py` — `execute()` now accepts optional `quantity` param for dynamic sizing
- `bot/main.py` — fully rewritten to import from `cfg`; no hardcoded constants; uses `cfg.calc_trade_qty()` each tick
- `backtest.py` — fully rewritten to import from `cfg`; no hardcoded constants
- `bot/backtest/engine.py` — replaced fixed `trade_qty` param with `risk_per_trade_pct`; quantity calculated dynamically per candle

**Key design decisions:**
- `config.py` has zero imports from `bot/` — clean boundary
- `cfg` is a module-level singleton: `from config import cfg`
- Dynamic sizing: `qty = cash × risk_per_trade_pct / price` — scales with balance automatically
- Max drawdown circuit breaker never resets (unlike daily loss which resets each day)
- All validation in `__post_init__` — bot refuses to start with invalid config

**New risk defaults (industry standard):**

| Setting | Old | New |
|---|---|---|
| `RISK_PER_TRADE_PCT` | fixed 0.01 BTC | 1% of cash (dynamic) |
| `RISK_MAX_POSITION_PCT` | 10% | 5% |
| `RISK_DAILY_LOSS_LIMIT` | 5% | 2% |
| `RISK_MAX_DRAWDOWN` | not existed | 10% (new circuit breaker) |
| `RISK_MAX_TRADES_PER_DAY` | 10 | 5 |
| `COOLDOWN_TICKS` | 5 | 10 |

---

## 2026-05-29 — Backtesting (BUILT ✓)

**Decision:** Build backtesting before anything else. User confirmed.

**Why deferred multi-asset:** Strategy has never been validated on a single symbol. Running unvalidated strategy on 20 coins = 20× the bad trades. Backtesting must come first.

**Design agreed:**

| Item | Detail |
|---|---|
| Data source | ccxt `fetch_ohlcv()` — real historical OHLCV candles |
| New files | `bot/backtest/engine.py`, `bot/backtest/metrics.py`, `bot/backtest/report.py`, `bot/data/historical_feed.py`, `backtest.py` |
| Reused unchanged | Strategy, RiskManager, TradingStateMachine, PaperExecutor, PositionManager |
| Config | `EXCHANGE`, `SYMBOL`, `TIMEFRAME` (1h/4h/1d), `LIMIT` (candles), `FEE_PCT` (0.1%) |
| Output | Terminal report + `logs/backtest_SYMBOL_TIMEFRAME_DATE.csv` |

**Metrics to compute:**
- Total return %
- Win rate (% profitable trades)
- Profit factor (gross profit ÷ gross loss)
- Max drawdown %
- Avg win / avg loss
- Sharpe ratio
- Total trades, best trade, worst trade, total fees

**Lookahead bias prevention:** candles fed one at a time in order — strategy never sees future data.

**Files built:**
- `bot/data/historical_feed.py` — `fetch_candles()` via ccxt `fetch_ohlcv()`, returns `list[Candle]`
- `bot/backtest/engine.py` — `run()` loops candles through full pipeline, returns `BacktestResult`
- `bot/backtest/metrics.py` — `compute()` calculates all stats from result
- `bot/backtest/report.py` — `print_report()` terminal output + `save_csv()` to logs/
- `backtest.py` — CLI entry point, run with `python backtest.py`

**Bug found and fixed during build:** `RISK_MAX_POSITION_PCT = 0.02` (2%) was blocking ALL trades in both the live bot and backtest. At $70k BTC with $10k portfolio, 0.01 BTC = 7% of portfolio > 2% limit. Fixed to `0.10` (10%) in both `main.py` and `backtest.py`. This would have silently blocked every trade in the live bot too.

---

## 2026-05-29 — HTML Dashboard (BUILT)

Single `dashboard.html` overwritten every tick. Auto-refreshes via `<meta http-equiv="refresh">` at `LOOP_INTERVAL` seconds. No server needed, open once in browser.

Shows: price, cash, position, unrealized/realized P&L, total value, state, RSI, trend, signal, trade history table, last 30 ticks log.

Config: `DASHBOARD_ENABLED = True`, `DASHBOARD_PATH` in `main.py`.
File written to project root as `dashboard.html`.

---

## 2026-05-29 — Multi-Asset Trading (DEFERRED)

User asked about trading BTC, ETH, and all other coins simultaneously with auto buy/sell.

**Deferred because:** Strategy not yet validated. Multi-asset before backtesting = multi-asset bad trades. Revisit after backtesting confirms strategy works.

**Design discussed when ready:**
- `SYMBOLS` list instead of single `SYMBOL`
- Per-symbol: `CcxtFeed`, `IndicatorStrategy`, `TradingStateMachine`, `PositionManager`
- Shared: `PaperExecutor` (cash pool), `RiskManager`
- Cash allocation: % of cash per symbol (via existing `max_position_pct`)
- Auto-discovery option: `exchange.load_markets()` → filter top 20 by 24h volume
- Dashboard: symbol table (one row per coin)

---

## 2026-05-29 — Stock / Forex Support (DISCUSSED, NOT PLANNED)

User asked if bot works beyond crypto. Currently crypto-only via ccxt.

Architecture is market-agnostic above the data layer. To add stocks/forex, only `ccxt_client.py` + `price_feed.py` need replacing with a new provider (Alpaca for US stocks, yfinance for read-only, Zerodha Kite for Indian stocks).

No timeline set. Not a current priority.

---

## 2026-05-28 — Security Audit (COMPLETED)

See [[security-audit]] for full details. All issues fixed. API key rotation still pending (user action required).

---

## 2026-05-27 — Phase 1–6 Complete (BUILT)

All 6 phases from CLAUDE.md built and working:
- Phase 1: SimulatedFeed
- Phase 2: CcxtFeed (live market data)
- Phase 3: PaperExecutor (paper trading)
- Phase 4: RiskManager (4-check gate)
- Phase 5: IndicatorStrategy (RSI + EMA)
- Phase 6: AIEngine (OpenRouter advisory, cannot execute trades)
