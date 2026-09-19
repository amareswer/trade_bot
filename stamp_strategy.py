"""
stamp_strategy.py — record the current strategy code hash as the validated baseline.

Run this immediately after a passing walk-forward to mark the current code
as the version whose results are trusted.  The live bot and backtest.py will
emit a loud WARNING if strategy code diverges from this stamp.

Also stamps the execution/config fingerprint (2026-09-18 follow-up review
finding, P2: compute_execution_hash() existed with no production caller —
wired into config.py's own startup validation, which needs this file to
compare against, same as the strategy hash). This is a SEPARATE identity
from the strategy hash — it can drift (a config change: order type, ATR
sizing, a risk threshold) without the strategy code itself changing at all,
and is not itself a walk-forward re-validation trigger the way the strategy
hash is; it's informational drift visibility only.

Usage:
    python stamp_strategy.py

Optional env overrides:
    STRATEGY_HASH_FILE=/custom/path/validated_strategy_hash python stamp_strategy.py
    EXECUTION_HASH_FILE=/custom/path/validated_execution_hash python stamp_strategy.py
"""
import os
from pathlib import Path

from bot.strategy.fingerprint import compute_execution_hash, compute_strategy_hash
from config import cfg

hash_file = Path(os.getenv("STRATEGY_HASH_FILE", "logs/validated_strategy_hash"))
hash_val  = compute_strategy_hash()

hash_file.parent.mkdir(parents=True, exist_ok=True)
hash_file.write_text(hash_val + "\n")

print(f"Strategy hash stamped:  {hash_val}")
print(f"Written to:             {hash_file}")

# Same non-secret execution-affecting config snapshot config.py's own
# startup validation builds — keep these two in sync if either changes.
_exec_snapshot = {
    "order_type":                 cfg.exchange.order_type,
    "limit_order_enabled":        cfg.exchange.limit_order_enabled,
    "native_stop_loss_enabled":   cfg.exchange.native_stop_loss_enabled,
    "max_slippage_pct":           cfg.exchange.max_slippage_pct,
    "atr_sizing_enabled":         cfg.strategy.atr_sizing_enabled,
    "atr_sl_mult":                cfg.strategy.atr_sl_mult,
    "risk_max_position_pct":      cfg.risk.max_position_pct,
    "risk_daily_loss_limit_pct":  cfg.risk.daily_loss_limit_pct,
    "risk_max_drawdown_pct":      cfg.risk.max_drawdown_pct,
    "risk_max_trades_per_day":    cfg.risk.max_trades_per_day,
    "risk_weekly_loss_limit_pct": cfg.risk.weekly_loss_limit_pct,
    "risk_kill_switch_pct":       cfg.risk.kill_switch_pct,
}
exec_hash_file = Path(os.getenv("EXECUTION_HASH_FILE", "logs/validated_execution_hash"))
exec_hash_val  = compute_execution_hash(_exec_snapshot)
exec_hash_file.parent.mkdir(parents=True, exist_ok=True)
exec_hash_file.write_text(exec_hash_val + "\n")

print(f"Execution hash stamped: {exec_hash_val}")
print(f"Written to:             {exec_hash_file}")
print()
print("The live bot and backtest.py will now warn if strategy code changes")
print("without a matching walk-forward re-run, and config.py will warn if")
print("execution/risk-gate config drifts from this stamp.")
