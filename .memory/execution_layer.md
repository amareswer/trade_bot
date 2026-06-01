---
name: execution-layer
description: "PaperExecutor design — Order lifecycle, Portfolio state, P&L tracking, rejection rules"
metadata: 
  node_type: memory
  type: project
  originSessionId: 7b844a82-ebf8-4841-a759-7f5d46ebbd65
---

`bot/execution/executor.py` — paper trading engine. No real money, no exchange calls.

**Why:** Needed a realistic order lifecycle (not just print statements) with cash guards and P&L so the bot behaves like a real system.

**How to apply:** Always use `PaperExecutor` in the main loop. `simulated_executor.py` is the old simple version — leave it in place but don't wire it into main.

## Key types

| Type | Role |
|---|---|
| `Order` | Immutable record: id, side, qty, price, status, timestamps, total_value |
| `OrderStatus` | `PENDING → FILLED or REJECTED` (also `CANCELLED` for future use) |
| `OrderSide` | `BUY / SELL` |
| `Portfolio` | cash, position (units), realized_pnl, unrealized_pnl(), total_value() |
| `PaperExecutor` | entry point — `execute(signal, price)` returns `Order or None` |

## Rejection rules (inside executor, before risk layer)
- BUY: rejected if `cash < order.total_value`
- SELL: rejected if `position < trade_qty`
- HOLD: returns `None`, no order created

## Portfolio property
`executor.portfolio` exposes the `Portfolio` object — needed by `RiskManager.evaluate()`.

## P&L model
- `realized_pnl`: credited on each SELL as `(sell_price - cost_basis) * qty`
- `unrealized_pnl(price)`: `(price - cost_basis) * position`
- Cost basis uses **weighted average** across all BUYs — `(prev_cost + new_price * qty) / new_total_qty`
- `PositionManager` is the authoritative display P&L source; `Portfolio._cost_basis` is kept consistent with it

## Float safety
- SELL position check uses `position < order.quantity - 1e-9` (not strict `<`) to handle float drift
- `PositionManager` uses `< 1e-9` instead of `== 0.0` when zeroing quantity/avg_entry
