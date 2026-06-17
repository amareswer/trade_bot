# Stock Bot — Phases 1–6

Advisory-only stock research and AI analysis bot for US (NYSE/NASDAQ) and Canadian (TSX) markets.
No orders, no execution, no real money — data, indicators, research, AI advisory, dashboard, and alerts only.

---

## What it does

Every `LOOP_INTERVAL` seconds it runs this pipeline per symbol:

1. **Price + Indicators** — yfinance OHLCV → RSI, MACD, EMA trend, ADX
2. **Web Research** — RSS news, Reddit sentiment, earnings, CNN Fear & Greed, Google Trends
3. **AI Verdict** — structured prompt → AI model → BUY / SELL / HOLD with confidence + reasoning
4. **Dashboard** — writes `stock_dashboard.html` (two sections: My Watchlist + Top Movers)
5. **Alerts** — terminal box, optional email, optional desktop notification
6. **Paper Trading** — optional virtual-cash executor + portfolio tracker

Symbols are split into two tracked groups:

| Group | Source | Screener | Dashboard section |
|---|---|---|---|
| **My Watchlist** | `WATCHLIST` env var | always scanned | 📋 blue left-border cards |
| **Top Movers** | S&P500 + TSX60 universe | screener-gated | 🔥 green left-border cards |

---

## Setup

```bash
# 1. Install dependencies (from repo root)
pip install -r requirements.txt

# 2. Configure
cp stock_bot/.env.example stock_bot/.env
# edit stock_bot/.env — set WATCHLIST, AI_PROVIDER, credentials, etc.

# 3. Run
python -m stock_bot.main
```

Open `stock_dashboard.html` in your browser once — it auto-refreshes every cycle.

---

## Configuration (`stock_bot/.env`)

### Core

| Variable | Default | Description |
|---|---|---|
| `WATCHLIST` | `SHOP.TO,RY.TO,AAPL,NVDA,AC.TO` | Comma-separated symbols. TSX uses `.TO` suffix |
| `INTERVAL` | `1d` | yfinance candle interval (`1d`, `1h`, `5m`, …) |
| `LOOKBACK_DAYS` | `200` | Days of history to fetch (≥ 50 recommended) |
| `LOOP_INTERVAL` | `60` | Seconds between full scans |

### AI Engine

| Variable | Default | Description |
|---|---|---|
| `AI_ENABLED` | `true` | Set `false` to skip AI and save API quota |
| `AI_PROVIDER` | `openrouter` | `openrouter` \| `ollama_local` \| `ollama_cloud` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama local server URL |
| `OLLAMA_CLOUD_API_KEY` | *(blank)* | API key from ollama.com |
| `OLLAMA_MODEL` | `llama3.2` | Model name for both Ollama providers |

`OPENROUTER_API_KEY` lives in the **root `.env`** (shared with the crypto bot) — do not copy it here.

### Reddit (optional)

| Variable | Default | Description |
|---|---|---|
| `REDDIT_CLIENT_ID` | *(blank)* | App client ID from reddit.com/prefs/apps |
| `REDDIT_CLIENT_SECRET` | *(blank)* | App client secret |
| `REDDIT_USER_AGENT` | `StockBot/1.0` | User agent string |

Leave blank to skip Reddit sentiment — bot continues without it.

### Alerts

| Variable | Default | Description |
|---|---|---|
| `ALERT_EMAIL_ENABLED` | `false` | Send HIGH-priority alerts via email |
| `ALERT_EMAIL_FROM` | *(blank)* | Gmail sender address |
| `ALERT_EMAIL_TO` | *(blank)* | Alert recipient address |
| `ALERT_EMAIL_PASSWORD` | *(blank)* | Gmail app password (not login password) |
| `ALERT_DESKTOP_ENABLED` | `false` | Desktop toast notifications (requires `plyer`) |

Email and desktop alerts fire on HIGH-priority alerts only (PORTFOLIO_SELL, RSI extremes, imminent earnings). Terminal alerts always fire.

### Portfolio

| Variable | Default | Description |
|---|---|---|
| `PORTFOLIO` | *(blank)* | Holdings: `SYM:SHARES:AVGCOST,SYM:SHARES:AVGCOST,...` |
| `BASE_CURRENCY` | `CAD` | Display currency for mixed portfolios |

Example: `PORTFOLIO=SHOP.TO:10:155.00,AAPL:5:180.50`

### Paper Trading

| Variable | Default | Description |
|---|---|---|
| `PAPER_TRADING_ENABLED` | `false` | Enable virtual-cash paper executor |
| `PAPER_STARTING_CASH` | `10000.0` | Virtual cash balance at startup |
| `PAPER_RISK_PCT` | `0.10` | Fraction of cash allocated per paper trade |
| `PAPER_MIN_CONFIDENCE` | `65` | Min AI confidence to trigger a paper trade |

### Universe Scanner

| Variable | Default | Description |
|---|---|---|
| `UNIVERSE_ENABLED` | `false` | Scan S&P500 + TSX60 top movers in addition to watchlist |
| `UNIVERSE_SIZE` | `20` | Top N universe symbols to scan per cycle |
| `UNIVERSE_REFRESH_HOURS` | `24` | How often to refresh the universe symbol list |
| `SCREENER_ENABLED` | `true` | Gate AI on momentum signal for universe symbols |

---

## Dashboard

`stock_dashboard.html` is written to the repo root after every cycle. Open it in a browser once — it auto-refreshes.

### Two-section layout

**📋 My Watchlist** (dark-blue header, blue card left-border)
- All symbols from `WATCHLIST` — always scanned, no screener
- Cards show: signal badge, price, RSI, trend, MACD, news, sentiment, earnings, AI verdict

**🔥 Top Movers** (dark-green header, green card left-border)
- Symbols from S&P500 + TSX60 universe ranked by volume × momentum
- Only shown when `UNIVERSE_ENABLED=true`
- Screener-filtered before AI runs

### Other dashboard sections

- **Fear & Greed meter** — CNN index with gradient bar
- **BUY / HOLD / SELL summary** — count + symbol list per signal
- **Top Picks** — scrollable pills for BUY signals ≥ 65% confidence
- **Portfolio table** — P&L per holding when `PORTFOLIO` is set
- **Alerts panel** — active alerts with priority colour coding

---

## Alerts

| Alert Type | Condition | Priority |
|---|---|---|
| `STRONG_BUY` | BUY signal ≥ 70% confidence | MEDIUM |
| `STRONG_SELL` | SELL signal ≥ 70% confidence | MEDIUM |
| `PORTFOLIO_SELL` | SELL ≥ 65% conf on an owned symbol | HIGH |
| `PORTFOLIO_BUY_MORE` | BUY ≥ 70% conf on an owned symbol | MEDIUM |
| `EARNINGS_SOON` | Earnings within 3 days | HIGH (≤1d) / MEDIUM |
| `RSI_OVERBOUGHT` | RSI > 75 on an owned symbol | HIGH |
| `RSI_OVERSOLD` | RSI < 25 on an owned symbol | HIGH |

Terminal output example:
```
  ╔════════════════════════════════════════════════╗
  ║  🔔 ALERTS — 2 triggered                      ║
  ╠════════════════════════════════════════════════╣
  ║  🟡 MEDIUM  · STRONG_BUY · watchlist          ║
  ║  AAPL @ $189.50 USD                           ║
  ║    STRONG BUY: AAPL @ $189.50 — 74% confide  ║
  ╚════════════════════════════════════════════════╝
```

Each alert is tagged with its source (`watchlist` or `top mover`).

---

## AI Providers

### Option A — OpenRouter (default)
Uses `meta-llama/llama-3.3-70b-instruct:free` on the free tier.
1. Add `OPENROUTER_API_KEY=...` to root `.env`
2. Set `AI_PROVIDER=openrouter` in `stock_bot/.env`

### Option B — Ollama Local
```bash
ollama pull llama3.2
```
1. Set `AI_PROVIDER=ollama_local` in `stock_bot/.env`
2. Set `OLLAMA_MODEL=llama3.2`

### Option C — Ollama Cloud
1. Get a key at ollama.com
2. Set `OLLAMA_CLOUD_API_KEY=...` and `AI_PROVIDER=ollama_cloud` in `stock_bot/.env`

---

## AI verdict rules

- `confidence < 55` → always output HOLD (low confidence = no trade)
- Never BUY if RSI > 75 (overbought)
- Never SELL if RSI < 25 (oversold)
- `trading_style`: DAY (RSI extreme + momentum), LONGTERM (earnings-driven), SWING (default)
- Prompt is < 800 tokens (3 headlines max, concise sections)
- Any API failure → safe fallback HOLD with `confidence=0`

---

## Indicators

| Indicator | Params | Interpretation |
|---|---|---|
| RSI | period=14 | < 30 oversold ⚠, > 70 overbought ⚠ |
| MACD | fast=12, slow=26, signal=9 | histogram > 0 = bullish momentum |
| EMA trend | fast=9, slow=21 | BULLISH / BEARISH / NEUTRAL crossover |
| ADX | period=14 | > 25 = trending market, < 20 = ranging |

---

## Folder structure

```
stock_bot/
  data/
    price_feed.py       ← yfinance OHLCV fetcher (Candle dataclass)
    watchlist.py        ← default symbol list + parser
    universe.py         ← StockUniverse: S&P500+TSX60 ranked by volume×momentum
    screener.py         ← StockScreener: momentum gate for universe symbols
  indicators/
    indicators.py       ← RSI, EMA, SMA, ADX, MACD — pure functions, no state
  research/
    news_fetcher.py     ← RSS headlines (Yahoo Finance + Google News)
    reddit_scraper.py   ← praw sentiment, keyword scoring
    earnings.py         ← yfinance next earnings + EPS actual vs estimate
    fear_greed.py       ← CNN Fear & Greed, 1-hour module-level cache
    google_trends.py    ← PyTrends 7-day interest score
    aggregator.py       ← ThreadPoolExecutor(3), ResearchReport dataclass
  ai/
    verdict.py          ← AIVerdict dataclass (signal, confidence, target, stop, reasoning)
    prompt_builder.py   ← assembles indicators + research into <800-token prompt
    ai_engine.py        ← multi-provider HTTP client, JSON parse, HOLD fallback
  dashboard/
    renderer.py         ← DashboardRenderer, ScanResult dataclass → stock_dashboard.html
  alerts/
    alert.py            ← Alert dataclass, AlertType enum
    evaluator.py        ← AlertEvaluator: 7 check types per cycle
    notifier.py         ← AlertNotifier: terminal + Gmail SMTP + plyer desktop
  portfolio/
    tracker.py          ← PortfolioTracker (static holdings), PortfolioSummary
  execution/
    paper.py            ← StockPaperExecutor: virtual cash, paper orders, realized PnL
    base.py             ← Order, OrderStatus base types
  config.py             ← StockConfig — loads stock_bot/.env, validates all settings
  main.py               ← entry point: scan loop, terminal output, dashboard + alerts
  .env                  ← your local config (not committed)
  .env.example          ← template with all options documented
  README.md             ← this file
```

---

## Phase roadmap

| Phase | Status | Description |
|---|---|---|
| 1 | ✅ Done | Price feed + indicators (RSI, MACD, EMA, ADX) |
| 2 | ✅ Done | Web research (news, Reddit, earnings, Fear & Greed, Google Trends) |
| 3 | ✅ Done | AI analysis engine (multi-provider: OpenRouter, Ollama local, Ollama cloud) |
| 4 | ✅ Done | HTML dashboard (two-section layout, portfolio table, alerts panel) |
| 5 | ✅ Done | Alerts system (7 types, terminal + email + desktop, source-tagged) |
| 6 | ✅ Done | Paper trading, portfolio tracker, universe scanner (S&P500+TSX60), screener |
| — | Future | Questrade broker integration (paper orders via real API) |
