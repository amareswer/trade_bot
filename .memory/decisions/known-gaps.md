---
name: known-gaps
description: Known bugs and inconsistencies that are logged but not yet fixed — prevents re-discovering from scratch
metadata:
  type: project
---

Logged 2026-07-01. Updated 2026-07-02 after session audit.

**Why:** Saves re-investigation time; each took real debugging to surface.
**How to apply:** Check here before assuming a symptom is new.

---

## 1. BUY fills not written to trades.db — RESOLVED 2026-07-02

**Where:** `bot/main.py` — unified execute block (~lines 1391–1408)
**Outcome:** Verified RESOLVED. The unified `trade_log.log_fill(side=order.side.value, ...)` call
at lines 1391–1398 handles both BUY and SELL. `OrderSide.BUY.value == "BUY"` confirmed.
`alerter.fill(side=order.side.value, ...)` is also called for BUY in the same block.
**Note:** The known-gap was written before the unified call existed; code was already correct
by the time of this audit. If trades.db shows only SELLs, check live_comparison.py query filters.

---

## 2. Regime monitor rolling PF omits MIN_EMA_SPREAD_PCT filter — RESOLVED 2026-07-02

**Where:** `regime_monitor.py` — `compute_rolling_pf()` is_buy block
**Fix:** Added `ema_spread_val >= MIN_EMA_SPREAD_PCT` to the is_buy condition.
Also added: when trade_count == 0, PF shows "N/A (no signals in window)" instead of WARN,
and the verdict excludes PF from the pass/fail count, reporting "X/3 measurable conditions met".

---

## 3. live_state.json (no symbol suffix) is dead code — ADDRESSED 2026-07-02

**Where:** `bot/execution/live_executor.py` — `_DEFAULT_STATE_PATH` constant (line ~28)
**Status:** Confirmed no active readers — every LiveExecutor call in bot/main.py passes an
explicit per-symbol path (logs/live_state_BTC_CAD.json etc.). Added comment to live_executor.py
clarifying this is a legacy fallback never reached at runtime. The file logs/live_state.json
(if it exists on disk) is inert — safe to delete.

---

## 4. Backtest fingerprint in CLAUDE.md is stale — discovered 2026-07-02

**Where:** CLAUDE.md "How to verify the config is active" section
**Symptom:** Running `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` gives 33 trades /
PF 2.19 / WR 42.4%, not the ~58 trades / PF 1.79 / WR 32.8% stated in CLAUDE.md.
**Why:** The .env now has `ATR_SL_ENABLED=true` with the backtest reading `ATR_SL_MULTIPLIER`
(default 2.0 since .env uses `ATR_SL_MULT=0.0` which maps to a different env key). ATR-based
SL changes exit mix and trade count. The fingerprint in CLAUDE.md was validated without ATR SL.
**Risk:** None — my changes (Tasks 1–6) were confirmed NOT to have caused this divergence
(baseline before/after my changes both give 33 trades).
**Action needed:** Re-run validation with ATR_SL_ENABLED=false to restore the 58-trade baseline,
or accept the new fingerprint. Do not change strategy params to restore old number.
