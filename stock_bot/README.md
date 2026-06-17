# Stock Bot — Phases 1 · 2 · 3

Advisory-only stock research and AI analysis bot for US (NYSE/NASDAQ) and Canadian (TSX) markets.
No orders, no execution, no real money — data, indicators, research, and AI advisory only.

---

## What it does (Phase 3)

Every `LOOP_INTERVAL` seconds it runs this pipeline per symbol:

1. **Price + Indicators** — yfinance OHLCV → RSI, MACD, EMA trend, ADX
2. **Web Research** — RSS news headlines, Reddit sentiment, earnings data, CNN Fear & Greed
3. **AI Verdict** — structured prompt → AI model → BUY / SELL / HOLD with confidence + reasoning

Example output:
```
  ── Scan #0001  2026-06-15 22:00:00 ──────────────────────────────
  Symbol           Price    RSI    Trend            ADX       MACD
  ──────────  ──────────  ───────  ──────────  ─────────────  ──────────────────────────────
  SHOP.TO     $    157.27  RSI=52.4   ▲ BULLISH   ADX=13.2 ranging   MACD= +0.06  sig= -0.30  hist=+0.37
  📰 News (5):  "Shopify beats Q2 estimates" · "Analyst raises target to $110" · "..."
  💬 Reddit:    POSITIVE (score: +0.18) | 7 posts this week
  📅 Earnings:  Next: 2026-08-05 | Last: Beat by 9.1%
  😨 Fear & Greed: 41 — fear
  🤖 AI (SWING   ):  ✅ BUY  | Confidence: 72%
                     Target: $170.00 | Stop: $150.00
                     "RSI is neutral with a bullish EMA cross and strong earnings beat. ADX is low..."
  ──────────────────────────────────────────────────────────────────────
```

RSI ≥ 70 or ≤ 30 is flagged with `⚠`. ADX ≥ 25 = trending market. Confidence < 55 is always coerced to HOLD.

Logs are written to `logs/stock_bot.log`.

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

---

## Configuration (`stock_bot/.env`)

### Core

| Variable | Default | Description |
|---|---|---|
| `WATCHLIST` | `SHOP.TO,RY.TO,AAPL,NVDA,AC.TO` | Comma-separated symbols. TSX uses `.TO` suffix |
| `INTERVAL` | `1d` | yfinance candle interval (`1d`, `1h`, `5m`, …) |
| `LOOKBACK_DAYS` | `200` | Days of history to fetch (≥ 50 recommended for all indicators) |
| `LOOP_INTERVAL` | `60` | Seconds between full watchlist scans |

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

Leave blank to skip Reddit sentiment gracefully — bot continues without it.

This file is **separate from the root `.env`** (crypto bot). They do not share settings.

---

## AI Providers

### Option A — OpenRouter (default)
Uses `meta-llama/llama-3.3-70b-instruct:free` on the free tier.
1. Add `OPENROUTER_API_KEY=...` to root `.env` (already done if crypto AI is working)
2. Set `AI_PROVIDER=openrouter` in `stock_bot/.env`

### Option B — Ollama Local
Runs any locally-pulled model. No API key, no cloud.
```bash
ollama pull llama3.2      # or mistral, phi3, gemma2, etc.
```
1. Set `AI_PROVIDER=ollama_local` in `stock_bot/.env`
2. Set `OLLAMA_MODEL=llama3.2` (or whichever model you pulled)
3. `OLLAMA_BASE_URL` defaults to `http://localhost:11434` — change if running on another machine

### Option C — Ollama Cloud
Uses ollama.com's hosted inference.
1. Get a key at ollama.com
2. Set `OLLAMA_CLOUD_API_KEY=...` and `AI_PROVIDER=ollama_cloud` in `stock_bot/.env`
3. Set `OLLAMA_MODEL=llama3.2` (or any model available on ollama.com)

---

## Folder structure

```
stock_bot/
  data/
    price_feed.py       ← yfinance OHLCV fetcher (Candle dataclass); TSX + US transparent
    watchlist.py        ← default symbol list + parser
  indicators/
    indicators.py       ← RSI, EMA, SMA, ADX, MACD — pure functions, no state
  research/
    news_fetcher.py     ← RSS headlines (Yahoo Finance + Google News), 5 per symbol
    reddit_scraper.py   ← praw, 5 subreddits, keyword sentiment score
    earnings.py         ← yfinance next earnings date + EPS actual vs estimate
    fear_greed.py       ← CNN Fear & Greed index, 1-hour module-level cache
    aggregator.py       ← ThreadPoolExecutor(3), ResearchReport dataclass
  ai/
    verdict.py          ← AIVerdict dataclass (signal, confidence, target, stop, reasoning)
    prompt_builder.py   ← assembles indicators + research into <800-token prompt
    ai_engine.py        ← multi-provider HTTP client, JSON parse, HOLD fallback
  config.py             ← loads stock_bot/.env, validates, returns StockConfig
  main.py               ← entry point — scan loop, terminal output
  .env                  ← your local config (not committed)
  .env.example          ← template with all options documented
  README.md             ← this file
```

---

## Indicators

| Indicator | Params | Interpretation |
|---|---|---|
| RSI | period=14 | < 30 oversold ⚠, > 70 overbought ⚠ |
| MACD | fast=12, slow=26, signal=9 | histogram > 0 = bullish momentum |
| EMA trend | fast=9, slow=21 | BULLISH / BEARISH / NEUTRAL EMA crossover |
| ADX | period=14 | > 25 = trending, < 20 = ranging |

---

## AI verdict rules

- `confidence < 55` → always output HOLD (low confidence = no trade)
- Never BUY if RSI > 75 (overbought)
- Never SELL if RSI < 25 (oversold)
- `trading_style`: DAY (RSI extreme + momentum), LONGTERM (earnings-driven), SWING (default)
- Prompt is < 800 tokens (3 headlines max, concise sections)
- Any API failure → safe fallback HOLD with `confidence=0`

---

## Phase roadmap

| Phase | Status | Description |
|---|---|---|
| 1 | ✅ Done | Price feed + indicators (RSI, MACD, EMA, ADX) |
| 2 | ✅ Done | Web research engine (news, Reddit, earnings, Fear & Greed) |
| 3 | ✅ Done | AI analysis engine (multi-provider: OpenRouter, Ollama local, Ollama cloud) |
| 4 | ⬜ Next | HTML dashboard (per-symbol cards, verdict history, sentiment timeline) |
| 5 | ⬜ Future | Questrade broker integration (paper orders first) |
| 6 | ⬜ Future | Per-symbol position tracking and P&L |
