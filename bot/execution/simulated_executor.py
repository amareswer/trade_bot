"""
Simulated execution layer — prints orders instead of placing them.

Future extension point: replace SimulatedExecutor with BrokerExecutor that
calls a real brokerage API while keeping the same execute() interface.
"""

import logging
from bot.strategy.threshold_strategy import Signal

logger = logging.getLogger(__name__)


class SimulatedExecutor:
    def __init__(self, symbol: str, quantity: int = 1):
        self.symbol = symbol
        self.quantity = quantity
        self._position: int = 0

    def execute(self, signal: Signal, price: float) -> None:
        if signal == Signal.BUY:
            self._position += self.quantity
            logger.info(
                "ORDER | BUY  %d %s @ %.2f | position=%d",
                self.quantity, self.symbol, price, self._position,
            )
        elif signal == Signal.SELL:
            if self._position < self.quantity:
                logger.warning(
                    "ORDER | SELL rejected — insufficient position: have %d, need %d",
                    self._position, self.quantity,
                )
                return
            self._position -= self.quantity
            logger.info(
                "ORDER | SELL %d %s @ %.2f | position=%d",
                self.quantity, self.symbol, price, self._position,
            )
        else:
            logger.info(
                "ORDER | HOLD   %s @ %.2f | position=%d",
                self.symbol, price, self._position,
            )

    @property
    def position(self) -> int:
        return self._position


# ---------------------------------------------------------------------------
# FUTURE: BrokerExecutor (placeholder)
# ---------------------------------------------------------------------------
# class BrokerExecutor:
#     """Submits real orders via broker API."""
#     def execute(self, signal: Signal, price: float) -> None: ...
