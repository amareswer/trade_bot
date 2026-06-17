---
name: stock-bot
description: "Stock bot architecture, phase status, provider config, known issues — load when stock_bot/ work comes up"
metadata:
  type: project
---

## What it is

Advisory-only stock research + AI analysis bot living in `stock_bot/` inside the same repo as the crypto bot. No execution, no orders, no real money. Runs independently with `python -m stock_bot.main`.

## Phase status

| Phase | Status | What it does |
|---|---|---|
| 1 | ✅ Built | yfinance price feed → RSI, MACD, EMA trend, ADX per symbol |
| 2 | ✅ Built | Web research: RSS news, Reddit sentiment (praw), earnings (yfinance), CNN Fear & Greed |
| 3 | ✅ Built | AI analysis engine: multi-provider, structured prompt → BUY/SELL/HOLD verdict |
| 4 | ⬜ Next | HTML dashboard — per-symbol cards, verdict history, sentiment timeline |
| 5 | ⬜ Future | Questrade broker integration (paper orders first) |
| 6 | ⬜ Future | Position tracking + P&L |

## Run command

```bash
python -m stock_bot.main    # from repo root
```

## Config isolation

`stock_bot/.env` is **separate** from root `.env` (crypto bot). Never merge them.
- `OPENROUTER_API_KEY` lives in root `.env` — `ai_engine.py` loads it separately via `load_dotenv(_ROOT_ENV, override=False)`
- Everything else (watchlist, AI provider, Reddit, Ollama) lives in `stock_bot/.env`

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

## Known issues / watch items

- Yahoo Finance RSS returns generic finance headlines (not always symbol-specific) for some tickers — Google News RSS is more targeted
- OpenRouter free tier (`llama-3.3-70b-instruct:free`) hits 429 rate limits under rapid successive calls; bot handles gracefully with HOLD fallback
- Reddit sentiment returns "no posts" when credentials are blank — expected, not a bug
- Earnings data coverage varies: TSX symbols sometimes have no next-earnings-date from yfinance

## File map

```
stock_bot/
  data/           price_feed.py, watchlist.py
  indicators/     indicators.py (RSI, EMA, SMA, ADX, MACD — pure functions)
  research/       news_fetcher.py, reddit_scraper.py, earnings.py, fear_greed.py, aggregator.py
  ai/             verdict.py, prompt_builder.py, ai_engine.py
  config.py       StockConfig — watchlist, interval, lookback, loop, ai_enabled
  main.py         scan loop — indicators → research → AI → print
```
