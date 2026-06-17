---
name: stock-bot
description: "Stock bot architecture, phase status, all modules, config knobs, known issues — load when stock_bot/ work comes up"
metadata:
  type: project
---

## What it is

Advisory-only stock research + AI analysis bot living in `stock_bot/` inside the same repo as the crypto bot. No execution, no real money. Runs independently with `python -m stock_bot.main`. Writes `stock_dashboard.html` every cycle.

## Phase status (as of 2026-06-16)

| Phase | Status | What it does |
|---|---|---|
| 1 | ✅ Built | yfinance price feed → RSI, MACD, EMA trend, ADX per symbol |
| 2 | ✅ Built | Web research: RSS news, Reddit sentiment, earnings, CNN Fear & Greed |
| 3 | ✅ Built | AI analysis engine: multi-provider (OpenRouter / Ollama local / Ollama cloud) |
| 4 | ✅ Built | HTML dashboard: per-symbol cards, portfolio table, Fear & Greed meter, alerts panel |
| 5 | ✅ Built | Alerts: STRONG_BUY/SELL, EARNINGS_SOON, RSI extremes — terminal + email + desktop |
| 6 | ✅ Built | Paper trading executor, portfolio tracker, universe scanner (S&P500+TSX60), screener |

## Run command

```bash
python -m stock_bot.main    # from repo root
```

Dashboard written to `stock_dashboard.html` in repo root after every cycle.

## Config isolation

`stock_bot/.env` is **separate** from root `.env` (crypto bot). Never merge them.
- `OPENROUTER_API_KEY` lives in root `.env` — `ai_engine.py` loads it separately via `load_dotenv(_ROOT_ENV, override=False)`
- Everything else (watchlist, AI provider, alerts, paper trading, universe) lives in `stock_bot/.env`

## Key design: source separation (added 2026-06-16)

Symbols are tracked by origin throughout the entire pipeline:

```
watchlist_symbols = cfg.watchlist          # always force_scan=True
universe_symbols  = universe.pre_filter()  # go through screener
all_symbols       = deduped union of both

ScanResult.source = "watchlist" | "universe"
Alert.source      = "watchlist" | "universe"
```

Dashboard renders two distinct sections:
- **📋 My Watchlist** — dark-blue header (`#1c2333`), blue card left-border (`#388bfd`)
- **🔥 Top Movers** — dark-green header (`#1c2820`), green card left-border (`#2ea043`)

Terminal alert box shows: `🟡 MEDIUM · STRONG_BUY · watchlist` or `· top mover`

## AI providers (set AI_PROVIDER in stock_bot/.env)

| Value | Auth | Model |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` in root `.env` | `meta-llama/llama-3.3-70b-instruct:free` |
| `ollama_local` | none (local server) | `OLLAMA_MODEL` (e.g. `llama3.2`) |
| `ollama_cloud` | `OLLAMA_CLOUD_API_KEY` in `stock_bot/.env` | `OLLAMA_MODEL` |

Currently active: `ollama_cloud` with `llama3.2`.

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

Delivery: terminal box (always), Gmail SMTP (opt-in HIGH only), plyer desktop (opt-in HIGH only).

## Universe scanner

`data/universe.py` — StockUniverse
- Fetches S&P500 + TSX60 symbols from Wikipedia
- Ranks by `volume × |price_change|` (momentum × volume)
- Returns top N (default 20) as `universe_symbols`
- TTL cache: refreshes every `UNIVERSE_REFRESH_HOURS` (default 24)

`data/screener.py` — StockScreener  
- Gate applied to universe symbols only (not watchlist)
- Filters out low-momentum stocks before running AI
- `force_scan=True` bypasses screener for watchlist symbols

## Known issues / watch items

- Yahoo Finance RSS returns generic finance headlines for some tickers; Google News RSS is more targeted
- OpenRouter free tier (`llama-3.3-70b-instruct:free`) hits 429 rate limits under rapid successive calls; bot handles gracefully with HOLD fallback
- Reddit sentiment returns "no posts" when credentials are blank — expected, not a bug
- Earnings data coverage varies: TSX symbols sometimes have no next-earnings-date from yfinance

## File map

```
stock_bot/
  data/
    price_feed.py     ← yfinance OHLCV, Candle dataclass, TSX .TO transparent
    watchlist.py      ← default symbol list + parser
    universe.py       ← StockUniverse: S&P500+TSX60 ranked by volume×momentum
    screener.py       ← StockScreener: momentum gate for universe symbols
  indicators/
    indicators.py     ← RSI, EMA, SMA, ADX, MACD — pure functions
  research/
    news_fetcher.py   ← RSS headlines (Yahoo Finance + Google News), 5 per symbol
    reddit_scraper.py ← praw, 5 subreddits, keyword sentiment score
    earnings.py       ← yfinance next earnings date + EPS actual vs estimate
    fear_greed.py     ← CNN Fear & Greed index, 1-hour module-level cache
    google_trends.py  ← PyTrends 7-day interest score per symbol
    aggregator.py     ← ThreadPoolExecutor(3), ResearchReport dataclass
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
    tracker.py        ← PortfolioTracker (static holdings), PortfolioSummary
  execution/
    paper.py          ← StockPaperExecutor: virtual cash, paper buy/sell, realized PnL
    base.py           ← Order, OrderStatus base types
  config.py           ← StockConfig from stock_bot/.env, all settings + validation
  main.py             ← entry point — scan loop, terminal output, dashboard render
  .env                ← local config (not committed)
  .env.example        ← template with all options documented
```
