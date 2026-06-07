"""
Live order executor — places real orders on Kraken via ccxt.

WARNING: This executor uses real money. Only enable when:
  1. LIVE_TRADING=true in .env
  2. Kraken API key and secret are set
  3. You have verified dry run behavior is correct

The interface is identical to PaperExecutor so main.py can
swap between them with a single config flag.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import ccxt

logger = logging.getLogger(__name__)


class OrderSide(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    FILLED   = "FILLED"
    REJECTED = "REJECTED"
    PENDING  = "PENDING"


@dataclass
class Order:
    side:        OrderSide
    price:       float
    quantity:    float
    total_value: float
    status:      OrderStatus
    timestamp:   str
    order_id:    str = ""
    raw:         dict | None = None


class LiveExecutor:
    """
    Places real market orders on Kraken via ccxt.
    Interface identical to PaperExecutor.
    """

    def __init__(
        self,
        exchange_id:   str,
        symbol:        str,
        api_key:       str,
        api_secret:    str,
        starting_cash: float = 10_000.0,
        dry_run:       bool  = False,
    ):
        self.symbol        = symbol
        self.dry_run       = dry_run
        self._cash         = starting_cash
        self._position     = 0.0
        self._avg_entry    = 0.0
        self._realized_pnl = 0.0

        exchange_cls = getattr(ccxt, exchange_id.lower())
        self._exchange = exchange_cls({
            "apiKey":          api_key,
            "secret":          api_secret,
            "timeout":         15_000,
            "enableRateLimit": True,
        })

        logger.info(
            "LiveExecutor initialized | symbol=%s dry_run=%s",
            symbol, dry_run,
        )

    # ── Read-only properties ──────────────────────────────────────────

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def position(self) -> float:
        return self._position

    @property
    def avg_entry(self) -> float:
        return self._avg_entry

    # ── Portfolio value helper ────────────────────────────────────────

    class _Portfolio:
        def __init__(self, executor: "LiveExecutor"):
            self._ex = executor

        def total_value(self, price: float) -> float:
            return self._ex.cash + self._ex.position * price

    @property
    def portfolio(self):
        return self._Portfolio(self)

    # ── Core execution ────────────────────────────────────────────────

    def execute(
        self,
        signal,
        price:    float,
        quantity: float,
    ) -> Order | None:
        from bot.strategy.threshold_strategy import Signal

        if signal not in (Signal.BUY, Signal.SELL):
            return None

        side = OrderSide.BUY if signal == Signal.BUY else OrderSide.SELL

        if side == OrderSide.BUY and quantity <= 0:
            logger.warning("LiveExecutor: BUY quantity=0, skipping")
            return None
        if side == OrderSide.SELL and self._position <= 0:
            logger.warning("LiveExecutor: SELL with no position, skipping")
            return None

        quantity = self._position if side == OrderSide.SELL else quantity
        ts       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

        if self.dry_run:
            logger.warning(
                "DRY RUN — would place %s %.6f %s @ ~%.2f",
                side.value, quantity, self.symbol, price,
            )
            print(
                f"  [DRY RUN] {side.value} {quantity:.6f} {self.symbol}"
                f" @ ~{price:,.2f}",
                flush=True,
            )
            fill_price = price
            order_id   = "dry_run"
        else:
            try:
                logger.warning(
                    "LIVE ORDER: %s %.6f %s",
                    side.value, quantity, self.symbol,
                )
                ccxt_side = "buy" if side == OrderSide.BUY else "sell"
                raw = self._exchange.create_order(
                    symbol = self.symbol,
                    type   = "market",
                    side   = ccxt_side,
                    amount = quantity,
                )
                fill_price = float(
                    raw.get("average") or
                    raw.get("price")   or
                    price
                )
                order_id = str(raw.get("id", ""))
                logger.info("Order filled: %s", raw)

            except ccxt.InsufficientFunds as e:
                logger.error("Insufficient funds: %s", e)
                return Order(
                    side=side, price=price, quantity=quantity,
                    total_value=0, status=OrderStatus.REJECTED,
                    timestamp=ts,
                )
            except ccxt.BaseError as e:
                logger.error("ccxt order error: %s", e)
                return Order(
                    side=side, price=price, quantity=quantity,
                    total_value=0, status=OrderStatus.REJECTED,
                    timestamp=ts,
                )

        total_value = fill_price * quantity

        if side == OrderSide.BUY:
            self._cash     -= total_value
            total_held      = self._position * self._avg_entry + total_value
            self._position += quantity
            self._avg_entry = (
                total_held / self._position if self._position > 0 else 0.0
            )
        else:
            pnl                 = (fill_price - self._avg_entry) * quantity
            self._realized_pnl += pnl
            self._cash         += total_value
            self._position      = max(0.0, self._position - quantity)
            if self._position == 0:
                self._avg_entry = 0.0

        return Order(
            side        = side,
            price       = fill_price,
            quantity    = quantity,
            total_value = total_value,
            status      = OrderStatus.FILLED,
            timestamp   = ts,
            order_id    = order_id,
        )

    def filled_orders(self):
        """Return filled orders list — compatible with PaperExecutor interface."""
        return []

    def reset(self):
        """Reset executor state — compatible with PaperExecutor interface."""
        self._cash         = self._cash
        self._position     = 0.0
        self._avg_entry    = 0.0
        self._realized_pnl = 0.0

    def rejected_orders(self):
        """Return rejected orders — compatible with PaperExecutor interface."""
        return []

    @property
    def orders(self):
        """Return all orders — compatible with PaperExecutor interface."""
        return []

    def portfolio_snapshot(self, current_price: float) -> None:
        """Log portfolio summary — compatible with PaperExecutor interface."""
        import logging
        logger = logging.getLogger(__name__)
        logger.info(
            "PORTFOLIO | cash=$%.2f | pos=%.6f %s | total=$%.2f",
            self._cash,
            self._position,
            self.symbol,
            self._cash + self._position * current_price,
        )
