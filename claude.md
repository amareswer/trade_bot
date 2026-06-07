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

## VALIDATED TRADING CONFIG

As of 2026-06-05, the following configuration has passed:
- 5000-candle backtest (PF 1.21, 88 trades, win rate 36%)
- Walk-forward validation (training PF 1.21 → validation PF 1.22)
- Fee/slippage stress test (PF holds to 0.15% fee + 0.05% slip)
- Regime analysis (positive PF in bull, correction, and recovery)
- Monte Carlo (0% ruin, 0.64% 95th pct drawdown at 2% risk/trade)

### Active .env settings (do not change without re-running validation)
ADX_THRESHOLD=15
MAX_EMA_SPREAD_PCT=0.005
RSI_FILTER_ENABLED=true
RISK_PER_TRADE_PCT=0.02
STOP_LOSS_PCT=0.02
TAKE_PROFIT_PCT=0.04
BACKTEST_LIMIT=5000
BACKTEST_TIMEFRAME=4h
EXCHANGE=binance
SYMBOL=BTC/USDT

### How to verify the config is active
Run: python backtest.py
Check filter breakdown: ADX rejected should be ~513 (10.3%), NOT 2365 (47.6%)
Check trade count: should be ~88, NOT 69
If you see 69 trades, the .env is on the old config.

### Next steps remaining
1. Multi-symbol test: run backtest.py on ETH, SOL, BNB, LINK with same params
2. Start paper trading: python -m bot.main (target 30-50 live trades)
3. After paper trading: compare live vs backtest win rate and PF
- Can evolve into a professional-grade trading platform
## Exchange Setup
- Backtesting: EXCHANGE=binance, SYMBOL=BTC/USDT
- Live trading: EXCHANGE=kraken, SYMBOL=BTC/USD
- Reason: Kraken OHLCV history limited to ~720 candles, Binance has 5000+
- Price diff confirmed: 0.048% — negligible
- Kraken API key: generate at Security → API once KYC clears
  - Enable: Query Funds, Query Orders, Create Orders, Cancel Orders
  - Disable: Withdrawals (never enable on bot key)
  - Restrict to your IP address
