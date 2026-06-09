---
name: progress-current
description: "Current stage and what is in progress"
metadata:
  type: project
---

**Status as of 2026-06-09:** LiveExecutor fully hardened. All hardening items built and test-covered. Bot is paused (DRY_RUN needs to be confirmed before next launch).

**What's running (when launched):** `python -m bot.main` — live Kraken BTC/CAD feed, 4h candles via `_fetch_completed_candle()`, IndicatorStrategy (EMA crossover + RSI + ADX≥15 filter), full risk gate, LiveExecutor.

**Active .env config (as of 2026-06-09):**
- `EXCHANGE=kraken`, `SYMBOL=BTC/CAD`
- `ADX_THRESHOLD=15.0` (validated config — see CLAUDE.md)
- `LIVE_TRADING=true`, `DRY_RUN=false` ← **WARNING: real orders will be placed on next launch**
- `RISK_PER_TRADE_PCT=0.10` — intentional at $100 CAD capital. Kraken min order is 0.00005 BTC (~$4.50 CAD); 2% of $100 = $2 would be rejected every time. The backtest-validated config (2%) requires ~$500 minimum capital. Revisit when capital grows.
- `STARTING_CASH=100.0` — matches actual Kraken CAD balance; used as fallback if `fetch_balance` fails
- `RISK_MAX_POSITION_PCT=0.10` — equal to RISK_PER_TRADE_PCT; first BUY passes (10% ≤ 10%), subsequent BUY before a SELL is blocked. Normal in single-position cycle.
- `RISK_DAILY_LOSS_LIMIT=0.01`, `RISK_MAX_DRAWDOWN=0.05`
- `AI_ENABLED=false`

**ADX note:** validated config uses ADX_THRESHOLD=15.0. Backtest fingerprint: running `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` should produce ADX rejected = 513 (10.3%). Confirmed 2026-06-09: ADX=513 ✓, trades=67, PF=1.27. Trade count drifted from original 88 to 67 as new candles replaced old at the 5000-candle tail — this is expected and not a regression.

**LiveExecutor hardening completed 2026-06-09:**
All items from the known-gaps list are now implemented:
- ✅ `_validate_order()` — min amount/cost check against `load_markets()`, runs in dry-run too
- ✅ `load_markets()` at init, fail-fast in live mode
- ✅ fetch_order polling (3 polls, partial-fill fallback with loud WARNING)
- ✅ `_sync_cash()` — fetches `free[CAD]` from Kraken, warns on wrong currency, falls back to `STARTING_CASH`
- ✅ `_save_state()` / `_load_state()` — `logs/live_state.json` persists cash/position/cost_basis/pnl across restarts; symbol mismatch guard
- ✅ Fee deduction — reads `raw['fee']['cost']`; skips if fee currency ≠ quote (warns); deducts from cash if quote currency
- ✅ `_fills` / `_rejects` lists — `filled_orders()` and `rejected_orders()` return real history
- ✅ `reset()` fixed — restores `_starting_cash` (not zero)
- ✅ Imports `Order/OrderSide/OrderStatus/Portfolio` from `executor.py` — eliminates enum type mismatch with `main.py`
- ✅ `test_live_executor.py` — 11 tests, 30/30 total pytest passing

**Before next launch:**
1. Confirm `DRY_RUN=true` or `DRY_RUN=false` is intentional in `.env`
2. Verify actual Kraken CAD balance matches `STARTING_CASH=100.0` (or update it)
3. If restarting after trades: `logs/live_state.json` will be loaded automatically; Kraken balance synced via `_sync_cash()` on startup

**Deferred:** Multi-asset trading — design discussed, not built. Requires validated single-symbol live trading first.
