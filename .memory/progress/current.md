---
name: progress-current
description: "Current stage and what is in progress"
metadata:
  type: project
---

**Status as of 2026-06-23:** Two bots active. Crypto bot live on Kraken. Stock bot in paper trading observation. Three new items built this session.

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
- [ ] Add logrotate on VPS (`/etc/logrotate.d/trade_bot`)
- [ ] Add position drift reconciliation (`fetch_balance()` vs `live_state.json`)
- [ ] Add candle watchdog alert (2× candle_minutes silence → Telegram error)
- [ ] External uptime monitor (UptimeRobot free)
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
