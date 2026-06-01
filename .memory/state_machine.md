---
name: state-machine
description: "TradingStateMachine — IDLE/LONG/COOLDOWN states, position-aware signal filtering, deduplication, cooldown countdown"
metadata: 
  node_type: memory
  type: project
  originSessionId: dbbe3778-3f4b-4e87-860a-0551e0a6dee6
---

`bot/state/trade_state.py` — position-aware trading state machine. Sits between strategy and risk engine.

**Why:** Bot was sending SELL with no position, duplicate BUY/SELL signals, and had no trade cooldown. State machine fixes all three in one place.

**How to apply:** Always call in this order each tick:
1. `state_machine.tick()` — advance cooldown
2. `state_machine.filter_signal(raw)` — returns `(filtered_signal, reason)`
3. risk gate
4. `state_machine.on_fill(action, price)` — after confirmed fill

## States

| State | Meaning | Allowed signals |
|---|---|---|
| `IDLE` | No open position | BUY or HOLD |
| `LONG` | Active position exists | SELL or HOLD |
| `COOLDOWN` | Locked after trade | HOLD only |

## Transitions

```
IDLE + BUY filled  → LONG
LONG + SELL filled → COOLDOWN (cooldown_ticks candles)
COOLDOWN exhausted → IDLE
```

## filter_signal() rules

- `IDLE + SELL`  → `HOLD "no active position to sell"`
- `LONG + BUY`   → `HOLD "position already open"`
- `COOLDOWN`     → `HOLD "cooldown active — N candles remaining"`
- same signal as last_action → `HOLD "duplicate — market state unchanged"`

## Config

```python
COOLDOWN_TICKS = 5   # in main.py — candles locked after each trade
```
At 30s ticks: 5 = 2.5 min cooldown. Range: 1 (aggressive) to 10 (conservative).

## Key properties

- `state_machine.state` — current `TradingState` enum
- `state_machine.cooldown_remaining` — ticks left in cooldown
- `state_machine.last_trade_label` — e.g. "BUY @ $74,250.00"
- `state_machine.history` — list of `TradeEvent` records
