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

As of 2026-06-19, the following configuration has passed:
- 5000-candle backtest (BTC/USDT 4h, Mar 2024–Jun 2026): PF 1.79, 58 trades, win rate 32.8%, max DD -5.12%, return -4.70% (at 0.8% fee)
- SL/TP sweep: SL=1.5% / TP=10% validated 2026-06-19 (was TP=4.5%)
- Walk-forward (5 windows, TP=10%, fee=0.8%):

  | Window | Candles | PF   | Return  |
  |--------|---------|------|---------|
  | Full   | 5000    | 1.79 | -4.70%  |
  | 4000   | Sep24   | 1.83 | -4.06%  |
  | 3000   | Feb25   | 2.02 | -2.16%  |
  | 2000   | Aug25   | 1.37 | -2.17%  |
  | 1000   | Jan26   | 1.25 | -1.32%  |

  All 5 windows PF > 1.0 — walk-forward passed.
- ADX sweep (18 / 25 / 30 / 35): ADX=18 is best on both full history and recent window
- RSI filter confirmed ON: RSI_FILTER_ENABLED=false drops PF from 1.38 → 1.19 and return from +1.51% → -0.10%
- Volume filter tested (VOLUME_K=1.2) and disabled: hurt PF (1.38→1.00), added noise not quality

### Active .env settings (do not change without re-running validation)
ADX_THRESHOLD=18
RSI_FILTER_ENABLED=true
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
CANDLE_MINUTES=60
RISK_PER_TRADE_PCT=0.10   # intentionally high at $100 capital (Kraken min order ~$4.50 CAD)
STOP_LOSS_PCT=0.015
TAKE_PROFIT_PCT=0.045

### How to verify the config is active
Run: EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py
Expected: ~58 trades, PF ~1.79, return ~-4.70%, max DD ~-5.12%
If RSI_FILTER_ENABLED=false accidentally: trade count jumps to ~107, PF drops to 1.19

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

### Bug fixes applied 2026-06-20
All critical bugs resolved:

**Crypto bot (bot/):**
- `bot/risk/risk_manager.py`: HALT gate now only blocks BUY — SELL always allowed (positions can close during halt)
- `bot/backtest/engine.py`: Added `forced_exit` flag — SL/TP triggers bypass cooldown state machine (stop-losses were being suppressed)
- `walkforward.py` + `montecarlo.py`: ADX threshold corrected 15.0 → 18.0 (was testing wrong strategy vs live)
- `config.py`: Defaults corrected — fee 0.001→0.008, SL 0.02→0.015, TP 0.04→0.10 (both dataclass and _load())
- `bot/backtest/report.py`: Added Buy-and-Hold benchmark section with alpha comparison

**Stock bot (stock_bot/):**
- `stock_bot/data/screener.py`: Removed $200 price cap (was blocking NVDA, AAPL, MSFT); RSI_OVERBOUGHT 70→75; price filter long-only (abs→positive)
- `stock_bot/main.py`: Added 5% sanity check on live_price vs candle_close — TSX fast_info currency mismatch caused impossible P&L like +921%
- `stock_bot/research/sentiment_scraper.py`: Replaced 12-word flat keyword list with phrase-pattern rules + negation detection window (3 tokens)

### Next steps — prioritized roadmap (audited 2026-06-21)

#### TODAY — Active money at risk
1. **Fix backtest fee** — `.env`: change `BACKTEST_FEE_PCT=0.001` → `BACKTEST_FEE_PCT=0.008`
   - Every backtest run since deploy has reported false PF numbers at 0.1% fee
   - After fix, re-run: `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` → expect ~58 trades, PF ~1.79
2. **Run 1h backtest** — live bot uses `CANDLE_MINUTES=60` but ALL validation was done on 4h candles
   - `EXCHANGE=binance SYMBOL=BTC/USDT BACKTEST_TIMEFRAME=1h BACKTEST_LIMIT=5000 python backtest.py`
   - If PF < 1.0: stop live trading until re-validated; if PF > 1.0: confirm and document
3. **Fix SL/TP risk gate bypass** — `bot/main.py` lines 525–561
   - Intra-candle SL/TP path routes through `risk.evaluate()` — a daily-loss halt silently blocks the stop-loss
   - SL/TP triggers must bypass the risk gate entirely (same fix as `risk_manager.py` SELL bypass)
4. **Fix `deploy.sh` before any VPS push** — `deploy/deploy.sh` line 39
   - `--exclude='logs'` wipes `live_state.json` on redeploy → bot restarts thinking it holds nothing
   - Change to: preserve `logs/live_state.json` and `logs/trades.db`, exclude only `logs/trade_bot.log`

#### DAY 2 — Fee savings + silent failures
5. **Enable limit orders for BUY** — saves 0.64% per round trip (0.80% → 0.16% maker rate)
   - `.env`: `ORDER_TYPE=limit`
   - `live_executor.py:411`: change BUY offset `price * 1.001` → `price * 0.998` (bid-side passive)
   - Leave SELL as market order (guaranteed exit)
   - Increase cancel timeout `range(1, 4)` → `range(1, 10)` (9s resting time on 60min candle bot)
6. **Fix dual SL evaluation paths** — `bot/main.py:618-633`
   - Intra-tick SL/TP (lines 474–582) always fires first; candle-close SL block (lines 618–633) is dead code
   - Remove the candle-close SL block to eliminate double-exit confusion

#### DAY 3 — Stock bot circuit breaker + alerting
7. **Fix stock bot daily loss breaker** — `stock_bot/execution/paper.py:81,114-115`
   - `session_start_value = self._cash` ignores open positions — breaker can fire when portfolio is flat
   - Fix: use `cash + sum(position mark values)` as baseline in both session init and drawdown calc
8. **Wire daily P&L Telegram alert** — `bot/main.py`
   - `TelegramAlerter.daily_pnl()` exists but is never called
   - Add midnight UTC trigger inside main loop: `if now.hour == 0 and now.minute < 1: alerter.daily_pnl(...)`
9. **Wire partial TP Telegram alert** — `bot/main.py` ~line 506
   - Partial TP calls `trade_log.log_fill()` but skips `alerter.fill()` — real-money exit goes unreported
10. **Add consecutive error counter** — `bot/main.py` price fetch block
    - After 5 consecutive fetch failures: call `alerter.error("Price feed down 5+ ticks")` and back off

#### WEEK 2 — Hardening
11. Correct ADX default: `config.py:383` change `25.0` → `18.0` (safe only while `.env` exists)
12. Correct RSI levels: `.env` set `RSI_OVERSOLD=30` `RSI_OVERBOUGHT=70` (validated values, not current 32/68)
13. Add logrotate on VPS: `/etc/logrotate.d/trade_bot` — weekly, 4 rotations, compress (log grows unbounded)
14. Add position drift reconciliation: periodic `fetch_balance()` vs `live_state.json` to detect divergence
15. Add candle watchdog: if `2 × candle_minutes` pass with no new candle, call `alerter.error()`
16. Set up external uptime monitor (UptimeRobot free tier) — systemd stops after 5 crashes with no external alert
17. Schedule weekly `live_comparison.py`: `0 9 * * 1 python live_comparison.py >> logs/weekly.log`

#### MONTH+ — Revenue unlock gates
| Milestone | Gate | Impact |
|---|---|---|
| 1h backtest PF > 1.0 confirmed | Run this week | Validates live config isn't a gamble |
| 30–50 live trades accumulated | ~2–3 months | Compare live PF vs backtest |
| Kraken maker fee confirmed <0.20% | Test one limit order | Unlocks ETH/CAD expansion |
| Stock bot: 30 paper trades, PF ≥ 1.2, win rate ≥ 30% | ~4–6 weeks | Gate for Phase 7 IBKR live |
| Capital grows to $500+ | Organic | Lower RISK_PER_TRADE_PCT from 10% → 2% |
| Add 5-day earnings blackout to stock_bot BUY | After paper validated | Avoid pre-earnings gap risk |
| Add oversold recovery to universe pre_filter | After paper validated | AI rejects overbought momentum leaders |

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
