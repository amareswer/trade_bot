---
name: core
description: "Project summary, phase, stack, critical rules, working style — always loaded"
metadata:
  type: project
---

**Project:** Modular crypto trading bot — Python 3.10+, paper trading only, no real money, no ML/AI trading authority.

**Phase:** All 6 phases built. Currently in paper trading observation (Binance BTC/USDT, 4h candles).

**Stack:** Python, ccxt (exchange-agnostic), OpenRouter AI advisory only, no direct exchange SDKs.

**Critical rules:**
1. All exchange calls go through ccxt — never hardcode an exchange
2. Risk engine is the mandatory gate before every execution — SELL always passes checks 2–5
3. Flow is always: Feed → Strategy → StateMachine → Risk → Executor → Portfolio — never collapse layers
4. Discuss and plan before writing any code — ask "ready to build?" first
5. After every session: update both [[feature-plan]] and [[project-trade-bot]] memory

**Working style:** Discuss first, build after confirmation. Update memory every session without being asked.
