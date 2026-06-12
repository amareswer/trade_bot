---
name: progress-current
description: "Current stage and what is in progress"
metadata:
  type: project
---

**Status as of 2026-06-12:** Bot is live on Kraken BTC/CAD with an open position. Multi-symbol validation complete. Fee discovery in progress.

**What's running:** `python -m bot.main` — live Kraken BTC/CAD feed, 4h candles, IndicatorStrategy (EMA crossover + RSI + ADX≥15), full risk gate, LiveExecutor (DRY_RUN=false).

**Open position (as of 2026-06-11 20:00 UTC):**
- Entry: 0.000113 BTC/CAD @ $88,870.20
- Stop-loss: $87,093 (2% below entry) — checked every 30s tick
- Take-profit: $92,425 (4% above entry) — checked every 30s tick
- Actual fill fee: $0.0803 CAD = **0.80%** (not 0.26% modeled) — cause under investigation

**Active .env config:**
- `EXCHANGE=kraken`, `SYMBOL=BTC/CAD`
- `LIVE_TRADING=true`, `DRY_RUN=false`
- `RISK_PER_TRADE_PCT=0.10`, `RISK_MAX_POSITION_PCT=0.15` (raised from 0.10 to clear rounding-drift rejection — see [[live-loop-bugs]])
- `STARTING_CASH=100.0`
- `ADX_THRESHOLD=15.0`, `STOP_LOSS_PCT=0.02`, `TAKE_PROFIT_PCT=0.04`
- `AI_ENABLED=false`

**Fee situation:**
Actual Kraken fee 0.80% vs 0.26% modeled. At 0.80%, strategy is net-negative even with PF 1.21 signal quality (fees exceed gross profit at $100 capital). Fee-dict logging added to LiveExecutor — next fill reveals raw ccxt response. Fee levers to investigate: maker orders (0.16%), volume tier, BTC/USD vs BTC/CAD.

**Multi-symbol validation complete (2026-06-11):**
See [[multi-symbol-validation]] for full results.
- ETH: first expansion symbol (PF 1.20/1.22 — most stable across regimes)
- SOL, BNB: watchlist (strong currently, regime-dependent historically)
- LINK: permanently excluded (PF 0.95/0.92 — never profitable)
- BTC: weakest current-regime symbol (PF 0.45 on same Nov25–Jun26 dates where ETH=1.22)
- Decision: no symbol changes until fee fix proven

**Dashboard:** now renders every 30s (was only at 4h candle closes — see [[live-loop-bugs]]). Shows live position panel with SL/TP levels, mode badge (LIVE/DRY RUN/PAPER), regime gauge, last 10 candle evaluations.

**Backtest validation fingerprint:**
`EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` → ADX rejected = 513 (10.3%) confirms validated config. Trade count is 67 (not original 88) due to rolling 5000-candle window — expected.

**Restart checklist — restart-with-position is only valid if ALL of the following appear:**
1. `LIVE BALANCE: $XX.XX CAD | position: 0.000113 BTC | source: kraken fetch_balance` — confirms _sync_cash ran
2. `POSITION RECOVERED: 0.000113 BTC @ $88,870.20 — state machine set to LONG` — confirms seeding ran; **if this line is absent, SL/TP and SELL exit are broken**
3. Dashboard and terminal display show position (not "no open position") and state=LONG
4. Balance line alone is insufficient — a missing POSITION RECOVERED line means PositionManager and/or TradingStateMachine were not seeded and the position is unprotected

**Next steps:**
1. Confirm fee-dict structure on next fill — determine if 0.80% is real CAD surcharge or extraction bug
2. Investigate maker orders on Kraken as fee lever (limit orders → 0.16%)
3. Hold current position through SL/TP; do not change params mid-trade
4. After position closes: compare live fill vs backtest metrics (win rate, fees, hold time)
5. Once fee path below 0.20% is confirmed: consider ETH expansion

**Deferred:** Multi-asset trading design discussed; requires single-symbol validation + fee fix first.
