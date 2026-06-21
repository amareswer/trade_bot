---
name: progress-current
description: "Current stage and what is in progress"
metadata:
  type: project
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

## Active .env — Crypto Bot (bot/.env)

| Setting | Value |
|---|---|
| EXCHANGE | kraken |
| SYMBOL | BTC/CAD |
| CANDLE_MINUTES | 60 |
| ADX_THRESHOLD | 18 |
| RSI_FILTER_ENABLED | true |
| VOLUME_K | 0 |
| STOP_LOSS_PCT | 0.015 |
| TAKE_PROFIT_PCT | 0.045 |
| RISK_PER_TRADE_PCT | 0.10 |
| LIVE_TRADING | true |
| DRY_RUN | false |
