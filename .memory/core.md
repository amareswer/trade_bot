---
name: core
description: "Project summary, phase, stack, critical rules, working style — always loaded"
metadata:
  type: project
---

**Project:** Two-bot trading system — crypto bot (ccxt/Kraken) + stock bot (yfinance/paper trading). Python 3.10+.

**Phase:** All phases built. Crypto bot live on Kraken BTC/CAD. Stock bot in paper trading observation.

**Stack:** Python, ccxt (crypto), yfinance (stocks), nvidia_nim primary AI + openrouter fallback, no direct exchange SDKs.

**Critical rules — crypto bot:**
1. All exchange calls go through ccxt — never hardcode an exchange
2. Risk engine is the mandatory gate before every execution — SELL always passes checks 2–5
3. Flow is always: Feed → Strategy → StateMachine → Risk → Executor → Portfolio — never collapse layers
4. Discuss and plan before writing any code — ask "ready to build?" first
5. After every session: update both [[stock-bot]] and [[project_trade_bot]] memory

**Critical rules — stock bot (PERMANENT — do not change):**
6. Never add session management to yfinance — `yf.download()` manages its own session internally. Custom sessions break it.
7. Never use `ticker.info`, `fast_info`, or any yfinance metadata call for company names — use `symbol.replace(".TO", "")` only. Each metadata call adds 2-3s per symbol.
8. Always validate price > 0 and < 500,000 before any trade calculation in paper.py
9. Always use `int(risk_amount / price)` for share counts — never float shares for stocks
10. Paper state files (`paper_state.json`, `paper_trades.csv`) must be manually deleted when switching strategies or after suspected corruption
11. One change at a time — test after each change before the next

**Working style:** Discuss first, build after confirmation. Update memory every session without being asked.
