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
