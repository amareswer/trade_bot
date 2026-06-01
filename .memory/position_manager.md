---
name: position-manager
description: "PositionManager — weighted avg entry, unrealized/realized PnL, trade history. Separate from PaperExecutor cash ledger."
metadata: 
  node_type: memory
  type: project
  originSessionId: dbbe3778-3f4b-4e87-860a-0551e0a6dee6
---

`bot/portfolio/position_manager.py` — position accounting module.

**Why:** PaperExecutor used simple last-buy cost basis (not weighted average). PositionManager adds proper weighted avg entry, accurate PnL, and full trade history.

**How to apply:** Update both PaperExecutor AND PositionManager after every fill (they track different things — keep them in sync via main.py).

## Separation of concerns

| Module | Owns |
|---|---|
| `PaperExecutor` | cash ledger, order lifecycle, order history, `executor.portfolio` (used by risk manager) |
| `PositionManager` | position quantity, weighted avg entry, unrealized/realized PnL, trade history |

## Key methods

```python
pm.on_buy(price, quantity)        # updates weighted avg entry
pm.on_sell(price, quantity)       # returns realized PnL float
pm.unrealized_pnl(current_price)  # (price - avg_entry) * qty
pm.position_value(current_price)  # qty * price
pm.has_position                   # bool
pm.quantity                       # float
pm.avg_entry                      # float
pm.realized_pnl                   # float
pm.history                        # list[TradeRecord]
```

## main.py wiring (after fill)

```python
if order.side == OrderSide.BUY:
    position_manager.on_buy(order.price, order.quantity)
else:
    pnl = position_manager.on_sell(order.price, order.quantity)
```
