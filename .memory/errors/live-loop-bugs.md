---
name: live-loop-bugs
description: "Three continue-path bugs and a logging misconfiguration found during Jun 10–12 live trading"
metadata:
  type: project
---

All four bugs found and fixed 2026-06-10 to 2026-06-12 during the first live run.

**Why:** The main loop uses `continue` to skip candle evaluation when no new 4h candle is ready. Several critical checks were placed AFTER that `continue`, meaning they only ran at 4h closes rather than every 30s tick.

---

## Bug 1: Intra-candle SL/TP never fired

**Symptom:** Open position at $88,870 with SL at $87,093 had no intra-candle protection. If price dropped sharply and recovered within a 4h window, the stop would never trigger.

**Root cause:** Step 3b (SL/TP check) was placed after the `if candle is None: … continue` block. The `continue` jumped past it on every no-candle tick (~3h 59m of every 4h cycle).

**Fix:** Intra-candle SL/TP block moved BEFORE the candle-availability check. When triggered, executes full pipeline (risk → execute → position_manager → display.fill) then `continue`s. Old step 3b retained as belt-and-suspenders at candle close. `_ic_approval.approved` (explicit) used instead of implicit bool.

**How to apply:** Any check that must run every 30s tick must be placed before the `if candle is None: continue` block. The same pattern applies to any future per-tick guard.

---

## Bug 2: Dashboard only rendered at 4h candle closes

**Symptom:** `dashboard.html` last-updated timestamp stuck at the candle-close time. Price/portfolio not refreshing between closes.

**Root cause:** Same `continue` pattern. Section 12 (tick_log + dashboard write) was after the candle-availability guard. Dashboard only rendered when a new candle evaluated — every 4 hours.

**Fix:** Extracted `_render_dashboard(sig, rsi_v, trend_v)` helper inside `run()`. Called in:
- No-candle branch (every 30s tick): uses sticky signal/indicator vars (`_dash_signal`, `_dash_rsi`, `_dash_trend`) that hold last-known values between closes
- SL/TP branch: calls with `"SELL"` immediately after position exit
- Section 12 (candle-close): updates stickies first, then calls with fresh values

Sticky vars initialized to `"HOLD"` / `None` / `None` before the loop. `_render_dashboard` is wrapped in `try/except → logger.warning` so renderer bugs log at WARNING instead of crashing the bot.

**How to apply:** Any display or side-effect that should happen every 30s must be in `_render_dashboard` or called before the candle guard.

---

## Bug 3: Position-size drift — first live BUY blocked

**Symptom:** First live BUY blocked with `10.03% > 10%` despite `RISK_PER_TRADE_PCT = RISK_MAX_POSITION_PCT = 0.10`.

**Root cause:** `calc_trade_qty` calls `round(qty, 6)`. At BTC price ~$88,761: exact qty = 0.00011265..., rounded = 0.000113. Risk check: 0.000113 × $88,761 = $10.030 → 10.030% > 10.000% → BLOCKED. The rounding error adds up to $0.048 at BTC prices ~$90k. An equal limit is always inside that band.

**Fix:** Raised `RISK_MAX_POSITION_PCT=0.15` in `.env`. Added startup warning in `config.log_startup()`: fires when `max_position_pct ≤ risk_per_trade_pct × 1.05`, explaining the mechanism and minimum safe value.

**How to apply:** Never set `RISK_MAX_POSITION_PCT = RISK_PER_TRADE_PCT`. Keep at least 5% headroom (e.g. 10% per-trade → 15% max-position). The startup warning catches this automatically on next launch.

---

## Bug 4: Root logger level swallowed all INFO records

**Symptom:** `logs/trade_bot.log` contained only WARNING-level lines. LiveExecutor INFO events (balance sync, state save/restore, markets loaded) never appeared. No live trading events recorded at INFO level.

**Root cause:** `logging.basicConfig(level=logging.WARNING, …)` set the root logger level to WARNING. Even though the file handler was configured at INFO, the root logger filtered records before they reached any handler. INFO records were dropped at the root level, never seen by any handler.

**Fix:** Rebuilt logging in `main.py`: `root_logger.setLevel(INFO)`, `console_handler.setLevel(WARNING)`, `file_handler.setLevel(INFO)`. Root at INFO, console stays clean, file captures everything. Also upgraded live-critical LiveExecutor events to WARNING (balance sync, state save/restore, fee deduction, order placement) so they survive even a misconfigured root level.

**How to apply:** In any Python project, the root logger level is a gate — it must be ≤ the lowest handler level, or records are silently dropped before handlers see them.
