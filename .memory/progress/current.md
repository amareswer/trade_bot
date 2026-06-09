---
name: progress-current
description: "Current stage and what is in progress"
metadata:
  type: project
---

**Status as of 2026-06-09:** LiveExecutor built and wired. Bot is running in DRY_RUN mode against Kraken while hardening work is completed.

**What's running:** `python -m bot.main` — live Kraken BTC/CAD feed, 4h candles fetched directly via `_fetch_completed_candle()` (polls completed OHLCV candles from exchange; replaced the old CandleAggregator approach), IndicatorStrategy (EMA crossover + RSI + ADX≥15 filter), full risk gate, LiveExecutor in DRY_RUN mode.

**Active .env config (as of 2026-06-09):**
- `EXCHANGE=kraken`, `SYMBOL=BTC/CAD`
- `ADX_THRESHOLD=15.0` (validated config — see CLAUDE.md)
- `LIVE_TRADING=true`, `DRY_RUN=true`
- `RISK_PER_TRADE_PCT=0.02` (corrected to validated config value)
- `RISK_DAILY_LOSS_LIMIT=0.01`, `RISK_MAX_DRAWDOWN=0.05`
- `AI_ENABLED=false`

**ADX note:** current.md previously recorded `ADX_THRESHOLD=30.0` — this was stale. The validated config (CLAUDE.md, 2026-06-05) and actual `.env` both say **15.0**. 30 was an earlier experiment.

**Risk note:** `.env` previously had `RISK_PER_TRADE_PCT=0.10` and `DRY_RUN=false`. This conflicted with the validated config (0.02) and predated LiveExecutor hardening. Corrected 2026-06-09 before resuming live testing.

**LiveExecutor built 2026-06-07:** `bot/execution/live_executor.py`. Wired into `main.py` via `cfg.exchange.live_trading` flag. See [[feature-plan]] for known gaps.

**Next step:** Complete LiveExecutor hardening — balance sync, min order size validation, fee deduction, restart recovery — then validate in DRY_RUN over multiple candle cycles before re-enabling real orders (`DRY_RUN=false`).

**Deferred:** Multi-asset trading — design discussed, not built. Requires validated single-symbol live trading first. See [[feature-plan]] for design notes.
