"""
Paper trading execution engine.

Simulates a real order lifecycle — creation, validation, fill, rejection —
without touching any real exchange or money.

Flow:
    Strategy emits Signal
        → PaperExecutor.execute()
            → Order created (PENDING)
            → Validated against cash / position
            → Filled immediately at market price  (FILLED)
               or rejected with reason            (REJECTED)
            → Portfolio updated
            → All events logged
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from bot.strategy.threshold_strategy import Signal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

class OrderSide(Enum):
    BUY  = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    PENDING  = "PENDING"
    FILLED   = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass
class Order:
    order_id:     str
    symbol:       str
    side:         OrderSide
    quantity:     float
    price:        float          # fill price (market simulation)
    status:       OrderStatus
    created_at:   datetime
    filled_at:    Optional[datetime] = None
    reject_reason: Optional[str]    = None

    # Fee data from exchange (live only; zero for paper/simulation)
    fee_cost:     float = field(default=0.0)
    fee_currency: str   = field(default="")

    # Computed on fill
    total_value:  float = field(default=0.0, init=False)

    def __post_init__(self):
        self.total_value = round(self.price * self.quantity, 2)

    def summary(self) -> str:
        ts = self.filled_at or self.created_at
        return (
            f"[{self.order_id[:8]}] {self.status.value:8s} | "
            f"{self.side.value:4s} {self.quantity} {self.symbol} "
            f"@ {self.price:,.2f} = ${self.total_value:,.2f} | "
            f"{ts.strftime('%H:%M:%S')}"
        )


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------

@dataclass
class Portfolio:
    cash:          float
    position:      float = 0.0   # units held (can be fractional for crypto)
    realized_pnl:  float = 0.0
    _cost_basis:   float = field(default=0.0, repr=False)

    def unrealized_pnl(self, current_price: float) -> float:
        if self.position == 0:
            return 0.0
        return round((current_price - self._cost_basis) * self.position, 2)

    def total_value(self, current_price: float) -> float:
        return round(self.cash + self.position * current_price, 2)


# ---------------------------------------------------------------------------
# Paper executor
# ---------------------------------------------------------------------------

class PaperExecutor:
    """
    Simulates order execution against an in-memory paper portfolio.

    Orders are filled instantly at the price supplied by the strategy tick
    (market-order semantics). No slippage or fees are modelled yet.

    Rejection conditions:
        BUY  — insufficient cash to cover the order value
        SELL — no open position to sell
    """

    def __init__(
        self,
        symbol: str,
        quantity: float = 1.0,
        starting_cash: float = 10_000.0,
    ):
        self.symbol   = symbol
        self.quantity = quantity
        self._portfolio = Portfolio(cash=starting_cash)
        self._orders: list[Order] = []

        logger.info(
            "PaperExecutor ready | symbol=%s | qty=%.4f | cash=$%.2f",
            symbol, quantity, starting_cash,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def execute(
        self,
        signal:   Signal,
        price:    float,
        quantity: Optional[float] = None,
        urgent:   bool = False,
    ) -> Optional[Order]:
        """
        Create, validate, and fill (or reject) an order for *signal*.
        *quantity* overrides the default self.quantity — pass it for dynamic sizing.
        *urgent* is accepted for interface parity with LiveExecutor (where it
        forces a market order for SL/TP exits); paper fills are instant anyway.
        """
        if signal == Signal.HOLD:
            logger.info("HOLD — no order created | price=%.2f", price)
            return None

        qty  = quantity if quantity is not None else self.quantity
        side = OrderSide.BUY if signal == Signal.BUY else OrderSide.SELL
        order = self._create_order(side, price, qty)
        self._process(order)
        self._orders.append(order)
        logger.info(order.summary())
        return order

    def portfolio_snapshot(self, current_price: float) -> None:
        """Log a one-line portfolio summary."""
        p = self._portfolio
        logger.info(
            "PORTFOLIO | cash=$%.2f | pos=%.4f %s | "
            "unrealized_pnl=$%.2f | realized_pnl=$%.2f | total=$%.2f",
            p.cash,
            p.position, self.symbol,
            p.unrealized_pnl(current_price),
            p.realized_pnl,
            p.total_value(current_price),
        )

    @property
    def orders(self) -> list[Order]:
        return list(self._orders)

    def filled_orders(self) -> list[Order]:
        return [o for o in self._orders if o.status == OrderStatus.FILLED]

    def rejected_orders(self) -> list[Order]:
        return [o for o in self._orders if o.status == OrderStatus.REJECTED]

    @property
    def position(self) -> float:
        return self._portfolio.position

    @property
    def cash(self) -> float:
        return self._portfolio.cash

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_order(self, side: OrderSide, price: float, quantity: float) -> Order:
        return Order(
            order_id   = str(uuid.uuid4()),
            symbol     = self.symbol,
            side       = side,
            quantity   = quantity,
            price      = price,
            status     = OrderStatus.PENDING,
            created_at = datetime.now(timezone.utc),
        )

    def _process(self, order: Order) -> None:
        now = datetime.now(timezone.utc)

        if order.side == OrderSide.BUY:
            if self._portfolio.cash < order.total_value:
                order.status = OrderStatus.REJECTED
                order.reject_reason = (
                    f"Insufficient cash: have ${self._portfolio.cash:,.2f}, "
                    f"need ${order.total_value:,.2f}"
                )
                logger.warning("ORDER REJECTED | %s", order.reject_reason)
                return

            prev_cost = self._portfolio._cost_basis * self._portfolio.position
            self._portfolio.cash         -= order.total_value
            self._portfolio.position     += order.quantity
            self._portfolio._cost_basis   = (prev_cost + order.price * order.quantity) / self._portfolio.position

        else:  # SELL
            if self._portfolio.position < order.quantity - 1e-9:
                order.status = OrderStatus.REJECTED
                order.reject_reason = (
                    f"Insufficient position: have {self._portfolio.position}, "
                    f"need {order.quantity}"
                )
                logger.warning("ORDER REJECTED | %s", order.reject_reason)
                return

            proceeds = order.total_value
            pnl      = (order.price - self._portfolio._cost_basis) * order.quantity
            self._portfolio.cash         += proceeds
            self._portfolio.position     -= order.quantity
            self._portfolio.realized_pnl += round(pnl, 2)

        order.status    = OrderStatus.FILLED
        order.filled_at = now
