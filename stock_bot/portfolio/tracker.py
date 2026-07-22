"""
Portfolio dataclasses — shared shape for both the bot's real executor
positions (StockExecutorBase.build_summary(), stock_bot/execution/base.py)
and paper-trade records.

The manual PORTFOLIO-env static-holdings tracker (PortfolioTracker) that
used to also produce a PortfolioSummary from a hand-typed
"SYMBOL:SHARES:AVG_COST,..." string was removed 2026-07-22 — it had gone
fully dormant once an executor became mandatory (2026-07-17): portfolio
summaries and "is this symbol held" checks always source from the live
executor from that point on, and the manual list was never read otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from stock_bot.ai.verdict import AIVerdict


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PortfolioPosition:
    symbol:         str
    shares:         float
    avg_cost:       float
    current_price:  float
    current_value:  float         # shares × current_price
    total_cost:     float         # shares × avg_cost
    gain_loss:      float         # current_value - total_cost
    gain_loss_pct:  float         # (gain_loss / total_cost) × 100
    currency:       str           # "CAD" (.TO) | "USD"
    verdict:        Optional[AIVerdict]


@dataclass
class PortfolioSummary:
    positions:          list[PortfolioPosition]
    total_invested:     float
    total_value:        float
    total_gain_loss:    float
    total_gain_loss_pct: float


@dataclass
class PaperTrade:
    """One filled paper-trade record — written to paper_trades.csv and shown in dashboard."""
    timestamp:      str    # "YYYY-MM-DD HH:MM:SS"
    symbol:         str
    side:           str    # "BUY" | "SELL"
    shares:         float
    price:          float
    total_value:    float
    cash_remaining: float
    reason:         str    # e.g. "BUY 72% SWING"


@dataclass
class PaperSummary:
    """Full paper-portfolio state for one scan cycle, passed to the dashboard renderer."""
    cash:           float
    starting_cash:  float
    positions:      list[PortfolioPosition]  # open positions with live P&L
    realized_pnl:   float
    unrealized_pnl: float
    total_value:    float             # cash + open-position market value
    recent_trades:  list[PaperTrade]  # last 10 filled, newest first
