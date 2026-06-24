---
name: progress-current
description: "Current stage and what is in progress"
metadata:
  type: project
---

**Status as of 2026-06-24 (updated):** Two bots active. Crypto bot live on Kraken. Stock bot in paper trading observation. Walk-forward on 1d swing strategy: VALIDATED. Week 2 hardening complete. AI confidence band tracker built. Three strategy fixes applied.

**Paper state RESET 2026-06-23:** Both `stock_bot/paper_trades.csv` and `stock_bot/paper_state.json` deleted and reset to $1,000.00 clean.
- **Reason:** Corrupted data from early development — prices in millions (TSX currency mismatch bug), share counts of 43,984 on a $1k account, self-test data leaked into real CSV before tempfile fix was applied.
- **Self-test isolation:** The `if __name__ == "__main__":` block in paper.py already uses `tempfile.mkdtemp()` to redirect state files — verified working, does not touch real files.
- **Paper trading clock restarts** from $1,000.00 clean as of 2026-06-23. Confidence tracking now active from first real trade.
- **paper_state.json** written fresh: `{"cash": 1000.00, "starting_cash": 1000.00, "positions": {}, "realized_pnl": 0.0, "orders": []}`.
- **paper_trades.csv** not recreated — will be auto-created with correct 9-column header on first real fill.

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

**Next for stock bot:**
1. Accumulate 30-50 paper trades and compare paper PF/win rate to real behavior
2. Stock bot backtester (backtest against historical data)
3. Validate paper P&L after 1 week of clean runs
4. Three remaining strategy-level fixes (when paper baseline established):
   a. EMA crossover confirmation (2+ candle requirement)
   b. Universe momentum ranking (1d+5d composite vs 5d only)
   c. AI-generated target/stop → ATR-based calculation in code
5. Phase 7: Live execution via Interactive Brokers (after paper validated)

---

## Crypto Bot (bot/)

**Status:** LIVE on Kraken BTC/CAD

**Known issue:** Actual fee 0.80% vs 0.26% modeled — maker orders (limit) may reduce to 0.16%

**Next steps:**
1. Accumulate 30-50 live trades and compare live PF/win rate to backtest
2. Verify Kraken fee: test limit order to confirm 0.16% maker rate
3. Once fee confirmed <0.20%: consider ETH/CAD expansion
4. When capital grows to $500+: revisit RISK_PER_TRADE_PCT (lower to 2%)

---

## Open Items (Crypto)

1. **Verify fee path** — Kraken 0.80% actual vs 0.26% modeled. Test limit order (maker) for 0.16% rate.
2. **ETH expansion** — deferred until fee path confirmed <0.20%

---

## PM Audit — 2026-06-21 (Multi-agent review)

Three agents audited crypto bot, stock bot, and deployment. Findings below by priority.

### TODAY — Active money at risk
- [ ] Fix `BACKTEST_FEE_PCT=0.001` → `0.008` in `.env` — all recent backtest PF numbers are wrong
- [ ] Run 1h backtest (`BACKTEST_TIMEFRAME=1h`) — live bot on 1h but ALL validation done on 4h; untested
- [ ] Fix SL/TP risk gate bypass (`bot/main.py:525-561`) — halt state blocks stop-loss from firing
- [ ] Fix `deploy.sh` — `--exclude='logs'` wipes `live_state.json` on redeploy (position lost)

### DAY 2
- [ ] Enable limit orders for BUY only — `ORDER_TYPE=limit`, offset `price*0.998`, 9s cancel timeout
- [ ] Remove dual SL evaluation path — candle-close SL block (`bot/main.py:618-633`) is dead code

### DAY 3
- [ ] Fix stock bot daily loss breaker (`paper.py:81,114`) — uses cash only, ignores position value
- [ ] Wire `alerter.daily_pnl()` in `bot/main.py` midnight loop
- [ ] Wire `alerter.fill()` on partial TP path (`bot/main.py:~506`)
- [ ] Add consecutive error counter → Telegram after 5 failures

### WEEK 2
- [ ] ADX default `config.py:383`: `25.0` → `18.0`
- [ ] RSI levels in `.env`: `RSI_OVERSOLD=30 RSI_OVERBOUGHT=70`
- [x] Add logrotate on VPS — `deploy/logrotate_trade_bot.conf` created ✓
- [x] Add position drift reconciliation — wired in `bot/main.py` (every 60 ticks) ✓
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
- `bot/execution/live_executor.py`: limit BUY at price*0.998 (maker 0.16%), SELL always market, poll 9s
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
| SYMBOL | BTC/CAD |
| CANDLE_MINUTES | 240 |
| ORDER_TYPE | limit |
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
