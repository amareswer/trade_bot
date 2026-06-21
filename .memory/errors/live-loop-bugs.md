---
name: live-loop-bugs
description: "Five bugs found during Jun 10–12 live trading: continue-path issues, logging misconfiguration, restart state fragmentation"
metadata:
  type: project
---

All five bugs found and fixed 2026-06-10 to 2026-06-12 during the first live run.

**Why (bugs 1–2):** The main loop uses `continue` to skip candle evaluation when no new 4h candle is ready. Several critical checks were placed AFTER that `continue`, meaning they only ran at 4h closes rather than every 30s tick.

**Pattern (bugs 1, 2, 5):** "Component A updated, downstream component B never informed." This has now appeared five times in this project. Any time a new component is wired in or state is persisted, explicitly audit every downstream consumer that reads that state.

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

---

## Bug 5: Restart recovery seeded LiveExecutor only — PositionManager and TradingStateMachine started fresh

**Symptom:** After restart with an open position (0.000113 BTC @ $88,870), the 2026-06-12 08:00 candle close showed state machine IDLE and display "no open position". A duplicate BUY signal fired and was only blocked by the 15% position cap (the risk gate computed 19.08% — executor correctly knew about the position, but nothing else did). The open position had no functioning SL/TP and no exit path at all.

**Root cause:** `LiveExecutor._load_state()` correctly restores position/cash/cost_basis from `logs/live_state.json` on startup. But `PositionManager` and `TradingStateMachine` are always created fresh in `run()` and were never seeded from executor state. Consequences:
- `position_manager.has_position == False` → intra-candle SL/TP gate never fires
- `state_machine.state == IDLE` → `filter_signal(SELL)` returns HOLD ("no active position to sell")
- `state_machine.state == IDLE` → BUY signals pass the state machine filter, only stopped by risk position cap

**Detected at:** 2026-06-12 08:00 UTC candle close. Position had been live and unprotected since the 2026-06-11 20:00 UTC restart, approximately 12 hours.

**Fix:** Added `PositionManager.seed(quantity, avg_entry, realized_pnl)` and `TradingStateMachine.recover_long(entry_price)` — both set internal state without creating fake trade records. In `main.py run()`, after both objects are created, if `cfg.exchange.live_trading and executor.position > 1e-9`: call both seed methods, log WARNING, and print `POSITION RECOVERED` to terminal. Test added: `test_restart_recovery_seeds_position_manager_and_state_machine` asserts all three components consistent (executor.position == pm.quantity, state=LONG, history lists empty).

**How to apply:** Any time LiveExecutor state is persisted and loaded on restart, audit every downstream consumer of that state. Current consumers that must be seeded: PositionManager (for P&L / SL/TP gate), TradingStateMachine (for signal filtering). Future consumers (e.g. RiskManager's peak tracking for max drawdown) may also need seeding.

**Pattern note:** This is the fifth instance of "component A updated, downstream component B never informed" in this project. The pattern appears when components are added incrementally — each component works in isolation but state flow between them is only audited at integration time. Any new component that reads position/cash state must be explicitly seeded on restart.

---

## Bug 6: Backtest trade count regression (62→10) — 2026-06-20

**Symptom:** After risk config hardening commit (`e82ef1c`), backtest dropped from 62 to 10 trades with near-zero PF.

**Root cause:** `RISK_MAX_DRAWDOWN=0.05` in live `.env` (tuned for live $100 account) blocked all new BUYs after ~6 SL losses (~0.85% each ≈ 5.1% cumulative > 5% limit). Live and backtest shared the same `cfg.risk.max_drawdown_pct`. No way to run backtest without applying live risk circuit breakers.

**Fix:** `backtest.py` now has `--max_drawdown` CLI arg (default=0.25). `walkforward.py` + `montecarlo.py` hardcoded to 0.25. Live `.env` RISK_MAX_DRAWDOWN unchanged. Backtest never sees live drawdown limits.

**How to apply:** Backtest circuit breakers must always use generous limits (0.25+). Live risk limits are for capital protection — they are intentionally conservative and will cap trade count in backtests. Keep them separate. Any new risk limit added to `.env` must also have a backtest override.

---

## Bug 7: Partial TP default auto-activated — 2026-06-20

**Symptom:** When wiring partial TP into backtest engine, deriving `partial_tp_pct = take_profit_pct / 2` when the arg was 0 changed the baseline from 62 to 10 trades.

**Root cause:** Dividing TP in half at 50% of position while keeping the state machine in LONG caused trades to exit earlier and at different prices than the validated baseline.

**Fix:** Partial TP only activates when `partial_tp_pct > 0` explicitly. Default=0 means completely disabled. No auto-derivation from take_profit_pct.

**How to apply:** Any new exit mechanism (trailing stop, partial TP, time-based exit) must default to disabled (0 or False) in engine.py so the validated baseline is preserved unless the feature is explicitly enabled.
