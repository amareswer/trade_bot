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

**Last session (2026-06-19):** Stability fixes
- Reverted session management from price_feed.py (broke yfinance)
- Reverted ticker.info company name lookup (2-3s penalty per symbol)
- Added price validation in paper.py buy(): type check, 0 < price < 500k, shares < 100k
- Added state corruption guard in _load_state(): rejects cash > $1M or |realized_pnl| > $1M
- Added int(shares) storage in portfolio tracker
- Screener price filter: $5–$200 (universe symbols)
- Max 4 positions enforced in main.py
- Stop loss -5% / take profit +12% using fresh fetch_candles per position
- WATCHLIST changed to HOOD,MRNA,NCLH,AC.TO,CCL,INTC (affordable at $1k)

**Active config:**
- `PAPER_STARTING_CASH=1000.00` | `PAPER_RISK_PCT=0.25` | `PAPER_MIN_CONFIDENCE=70`
- `UNIVERSE_SIZE=10` | `WATCHLIST=HOOD,MRNA,NCLH,AC.TO,CCL,INTC`
- `AI_PROVIDER=nvidia_nim` | `NVIDIA_MODEL=openai/gpt-oss-120b`

**Real portfolio (display only, no paper trading):**
- BMO.TO: 5 shares @ $66.10 | CM.TO: 4 @ $41.15 | SPCX: 2 @ $160.00
- EBON: 3 @ $1.95 | IGC: 50 @ $0.2799

**Next for stock bot:**
1. Accumulate 30-50 paper trades and compare paper PF/win rate to real behavior
2. Stock bot backtester (Part 2 of this session)
3. Validate paper P&L after 1 week of clean runs

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
