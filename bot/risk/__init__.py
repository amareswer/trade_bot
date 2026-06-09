"""
Risk management package.

RiskManager (risk_manager.py) is a 5-check approval gate that runs before
every trade. A signal must pass all checks to be executed.

Checks (in order):
  1. HALT flag — manual kill switch; blocks all BUYs and SELLs
  2. Max drawdown — blocks new BUYs if portfolio is down more than
     RISK_MAX_DRAWDOWN from its all-time peak (never resets)
  3. Daily trade cap — blocks new BUYs once RISK_MAX_TRADES_PER_DAY
     fills have occurred today; resets at midnight
  4. Daily loss limit — blocks new BUYs if portfolio is down more than
     RISK_DAILY_LOSS_LIMIT today; resets at midnight
  5. Position size — blocks BUYs where the order would exceed
     RISK_MAX_POSITION_PCT of total portfolio value

SELL always bypasses checks 2–5. Only check 1 (HALT) can block a SELL,
ensuring an open position can always be exited.

Config: RiskConfig dataclass, populated from .env via config.py _load().
"""
