---
name: progress-current
description: "Current stage and what is in progress"
metadata:
  type: project
---

**Status as of 2026-05-31:** All 6 phases complete. In paper trading validation period.

**What's running:** `python -m bot.main` — live Binance BTC/USDT feed, 4h candles via CandleAggregator, IndicatorStrategy (EMA crossover + RSI + ADX≥30 filter), full risk gate, PaperExecutor.

**Last built (2026-05-30):** CandleAggregator — accumulates 30s live ticks into real 4h OHLCV candles so ADX/RSI work correctly in live mode. Fixes the bug where live bot never traded.

**Active config:** `EXCHANGE=binance`, `SYMBOL=BTC/USDT`, `CANDLE_MINUTES=240`, `ADX_THRESHOLD=30.0`, `STOP_LOSS_PCT=0.02`, `TAKE_PROFIT_PCT=0.04`, `AI_ENABLED=false`.

**Next step:** Paper trade for 2–4 weeks and verify signals look sensible. Do not change strategy settings until after this run.

**Deferred:** Multi-asset trading — design discussed, not built. Requires validated single-symbol strategy first. See [[feature-plan]] for design notes.

**Pending user action:** Rotate OpenRouter API key — was in plaintext `.env` at security audit (2026-05-28).
