"""
Alert dataclass and AlertType enum — Phase 5.

A single Alert represents one triggered condition for one symbol in one scan cycle.
Priority is computed at creation time by the AlertEvaluator, not set by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class AlertType(Enum):
    STRONG_BUY          = "STRONG_BUY"           # BUY ≥ 70 confidence
    STRONG_SELL         = "STRONG_SELL"          # SELL ≥ 70 confidence
    PORTFOLIO_SELL      = "PORTFOLIO_SELL"       # SELL ≥ 65 on owned symbol
    PORTFOLIO_BUY_MORE  = "PORTFOLIO_BUY_MORE"  # BUY ≥ 70 on owned symbol
    EARNINGS_SOON       = "EARNINGS_SOON"        # earnings within 3 days
    RSI_OVERBOUGHT      = "RSI_OVERBOUGHT"       # RSI > 75 on owned symbol
    RSI_OVERSOLD        = "RSI_OVERSOLD"         # RSI < 25 on owned symbol


# Priority assignment by type:
# HIGH:   PORTFOLIO_SELL, RSI_OVERBOUGHT, RSI_OVERSOLD + EARNINGS_SOON (≤1 day)
# MEDIUM: STRONG_BUY, STRONG_SELL, PORTFOLIO_BUY_MORE, EARNINGS_SOON (2-3 days)
_HIGH_TYPES = {
    AlertType.PORTFOLIO_SELL,
    AlertType.RSI_OVERBOUGHT,
    AlertType.RSI_OVERSOLD,
}


@dataclass
class Alert:
    alert_type:  AlertType
    symbol:      str
    message:     str
    confidence:  Optional[int]   # from AI verdict when applicable
    price:       float
    currency:    str
    timestamp:   datetime
    priority:    str             # "HIGH" | "MEDIUM"
    source:      str             # "watchlist" | "universe"
