---
name: stock-bot-stability
description: "Technical decisions made during stock bot stability session — what was changed, reverted, and why"
metadata:
  type: project
---

## Stock Bot Stability Session — 2026-06-19

### Decisions Table

| Date | Decision | Reason | Outcome |
|---|---|---|---|
| Jun 2026 | Reverted yfinance session management | Custom `_get_fresh_session()` + `requests.Session()` broke ALL price fetches | Stable again — plain `yf.download()` manages session internally |
| Jun 2026 | Reverted `ticker.info` company name lookup | Each call added 2-3s per symbol per cycle (total: 15-30s for 10 symbols) | Fast again — `symbol.replace(".TO", "")` is instant and accurate enough |
| Jun 2026 | Removed `_name_cache` + `get_cached_name()` from price_feed.py | No longer needed after removing ticker.info; caching empty-string names is wasteful | `aggregator.get_company_name()` now returns ticker without suffix |
| Jun 2026 | Whole shares only in paper trading | Fractional shares = crypto behavior, not stock behavior | Clean P&L, no floating-point accumulation bugs |
| Jun 2026 | nvidia_nim as primary AI (not ollama) | 40 rpm free tier, fast model (gpt-oss-120b), streaming required for 550B | Good AI verdicts, ~3-8s per call |
| Jun 2026 | openrouter as automatic fallback | nvidia_nim occasionally 429s | Resilient — bot never runs without AI |
| Jun 2026 | Max 4 positions at $1k account | Risk management — 4 × $250 = fully invested at 25% risk per trade | Capital protected, diversified |
| Jun 2026 | 5% stop loss / 12% take profit | Proven 1:2.4 risk/reward ratio for swing trades | Automated exits, no manual monitoring |
| Jun 2026 | Screener price filter $5–$200 | Affordable at $1k account — penny stocks corrupt share count, >$200 leaves only 0-1 shares | Right sizing for $1k capital |
| Jun 2026 | State corruption guard in _load_state() | Bot loaded $85 trillion cash from corrupted paper_state.json | Rejects and deletes any state file with cash > $1M |
| Jun 2026 | Price guard in paper.py buy() | Prices near zero (penny stocks, bad data) caused share count of 10^15+ | Rejects: non-numeric, price ≤ 0, price > 500k, shares > 100k |
| Jun 2026 | WATCHLIST changed to HOOD,MRNA,NCLH,AC.TO,CCL,INTC | Old watchlist had EBON ($1.95) and IGC ($0.28) — penny stocks triggered corruption | All 6 symbols are in $5-$200 range |

### Why **Why:** lines matter

- **yfinance session management**: yfinance uses Yahoo Finance's crumb-based authentication internally. Adding a custom requests.Session overrides yfinance's own cookie management, causing 401 Unauthorized errors on every request after the first. This was discovered after the session code caused complete price feed failure.
- **ticker.info**: yfinance fetches the full ticker metadata (earnings, options, analyst ratings, etc.) for each `Ticker.info` call. On a 10-symbol watchlist + 10 universe symbols, this adds 30-60 seconds per cycle. The company name is only used for display — the ticker symbol itself is sufficient.
- **int(shares)**: Stock markets trade in whole share increments. Fractional shares exist at some brokers but are not standard. Using `int(risk/price)` prevents floating-point accumulation that compounds into incorrect P&L.

### Do NOT Revisit

- Session management in price_feed.py — NEVER add this back
- ticker.info / fast_info for any display purpose — NEVER add this back  
- float shares for stocks — NEVER use fractional shares unless a broker explicitly supports them

### How to apply in future sessions

If a future session suggests "add a session to fix 401 errors" — the correct fix is to upgrade yfinance (`pip install --upgrade yfinance`) or wait (Yahoo Finance crumb expires every ~30 minutes; plain yf.download() auto-refreshes).

If a future session suggests "use ticker.info to get a better company name" — decline. The display name difference is not worth 2-3s per symbol.
