---
name: stock-bot
description: "Stock bot architecture, phase status, all modules, config knobs, known issues — load when stock_bot/ work comes up"
metadata:
  type: project
---

## What it is

Advisory + paper trading stock research bot living in `stock_bot/` inside the same repo as the crypto bot.
Runs independently: `python -m stock_bot.main`. Writes `stock_dashboard.html` every cycle.

## Phase status (as of 2026-06-19)

| Phase | Status | What it does |
|---|---|---|
| 1 | ✅ Built | yfinance price feed → RSI, MACD, EMA trend, ADX per symbol |
| 2 | ✅ Built | Web research: RSS news, sentiment from headlines, earnings, CNN Fear & Greed |
| 3 | ✅ Built | AI analysis engine: nvidia_nim primary, openrouter fallback |
| 4 | ✅ Built | HTML dashboard: watchlist (blue) + top movers (green) sections |
| 5 | ✅ Built | Alerts: STRONG_BUY/SELL, EARNINGS_SOON, RSI extremes — terminal + email + desktop |
| 6 | ✅ Built | Paper trading ($1,000, max 4 positions), universe scanner, screener, stop loss/take profit |

## Run command

```bash
python -m stock_bot.main    # from repo root
```

Dashboard written to `stock_dashboard.html` in repo root after every cycle.

## Config isolation

`stock_bot/.env` is **separate** from root `.env` (crypto bot). Never merge them.
- `OPENROUTER_API_KEY` lives in root `.env` — `ai_engine.py` loads it separately via `load_dotenv(_ROOT_ENV, override=False)`
- Everything else lives in `stock_bot/.env`

## Active .env (2026-06-19 — STABLE)

```
WATCHLIST=HOOD,MRNA,NCLH,AC.TO,CCL,INTC   # affordable at $1k, passes screener
INTERVAL=1d
LOOKBACK_DAYS=200
LOOP_INTERVAL=120

AI_PROVIDER=nvidia_nim
NVIDIA_MODEL=openai/gpt-oss-120b           # fast, good quality
AI_ENABLED=true

PAPER_TRADING_ENABLED=true
PAPER_STARTING_CASH=1000.00
PAPER_RISK_PCT=0.25                        # $250 max per trade
PAPER_MIN_CONFIDENCE=70                    # only high-confidence signals
PAPER_MAX_POSITIONS=4                      # enforced in main.py

UNIVERSE_ENABLED=true
UNIVERSE_SIZE=10
SCREENER_ENABLED=true

PORTFOLIO=BMO.TO:5:66.10,CM.TO:4:41.15,SPCX:2:160.00,EBON:3:1.95,IGC:50:0.2799
BASE_CURRENCY=CAD
PROTECTED=BMO.TO,CM.TO,SPCX              # display only, never paper-sell
```

## Paper trading rules (enforced in code)

- Max 4 open positions at once (`_MAX_POSITIONS = 4` in main.py)
- Stop loss: -5% of entry price (`_STOP_LOSS_PCT = -0.05`)
- Take profit: +12% of entry price (`_TAKE_PROFIT_PCT = 0.12`)
- Min confidence: 70% for any paper trade
- Whole shares only (`int(shares)` before every order)
- Price guard in paper.py buy(): rejects price ≤ 0, > $500,000, or share count > 100,000
- State validation in _load_state(): rejects cash > $1M or |realized_pnl| > $1M (corrupted state guard)
- Screener price filter: $5–$200 only (universe symbols only; watchlist bypasses)

## Key design: source separation

```
watchlist_symbols = cfg.watchlist          # always force_scan=True, bypass screener
universe_symbols  = universe.pre_filter()  # must pass screener
all_symbols       = deduped union of both

ScanResult.source = "watchlist" | "universe"
Alert.source      = "watchlist" | "universe"
```

Dashboard renders two distinct sections:
- **📋 My Watchlist** — dark-blue header (`#1c2333`), blue card left-border (`#388bfd`)
- **🔥 Top Movers** — dark-green header (`#1c2820`), green card left-border (`#2ea043`)

## AI providers (set AI_PROVIDER in stock_bot/.env)

| Provider | Auth | Model | Rate | Notes |
|---|---|---|---|---|
| `nvidia_nim` ← **ACTIVE** | `NVIDIA_API_KEY` in `.env` | `openai/gpt-oss-120b` | 40 rpm free | Primary |
| `openrouter` | `OPENROUTER_API_KEY` in root `.env` | `meta-llama/llama-3.3-70b-instruct:free` | 10-20 rpm | Auto-fallback |
| `ollama_cloud` | `OLLAMA_CLOUD_API_KEY` in `.env` | `gpt-oss:120b` | weekly limit | Backup |
| `ollama_local` | none | `OLLAMA_MODEL` | local GPU | Dev only |

nvidia_nim uses `openai` SDK with `stream=True` (mandatory — model times out without streaming).
On first nvidia failure, automatically falls back to openrouter.

## AI verdict rules

- `confidence < 55` → always HOLD regardless of signal
- Never BUY if RSI > 75; never SELL if RSI < 25
- Prompt < 800 tokens (3 headlines, concise sections)
- Any failure → safe fallback HOLD(confidence=0) — never crashes loop

## Alert types and thresholds

| AlertType | Condition | Priority |
|---|---|---|
| STRONG_BUY | BUY signal ≥ 70% conf | MEDIUM |
| STRONG_SELL | SELL signal ≥ 70% conf | MEDIUM |
| PORTFOLIO_SELL | SELL ≥ 65% conf on owned symbol | HIGH |
| PORTFOLIO_BUY_MORE | BUY ≥ 70% conf on owned symbol | MEDIUM |
| EARNINGS_SOON | Next earnings ≤ 3 days away | HIGH (≤1d) / MEDIUM |
| RSI_OVERBOUGHT | RSI > 75 on owned symbol | HIGH |
| RSI_OVERSOLD | RSI < 25 on owned symbol | HIGH |

## Universe scanner

`data/universe.py` — StockUniverse
- Fetches S&P500 + TSX60 symbols from Wikipedia
- Ranks by `volume × |price_change|` (momentum × volume)
- Returns top 10 (UNIVERSE_SIZE=10) as `universe_symbols`
- TTL cache: refreshes every 24 hours
- **Market-aware (2026-06-19):** `pre_filter(symbols, n, market_status=...)` accepts
  the market_status dict — US closed → drops all non-.TO symbols before ranking;
  CA closed → drops all .TO symbols; both closed → returns []; both open → all 560 ranked.
  main.py passes market_status at startup and on every TTL refresh.

`data/screener.py` — StockScreener
- Applied to universe symbols ONLY (watchlist bypasses)
- Price range filter: `_MIN_PRICE = 5.0`, `_MAX_PRICE = 200.0`
- Technical filter: RSI extremes, MACD cross, ≥3% price move (any one passes)

## What was reverted and why (2026-06-19 stability session)

| What | Why reverted | Lesson |
|---|---|---|
| Custom `_get_fresh_session()` + `requests.Session()` in price_feed.py | Broke all price fetches — yfinance manages its own session internally | Never add custom session management to yfinance |
| `ticker.info` / `fast_info` company name lookup in price_feed.py | Added 2-3s penalty per symbol per cycle | Use `symbol.replace(".TO", "")` only — simple and fast |
| `_name_cache` + `get_cached_name()` in price_feed.py | Unnecessary — name is only used for display | Removed entirely; aggregator.py now returns `symbol.replace(".TO", "")` |

## 24/7 operation (added 2026-06-19)

`_get_loop_mode(market_status)` in main.py returns one of four modes:

| Mode | Condition | What runs | Sleep |
|---|---|---|---|
| `LIVE` | any market open | Full scan: prices + AI + trades | `LOOP_INTERVAL` (120s) |
| `PRE_MARKET` | weekday, 6:00am–9:30am ET, no holiday | `_run_news_scan()` — news catalysts only | 900s (15 min) |
| `AFTER_HOURS` | weekday, 4:00pm–midnight ET, no holiday | `_run_news_scan()` — news catalysts only | 1800s (30 min) |
| `WEEKEND` | Sat/Sun or full holiday | idle print, no scan | 3600s (1 hr) |

`_run_news_scan(symbols)` — accepts `watchlist_symbols + universe_symbols`.
Pre/after-hours scans now cover ALL known symbols (watchlist + last-known
universe movers), not just watchlist. If universe is empty (bot just started),
gracefully falls back to watchlist only. Prints strongly +/- news catalysts (score ≥ 0.8 or ≤ -0.8), no prices, no AI, no trades.

Dashboard mode badge in header shows current mode: 🟢 LIVE / 🌅 PRE-MARKET / 🌙 AFTER HOURS / 📅 WEEKEND.

Log file: `stock_bot/logs/bot.log` — `RotatingFileHandler`, max 10 MB, 7 files kept.

Keep-alive on Mac: `caffeinate -i python -m stock_bot.main`
Background: `nohup caffeinate -i python -m stock_bot.main > stock_bot/logs/bot.log 2>&1 &`

## Market hours — dynamic holiday computation (2026-06-19)

Hardcoded `_US_HOLIDAYS_2026` / `_CA_HOLIDAYS_2026` frozensets removed.
Replaced with computed functions — no manual update ever needed again.

Helper functions in main.py (all stdlib, no new packages):
```
_nth_weekday(year, month, weekday, n) → date   # e.g. 3rd Monday of Jan
_last_weekday(year, month, weekday)  → date   # e.g. last Monday of May
_observed(d)                         → date   # Sat→Fri, Sun→Mon
_easter(year)                        → date   # Anonymous Gregorian algorithm
_victoria_day(year)                  → date   # Monday immediately before May 25
_us_holidays(year)                   → dict[date, str]   # 10 NYSE holidays
_ca_holidays(year)                   → dict[date, str]   # 12 TSX/Ontario holidays
```

`_get_market_status()` now calls `_us_holidays(today.year)` and `_ca_holidays(today.year)`
each invocation — correct for 2026, 2027, 2028, and beyond.

Boxing Day collision rule: if observed Christmas and observed Boxing Day land on the
same weekday, Boxing Day advances until it finds a free weekday. Handles
2027 (Dec 25=Sat → observe Fri Dec 24; Boxing Dec 26=Sun → observe Mon Dec 27).

`_get_loop_mode()` partial-holiday fix:
- Old: `if ... and us_holiday is None and ca_holiday is None`
  → on US-only holidays (Juneteenth, Thanksgiving), mode fell through
    to WEEKEND and killed TSX pre-market news scan entirely.
- New: `if ... and not (us_holiday and ca_holiday)`
  → PRE_MARKET/AFTER_HOURS runs on partial holidays (one market closed).
    Only falls to WEEKEND when BOTH markets are closed (Christmas, New Year's).

## Market hours — US and CA independent gating (2026-06-19)

`_get_market_status()` in main.py returns:
```python
{
  "us_open":    bool,   # NYSE/NASDAQ open right now
  "ca_open":    bool,   # TSX open right now
  "any_open":   bool,   # scan loop gate — if False, entire cycle is skipped
  "is_weekend": bool,
  "us_holiday": str | None,  # e.g. "Juneteenth", "MLK Day"
  "ca_holiday": str | None,  # e.g. "Canada Day", "Family Day"
  "in_hours":   bool,   # 9:30–16:00 ET window
}
```

Per-symbol routing in `_fetch_symbol_data()`:
- `.TO` symbols → skip if `ca_open` is False
- US symbols → skip if `us_open` is False

Holidays:
- US (NYSE): New Year's, MLK Day, Presidents' Day, Good Friday, Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas
- CA (TSX): New Year's, Family Day, Good Friday, Victoria Day, Canada Day, Civic Holiday, Labour Day, Thanksgiving, Remembrance Day, Christmas Day, Boxing Day

Both use 9:30am–4:00pm ET window (TSX and NYSE share the same clock).

Dashboard renders two market status badges (NYSE / TSX) with green=OPEN, yellow=holiday name, grey=CLOSED.

## Signal quality fixes (2026-06-19)

### Sentiment — Laplace smoothing + confidence
File: `stock_bot/research/sentiment_scraper.py`
- Score formula: `(pos - neg) / (denom + K)` where K=4
- Single keyword can no longer produce ±1.00. Max from 1 hit = +0.200.
- `SentimentData.confidence: float = min(1.0, n_headlines / 5)`
  1 headline = 20%, 3 = 60%, 5+ = 100%
File: `stock_bot/ai/prompt_builder.py`
- Prompt now shows: `"+0.200 (POSITIVE, confidence=60%) | 3 headlines scored"`

### Google Trends — None vs 0
File: `stock_bot/research/google_trends.py`
- All return 0 fallbacks replaced with `return None`
- Return type: `int | None`
File: `stock_bot/research/aggregator.py`
- `ResearchReport.market_trends_score: int | None` (default None)
File: `stock_bot/ai/prompt_builder.py`
- None → "unavailable (rate limited — ignore this cycle)" in prompt
- 0/100 no longer sent to AI on first cycle

### Intraday execution price
New file: `stock_bot/data/intraday_price.py`
- `get_live_price(symbol) → float | None`
- Uses `yf.Ticker.fast_info.last_price` — lightweight, not full OHLCV
- Returns None on any failure — callers fall back to daily close
File: `stock_bot/main.py`
- Paper BUY/SELL now use `execution_price = live_price if live_price else candle.close`
- Indicators still use daily OHLCV (correct). Only execution price uses live tick.

### SL/TP watcher thread
File: `stock_bot/main.py`
- `_check_open_positions_sl_tp(executor, cfg)` — module-level function
  Uses `positions_snapshot()`, `get_live_price()`, `cfg.paper_stop_loss_pct / take_profit_pct`
- Daemon thread starts before `while True` loop, runs every 30s
- Replaces old inline SL/TP block (which waited up to 120s for next OHLCV scan)
- Thread catches + logs all exceptions — never silently dies

### Volume ratio in AI prompt
File: `stock_bot/data/price_feed.py`
- `Candle.volume_ratio: float | None = None` (new optional field)
- Set on `candles[-1]` only: current_vol / 20-day avg vol
File: `stock_bot/ai/prompt_builder.py`
- Volume vs 20-day avg line added under Current Price in PRICE & TECHNICALS
- Buckets: ≥2.0× = "unusually high", ≥1.3× = "above average",
           ≤0.5× = "low volume — treat signals cautiously", else "normal"
- AI rule added: low volume moves (<0.5× avg) lower confidence by 10-15 points

### News ticker collision fix
File: `stock_bot/research/news_fetcher.py`
- `_is_relevant()` upgraded: short tickers (≤3 chars after stripping .TO) use
  word-boundary regex (`\b...\b`) — "AC" no longer matches "black", "ACADIA", "APUR"
- Long tickers (4+ chars): substring match unchanged
- Company name match always takes priority

## Paper trading hardening (2026-06-19)

### Daily loss circuit breaker (UPDATED 2026-06-23)
File: `stock_bot/execution/paper.py`
- **Bug fixed 2026-06-23**: breaker now uses `cash + _open_position_value` (not cash only)
  - Added `_open_position_value: float = 0.0` in `__init__`
  - New `_update_position_value(prices: dict[str, float])` — called in `buy()` + `sell()` after FILLED
  - Uses fill price for traded symbol; avg_cost proxy for others (no extra API calls)
  - `_is_daily_loss_tripped()` now: `current_total = cash + _open_position_value`
  - Self-test at bottom of paper.py: `python stock_bot/execution/paper.py` → 5/5 PASS
- `StockPaperExecutor._session_start_value` synced after `_load_state()` (not constructor)
- `set_daily_loss_limit(pct)` wired from `cfg.paper_daily_loss_pct`
Config: `PAPER_DAILY_LOSS_PCT=0.03` (default 3%)

### Slippage model
File: `stock_bot/execution/paper.py`
- `_fill_price(price, side)` applies `_slippage_bps / 10_000` factor
- BUY: fills at `price × (1 + factor)`. SELL: `price × (1 - factor)`
- Used for cost, proceeds, P&L, `PaperTrade.price` — raw `price` param untouched
Config: `PAPER_SLIPPAGE_BPS=15` (default 0.15%; use 30 for TSX small-caps)

New config keys added to `stock_bot/.env` and `stock_bot/config.py`:
```
PAPER_STOP_LOSS_PCT=0.05      # SL/TP watcher threshold
PAPER_TAKE_PROFIT_PCT=0.12    # SL/TP watcher threshold
PAPER_DAILY_LOSS_PCT=0.03     # circuit breaker
PAPER_SLIPPAGE_BPS=15         # fill slippage simulation
```

## Backtester (2026-06-23)

File: `stock_backtest.py` (project root)
- Indicator-only strategy: RSI<35 + BULLISH EMA trend + ADX≥20 → BUY; SL(5%)/TP(12%)/strategy SELL
- 5 years daily candles, 11 symbols (8 US + 3 CA), shared cash pool, max 4 positions
- Config: SL=5%, TP=12%, Risk=25%, Commission=0.5%, Slippage=15bps, Starting cash=$10k
- Saves fills to `stock_bot/logs/stock_backtest_YYYYMMDD.csv`
- **Baseline verdict (2026-06-23): FAIL** — only 2 trades generated (AMD +14.88%, BMO.TO -5.78%)
- Key insight: RSI<35 AND BULLISH AND ADX≥20 rarely fire simultaneously. Live bot uses AI as
  primary signal; indicators are pre-screening gates. Cannot compare paper PF to this baseline.
- Paper trading needs 30+ trades before meaningful PF can be assessed.

## Known issues

- Yahoo Finance crumb (401) errors on cycle 2+ when yfinance session expires between cycles — bot handles gracefully (returns None, skips symbol, continues)
- On US market holidays, yfinance returns NaN or stale cross-contaminated prices — resolved on next trading day
- EBON ($1.95) and IGC ($0.2799) in PORTFOLIO are penny stocks — screener would reject them as universe symbols (price < $5), but they're watchlist and pass through to display-only portfolio section
- SPCX: no earnings data (may be delisted from yfinance index)
- OpenRouter free tier hits 429 under rapid calls — bot handles with HOLD fallback

## File map

```
stock_bot/
  data/
    price_feed.py     ← yfinance OHLCV (plain yf.download only — NO session management)
    watchlist.py      ← default symbol list + parser
    universe.py       ← StockUniverse: S&P500+TSX60 ranked by volume×momentum
    screener.py       ← StockScreener: price filter ($5-$200) + momentum gate
  indicators/
    indicators.py     ← RSI, EMA, SMA, ADX, MACD — pure functions
  research/
    news_fetcher.py   ← RSS headlines (Yahoo Finance + Google News), 5 per symbol
    sentiment_scraper.py ← headline sentiment scoring
    earnings.py       ← yfinance next earnings date + EPS actual vs estimate
    fear_greed.py     ← CNN Fear & Greed index, 1-hour module-level cache
    google_trends.py  ← PyTrends 7-day interest score per symbol
    aggregator.py     ← ThreadPoolExecutor(2), ResearchReport dataclass
                         get_company_name() = symbol.replace(".TO", "") — no API call
  ai/
    verdict.py        ← AIVerdict dataclass
    prompt_builder.py ← assembles indicators + research into <800-token prompt
    ai_engine.py      ← multi-provider HTTP client, JSON parse, HOLD fallback
  dashboard/
    renderer.py       ← DashboardRenderer, ScanResult dataclass → stock_dashboard.html
  alerts/
    alert.py          ← Alert dataclass, AlertType enum
    evaluator.py      ← AlertEvaluator: runs all checks each cycle
    notifier.py       ← AlertNotifier: terminal + email + desktop delivery
  portfolio/
    tracker.py        ← PortfolioTracker (static PORTFOLIO env var holdings)
                         Validates: shares 0-100k, avg_cost 0-100k, stores int(shares)
  execution/
    paper.py          ← StockPaperExecutor: virtual $1k, paper buy/sell, realized PnL
                         Guards: price type check, price 0-500k, shares 0-100k
                         State guard: rejects cash>$1M or |realized_pnl|>$1M on load
    base.py           ← Order, OrderStatus base types
  config.py           ← StockConfig from stock_bot/.env, all settings + validation
  main.py             ← entry point — scan loop, stop loss/take profit, max positions, dashboard
  .env                ← local config (not committed)
  .env.example        ← template with all options documented
```

## Next steps

1. Accumulate 30-50 paper trades and compare paper PF/win rate to real market behavior
2. Consider backtester for stock bot (Part 2 of this stability session)
3. ETH/BTC universe expansion once stock paper trading is validated
