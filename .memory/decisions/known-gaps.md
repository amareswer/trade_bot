---
name: known-gaps
description: Known bugs and inconsistencies that are logged but not yet fixed — prevents re-discovering from scratch
metadata:
  type: project
---

Logged 2026-07-01. Updated 2026-07-02 (Session 3 verification).

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

## 5. XRP/CAD walk-forward fails with current strategy — RESOLVED 2026-07-02

**Where:** Live bot `bot/main.py`, config UNIVERSE_WHITELIST
**Symptom:** Walk-forward re-run on XRP/USDT (Binance proxy) with current code (Mode A/B + EMA filter):
  - 5000c PF **0.99** (62 trades, 12.9% win rate, 54/62 exits at SL)
  - 3000c PF **0.98** (31 trades, 12.9% win rate)
  - 1000c PF 1.33 (6 trades — too small to be meaningful)
  Two of three windows fail (PF < 1.0). Walk-forward FAILS for XRP on current strategy.
**Root cause:** Original XRP validation was done on the OLD strategy (simple RSI < 30 BUY gate).
  Current strategy (Mode A/B: pullback RSI 38–58 + breakout) doesn't have edge on XRP.
**Decision (2026-07-02):** XRP/CAD removed from UNIVERSE_WHITELIST and moved to watchlist.
  - `.env`: UNIVERSE_WHITELIST=BTC/CAD, MAX_CONCURRENT_POSITIONS=1, UNIVERSE_SIZE=1, STARTING_CASH=100
  - `regime_monitor.py`: XRP/CAD moved to MONITOR_WATCHLIST (health metrics, labeled NOT TRADED)
  - CLAUDE.md: XRP → WATCHLIST with exact basis text
  - Re-entry gate: full 3-window walk-forward pass on current strategy code

## 4. ATR SL config drift — RESOLVED 2026-07-02

**Where:** `config.py` BacktestConfig, `.env`, `bot/backtest/engine.py`, `backtest.py`
**Root cause:** Two independent ATR SL config systems existed:
  - `StrategyConfig` (live bot): reads `ATR_SL_MULT` — with .env `ATR_SL_MULT=0.0`, live was correct
  - `BacktestConfig` (backtest only): reads `ATR_SL_ENABLED` + `ATR_SL_MULTIPLIER` (not in .env.example)
  With `ATR_SL_ENABLED=true` in .env, backtest ran ATR SL at 2× → 33 trades / PF 2.19 (drift from validated 58 / 1.79)
**Live impact:** 1 fill at 2026-06-22 16:36 UTC under ATR config (pnl=-0.02 CAD, reason='trail_stop')
**Fix applied 2026-07-02:**
  - Removed `atr_sl_enabled`/`atr_sl_multiplier` from BacktestConfig; added `atr_sl_mult` (reads same key as StrategyConfig)
  - Removed `ATR_SL_ENABLED=true` from .env
  - Updated engine.py and backtest.py to use `atr_sl_mult` (0 = disabled convention)
  - Added startup drift guard in config.py: warns on unrecognised strategy-critical env keys
  - CLAUDE.md fingerprint updated: now expect ~39 trades / PF 1.79 (count lower due to EMA spread filter added 2026-06-27)
**Verified:** backtest gives PF 1.79, 12/12 tests pass

---

## 6. trades.db missing BUY fills + fee_cost columns — RESOLVED 2026-07-03

**Where:** `bot/data/trade_log.py`, `bot/execution/executor.py`, `bot/execution/live_executor.py`, `bot/main.py`
**Root cause A (no fee capture):** `trades.db` had no `fee_cost`/`fee_currency` columns. Fee dict was
  logged as WARNING text only — not persisted.
**Root cause B (5 Kraken trades missing):** The 3 original DB rows (2 phantom, 1 ATR trail_stop SELL)
  were missing 6 Kraken fills (3 BUYs + 3 SELLs from Jun 12–Jun 27). Phantom rows had qty=0 because
  executor SELL fired when bot state machine had no position (state machine mismatch, not real fills).
**Fixes applied 2026-07-03:**
  - `executor.py` Order dataclass: added `fee_cost: float = 0.0` and `fee_currency: str = ""` fields
  - `live_executor.py`: Order now carries fee_cost/fee_currency from exchange fill response
  - `trade_log.py`: added `fee_cost`/`fee_currency` columns + in-place migration (ALTER TABLE ADD COLUMN)
  - `bot/main.py`: all 3 log_fill() calls now pass fee_cost/fee_currency from order
  - `shadow_signal.py`: fill fidelity shows `actual_fee=X.XX%` when fee_cost is populated (falls back to `assumed_fee`)
  - `reconcile_ledger.py`: one-shot reconciliation script — fetches Kraken history, marks phantoms, backfills orphan fills
**Kraken reconciliation result (2026-07-03):**
  - True Kraken balance: 154.11 CAD (matches live_state files: 2 × 77.05)
  - True realized P&L: -2.20 CAD (Kraken data, all fees included); DB had recorded -0.02
  - 7 Kraken trades total (all BTC/CAD); XRP/CAD: 0 trades
  - 2 phantom rows marked (qty=0 SELLs fired without position): id=2 (BTC/CAD), id=3 (DOGE/CAD)
  - 6 orphan Kraken trades backfilled with source='kraken_backfill'
  - 1 SELL (Kraken id=TEYLVF, Jun 27 0.000378 BTC) — RESOLVED: came from pre-existing BTC deposit
    to Kraken wallet before the bot's first trade. BTC flow reconciled to 0 unexplained satoshis.
  - Pre-existing assets found: 0.000377 BTC (sold Jun 27), 218 DOGE (sold Jun 30), 0.034 ETH (still held)
  - Funding source: $100 CAD Interac E-Transfer deposit on 2026-06-07
  - Report: logs/reconciliation_20260703.md (updated 2026-07-03 Session 4)

## 7. 58% bot downtime — DIAGNOSED + MITIGATED 2026-07-03

**Root cause:** Bot was running locally on Mac via caffeinate. Mac sleep and manual stops
  between dev sessions caused all downtime. No automatic restart mechanism on Mac.
**Systemd fix (deploy/trade_bot.service):**
  - `Restart=on-failure` → `Restart=always`
  - `StartLimitIntervalSec=300` + `StartLimitBurst=5` → `StartLimitIntervalSec=0` (never give up)
  - With old config: systemd permanently stopped restarting after 5 crashes in 5 min with no alert.
**In-process crash-loop detection added (bot/main.py):**
  - `_record_startup_and_check_crash_loop()`: writes startup timestamps to logs/startup_timestamps.txt
  - Fires `alerter.error()` if 3+ restarts in 5 minutes
**Candle watchdog extracted to `_check_candle_watchdog()` helper (bot/main.py):**
  - Unit-testable with mocked clock
  - 5 new tests in test_candle_watchdog.py — all PASS
**deploy/UPTIME.md written:** How to check uptime, what each alert means, systemd commands.
**Resolution:** Only full fix is VPS deployment with systemd service. Mac-local running will always
  have downtime from sleep/manual stops.
**Tests:** 114/114 PASS (was 109 before this session)

## 8. qty=0 phantom SELL fills + fill-confirmation drift — RESOLVED 2026-07-03

**Root cause (BUG 1 — phantom rows):** In `live_executor.execute()`, a SELL market order's
  immediate Kraken response returns `filled=0` before settlement. The poll loop uses
  `float(last_raw.get("filled") or filled_qty)` which keeps `filled_qty=0` if Kraken always
  returns 0 in the filled field (even on a closed order). This propagated `quantity=0` to
  `Order.quantity`, then `on_sell(price, 0)` was a no-op in PositionManager, and
  `log_fill(quantity=0)` wrote a phantom row to trades.db.
**Fix (BUG 1):**
  - `bot/execution/live_executor.py`: After poll loop, if `side==SELL and quantity<=0`:
    - If `status=="closed"` and `amount>0`: use `amount` as fill qty (closed market SELL = fully filled)
    - If not closed or amount=0: log error and return None — do not create an Order
  - `bot/data/trade_log.py`: `log_fill(quantity<=0)` raises ValueError — prevents phantom rows at the DB layer
**Root cause (BUG 2 — drift never resolves):** After the phantom SELL (qty=0), PositionManager
  showed pos=0.000378 while exchange had 0. Drift check every 120 ticks called `alerter.error()`
  on EVERY detection — firing for hours with no escalation logic.
**Fix (BUG 2):**
  - `config.py`: Added `drift_alert_threshold: int = 3` to ExchangeConfig (env `DRIFT_ALERT_THRESHOLD`)
  - `bot/main.py`: Added `_drift_consecutive_count`. Drift warnings fire immediately; `alerter.error()`
    only after N consecutive drift checks (then counter resets). Drift resolution logs info + resets counter.
**Tests:** 11 new tests — all PASS. `test_fill_recording.py` (5 tests), `test_drift_escalation.py` (6 tests)
  Total suite: 110 PASS.
