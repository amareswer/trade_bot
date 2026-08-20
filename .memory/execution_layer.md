---
name: execution-layer
description: "PaperExecutor + LiveExecutor design — Order lifecycle, Portfolio state, P&L tracking, fees, state persistence"
metadata:
  type: project
---

Two executors share an identical interface. `main.py` selects between them via `cfg.exchange.live_trading`.

## LiveExecutor — `bot/execution/live_executor.py`

Built 2026-06-07. Hardened 2026-06-09/10. Places real market orders on Kraken via ccxt.

**Activation:** `LIVE_TRADING=true` in `.env`. Use `DRY_RUN=true` first for candle-by-candle validation.

**All hardening items complete (as of 2026-06-09):**

| Feature | Method | Notes |
|---|---|---|
| Balance sync | `_sync_cash()` | Fetches `free[quote]` from exchange on init; warns if currency absent; falls back to `STARTING_CASH` |
| State persistence | `_save_state()` / `_load_state()` | JSON at `logs/live_state.json`; saves cash, position, cost_basis, realized_pnl, fees_paid; symbol mismatch guard on load |
| Order validation | `_validate_order()` | Checks `limits.amount.min` and `limits.cost.min` from `load_markets()`; runs in dry-run too; rejection message states minimum viable trade in quote currency |
| Market data | `load_markets()` at init | Public endpoint; fail-fast in live mode if unavailable |
| Order polling | `fetch_order` × 3 polls | After `create_order`; uses last `filled` amount on timeout (partial fill); loud WARNING if not closed |
| Fee deduction | From `raw['fee']` | Deducts if `fee.currency == quote`; logs WARNING and skips if currency mismatch; raw fee-dict logged at WARNING for audit |
| Order history | `_fills` / `_rejects` lists | `filled_orders()` and `rejected_orders()` return real history |
| Fee tracking | `_fees_paid` | Accumulates session fees; persisted in state file; exposed as `fees_paid` property |
| Reset | `reset()` | Restores `_starting_cash`, clears lists |
| Imports | from `executor.py` | `Order`, `OrderSide`, `OrderStatus`, `Portfolio` — no enum type mismatch with main.py |

**Startup init order:**
1. Build ccxt exchange instance
2. `load_markets()` — public endpoint; fail-fast in live mode
3. `_load_state()` — restore position/cost_basis/fees_paid from disk (dry-run also loads)
4. `_sync_cash()` — override cash with real exchange balance (live only); warns if drift > $0.50

**Test coverage:** `test_live_executor.py` — 11 tests, all mocked exchange.

**Known fee finding (2026-06-11):** Actual Kraken fee on first fill was 0.80% (not 0.26% modeled). Raw fee-dict logging added; next fill will reveal true structure. Likely BTC/CAD FX surcharge on top of 0.26% taker.

**Native trailing-stop fix (2026-08-19):** `sync_protective_stop(stop_price, trailing_pct=None)` now
takes an optional `trailing_pct` — when set, places a Kraken `trailing-stop` order
(`params={"trailingPercent": "X.XXXX"}`, ccxt derives the ordertype) instead of the static
`stopLossPrice` order. Why: `ss['native_stop_price']` (bot/main.py) was set once at BUY-fill
and never updated as the software trailing stop's `trail_peak` rose — the native backstop
under-protected exactly when there was profit to protect. **Scoped narrowly**: only matters
when `ss['atr_sl'] == 0` (ATR SL otherwise always wins `_trail_sl_level` in bot/main.py, so a
flat native stop already mirrors it exactly) — with the current live config (`ATR_SL_MULT=2.0`
always set), this path is dormant; `TRAILING_STOP_PCT=0` in `.env` too. Kraken trailing-stop
orders track the peak server-side once placed — no repeated repricing calls needed, only a
one-shot static→trailing swap the instant `trail_peak` arms (`bot/main.py`, guarded by new
`ss['native_stop_is_trailing']` flag). Quantity changes (partial TP, partial fill) still
cancel/replace (`_resync_native_stop()` helper in bot/main.py) — Kraken has no in-place volume
amend via `create_order`; a trailing re-place restarts the tracked peak from the price at
re-placement (accepted precision loss, same as the static order's own per-resize snapshot).
Restart always re-arms **static** regardless of prior kind — `trail_peak`/`atr_sl` already
reset to 0 in-memory every restart, so there's nothing to resume a trailing distance from;
`native_stop_is_trailing` is persisted in the state file for observability only, not decision
logic. **Related pre-existing gap found, not fixed (out of scope):** `ss['native_stop_price']`
is never seeded from the executor's actual resting-stop state on restart recovery — a
quantity-changing event (partial TP) firing before the next BUY fill after a restart would
call `sync_protective_stop(None)`, cancelling whatever's resting without replacing it. Applies
identically to the pre-existing static path; unrelated to this fix. Tests: `test_live_executor.py`,
7 new cases (trailing placement param, priority-over-static, dry-run no-op, cancel, resync-on-
quantity-change, failure-alert, state persist/restore across restart).

---

## PaperExecutor — `bot/execution/executor.py`

Paper trading engine. No real money, no exchange calls.

**How to apply:** Used when `LIVE_TRADING=false`. `simulated_executor.py` is the old simple version — don't wire it into main.

## Key types

| Type | Role |
|---|---|
| `Order` | Immutable record: id, symbol, side, qty, price, status, timestamps, total_value, reject_reason |
| `OrderStatus` | `PENDING → FILLED or REJECTED` (also `CANCELLED`) |
| `OrderSide` | `BUY / SELL` |
| `Portfolio` | cash, position (units), realized_pnl, `_cost_basis`, `unrealized_pnl()`, `total_value()` |

## Rejection rules (inside executor, before risk layer)
- BUY: rejected if cash < order.total_value (PaperExecutor) or exchange error / validation fail (LiveExecutor)
- SELL: rejected if position ≤ 0
- HOLD: returns `None`, no order created

## P&L model
- `realized_pnl`: credited on each SELL as `(sell_price - cost_basis) * qty`
- `unrealized_pnl(price)`: `(price - cost_basis) * position`
- Cost basis: **weighted average** — `(prev_cost + new_price * qty) / new_total_qty`
- `PositionManager` is the authoritative display P&L source; `Portfolio._cost_basis` kept consistent

## Float safety
- SELL position check uses `position < qty - 1e-9` (not strict `<`) to handle float drift
- `PositionManager` uses `< 1e-9` instead of `== 0.0` when zeroing quantity/avg_entry
