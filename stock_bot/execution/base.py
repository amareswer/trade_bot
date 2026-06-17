"""
Stock executor interface — Phase 6.

Defines the contract that both PaperExecutor and any real broker executor
(Interactive Brokers, Questrade, Alpaca, …) must satisfy.

To swap in a live broker:
    1. Create a new class, e.g. IBrokerExecutor(StockExecutorBase)
    2. Implement every @abstractmethod
    3. Replace StockPaperExecutor with IBrokerExecutor in main.py
    — no other code changes needed.

build_summary() is provided here as a concrete method that all subclasses
inherit.  It converts positions_snapshot() + live scan prices into the
PortfolioSummary object the dashboard already knows how to render.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from stock_bot.portfolio.tracker import PortfolioPosition, PortfolioSummary

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Order domain types
# ---------------------------------------------------------------------------

class OrderSide(Enum):
    BUY  = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    PENDING  = "PENDING"
    FILLED   = "FILLED"
    REJECTED = "REJECTED"


@dataclass
class StockOrder:
    order_id:      str
    symbol:        str
    side:          OrderSide
    quantity:      float           # shares
    price:         float           # fill price (or attempt price on rejection)
    status:        OrderStatus
    created_at:    datetime
    filled_at:     Optional[datetime] = None
    reject_reason: Optional[str]      = None
    total_value:   float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.total_value = round(abs(self.price * self.quantity), 2)

    def summary(self) -> str:
        ts = self.filled_at or self.created_at
        return (
            f"[{self.order_id[:8]}] {self.status.value:8s} | "
            f"{self.side.value:4s} {self.quantity:.4f} {self.symbol} "
            f"@ ${self.price:,.2f} = ${self.total_value:,.2f} | "
            f"{ts.strftime('%H:%M:%S')}"
        )


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class StockExecutorBase(ABC):
    """
    Abstract stock executor — implement this to plug in any broker.
    All concrete classes automatically get build_summary() for free.
    """

    # ── Core trading operations ───────────────────────────────────────────────

    @abstractmethod
    def buy(self, symbol: str, shares: float, price: float) -> StockOrder:
        """Place a market BUY for `shares` of `symbol` at `price`."""
        ...

    @abstractmethod
    def sell(self, symbol: str, shares: float, price: float) -> StockOrder:
        """Place a market SELL for `shares` of `symbol` at `price`."""
        ...

    # ── Portfolio state ───────────────────────────────────────────────────────

    @property
    @abstractmethod
    def cash(self) -> float:
        """Available cash balance."""
        ...

    @abstractmethod
    def position(self, symbol: str) -> float:
        """Shares currently held for `symbol` (0 if none)."""
        ...

    @abstractmethod
    def avg_cost(self, symbol: str) -> float:
        """Average cost per share for `symbol` (0 if not held)."""
        ...

    @abstractmethod
    def realized_pnl(self) -> float:
        """Total realised profit/loss across all closed trades."""
        ...

    @abstractmethod
    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        """Total unrealised P&L given a {symbol → current_price} map."""
        ...

    @abstractmethod
    def total_value(self, prices: dict[str, float]) -> float:
        """Cash + market value of all open positions."""
        ...

    @abstractmethod
    def positions_snapshot(self) -> dict[str, tuple[float, float]]:
        """Return {SYMBOL: (shares, avg_cost)} for every open position."""
        ...

    # ── Order history ─────────────────────────────────────────────────────────

    @abstractmethod
    def all_orders(self) -> list[StockOrder]:
        ...

    @abstractmethod
    def filled_orders(self) -> list[StockOrder]:
        ...

    # ── Dashboard integration (concrete — all subclasses inherit) ────────────

    def build_summary(self, scan_results: list) -> Optional[PortfolioSummary]:
        """
        Convert open positions + live scan prices into a PortfolioSummary
        ready for the dashboard renderer.  Works for any executor subclass.

        scan_results: list of ScanResult (duck-typed, no import needed).
        Returns None when no positions are open.
        """
        snapshot = self.positions_snapshot()
        if not snapshot:
            return None

        price_map    = {r.symbol.upper(): r.price    for r in scan_results}
        verdict_map  = {r.symbol.upper(): r.verdict  for r in scan_results}
        currency_map = {r.symbol.upper(): r.currency for r in scan_results}

        positions: list[PortfolioPosition] = []
        for sym, (shares, avg_cost) in snapshot.items():
            if sym not in price_map:
                logger.debug("Executor: %s not in scan cycle — skipped in summary", sym)
                continue

            current_price = price_map[sym]
            current_value = round(shares * current_price, 2)
            total_cost    = round(shares * avg_cost, 2)
            gain_loss     = round(current_value - total_cost, 2)
            gain_loss_pct = round((gain_loss / total_cost * 100) if total_cost else 0.0, 2)
            currency      = currency_map.get(sym, "CAD" if sym.endswith(".TO") else "USD")

            positions.append(PortfolioPosition(
                symbol        = sym,
                shares        = shares,
                avg_cost      = avg_cost,
                current_price = current_price,
                current_value = current_value,
                total_cost    = total_cost,
                gain_loss     = gain_loss,
                gain_loss_pct = gain_loss_pct,
                currency      = currency,
                verdict       = verdict_map.get(sym),
            ))

        if not positions:
            return None

        total_invested     = round(sum(p.total_cost    for p in positions), 2)
        total_value        = round(sum(p.current_value for p in positions), 2)
        total_gain_loss    = round(total_value - total_invested, 2)
        total_gl_pct       = round(
            (total_gain_loss / total_invested * 100) if total_invested else 0.0, 2
        )

        return PortfolioSummary(
            positions           = positions,
            total_invested      = total_invested,
            total_value         = total_value,
            total_gain_loss     = total_gain_loss,
            total_gain_loss_pct = total_gl_pct,
        )
