---
name: project-trade-bot
description: "Full architecture, module map, config knobs, and run commands for the crypto trading bot"
metadata: 
  node_type: memory
  type: project
  originSessionId: dbbe3778-3f4b-4e87-860a-0551e0a6dee6
---

Modular algorithmic crypto trading bot in Python 3.10+. Paper trading only. No real money, no ML/AI trading authority.

**Why:** Clean local prototype built to be extended incrementally — each layer is a thin interface so real components can be swapped in without changing callers.

**How to apply:** Keep layers decoupled. Flow is always: Feed → Strategy → State Machine → Risk → Executor → Portfolio. Do not collapse these layers or add cross-cutting logic.

---

## Current module map

```
bot/
  data/
    price_feed.py           # SimulatedFeed (random walk) + CcxtFeed (live via ccxt)
    historical_feed.py      # fetch_candles() + fetch_candles_paginated() — OHLCV via ccxt; Candle dataclass
  exchanges/
    ccxt_client.py          # CcxtClient — thin ccxt wrapper, fetch_price()
  strategy/
    threshold_strategy.py   # ThresholdStrategy: BUY < threshold, SELL > threshold
    indicator_strategy.py   # IndicatorStrategy: evaluate(candle: Candle) → Signal
                            #   EMA crossover + RSI momentum + ADX regime filter
                            #   IndicatorConfig: rsi/ema/adx params; _closes/_highs/_lows deques
  indicators/
    indicators.py           # Pure functions: sma(), ema(), rsi(), trend(), adx()
  state/
    trade_state.py          # TradingStateMachine: IDLE/LONG/COOLDOWN (see state_machine memory)
  portfolio/
    position_manager.py     # PositionManager: avg entry, realized/unrealized PnL (see position_manager memory)
  execution/
    executor.py             # PaperExecutor: cash ledger, Order lifecycle
    simulated_executor.py   # old simple logger — kept, not used
  risk/
    risk_manager.py         # RiskManager: 5-check approval gate (see risk_layer memory)
  ai/
    ai_engine.py            # AIEngine: OpenRouter advisory (deepseek-v4-flash:free), advisory only
  backtest/
    engine.py               # run() — full pipeline replay on historical candles; SL/TP override
    metrics.py              # compute() — win rate, profit factor, Sharpe, drawdown, etc.
    report.py               # print_report() terminal + save_csv() → logs/
  display.py                # Terminal UI — ANSI colors, state/position/tick display; building_candle() progress bar
  main.py                   # Entry point — wires all layers; CandleAggregator class (live tick → OHLCV window)
config.py                   # ← CENTRAL CONFIG — all settings, env overrides, validation, dynamic sizing
backtest.py                 # CLI entry point: python backtest.py
requirements.txt            # ccxt, python-dotenv, openai
.env                        # OPENROUTER_API_KEY (gitignored via .gitignore)
.env.example                # placeholder for sharing
.gitignore                  # protects .env, __pycache__, logs/, .venv/
logs/trade_bot.log          # persistent INFO-level trade log (auto-created on first run)
test_executor.py            # 6 tests — order lifecycle
test_risk_manager.py        # 11 tests — risk checks
test_indicators.py          # 21 tests — SMA, EMA, RSI, trend, IndicatorStrategy
```

---

## Active config (current .env — updated 2026-06-14)

```
EXCHANGE=kraken
SYMBOL=BTC/CAD
LIVE_TRADING=true
DRY_RUN=false
CANDLE_MINUTES=60        # 1h candles (changed from 240 on 2026-06-14)
ADX_THRESHOLD=18.0       # lowered from 15.0 after zero-trades day (ADX 20-21 was blocking all signals)
MAX_EMA_SPREAD_PCT=0.0   # disabled
RSI_OVERBOUGHT=68.0      # tightened from 70.0
RSI_OVERSOLD=32.0        # tightened from 30.0
RISK_PER_TRADE_PCT=0.10
RISK_MAX_POSITION_PCT=0.15
STARTING_CASH=100.0
STOP_LOSS_PCT=0.02
TAKE_PROFIT_PCT=0.04
AI_ENABLED=false
```

**Why Kraken (not Binance):** Binance unavailable in Canada. BTC/CAD is the native pair.
**Why 1h candles:** Switched from 4h on 2026-06-14 after bot missed a full $89k→$92k move with zero trades. 1h gives 4× more decision points on a $100 account where missed trades are costly.
**Backtest note:** Config must be validated with `BACKTEST_TIMEFRAME=1h BACKTEST_LIMIT=5000 EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` before trusting — not yet done as of 2026-06-14.
**Fee situation:** Actual Kraken fee 0.80% (vs 0.26% modeled). Strategy net-negative at 0.80%; investigating maker orders (0.16%) as fee lever.

---

## Active flow (main.py)

```
CcxtFeed.get_price()
  → CandleAggregator.add_tick(price)             accumulate ticks; None until period elapses
      (displays "building candle X/240min" each tick while accumulating)
  → Candle(open, high, low, close, volume)       real OHLCV emitted every CANDLE_MINUTES
  → IndicatorStrategy.evaluate(candle)            raw signal  [ADX + RSI + EMA]
      → TradingStateMachine.filter()              position-aware filter + dedup
          → AIEngine.advise()                     optional advisory (can only downgrade to HOLD)
              → RiskManager.evaluate()            final authority
                  → PaperExecutor.execute()
                      → PositionManager.on_buy/sell()
                      → TradingStateMachine.on_fill()
                      → display

Note: CandleAggregator only active when FEED_MODE=live + STRATEGY_MODE=indicator.
Simulated mode keeps flat fake candles (open=high=low=close=price) for fast iteration.
```

---

## Key config knobs (.env / config.py)

All settings live in `.env`. `config.py` reads, validates, and exposes them as `cfg.*`.

| Variable | Default | Purpose |
|---|---|---|
| `FEED_MODE` | `"live"` | `"live"` or `"simulated"` |
| `STRATEGY_MODE` | `"indicator"` | `"indicator"` or `"threshold"` |
| `EXCHANGE` | `"kraken"` | any ccxt exchange id |
| `SYMBOL` | `"BTC/USDT"` | ccxt unified symbol |
| `LOOP_INTERVAL` | `30` | seconds between ticks |
| `CANDLE_MINUTES` | `240` | live tick aggregation window; use 60 for 1h candles |
| `RSI_PERIOD` | `14` | Wilder RSI lookback |
| `RSI_OVERBOUGHT` | `65.0` | skip BUY if RSI above (tightened) |
| `RSI_OVERSOLD` | `35.0` | skip SELL if RSI below (tightened) |
| `FAST_EMA_PERIOD` | `9` | fast EMA for trend crossover |
| `SLOW_EMA_PERIOD` | `21` | slow EMA for trend crossover |
| `ADX_PERIOD` | `14` | lookback for ADX calculation |
| `ADX_THRESHOLD` | `25.0` | ADX below this → ranging market → HOLD |
| `STARTING_CASH` | `10_000` | simulated capital |
| `RISK_PER_TRADE_PCT` | `0.02` | **2%** of cash per trade (dynamic sizing) |
| `RISK_MAX_POSITION_PCT` | `0.10` | **10%** max portfolio in one position |
| `RISK_DAILY_LOSS_LIMIT` | `0.01` | **1%** — blocks new BUYs (SELL always allowed) |
| `RISK_MAX_DRAWDOWN` | `0.05` | **5%** — blocks new BUYs (SELL always allowed) |
| `RISK_MAX_TRADES_PER_DAY` | `5` | daily fill cap |
| `COOLDOWN_TICKS` | `6` | candles locked after each trade |
| `AI_ENABLED` | `false` | OpenRouter advisory (currently off) |
| `AI_MODEL` | `"deepseek/deepseek-v4-flash:free"` | OpenRouter model |
| `AI_MIN_CONFIDENCE` | `0.65` | AI responses below this → HOLD |
| `BACKTEST_TIMEFRAME` | `"4h"` | candle timeframe for backtest |
| `BACKTEST_LIMIT` | `500` | candles to fetch (500 = ~83 days at 4h) |
| `STOP_LOSS_PCT` | `0.02` | exit if price drops 2% from entry (0 = disabled) |
| `TAKE_PROFIT_PCT` | `0.04` | exit if price rises 4% from entry (0 = disabled) |

---

## Problems fixed

| Problem | Fix |
|---|---|
| SELL with no position | State machine: IDLE + SELL → HOLD |
| Duplicate BUY/SELL | State machine dedup: same as last_action → HOLD |
| Unclear risk blocks | `approval.message` shown inline in terminal |
| No cooldown | COOLDOWN_TICKS — locked N candles after every fill |
| No position awareness | LONG blocks BUY, IDLE blocks SELL |
| Wrong cost basis on multi-buy | `executor.py` now uses weighted avg (not overwrite) |
| Negative position in SimulatedExecutor | Guard added — SELL with no position logs warning and returns |
| Stale price no TTL | `CcxtFeed` rejects fallback prices older than 120s |
| Float equality on accumulated floats | `1e-9` epsilon used in executor and position_manager |
| Trade logs not persisted | `logs/trade_bot.log` file handler at INFO level added in main.py |
| Hardcoded "BTC" in AI prompt | `ai_engine._build_prompt()` now uses `symbol.split('/')[0]` |
| No .gitignore | `.gitignore` created — `.env` and secrets protected |
| SELL blocked by circuit breakers | Checks 2 (MAX_DRAWDOWN) and 4 (DAILY_LOSS) now BUY-only — SELL always passes |
| Risk limits too loose | Defaults tightened: 2% per trade, 10% max pos, 1% daily loss, 5% drawdown, 5 trades/day |
| Strategy enters choppy markets | ADX filter added: `adx_val < adx_threshold → HOLD`. Cuts 49% of trades, improves PF 1.00→1.25 |
| evaluate() took price only | Signature changed to `evaluate(candle: Candle)` — strategy now receives full OHLCV per tick |
| Live bot never traded (ADX/RSI broken) | `CandleAggregator` added — accumulates 30s ticks into real OHLCV candles; strategy only fires every `CANDLE_MINUTES` minutes |
| 1000-candle API cap | `fetch_candles_paginated()` pages with `since` param — 5000 candles in 5 calls |
| No backtest exit rules | Stop-loss and take-profit added to engine; `FillRecord.reason` tracks which fired |

---

## Run command

```bash
cd /Users/nishita/Desktop/Amaresh/projects/trade_bot
python -m bot.main        # live mode, indicator strategy, AI advisory

# Simulated mode (no network):
FEED_MODE = "simulated"   # change in main.py

# Simple threshold strategy:
STRATEGY_MODE = "threshold"
```

## Test commands

```bash
python test_executor.py       # 6 tests
python test_risk_manager.py   # 11 tests
python test_indicators.py     # 21 tests
```

## Environment

```bash
# .env file in project root
OPENROUTER_API_KEY=sk-or-...
```
