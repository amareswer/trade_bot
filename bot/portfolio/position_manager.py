"""
Position manager — single source of truth for position accounting.

Responsibilities
────────────────
  - Track open quantity and weighted average entry price
  - Compute unrealized P&L against current market price
  - Compute realized P&L on every SELL fill
  - Maintain full trade history

Separation of concerns
───────────────────────
  PaperExecutor  → cash ledger, order lifecycle, order history
  PositionManager → position quantity, avg entry, P&L analytics

Both are updated in main.py after every confirmed fill.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    timestamp: datetime
    action:    str           # "BUY" | "SELL"
    quantity:  float
    price:     float
    pnl:       Optional[float] = None   # None for BUY; realized PnL for SELL


class PositionManager:
    """
    Tracks position state with weighted-average cost basis.

    Usage:
        pm = PositionManager()
        pm.on_buy(price=74_000, quantity=0.01)
        pm.on_sell(price=75_000, quantity=0.01)   # returns realized PnL
    """

    def __init__(self) -> None:
        self._quantity:     float              = 0.0
        self._avg_entry:    float              = 0.0
        self._realized_pnl: float              = 0.0
        self._history:      list[TradeRecord]  = []

    # ── Restart recovery ─────────────────────────────────────────────

    def seed(self, quantity: float, avg_entry: float, realized_pnl: float = 0.0) -> None:
        """
        Seed position state from persisted executor state on restart.
        Does NOT create a trade record — this is recovery, not a new fill.
        """
        self._quantity     = quantity
        self._avg_entry    = avg_entry
        self._realized_pnl = realized_pnl
        logger.warning(
            "PositionManager seeded: qty=%.6f avg_entry=%.2f realized_pnl=%.2f",
            quantity, avg_entry, realized_pnl,
        )

    # ── Fill handlers ────────────────────────────────────────────────

    def on_buy(self, price: float, quantity: float) -> None:
        """Record a BUY fill. Updates weighted average entry price."""
        prev_cost       = self._quantity * self._avg_entry
        self._quantity += quantity
        self._avg_entry = (prev_cost + quantity * price) / self._quantity
        self._history.append(TradeRecord(
            timestamp = datetime.now(timezone.utc),
            action    = "BUY",
            quantity  = quantity,
            price     = price,
        ))
        logger.info(
            "PositionManager: BUY %.4f @ $%.2f | avg_entry=$%.2f | pos=%.4f",
            quantity, price, self._avg_entry, self._quantity,
        )

    def on_sell(self, price: float, quantity: float) -> float:
        """Record a SELL fill. Returns realized PnL for this trade."""
        pnl              = round((price - self._avg_entry) * quantity, 2)
        self._realized_pnl += pnl
        self._quantity   = max(0.0, self._quantity - quantity)
        if self._quantity < 1e-9:
            self._quantity  = 0.0
            self._avg_entry = 0.0
        self._history.append(TradeRecord(
            timestamp = datetime.now(timezone.utc),
            action    = "SELL",
            quantity  = quantity,
            price     = price,
            pnl       = pnl,
        ))
        logger.info(
            "PositionManager: SELL %.4f @ $%.2f | pnl=$%.2f | realized=$%.2f | pos=%.4f",
            quantity, price, pnl, self._realized_pnl, self._quantity,
        )
        return pnl

    # ── Computed properties ──────────────────────────────────────────

    def unrealized_pnl(self, current_price: float) -> float:
        if self._quantity == 0:
            return 0.0
        return round((current_price - self._avg_entry) * self._quantity, 2)

    def position_value(self, current_price: float) -> float:
        return round(self._quantity * current_price, 2)

    @property
    def quantity(self) -> float:
        return self._quantity

    @property
    def avg_entry(self) -> float:
        return self._avg_entry

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def has_position(self) -> bool:
        return self._quantity >= 1e-9

    @property
    def history(self) -> list[TradeRecord]:
        return list(self._history)
