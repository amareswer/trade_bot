# Crypto Trading Bot — User Guide

A modular trading bot that uses real crypto market data via ccxt. Runs locally.
Defaults to paper trading — real orders happen only with `LIVE_TRADING=true`
(the current live deployment trades BTC/CAD on Kraken with a hard $77 slot cap).

> **Run commands, secrets inventory, and new-machine migration → [`SETUP.md`](SETUP.md).**
> That guide covers BOTH bots (this crypto bot and the stock bot in `stock_bot/`),
> the exact launch commands, every required secret and where to get it, which
> state files must be copied when moving computers, and TWS setup for the stock
> bot. Strategy/validation rules live in `CLAUDE.md`.

---

## Table of Contents

1. [Safety First — Read This Before Anything Else](#1-safety-first--read-this-before-anything-else)
2. [The Journey to Live Trading — Step by Step](#2-the-journey-to-live-trading--step-by-step)
3. [How to Use the Backtest Report](#3-how-to-use-the-backtest-report)
4. [Setup](#4-setup)
5. [Running the Bot](#5-running-the-bot)
6. [Configuration](#6-configuration)
7. [Understanding the Terminal Output](#7-understanding-the-terminal-output)
8. [Dashboard](#8-dashboard)
9. [Logs](#9-logs)
10. [Running Tests](#10-running-tests)
11. [Switching Modes](#11-switching-modes)
12. [Project Structure](#12-project-structure)

---

## 1. Safety First — Read This Before Anything Else

### Three modes — understand which one you are in

The bot has three operating modes, controlled entirely by two `.env` flags:

| `LIVE_TRADING` | `DRY_RUN` | Mode | What happens |
|---|---|---|---|
| `false` (default) | — | **Paper trading** | `PaperExecutor` — all trades simulated in memory, zero exchange calls, zero real money possible |
| `true` | `true` | **Dry run** | `LiveExecutor` connects to the exchange but logs what it *would* place — no orders are submitted |
| `true` | `false` | **Live trading** | `LiveExecutor` submits real market orders on Kraken — real money moves |

**Both flags default to `false`.** A fresh clone cannot place real orders until you explicitly set `LIVE_TRADING=true`.

> **Before enabling live orders:** run `DRY_RUN=true` first and watch several candle cycles. Verify the printed `[DRY RUN] BUY / SELL` lines match your expectations. Only set `DRY_RUN=false` once you are satisfied.

### The risk engine protects you in every mode

| Protection | Code default | What it does |
|---|---|---|
| `RISK_PER_TRADE_PCT = 1%` | `0.01` | Only 1% of your balance per trade — position size scales with balance automatically |
| `RISK_MAX_POSITION_PCT = 5%` | `0.05` | Never puts more than 5% of your balance in one open position |
| `RISK_DAILY_LOSS_LIMIT = 2%` | `0.02` | Stops new entries the moment you are down 2% in a day — open positions can still be closed |
| `RISK_MAX_DRAWDOWN = 10%` | `0.10` | Blocks new entries if portfolio drops 10% from its all-time peak — open positions can still be closed |
| `RISK_MAX_TRADES_PER_DAY = 5` | `5` | Hard cap — cannot overtrade and rack up fees |

> These are code defaults for a fresh clone with no `.env`. Your `.env` overrides them — check `.env` for what is actually running.

> **SELL always works.** Daily loss and drawdown limits only block new BUY entries — you can always exit an open position.

### The honest reality about trading

- No strategy wins 100% of the time
- Crypto is highly volatile — prices can drop 20% in hours
- Even professional funds lose money regularly
- Past backtest performance does not guarantee future results
- **Never trade money you cannot afford to lose**

---

## 2. The Journey to Live Trading — Step by Step

Follow these steps in order. **Never skip ahead.** Each step exists to protect you.

```
STEP 1 — Backtest  ✓  (you are here)
─────────────────────────────────────────────────────
  Run strategy on historical data.
  Zero risk. No network trading. Just numbers on a screen.
  Goal: find settings where the strategy is consistently profitable.

  Command: .venv/bin/python backtest.py
  When to move on: profit factor > 1.5, Sharpe ratio > 1.0,
                   tested on at least 3 different time periods.


STEP 2 — Paper Trading with Live Prices
─────────────────────────────────────────────────────
  Bot fetches real prices from the exchange every 30 seconds.
  Still zero risk — all trades are simulated in memory.
  Goal: watch the bot behave on real, live market conditions.
        Does it trade sensibly? Does it hold during bad markets?

  Command: .venv/bin/python -m bot.main
  When to move on: run for at least 2–4 weeks,
                   results look consistent with backtest.


STEP 3 — Live Trading with a Tiny Amount
─────────────────────────────────────────────────────
  Real money, but start with $50–$100 only.
  Prove the system works end-to-end before scaling.
  Watch every trade. Understand every decision.

  Prerequisites before this step:
    ✓ LiveExecutor built — bot/execution/live_executor.py
    ✓ Exchange API keys configured with trading permissions
    ✓ Steps 1 and 2 completed successfully
    ✓ You understand why the bot is buying and selling
    ✓ DRY_RUN=true validated over multiple candle cycles

  Not yet handled by LiveExecutor (hardening work remaining):
    ✗ Balance sync — bot tracks cash internally; does not query
      real exchange balance on startup (restart loses position state)
    ✗ Min order size — Kraken has per-pair minimums (e.g. 0.0001 BTC);
      not validated before order is submitted
    ✗ Fee deduction — exchange fees are not subtracted from the
      bot's tracked cash balance
    ✗ Restart recovery — open positions on the exchange are not
      detected on restart; bot starts blind


STEP 4 — Scale Up (Optional)
─────────────────────────────────────────────────────
  Only after Step 3 proves consistent over weeks/months.
  Increase position size gradually — never all at once.
```

> **Rule of thumb:** If you do not understand why the bot made a trade, do not trust it with real money yet.

---

## 3. How to Use the Backtest Report

The backtest report is **for you, not the bot**. The bot never reads it. It is your research tool to validate a strategy before trusting it with live signals.

### Running the backtest

```bash
.venv/bin/python backtest.py
```

All settings come from `.env`. To change symbol, timeframe, or strategy — edit `.env` and re-run. Results are saved to `logs/backtest_BTC_USDT_1h_DATE.csv`.

### Reading the numbers

**PERFORMANCE section**

| Field | What to look for |
|---|---|
| Total return | Should beat simply holding BTC over the same period |
| Total fees | If fees eat most of your profit, reduce trade frequency |

**TRADES section**

| Metric | Good range | What it means |
|---|---|---|
| Total trades | At least 10–20 | Fewer than 10 = sample too small, don't trust the stats |
| Win rate | > 50% | But win rate alone is not enough — check avg win vs avg loss |
| Profit factor | > 1.5 | Gross profit ÷ gross loss. Above 1.0 means profitable overall |
| Avg win vs Avg loss | Avg win should be larger | A 40% win rate can still be profitable if wins are 3× bigger than losses |
| Best / Worst trade | Check worst trade | If worst trade is huge, the risk engine may need tightening |

**RISK section**

| Metric | Good range | What it means |
|---|---|---|
| Max drawdown | Less than -10% | Worst peak-to-trough drop during the entire period |
| Sharpe ratio | > 1.0 = good, > 2.0 = excellent | Return per unit of risk. Below 0 means losing on a risk-adjusted basis |

### What to do with the results

| Result | Action |
|---|---|
| Total trades < 10 | Increase `BACKTEST_LIMIT` or shorten `BACKTEST_TIMEFRAME` in `.env` |
| Win rate low but profit factor > 1.5 | Strategy is fine — wins are bigger than losses |
| Sharpe ratio < 0 | Strategy is losing money. Tune RSI thresholds or change timeframe |
| Max drawdown > -10% | Too much risk. Lower `RISK_PER_TRADE_PCT` in `.env` |
| Total return < buy-and-hold | Strategy adds no value. Needs tuning |

### Tuning workflow

```
1. Run:  .venv/bin/python backtest.py
2. Note the results
3. Change ONE setting at a time in .env:
     RSI_OVERBOUGHT / RSI_OVERSOLD
     FAST_EMA_PERIOD / SLOW_EMA_PERIOD
     BACKTEST_TIMEFRAME (1h → 4h → 1d)
     COOLDOWN_TICKS
4. Run backtest again — compare results
5. Keep the change only if results improve
6. Once satisfied → those same .env settings apply to the live bot automatically
```

> **Never change multiple settings at once.** You won't know which change helped or hurt.

---

## 4. Setup

**Requirements:** Python 3.10+ — this repo runs everything through `.venv` (Python 3.11).
On this machine the system `python3` is 3.9 and **will not work** (`str | None` annotations
and yfinance both need 3.10+). Always use `.venv/bin/python`, never bare `python`.

```bash
# Create the virtualenv (once) and install dependencies
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Create your .env file
cp .env.example .env
```

Edit `.env` and add your OpenRouter API key (needed only for AI advisory):

```
OPENROUTER_API_KEY=sk-or-your-key-here
```

Get a free key at https://openrouter.ai — the default model (`deepseek-v4-flash:free`) costs nothing.

To disable AI advisory, set in `.env`:
```
AI_ENABLED=false
```

---

## 5. Running the Bot

```bash
.venv/bin/python -m bot.main      # live paper trading
.venv/bin/python backtest.py      # run backtest on historical data
```

Stop the bot at any time with `Ctrl+C` — it prints a final summary before exiting.

---

## 6. Configuration

**All settings live in `.env`** — no code changes ever needed.
`config.py` reads `.env`, validates every value on startup, and the bot refuses to run with invalid settings.

### Exchange

| Setting | Default | What it does |
|---|---|---|
| `EXCHANGE` | `kraken` | Any ccxt exchange: `kraken`, `binance`, `kucoin`, `okx`, `coinbase` |
| `SYMBOL` | `BTC/USDT` | Trading pair in ccxt format |
| `FEED_MODE` | `live` | `live` = real exchange prices, `simulated` = random walk (no internet needed) |
| `LOOP_INTERVAL` | `30` | Seconds between each tick |

### Strategy

| Setting | Default | What it does |
|---|---|---|
| `STRATEGY_MODE` | `indicator` | `indicator` = RSI + EMA crossover, `threshold` = simple price levels |
| `RSI_PERIOD` | `14` | RSI lookback period |
| `RSI_OVERBOUGHT` | `70.0` | Skip BUY when RSI is above this |
| `RSI_OVERSOLD` | `30.0` | Skip SELL when RSI is below this |
| `FAST_EMA_PERIOD` | `9` | Fast EMA for trend detection |
| `SLOW_EMA_PERIOD` | `21` | Slow EMA for trend detection |

### Risk (most important — read carefully)

| Setting | Default | What it does |
|---|---|---|
| `RISK_PER_TRADE_PCT` | `0.01` | **1% of cash per trade** — position size calculated dynamically each trade |
| `RISK_MAX_POSITION_PCT` | `0.05` | Max 5% of portfolio in one open position |
| `RISK_DAILY_LOSS_LIMIT` | `0.02` | Block new BUYs if portfolio drops 2% today (SELL always allowed) |
| `RISK_MAX_DRAWDOWN` | `0.10` | Block new BUYs if portfolio drops 10% from all-time peak (SELL always allowed) |
| `RISK_MAX_TRADES_PER_DAY` | `5` | Hard cap on fills per calendar day |
| `COOLDOWN_TICKS` | `10` | Candles to wait after each trade before the next one |

> **Defaults shown are code defaults** (`config.py` `_load()` function). Your `.env` overrides every value — check `.env` for what is actually running.

### Portfolio & Other

| Setting | Default | What it does |
|---|---|---|
| `STARTING_CASH` | `10000.0` | Simulated starting balance (paper trading only) |
| `AI_ENABLED` | `true` | Enable/disable the AI advisory layer |
| `AI_MIN_CONFIDENCE` | `0.65` | Ignore AI signals below 65% confidence |
| `DASHBOARD_ENABLED` | `true` | Write `dashboard.html` after each tick |
| `DASHBOARD_REFRESH` | `30` | Browser auto-refresh interval in seconds |

### Backtest

| Setting | Default | What it does |
|---|---|---|
| `BACKTEST_TIMEFRAME` | `1h` | Candle size: `1m` `5m` `15m` `1h` `4h` `1d` etc. |
| `BACKTEST_LIMIT` | `500` | Number of historical candles to fetch |
| `BACKTEST_FEE_PCT` | `0.001` | 0.1% per trade (typical exchange taker fee) |

---

## 7. Understanding the Terminal Output

Every tick prints two lines:

```
  12:34:01  #0021  $  68,450.00  RSI 47.3  BULLISH  →  HOLD  [position already open]
                   STATE LONG  last BUY @ $68,200.00
```

**Line 1 — Tick summary**

| Part | Meaning |
|---|---|
| `12:34:01` | UTC time |
| `#0021` | Tick number |
| `$68,450.00` | Current price |
| `RSI 47.3` | RSI value (green < 30, red > 70) |
| `BULLISH` | EMA trend direction |
| `→ HOLD` | Final signal after all filters |
| `[position already open]` | Reason signal was filtered (if any) |

**Line 2 — State summary**

| Part | Meaning |
|---|---|
| `STATE LONG` | Current state: IDLE / LONG / COOLDOWN |
| `cooldown 3 remaining` | Ticks left in cooldown (only shown during cooldown) |
| `last BUY @ $68,200` | Last executed trade |

**When a trade fills:**
```
           ▶ BUY FILLED  0.00143 BTC/USDT @ $70,000.00  =  $100.00
           cash $  9,900.00  pos 0.0014 BTC  entry $70,000.00  unreal +$0.00  realized +$0.00
```

**When AI advisory is active:**
```
           AI  BUY  conf=78%  RSI below 50, uptrend confirmed  (312ms)
```

**When risk blocks a trade:**
```
           ⚠ Daily loss limit: portfolio down 2.10% (limit=2%) ...
```

---

## 8. Dashboard

When the bot is running, it writes a `dashboard.html` file in the project root after every tick.

The terminal prints the path on startup:
```
Dashboard → file:///path/to/trade_bot/dashboard.html
```

Open that path in any browser. The page **auto-refreshes automatically** — you do not need to manually refresh.

The dashboard shows:
- Live price, cash, position, unrealized P&L, realized P&L, total portfolio value
- Current bot state (IDLE / LONG / COOLDOWN), RSI, trend, signal
- Full trade history table
- Last 30 ticks log

To disable: set `DASHBOARD_ENABLED=false` in `.env`.

---

## 9. Logs

| File | Contents |
|---|---|
| `logs/trade_bot.log` | All fills, rejects, risk blocks, state changes — persistent across runs |
| `logs/backtest_*.csv` | One file per backtest run — every trade row by row |

View live log as the bot runs:
```bash
tail -f logs/trade_bot.log
```

---

## 10. Running Tests

```bash
.venv/bin/python -m pytest --tb=short -q    # full suite (see CLAUDE.md manifest for expected count)
```

All tests run standalone — no exchange connection needed.

---

## 11. Switching Modes

All changes are made in `.env` — no code editing required.

### Simulated mode (no internet, instant ticks)
```
FEED_MODE=simulated
LOOP_INTERVAL=1
```

### Switch exchange or symbol
```
EXCHANGE=binance
SYMBOL=ETH/USDT
```

### Switch to threshold strategy
```
STRATEGY_MODE=threshold
BUY_THRESHOLD=67000.0
SELL_THRESHOLD=70000.0
```

### Disable AI
```
AI_ENABLED=false
```

### Lower risk further (below current defaults)
```
RISK_PER_TRADE_PCT=0.002    # 0.2% per trade
RISK_DAILY_LOSS_LIMIT=0.005 # block new buys if down 0.5% today
RISK_MAX_DRAWDOWN=0.03      # block new buys if down 3% from peak
```

---

## 12. Project Structure

```
trade_bot/
  config.py             ← ALL CONFIG — edit .env, never touch this file directly
  backtest.py           ← run backtests: .venv/bin/python backtest.py
  bot/
    main.py             ← live bot entry point: .venv/bin/python -m bot.main
    display.py          ← terminal output (ANSI colors)
    data/
      price_feed.py     ← SimulatedFeed / CcxtFeed (live prices)
      historical_feed.py← fetch OHLCV candles for backtesting
    exchanges/
      ccxt_client.py    ← thin ccxt wrapper
    strategy/
      threshold_strategy.py   ← simple price threshold strategy
      indicator_strategy.py   ← RSI + EMA crossover strategy
    indicators/
      indicators.py     ← pure functions: sma, ema, rsi, trend
    state/
      trade_state.py    ← IDLE / LONG / COOLDOWN state machine
    risk/
      risk_manager.py   ← 5-check approval gate (runs before every trade)
    execution/
      executor.py       ← PaperExecutor: simulated orders + cash ledger
    portfolio/
      position_manager.py ← position quantity, avg entry, P&L tracking
    ai/
      ai_engine.py      ← OpenRouter advisory (cannot execute trades)
    dashboard/
      renderer.py       ← writes dashboard.html each tick
    backtest/
      engine.py         ← runs strategy pipeline on historical candles
      metrics.py        ← computes win rate, drawdown, Sharpe ratio etc.
      report.py         ← terminal report + CSV export
  logs/
    trade_bot.log       ← persistent trade log
    backtest_*.csv      ← one file per backtest run
  dashboard.html        ← auto-generated, open in browser
  .env                  ← your settings + API keys (never commit this)
  .env.example          ← template showing every available variable
  requirements.txt      ← pip dependencies
```

---

## How the Signal Pipeline Works

Every tick the bot runs this chain — a signal must pass all layers to become a trade:

```
Price Feed
  → Strategy            raw signal: BUY / SELL / HOLD
  → State Machine       filters: no SELL without position, no BUY while LONG, cooldown lock
  → Dynamic Sizing      qty = cash × RISK_PER_TRADE_PCT ÷ price
  → AI Advisory         optional: can downgrade to HOLD, cannot upgrade HOLD
  → Risk Engine         5 checks: halt, max drawdown, daily cap, daily loss, position size
  → PaperExecutor or LiveExecutor   executes the order, updates cash + position
  → PositionManager     tracks avg entry price and P&L
  → Dashboard + Log     records everything
```

The risk engine is the final authority. AI advisory is never able to override it.
