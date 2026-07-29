---
name: feature-plan
description: "Planned features, decisions made, and what was deferred — updated after every discussion or build session"
metadata: 
  node_type: memory
  type: project
  originSessionId: 561ac2ba-311f-4840-9ce1-8792408e3e37
---

Running log of feature decisions. Most recent first.

---

## Deferred / Tech Debt

| Item | Detail |
|---|---|
| Fix 5 pre-existing failing crypto bot tests | `test_halt_blocks_all_signals` (risk_manager), `test_fetch_order_polling_timeout_uses_partial_fill`, `test_state_save_load_roundtrip`, `test_sync_cash_uses_exchange_free_balance`, `test_restart_recovery_seeds_position_manager` (all in live_executor). All are balance-sync / mock issues in the test environment — live bot unaffected. Fix when test suite is next touched. |

---

## 2026-07-28 (cont'd) — Stock Bot Breaker Staleness + Stall Investigation (BUILT ✓)

Fixed: `StockPaperExecutor`'s daily-loss breaker used a stale position mark between fills
(only refreshed at `buy()`/`sell()` time) — added `refresh_position_marks()` +
`_mark_positions_to_market()`, called once per scan cycle in `stock_bot/main.py` right after
Phase 1 prices are fetched, so the breaker sees live prices even with zero fills that cycle;
`IBKRExecutor` gets a no-op version for parity. Verified the feeding price is already
sanity-checked inside `fetch_candles()` (bounds, duplicate-price, outlier, TSX cross-check)
before it reaches the mark-to-market call. Also fixed: Phase 1's price-fetch now logs
`"cycle N failed: <reason>"` on total fetch failure instead of silently completing an empty
cycle. Separately investigated an apparent ~6h stock-bot scan-loop stall (restarted, new PID
25877) that turned out to most likely be normal `AFTER_HOURS`-mode silence rather than a real
hang — corrected that diagnosis after further checking rather than letting it stand. Session
audit confirmed no session-leak bug in `price_feed.py` (yfinance manages its own sessions by
design, per the documented hard rule). 4 new tests (`test_stock_position_mark_refresh.py`),
328/328 → 332/332 passing throughout. Full detail: `.memory/decisions/known-gaps.md` gap #11.

---

## 2026-07-28 — Crypto Execution/Risk Audit (BUILT ✓)

Line-by-line review of `live_executor.py`, `risk_manager.py`, `retry.py`, and the
`bot/main.py` call sites. Fixed: limit-chase cancel-race double-fill risk (unverifiable
post-cancel state now always aborts the re-place); rejected orders now alert to Telegram,
not just console; `_sync_cash`/`_sync_position` `fetch_balance()` wrapped in
`fetch_with_retry` + alert on persistent failure; `_sync_position`'s cost_basis reseed no
longer writes a fabricated 0.0 on a ticker-fetch failure (was silently overstating
realized P&L); guarded a latent `None.reject_reason` crash in the rejected-order branch;
documented previously-undocumented risk-gate `.env` keys (`RISK_MAX_POSITION_PCT`,
`RISK_DAILY_LOSS_LIMIT`, `RISK_MAX_DRAWDOWN`, `RISK_MAX_TRADES_PER_DAY`, `COOLDOWN_TICKS`,
`RISK_HALT_BLOCKS_STOPS`) in CLAUDE.md; fixed a stale ATR SL line citation
(`bot/main.py:1813` → `1855-1870`). 328/328 tests passing throughout; strategy hash
`659d1c03987b72fd` unchanged (execution/risk files only). Full detail:
`.memory/decisions/known-gaps.md` gaps #9–#10.

---

## 2026-06-24 (continued) — Crypto Hardening + Stock Bot Stability (BUILT ✓)

### Limit order post-only chase with PO rejection retry (BUILT ✓)
Post-only limit BUY placed at bid-side offset; retries on POST_ONLY_REJECT with adjusted price.
Maker rate (0.40%) vs taker (0.80%) — 0.40% saving per round trip.

### REGIME_ENABLED flag (BUILT ✓)
`_ranging_signal()` disabled when `REGIME_ENABLED=false`. Unvalidated signal — safe to disable live.
New env var: `REGIME_ENABLED=false`.

### ATR_SL_MULT=0.0 guard (BUILT ✓)
When `ATR_SL_MULT=0.0`, ATR-based stop-loss is skipped entirely. Fixed 1.5% SL stays active.
Prevents division-by-zero and untested ATR stop widths.

### position_manager.py ZeroDivisionError guard (BUILT ✓)
`on_buy()` / `on_sell()` had division by zero when qty=0. Guard added — logs warning and skips.

### Stock bot pre-trade price sanity check (BUILT ✓)
`paper.py buy()`: rejects BUY if `|candle_close - live_price| / live_price > 0.10`.
Root cause: yfinance `fast_info` returned $103 for a $24 stock (CAD/USD currency mismatch).
Previous corruption: bot bought at $103, SL fired at $24 → -$300 paper loss.

### Stock bot duplicate price relative tolerance (BUILT ✓)
Changed from absolute `$0.01` to relative `0.1%` tolerance in `_is_duplicate_price()`.
Old: same price within $0.01 = corrupt. New: same price within 0.1% = corrupt.
Fix: legitimate stocks at similar price points ($24.29 ≈ $24.30) were being rejected as corrupt.

### Stock bot weekend message fix (BUILT ✓)
Weekend log message now shows actual day name + next trading day date.
Old: "Market closed — weekend". New: "Market closed — Saturday, next open Monday 2026-06-29".

---

## 2026-06-24 — Phase 5 REBUILD: Unified Tabbed Dashboard (BUILT ✓)

Rewrote `unified_dashboard.py`. Single entry point — open `unified_dashboard.html` in browser.

- **3 tabs:** Crypto | Stocks | Portfolio
- **Crypto tab:** embeds `dashboard.html` via `<iframe>` (full crypto bot dashboard)
- **Stocks tab:** embeds `stock_dashboard.html` via `<iframe>` (full stock bot dashboard)
- **Portfolio tab:** inline from `logs/live_state.json` + `stock_bot/paper_state.json`
  - Combined capital stat blocks (total value, realized P&L, crypto fees)
  - Crypto bot card: cash, position, cost basis, fees, total value
  - Stock bot card: cash, open positions, position value (est.), return %
  - Stock open positions table
- **Tab persistence:** `localStorage` — survives 30s auto-refresh, no tab jump
- **Bot renderers unchanged:** `dashboard.html` and `stock_dashboard.html` still written independently by each bot's main loop
- **CLI:** `python unified_dashboard.py` (once) or `python unified_dashboard.py --watch` (30s loop)

---

## 2026-06-24 — AI Confidence Tracker + Three Strategy Fixes (BUILT ✓)

### Confidence band accuracy tracker (BUILT ✓)
Validation system that reads paper_trades.csv and measures whether AI confidence scores predict profits.
Bands: LOW 70–79, MED 80–89, HIGH 90–100, PRE <70.
**Why:** AI confidence IS the primary signal — need to verify it has edge before going live.
Gate: 80+ confidence win% ≥ 55%, trades ≥ 10 → eligible for IBKR live.
New files: `stock_bot/analysis/accuracy_tracker.py`, `stock_bot/analysis/paper_report.py`, `stock_analysis.py`.
paper.py: buy() now takes `confidence=0`, writes to CSV; save_state() includes `starting_cash`.
main.py: executor.buy() passes `confidence=verdict.confidence`.

### FIX A — EMA 2-candle confirmation (BUILT ✓)
`trend(confirmation_candles=2)` checks prior candle EMA direction internally instead of
requiring external `_prev_trend` state. Removes noisy single-candle crossover signals.
`_prev_trend` dict removed from main.py. Default `confirmation_candles=1` preserves existing behavior.

### FIX B — Universe composite momentum (BUILT ✓)
Score: `volume_ratio × (0.40×|change_1d| + 0.60×|change_5d|)`.
Old: `volume_ratio × (0.60×change_1d + 0.40×change_5d)` (no abs, wrong weights).
**Why:** abs() means both rising and falling stocks rank; 60% weight to 5d prevents
overnight noise from dominating; 40% to 1d captures stocks just starting to move.

### FIX C — ATR volatility context for AI (BUILT ✓)
`atr(highs, lows, closes, period=14)` added to indicators.py (Wilder's smoothing).
Passed through main.py → indicators dict → prompt_builder.py.
Prompt shows: `ATR(14): $X.XX (X.X% of price) — high/moderate/low volatility`.
AI rule: high ATR means natural swings may noise-trigger a 5% SL — lower confidence.

---

## 2026-06-23 — Daily Loss Fix + Stock Backtester (BUILT ✓)

### Daily loss circuit breaker — paper.py (BUILT ✓)
Bug: breaker used cash-only drawdown, invisible to unrealized position losses.
Fix: `_update_position_value(prices)` caches mark-to-market after each fill.
`_is_daily_loss_tripped()` now uses `cash + _open_position_value` vs session start.
Self-test added at bottom of paper.py: `python stock_bot/execution/paper.py` → 5/5 PASS.

### Stock backtester — stock_backtest.py (BUILT ✓)
New script at project root. Indicator-only (no AI) strategy over 5 years.
Baseline: FAIL — only 2 trades from RSI<35 + BULLISH + ADX≥20 combo over 5 years.
Key insight: indicator-only strategy is too selective; live bot depends on AI as primary signal.
Paper trading results cannot be compared to this baseline directly.
Saved: `stock_bot/logs/stock_backtest_20260623.csv`

---

## 2026-06-23 — Swing Walk-Forward + Week 2 Hardening (BUILT ✓)

### 1D swing walk-forward — swing_walkforward.py (BUILT ✓)
New file: `swing_walkforward.py` — OOS walk-forward for SL=4% TP=25% on 1d BTC/USDT.
3 periods: Train (2017–2022), Val_1 (2023–mid24), Val_2 (mid24–now).
All 3 PASS: PF 2.67 / 2.30 / 1.54 at 0.8% fee.
**VALIDATED** — safe to paper-trade alongside 4h bot. NOT in live .env yet.
Next step: 4-week paper-trade observation before real capital activation.

### Candle watchdog — bot/main.py (BUILT ✓)
`_last_candle_time` initialized before main loop; updated each time a new live candle
fires. On every tick: if `time.time() - _last_candle_time > candle_minutes * 60 * 2`,
fires `alerter.error()` then resets to avoid spam. Guards with `feed_mode == "live"`.

### Position drift reconciliation — bot/main.py (BUILT ✓)
Runs every 60 ticks when `cfg.exchange.live_trading`. Calls `executor._exchange.fetch_balance()`,
compares `free[base]` vs `executor.position`. If drift > 0.000010, logs WARNING + fires
Telegram error. Exception-safe (silent on API failure).

### Logrotate config — deploy/logrotate_trade_bot.conf (BUILT ✓)
Weekly rotation, 4 rotations, compressed, copytruncate. Replace project path then:
`sudo cp deploy/logrotate_trade_bot.conf /etc/logrotate.d/trade_bot`

### UptimeRobot guide — deploy/UPTIME_MONITOR.md (BUILT ✓)
Step-by-step: Heartbeat monitor, VPS cron ping every 5min, alert contacts.
Covers systemd restart-limit pitfall and manual recovery command.

---

## 2026-06-23 — Alerting wiring + 1D swing backtest + DCA module (BUILT ✓)

### Telegram alert wiring — bot/main.py (BUILT ✓)
Three missing alert paths wired in `bot/main.py`:
1. Partial TP: `trade_log.log_fill()` + `alerter.fill()` added after partial TP fills (was missing both)
2. Midnight daily P&L: UTC midnight check at bottom of main loop → `alerter.daily_pnl()`
3. Price feed error counter: `_consecutive_errors` → `alerter.error()` after 5 consecutive failures

### 1D Swing Backtest — swing_backtest.py (BUILT ✓)
New script: `swing_backtest.py` — sweeps 6 SL/TP combos on 1d BTC/USDT.
Fixed params: fee=0.8%, ADX=18, RSI filter ON, cooldown=3, starting_cash=$10k.
4 PASS configs found. **Best: SL=4%, TP=25%, PF=1.85, 59 trades, +5.19% return, -5.17% maxDD.**
CANDIDATE only — do not promote to live .env without walk-forward on 1d timeframe.
Saved: `logs/swing_backtest_1d_20260623.csv`

### DCA Bot — dca_bot.py (BUILT ✓)
New standalone script: `dca_bot.py`.
- State: `logs/dca_state.json` (total_invested, total_units, last_buy_date, buys[])
- Config: DCA_AMOUNT_CAD, DCA_INTERVAL_DAYS, DCA_SYMBOL, DCA_EXCHANGE from .env
- Filters: RSI overbought skip + daily EMA9/EMA21 trend skip
- DCA_DRY_RUN=true (default): records as-if fill, never real orders
- DCA_DRY_RUN=false: places ccxt market BUY with KRAKEN_API_KEY/SECRET
- `python dca_bot.py --report`: buy history table, no network calls
- Dry-run confirmed: today BEARISH (EMA9 < EMA21) → correctly skipped

---

**Why:** User requested that every change or feature plan be noted here so nothing is forgotten across sessions.

**How to apply:** Check this before suggesting new features — don't re-propose deferred items without context. Use this to pick up exactly where we left off.

---

## 2026-06-19 — TP Sweep + Walk-Forward Validation: TP=10% promoted (BUILT ✓)

**Root cause found:** backtest.py had no argparse — --stop_loss, --take_profit, --fee
were silently ignored. All previous TP/SL sweeps ran .env values every time.
Fix: added argparse with defaults from cfg.backtest.* so no-arg behavior unchanged.

**Real TP sweep results (SL=1.5%, fee=0.8%, 5000c):**

| TP   | Trades | PF   | Return  | Fee resilience |
|------|--------|------|---------|----------------|
| 4.5% | 46     | 1.45 | -5.19%  | Low — TP-dominated exits |
| 6%   | 43     | 1.38 | -5.13%  | Worst PF       |
| 8%   | 44     | 1.46 | -4.83%  | Improving      |
| 10%  | 58     | 1.79 | -4.70%  | Best PF        |
| 12%  | 54     | 1.68 | -5.04%  | Good but TP inert in recent regime |
| 15%  | 52     | 1.68 | -4.80%  | No improvement over 12% |

**Zero-fee comparison revealed:** TP=12% has PF 1.84 at zero fee (best gross).
TP=10% has PF 1.79. TP=4.5% has PF 1.35. Fee drag kills TP=4.5% hardest.

**Walk-forward TP=10% (fee=0.8%):**

| Window | PF   | Return  | Trades |
|--------|------|---------|--------|
| 5000   | 1.79 | -4.70%  | 58     |
| 4000   | 1.83 | -4.06%  | 51     |
| 3000   | 2.02 | -2.16%  | 32     |
| 2000   | 1.37 | -2.17%  | 18     |
| 1000   | 1.25 | -1.32%  | 10     |

All 5 windows PF > 1.0. Recent regime (2000/1000c) holds at 1.37/1.25 —
significantly better than old TP=4.5% which was 1.02/1.06 at same windows.

**Decision: TAKE_PROFIT_PCT=0.045 → 0.10. Promoted 2026-06-19.**

Key insight: TP=10% exit mix (37 SL / 9 TP / 12 strategy) means strategy SELL
signals exit profitably before TP is needed. TP=4.5% relied on TP hits (25/84)
and those hits are what fees kill first. TP=12% has zero TP hits in recent regime
(2000/1000c windows) — holding through drawdowns for no benefit.

Note: negative returns at all windows = 0.8% fee at $100 capital, not strategy
failure. PF is positive and consistent across all 5 windows at both TP=10% and 12%.

**Known issue:** Actual fee 0.80% confirmed (fee-dict log: cost=0.079 CAD on ~$9.94 trade).
CAD pair surcharge ~0.54% on top of 0.26% taker. Strategy PF 1.79 positive at 0.8%
fee but net return negative at $100 capital — fee burden exceeds gross edge at this scale.

**Next steps:**
1. Confirm TP=10% canonical run reproduces ~58 trades PF 1.79 ✓ then restart live bot
2. Accumulate 30-50 live trades at new TP=10% config
3. Previous TP/SL sweep results in memory are INVALID — all ran .env values due to
   argparse bug now fixed. Only TP=10% walk-forward is validated.
4. ADX < 20 filter hypothesis not yet tested — 66.7% win rate in that bucket from
   attribution table. Test when 30+ live trades are in.
5. When capital grows to $500+: revisit RISK_PER_TRADE_PCT (lower from 10% to 2-5%)

---

## 2026-06-19 — Signal Quality + Infrastructure Fixes (BUILT ✓)

11 fixes applied in one session. All verified. See stock_bot.md for full detail.

| Fix | Files | What |
|---|---|---|
| Sentiment K=4 smoothing | sentiment_scraper.py, prompt_builder.py | ±1.00 from single keyword impossible |
| Trends None vs 0 | google_trends.py, aggregator.py, prompt_builder.py | Rate-limit 0 no longer looks like zero interest |
| Intraday price | data/intraday_price.py (new), main.py | Paper fills at live tick, not yesterday's close |
| SL/TP watcher thread | main.py, config.py | 30s SL/TP checks replace 120s scan-loop check |
| Volume ratio | price_feed.py, prompt_builder.py | vol/20d_avg in Candle + AI prompt with penalty rule |
| News ticker fix | news_fetcher.py | ≤3-char tickers word-boundary matched |
| Daily loss breaker | paper.py, config.py, main.py | 3% daily drawdown halts new paper BUYs |
| Slippage model | paper.py, config.py, main.py | 15 bps fill slippage on all paper trades |
| Dynamic holidays | main.py | _us_holidays(year) + _ca_holidays(year) — works any year |
| Partial-holiday mode fix | main.py | US-only holidays no longer kill TSX news scan |
| Universe in off-hours scan | main.py | Pre/after-hours now scans watchlist + universe |

**3 strategy-level fixes deferred** (require paper baseline before touching signal logic):
- EMA crossover confirmation (2+ candle requirement before BULLISH signal)
- Universe ranking composite (1d + 5d momentum, not 5d only)
- AI-generated target/stop → ATR-based calculation in code

---

## 2026-06-16 — Stock Bot Source Separation + Dashboard Visual Sections (BUILT ✓)

Two changes that flow the "where did this symbol come from" tag end-to-end through the pipeline.

### Source Separation — main.py (BUILT ✓)

**Problem (bug fixed):** The main loop iterated `for symbol in watchlist` which was only defined inside the universe refresh block. On the first cycle (elapsed_h < 24h refresh) this variable was undefined → NameError. Fixed by using `all_symbols` everywhere.

**What was built:**
- `watchlist_symbols` and `universe_symbols` kept as separate lists throughout the loop
- Universe refresh block now updates `all_symbols` (not a local `watchlist`) and prints both lists separately
- `for symbol in all_symbols:` — deduplicated union, watchlist first
- `force_scan = symbol in cfg.watchlist` — watchlist always bypasses screener
- `ScanResult.source = "watchlist" if symbol in cfg.watchlist else "universe"` — set at creation
- Terminal header prints: `My Watchlist : ...` and `Top Movers : ...` on separate lines

**Files changed:** `stock_bot/main.py`

### Source Tag in Alerts — notifier.py (BUILT ✓)

Terminal alert box now shows source on the type line:
```
🟡 MEDIUM · STRONG_BUY · watchlist
🟡 MEDIUM · STRONG_BUY · top mover
```

`Alert.source` and `evaluator._make(source=r.source)` were already wired in a prior session. This session adds the terminal display.

**Files changed:** `stock_bot/alerts/notifier.py`

### Dashboard Visual Sections — renderer.py (BUILT ✓)

Replaced the old single flat card grid with two visually distinct sections:

**Section A — 📋 My Watchlist**
- Section header background: `#1c2333` (dark blue tint)
- Card `border-left: 3px solid #388bfd` (blue accent) via `.watchlist-card`
- Sub-text: "Always scanned every cycle"
- Only rendered if watchlist has results

**Section B — 🔥 Top Movers**
- Section header background: `#1c2820` (dark green tint)
- Card `border-left: 3px solid #2ea043` (green accent) via `.mover-card`
- Sub-text: "S&P 500 + TSX 60 · ranked by volume × momentum"
- Only rendered if universe results exist

**New CSS classes:** `.section-header`, `.section-header.watchlist`, `.section-header.movers`, `.section-title`, `.section-badge`, `.section-sub`, `.watchlist-card`, `.mover-card`, `.screened-card`

**`_stock_card_html()`** gains `extra_class: str = ""` param applied to root div.

**Both sections:** sort BUY→HOLD→SELL by confidence independently. If universe disabled → only watchlist section renders (no empty header). Mobile responsive — same `@media` grid.

**Files changed:** `stock_bot/dashboard/renderer.py`

---

## 2026-06-16 — Stock Bot Phases 4 · 5 · 6 (BUILT ✓)

All remaining stock bot phases built. Recap for completeness — detailed build notes to be added if needed.

### Phase 4 — HTML Dashboard (BUILT ✓)

`stock_bot/dashboard/renderer.py` — `DashboardRenderer`, `ScanResult` dataclass.

- Writes `stock_dashboard.html` to repo root after every scan cycle
- Dark GitHub-palette theme, pure inline CSS, no external deps
- Sections: Fear & Greed meter, BUY/HOLD/SELL summary grid, top-picks scroll, portfolio table, per-symbol cards, alerts panel
- `ScanResult.source: str = "watchlist"` field added (default, overridden in Phase 6 universe work)
- Auto-refresh via `<meta http-equiv="refresh">`

### Phase 5 — Alerts (BUILT ✓)

`stock_bot/alerts/alert.py` — `Alert` dataclass with `source: str` field.
`stock_bot/alerts/evaluator.py` — `AlertEvaluator`: runs 7 check types each cycle, passes `source=r.source`.
`stock_bot/alerts/notifier.py` — `AlertNotifier`: terminal colorama box, Gmail SMTP (HIGH only), plyer desktop (HIGH only).

### Phase 6 — Paper Trading + Portfolio + Universe Scanner (BUILT ✓)

`stock_bot/execution/paper.py` — `StockPaperExecutor`: virtual cash, paper buy/sell, realized PnL, `build_summary()`.
`stock_bot/portfolio/tracker.py` — `PortfolioTracker`: static holdings from `PORTFOLIO` env var, `PortfolioSummary`.
`stock_bot/data/universe.py` — `StockUniverse`: Wikipedia S&P500+TSX60 fetch, ranked by `volume×|price_change|`, TTL cache.
`stock_bot/data/screener.py` — `StockScreener`: momentum gate for universe symbols only.

**New config keys added (all in stock_bot/.env):**

| Key | Default | Purpose |
|---|---|---|
| `PORTFOLIO` | `""` | `SYM:SHARES:AVGCOST,...` static holdings |
| `BASE_CURRENCY` | `CAD` | Display currency for portfolio |
| `ALERT_EMAIL_ENABLED` | `false` | Send HIGH alerts via Gmail |
| `ALERT_EMAIL_FROM/TO/PASSWORD` | `""` | Gmail SMTP credentials |
| `ALERT_DESKTOP_ENABLED` | `false` | plyer desktop toasts (HIGH only) |
| `PAPER_TRADING_ENABLED` | `false` | Enable paper executor |
| `PAPER_STARTING_CASH` | `10000.0` | Virtual cash at startup |
| `PAPER_RISK_PCT` | `0.10` | Fraction of cash per paper trade |
| `PAPER_MIN_CONFIDENCE` | `65` | Min AI conf to trigger paper trade |
| `UNIVERSE_ENABLED` | `false` | Scan S&P500+TSX60 top movers |
| `UNIVERSE_SIZE` | `20` | Top N universe symbols per cycle |
| `UNIVERSE_REFRESH_HOURS` | `24` | Universe TTL in hours |
| `SCREENER_ENABLED` | `true` | Skip AI on low-momentum universe stocks |

---

## 2026-06-15 — Stock Bot Phases 1 · 2 · 3 (BUILT ✓)

New module `stock_bot/` added to repo — fully separate from `/bot` (crypto). Advisory-only stock research + AI analysis for NYSE, NASDAQ, TSX.

### Phase 1 — Price + Indicators (BUILT ✓)
- `stock_bot/data/price_feed.py` — yfinance OHLCV, `Candle` dataclass, TSX `.TO` suffix transparent
- `stock_bot/data/watchlist.py` — default 5-symbol list (SHOP.TO, RY.TO, AAPL, NVDA, AC.TO)
- `stock_bot/indicators/indicators.py` — RSI, EMA, SMA, ADX, trend copied from crypto bot + MACD added
- `stock_bot/config.py` — StockConfig from `stock_bot/.env` (isolated from root .env)
- `stock_bot/main.py` — scan loop, one-line indicator output per symbol

### Phase 2 — Web Research Engine (BUILT ✓)
- `research/news_fetcher.py` — feedparser RSS (Yahoo Finance + Google News), 5 headlines, deduped
- `research/reddit_scraper.py` — praw, 5 subreddits, keyword sentiment (no ML), graceful no-creds fallback
- `research/earnings.py` — yfinance next earnings date + EPS actual vs estimate + surprise note
- `research/fear_greed.py` — CNN API, 1-hour module-level cache, safe fallback score=50
- `research/aggregator.py` — ThreadPoolExecutor(3) concurrent fetch, ResearchReport dataclass, company name map
- `main.py` updated — research block printed per symbol under indicator line, Fear & Greed fetched once per cycle

### Phase 3 — AI Analysis Engine (BUILT ✓)
- `ai/verdict.py` — AIVerdict(signal, confidence, target_price, stop_loss, reasoning, trading_style, timestamp)
- `ai/prompt_builder.py` — structured prompt < 800 tokens, rsi_note + macd_note helpers
- `ai/ai_engine.py` — multi-provider via `requests.post`; three providers:
  - `openrouter`: `meta-llama/llama-3.3-70b-instruct:free` via openrouter.ai
  - `ollama_local`: any model at `OLLAMA_BASE_URL/v1/chat/completions` (no auth)
  - `ollama_cloud`: `OLLAMA_MODEL` at ollama.com with `OLLAMA_CLOUD_API_KEY`
- Verdict rules: confidence < 55 → HOLD, never BUY if RSI > 75, never SELL if RSI < 25
- Any failure → HOLD(confidence=0) — never crashes loop
- `main.py` updated — `_scan_symbol()` now returns data dict; `_print_verdict()` with colorama coloring
- `stock_bot/.env` — `AI_PROVIDER=ollama_cloud`, `OLLAMA_CLOUD_API_KEY` set, `OLLAMA_MODEL=llama3.2`
- `stock_bot/.env.example` — all three provider sections documented

**Key decisions:**
- Originally used DeepSeek R1 `:free` → removed from OpenRouter free tier → switched to Llama 3.3 → rate limited → user set up Ollama Cloud as active provider
- OpenAI SDK replaced with raw `requests.post` to support all three providers uniformly
- `OPENROUTER_API_KEY` stays in root `.env`; all other AI config in `stock_bot/.env`

**Next: Phase 4** — HTML dashboard (per-symbol cards, verdict history, sentiment timeline). See [[stock-bot]].

---

## 2026-06-15 — Config Validation + Volume Filter + SL-Based Sizing (BUILT ✓)

Full session of backtesting, parameter sweeps, and live config corrections. No new strategy logic — only parameter validation and two new utility additions.

---

### Volume Filter — Built, Tested, Disabled (VOLUME_K=0)

**What was built:** `volume_k` parameter added end-to-end:
- `IndicatorConfig.volume_k` — deque `_volumes`, checked in `evaluate()` after RSI gate
- `StrategyConfig.volume_k` + `_float("VOLUME_K", 1.2)` in `_load()`
- `engine.py` — new `volume_k` param in `run()`, passed to `IndicatorConfig`
- `backtest.py` — `volume_k = cfg.strategy.volume_k` in `engine.run()` call
- `bot/main.py` — `volume_k = cfg.strategy.volume_k` in `build_strategy()`
- `backtest.py` filter breakdown — `Volume rejected N (X%)` line added
- `.env` — `VOLUME_K=0`

**Logic:** Current candle volume must be ≥ `volume_k × avg(prior 3 candles volume)`. Disabled when `volume_k=0`. Applied to both BUY and SELL paths after RSI gate.

**Test result (BTC/USDT 4h 5000 candles):**

| VOLUME_K | Trades | PF | Return |
|---|---|---|---|
| 0 (off) | 86 | **1.38** | **+1.51%** |
| 1.2 (on) | 68 | 1.00 | -1.28% |

**Decision: VOLUME_K=0 permanently.** Filter rejected 737 candles (15.4%) but didn't improve trade quality — win rate flat at ~33%, PF dropped 0.38 points. Volume confirmation at 4h timeframe blocks trend continuation entries that were actual winners.

---

### SL-Based Position Sizing — calc_trade_qty_sl() (BUILT ✓)

**Added to AppConfig:**
```python
def calc_trade_qty_sl(self, cash, entry_price, stop_loss_price) -> float:
    # dollar_risk = cash * risk_per_trade_pct
    # qty = dollar_risk / sl_distance
    # Falls back to calc_trade_qty() if stop_loss_price=0 or sl_distance~0
```

**Wired in main.py BUY path:**
```python
_sl_price = price * (1 - cfg.backtest.stop_loss_pct) if cfg.backtest.stop_loss_pct > 0 else 0.0
trade_qty = cfg.calc_trade_qty_sl(executor.cash, price, _sl_price)
```

**Why:** Fixed-fractional sizing (old method) risks a fixed % of cash regardless of stop distance. SL-based sizing ensures that if the stop is hit, loss = exactly `risk_per_trade_pct × cash` — properly calibrated to the actual risk per trade.

---

### SL/TP Sweep — Best Config Validated (ACTIVE ✓)

Tested 4 SL/TP ratios on BTC/USDT 4h 5000 candles (RSI=true, VOLUME_K=0, ADX=18):

| Config | Ratio | Trades | PF | Max DD | Return |
|---|---|---|---|---|---|
| SL=2% TP=4% | 1:2 | 85 | 1.06 | -2.13% | -1.12% |
| **SL=1.5% TP=4.5%** | **1:3** | **86** | **1.38** | **-1.37%** | **+1.51%** |
| SL=2% TP=6% | 1:3 | 72 | 1.20 | -1.94% | +0.36% |
| SL=1% TP=3% | 1:3 | 110 | 0.88 | -3.46% | -3.13% |

**Winner: SL=1.5% / TP=4.5%.** Only config with positive return and max DD < 1.4%. SL=1% killed by BTC 4h candle noise. TP=6% rarely hit — trades reversed before reaching it.

**Updated .env:** `STOP_LOSS_PCT=0.015`, `TAKE_PROFIT_PCT=0.045`

---

### Walk-Forward (5 × 1000 windows, new SL/TP)

| Window | Date Range | Trades | PF | Return |
|---|---|---|---|---|
| 5000 (full) | Mar 2024–Jun 2026 | 86 | 1.38 | +1.51% |
| 4000 | Aug 2024–Jun 2026 | 69 | 1.41 | +1.39% |
| 3000 | Feb 2025–Jun 2026 | 46 | 1.30 | +0.41% |
| 2000 | Jul 2025–Jun 2026 | 26 | 1.02 | -0.50% |
| 1000 | Dec 2025–Jun 2026 | 16 | 1.06 | -0.24% |

**Finding:** Jul 2025–now is choppier (RSI/EMA trend-following underperforms in ranging markets). Not a parameter problem — ADX sweep (18/25/30/35) confirmed no threshold fixes it. Strategy earns in trending periods. Watch live trades against this baseline.

---

### ADX Sweep — ADX=18 Confirmed, No Change

Tested ADX=18/25/30/35 on both 5000 and 2000 candles. ADX=18 is best on every metric in both windows. Higher thresholds don't fix recent underperformance — they just reduce trade count without improving win rate. ADX=18 stays.

---

### RSI_FILTER_ENABLED — Restored to true (BUG FIX)

`RSI_FILTER_ENABLED=false` was set in `.env` around 2026-06-15 06:30 UTC to unblock a signal when RSI was stuck at 51.8. This was a mistake.

**Backtest comparison (BTC/USDT 4h 5000c, ADX=18, SL=1.5%, TP=4.5%):**

| RSI filter | Trades | PF | Max DD | Return |
|---|---|---|---|---|
| false (was live) | 107 | 1.19 | -2.16% | -0.10% |
| true (restored) | 86 | **1.38** | **-1.37%** | **+1.51%** |

Disabling RSI filter adds 21 extra trades that are net-negative (more SL hits, lower PF, higher drawdown). Restored to `RSI_FILTER_ENABLED=true` in `.env` 2026-06-15 ~17:25 UTC.

---

### Live Bot — Restart Confirmed Clean (2026-06-15 17:25 UTC)

All recovery checks passed:
1. `State restored: cash=89.79 pos=0.000108 cost_basis=92050.90` ✓
2. `PositionManager seeded: qty=0.000108 avg_entry=92050.90` ✓
3. `State machine recovered to LONG | entry=92050.90` ✓
4. Warmup replayed, waiting for next 1h candle ✓

Current position: BUY 0.000108 BTC/CAD @ $92,050.90 | SL ~$90,671 | TP ~$96,143

---

## Live Trading Watch Items

Three open items requiring manual follow-up or monitoring. Do not close without confirmation.

- **Verify Jun 11 fill close price on Kraken** — Check History → Trades for a SELL around Jun 14 10:44 UTC. This is needed for accurate realized PnL on trade #1. The bot log shows no SELL was placed by the bot; the Jun 14 test burst sent a real oversized SELL 0.001000 (vs actual position 0.000113) — the position may have been swept as a partial fill. Note: balance evidence in log (Jun 13 23:59 sync shows exchange=99.81 vs saved=89.88) means the position was already gone by Jun 13 23:59 — may have been manually closed between Jun 12 08:20 and Jun 13 23:59 while the bot was stopped.

- **Isolate dev/test runs from production API** — Jun 14 10:44–10:45 test burst sent multiple real live orders to Kraken (several BUY 0.001 and one SELL 0.001 with `dry_run=False`). This should never happen. Fix: test code must use a separate ccxt instance pointed at Kraken's sandbox, or always set `dry_run=True` in test harness. Review `test_live_executor.py` and any ad-hoc test scripts to confirm they cannot reach the production API key.

- **Current open position — monitor until closed** — BUY 0.000108 BTC/CAD @ $92,050.90 (filled Jun 15 07:00 UTC). SL triggers at ~$90,671 (1.5% below entry). TP triggers at ~$96,143 (4.5% above entry). Last candle close: $93,078.80 (+$0.11 unrealized). Bot is LONG, RSI_FILTER_ENABLED now `true` (changed Jun 15) — next SELL signal or SL/TP hit will close it.

---

## 2026-06-10 to 2026-06-12 — First Live Fill + Critical Bug Fixes + Multi-Symbol Validation

The bot executed its first live fill (BUY 0.000113 BTC/CAD @ $88,870.20), which immediately revealed three bugs: (1) intra-candle SL/TP never fired between 4h closes — same `continue`-path issue as the earlier SL/TP gap; (2) the dashboard only rendered at candle closes for the same reason — fixed by extracting a `_render_dashboard()` helper called on every 30s tick using sticky indicator values; (3) the first BUY was initially blocked by position-size rounding drift (`round(qty,6) × price > max_position_pct` even at equal limits), fixed by raising `RISK_MAX_POSITION_PCT=0.15` and adding a startup warning. Fee discovery: actual Kraken charge was 0.80% (not 0.26% modeled) — raw fee-dict logging added; cause under investigation (likely BTC/CAD FX surcharge). Multi-symbol validation ran frozen params against ETH/SOL/BNB/LINK at two windows and two fee rates: strategy generalizes (ETH chosen as first expansion symbol for cross-regime robustness, LINK permanently excluded), but fee rate remains the gating constraint — everything is net-negative at 0.80% even with good signal quality. Details in [[multi-symbol-validation]] and [[live-loop-bugs]].

---

## 2026-06-11 — Intra-Candle SL/TP + Fee Logging (BUILT ✓)

**Gap fixed:** Live loop's `continue` (on "no new candle") fired before step 3b, so SL/TP only ran at 4h closes. A new intra-candle block now runs on every 30s tick, before the candle-availability check. Full execution pipeline: `risk.evaluate → executor.execute → position_manager.on_sell → display.fill`. Approval tested via `_ic_approval.approved` (explicit, consistent with `ApprovalResult` field name even though `__bool__` also exists).

**Live vs backtest SL/TP behaviour difference — note for live-vs-backtest comparison:**
The backtest engine checks SL/TP against the candle CLOSE price only (once per 4h). The live loop checks against the Kraken spot ticker every 30 seconds. Consequence: live will exit slightly earlier and more sensitively than the backtest — if price dips below the stop intra-candle and recovers by close, live triggers the stop and backtest does not. This is intentional and safer (real capital is at risk). When comparing live win rate and PF against backtest numbers, expect live to show slightly more stop-loss exits and marginally lower win rate on the same signals. Not a bug; document when reconciling.

**Fee logging added:** `logger.warning("Fee dict from exchange: %s", fee_data)` added in `live_executor.py` at fee extraction point. Next fill will log the raw ccxt fee structure from Kraken for inspection.

**Fee finding:** Actual Kraken fee on first live fill was 0.80% (not 0.26% modelled). Most likely Kraken's BTC/CAD surcharge (CAD settlement adds ~0.54% on top of 0.26% taker). Backtest at 0.80% fee: PF 1.21 (signals intact) but net −4.99% — fees 3× gross profit. Strategy is not viable at 0.80%; maker orders (0.40%) or Binance (0.10%) required.

**Files changed:** `bot/main.py` (intra-candle SL/TP block), `bot/execution/live_executor.py` (fee dict logging)

---

## 2026-06-11 — Position-Size Drift Incident + Startup Guard (BUILT ✓)

**Incident:** First live BUY was blocked. Risk manager reported `0.000113 BTC = 10.03% of portfolio` against `RISK_MAX_POSITION_PCT=10%`. Bot had been running but generating zero fills since launch.

**Root cause (single, confirmed):** `calc_trade_qty` calls `round(qty, 6)`. At price ≈ $88,761:
- exact qty = 10.00 / 88761 = 0.00011265974…
- rounded qty = 0.000113 (7th decimal is 6, rounds up)
- risk check: 0.000113 × 88761 = $10.030 → 10.030% > 10.000% → BLOCKED

The `round(..., 6)` can add up to `0.0000005 × price` to the effective position value — at BTC ~$90k that is $0.045, or 0.045% of a $100 portfolio. A `max_position_pct` equal to `risk_per_trade_pct` is always within this error band and will block on every order.

No price-staleness drift (sizing and check use the same `price` variable from the same tick). No `current_value` drift (first BUY from zero position, so `current_value = cash`).

**Operational fix (already applied):** `RISK_MAX_POSITION_PCT=0.15` in `.env`. With 15% max and 10% per-trade, the rounded position value ($10.03) is well inside the $15 limit.

**Code fix:** Added startup warning in `config.py → log_startup()`: if `max_position_pct <= risk_per_trade_pct * 1.05`, logs a `WARNING` explaining the rounding mechanism and the minimum safe value. Fires on old config (10%/10%), silent on current config (10%/15%).

**Files changed:** `config.py` (`log_startup`)

**Safe threshold:** `RISK_MAX_POSITION_PCT >= RISK_PER_TRADE_PCT * 1.05 + margin`. At $100 capital and BTC ~$90k, 15% gives $5 of headroom above a $10 trade — more than sufficient.

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
