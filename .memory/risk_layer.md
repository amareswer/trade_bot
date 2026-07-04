---
name: risk-layer
description: "RiskManager design — 5 checks, approval gate, SELL bypass rules, config knobs, daily state reset"
metadata: 
  node_type: memory
  type: project
  originSessionId: 7b844a82-ebf8-4841-a759-7f5d46ebbd65
---

`bot/risk/risk_manager.py` — approval gate that sits between strategy and executor.

**Why:** Prevent unsafe trades without modifying strategy or executor logic. Single responsibility: say yes or no with a reason.

**How to apply:** Every non-HOLD signal must pass `risk.evaluate()` before `executor.execute()` is called. After a confirmed FILLED order, call `risk.record_fill()`.

## 5 checks (run in order, first failure wins)

| # | Check | Applies to | Config key | Default | Blocks when |
|---|---|---|---|---|---|
| 1 | Manual halt | BUY + SELL | `RiskConfig.halt` | `False` | `True` |
| 2 | Max drawdown | **BUY only** | `max_drawdown_pct` | `5%` | portfolio down ≥ 5% from all-time peak |
| 3 | Daily trade cap | **BUY only** | `max_trades_per_day` | `3` | fills_today ≥ limit |
| 4 | Daily loss limit | **BUY only** | `daily_loss_limit_pct` | `1%` | portfolio down ≥ 1% from day-open |
| 5 | Max position size | **BUY only** | `max_position_pct` | `3%` | BUY would push position > 3% of total portfolio |

**SELL always bypasses checks 2–5.** Only the manual halt (check 1) can block a SELL. You must always be able to exit an open position.

**Why this matters:** If daily loss or drawdown fires while you hold a position, blocking SELL traps you in a losing trade. The fix (2026-05-30) added `if signal == Signal.BUY` guards to checks 2 and 4 — same pattern check 3 already used.

## Conservative defaults (tightened 2026-05-30)

| Setting | Before | After |
|---|---|---|
| `RISK_PER_TRADE_PCT` | 1% | **0.5%** |
| `RISK_MAX_POSITION_PCT` | 5% | **3%** |
| `RISK_DAILY_LOSS_LIMIT` | 2% | **1%** |
| `RISK_MAX_DRAWDOWN` | 10% | **5%** |
| `RISK_MAX_TRADES_PER_DAY` | 5 | **3** |

## Return type: ApprovalResult
- `approved: bool` — truthy
- `message: str` — human-readable reason
- `block_reason: BlockReason` — enum: HALT | MAX_DRAWDOWN | DAILY_TRADE_CAP | DAILY_LOSS | POSITION_SIZE

## Daily state
- Day-open portfolio value set on first tick each day
- `_fills_today` and `_day_open_value` reset automatically at midnight
- Peak value never resets — max drawdown is all-time
- Manual: `risk.halt()` / `risk.resume()` at any point

## Interface pattern
```python
approval = risk.evaluate(signal, price, executor.portfolio, trade_qty)
if not approval:
    block_reason = approval.message
    # display warning, skip execution
else:
    order = executor.execute(signal, price, quantity=trade_qty)
    if order and order.status == OrderStatus.FILLED:
        risk.record_fill()
```

## 2026-07-03 hardening + multi-coin support

- **State persistence:** pass `state_path` (live mode uses `logs/risk_state.json`) — peak value,
  day-open, and daily fill counts survive restarts. Daily counters restore only if saved on the
  same UTC day; peak always restores. Backtests pass no path → stateless, unchanged.
- **UTC daily reset:** `_utc_today()` replaced local `date.today()` — counters reset at UTC
  midnight, matching candles and the daily P&L alert.
- **Manual kill-switch:** `touch logs/HALT` engages halt via `_check_halt_flag()` in bot/main.py
  (checked every tick); `rm logs/HALT` resumes. Telegram alert on engage/lift. SL/TP exits still
  fire during halt unless `RISK_HALT_BLOCKS_STOPS=true`.
- **Multi-coin (keyword-only, defaults preserve old behavior):**
  `evaluate(..., account_value=X, symbol=s)` — daily-loss/drawdown/peak use the aggregate
  account value (`_account_value()` in bot/main.py sums all slots); position-size check stays
  per-slot. `record_fill(symbol)` + `fills_today_for(symbol)` give each symbol its own daily
  trade cap. Backtest engine uses the old positional signature — numerically identical.
- **Halt semantics correction:** manual halt blocks BUY + strategy SELL (not "SELL always allowed"
  as older notes said); the SL/TP path in bot/main.py bypasses the risk gate entirely, so stops
  always fire. Tested in test_risk_manager.py + test_halt_flag.py.
