"""
Stock paper trading executor — Phase 6.

Simulates stock orders against a virtual cash balance.
Fills are instantaneous at the price provided by the scan cycle (market-order semantics).
Tracks multiple symbols, weighted average cost basis, realized + unrealized P&L.

No slippage, no commissions, no partial fills modelled — add those when
moving to a real broker by implementing StockExecutorBase.

To swap in Interactive Brokers:
    Replace StockPaperExecutor with IBrokerExecutor(StockExecutorBase) in main.py.
    Everything else — alerts, dashboard, portfolio summary — stays unchanged.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from stock_bot.execution.base import (
    OrderSide, OrderStatus, StockExecutorBase, StockOrder,
)

logger = logging.getLogger(__name__)


class StockPaperExecutor(StockExecutorBase):
    """
    In-memory paper trading executor for the stock bot.

    State is held in RAM — resets on process restart.
    All prices come from yfinance data in the scan cycle (no extra API calls).
    """

    def __init__(self, starting_cash: float = 10_000.0) -> None:
        self._cash:          float                        = starting_cash
        self._starting_cash: float                        = starting_cash
        # {SYMBOL_UPPER: (shares, avg_cost_per_share)}
        self._positions:     dict[str, tuple[float, float]] = {}
        self._realized_pnl:  float                        = 0.0
        self._orders:        list[StockOrder]             = []

        logger.info(
            "StockPaperExecutor ready | starting_cash=$%.2f", starting_cash
        )

    # ── Core operations ───────────────────────────────────────────────────────

    def buy(self, symbol: str, shares: float, price: float) -> StockOrder:
        sym   = symbol.upper()
        order = self._new_order(sym, OrderSide.BUY, shares, price)
        cost  = round(shares * price, 2)

        if cost > self._cash + 1e-9:
            order.status       = OrderStatus.REJECTED
            order.reject_reason = (
                f"Insufficient cash: have ${self._cash:,.2f}, need ${cost:,.2f}"
            )
            logger.warning("PAPER BUY REJECTED  %s × %.4f @ $%.2f — %s",
                           sym, shares, price, order.reject_reason)
        else:
            held_shares, held_cost = self._positions.get(sym, (0.0, 0.0))
            new_shares = held_shares + shares
            new_cost   = (
                (held_shares * held_cost + shares * price) / new_shares
                if new_shares > 0 else price
            )
            self._positions[sym] = (new_shares, round(new_cost, 6))
            self._cash          -= cost
            order.status    = OrderStatus.FILLED
            order.filled_at = datetime.now(timezone.utc)
            logger.info(
                "PAPER BUY FILLED   %s  %.4f shares @ $%.2f  "
                "new_pos=%.4f  avg_cost=$%.2f  cash=$%.2f",
                sym, shares, price, new_shares, new_cost, self._cash,
            )

        self._orders.append(order)
        return order

    def sell(self, symbol: str, shares: float, price: float) -> StockOrder:
        sym   = symbol.upper()
        order = self._new_order(sym, OrderSide.SELL, shares, price)

        held_shares, held_cost = self._positions.get(sym, (0.0, 0.0))
        if shares > held_shares + 1e-9:
            order.status        = OrderStatus.REJECTED
            order.reject_reason = (
                f"Insufficient position: have {held_shares:.4f} shares, need {shares:.4f}"
            )
            logger.warning("PAPER SELL REJECTED  %s × %.4f — %s",
                           sym, shares, order.reject_reason)
        else:
            pnl             = round((price - held_cost) * shares, 2)
            proceeds        = round(shares * price, 2)
            self._cash     += proceeds
            self._realized_pnl += pnl
            new_shares      = round(held_shares - shares, 9)
            if new_shares < 1e-9:
                del self._positions[sym]
            else:
                self._positions[sym] = (new_shares, held_cost)
            order.status    = OrderStatus.FILLED
            order.filled_at = datetime.now(timezone.utc)
            logger.info(
                "PAPER SELL FILLED  %s  %.4f shares @ $%.2f  "
                "trade_pnl=$%.2f  total_realized=$%.2f  cash=$%.2f",
                sym, shares, price, pnl, self._realized_pnl, self._cash,
            )

        self._orders.append(order)
        return order

    # ── Portfolio state ───────────────────────────────────────────────────────

    @property
    def cash(self) -> float:
        return self._cash

    def position(self, symbol: str) -> float:
        return self._positions.get(symbol.upper(), (0.0, 0.0))[0]

    def avg_cost(self, symbol: str) -> float:
        return self._positions.get(symbol.upper(), (0.0, 0.0))[1]

    def realized_pnl(self) -> float:
        return self._realized_pnl

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, (shares, cost) in self._positions.items():
            px = prices.get(sym, prices.get(sym.lower(), 0.0))
            total += (px - cost) * shares
        return round(total, 2)

    def total_value(self, prices: dict[str, float]) -> float:
        pos_value = sum(
            prices.get(sym, prices.get(sym.lower(), 0.0)) * shares
            for sym, (shares, _) in self._positions.items()
        )
        return round(self._cash + pos_value, 2)

    def positions_snapshot(self) -> dict[str, tuple[float, float]]:
        return {sym: (shares, cost) for sym, (shares, cost) in self._positions.items()}

    # ── Order history ─────────────────────────────────────────────────────────

    def all_orders(self) -> list[StockOrder]:
        return list(self._orders)

    def filled_orders(self) -> list[StockOrder]:
        return [o for o in self._orders if o.status == OrderStatus.FILLED]

    # ── Stats helper ──────────────────────────────────────────────────────────

    def log_state(self, prices: dict[str, float] | None = None) -> None:
        prices = prices or {}
        logger.info(
            "PAPER PORTFOLIO | cash=$%.2f | realized_pnl=$%.2f | "
            "unrealized_pnl=$%.2f | total_fills=%d | open_positions=%d",
            self._cash,
            self._realized_pnl,
            self.unrealized_pnl(prices),
            len(self.filled_orders()),
            len(self._positions),
        )

    # ── Internal ─────────────────────────────────────────────────────────────

    @staticmethod
    def _new_order(
        symbol: str, side: OrderSide, shares: float, price: float
    ) -> StockOrder:
        return StockOrder(
            order_id   = str(uuid.uuid4()),
            symbol     = symbol,
            side       = side,
            quantity   = shares,
            price      = price,
            status     = OrderStatus.PENDING,
            created_at = datetime.now(timezone.utc),
        )
