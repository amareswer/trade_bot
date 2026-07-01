---
name: known-gaps
description: Known bugs and inconsistencies that are logged but not yet fixed — prevents re-discovering from scratch
metadata:
  type: project
---

Logged 2026-07-01. All items below are confirmed real issues, not hypothetical.

**Why:** Saves re-investigation time; each took real debugging to surface.
**How to apply:** Check here before assuming a symptom is new.

---

## 1. BUY fills not written to trades.db

**Where:** `bot/data/trade_log.py` — `log_fill()` call sites in `bot/main.py`
**Symptom:** `live_comparison.py` and `trades.db` queries show only SELL fills; BUY side is missing.
**Why:** `log_fill()` is called in the SELL path but the BUY path (around the executor.buy() return) does not call it.
**Status:** Not fixed. Audit `bot/main.py` around the BUY execution block and add a symmetric `trade_log.log_fill()` call.

---

## 2. Regime monitor rolling PF omits MIN_EMA_SPREAD_PCT filter

**Where:** `regime_monitor.py` — `compute_rolling_pf()` lines 168–230
**Symptom:** Rolling PF entry conditions are ADX + EMA cross + RSI < 30 only. The live bot also requires EMA spread ≥ 0.4% (`MIN_EMA_SPREAD_PCT=0.004`) before entering.
**Effect:** Rolling PF simulation is slightly more permissive than the live strategy. A "PF looked good in regime monitor but live bot didn't trigger" situation is explained by this gap.
**Status:** Minor, non-urgent. Fix: add `ema_spread ≥ MIN_EMA_SPREAD_PCT` check inside the `is_buy` block.

---

## 3. live_state.json (no symbol suffix) is dead code

**Where:** Project root `logs/live_state.json`
**Symptom:** Bot now writes per-symbol state to `logs/live_state_BTC_CAD.json` and `logs/live_state_XRP_CAD.json`. The legacy `live_state.json` is never read or written.
**Risk:** None — it's inert. But it could confuse a future reader (or a redeploy that accidentally restores it).
**Status:** Safe to archive or delete. Confirm `grep -r "live_state.json" bot/` returns no active read paths first.
