"""
Price feed implementations.

Both feeds expose the same interface — get_price() -> float — so they are
drop-in replacements for each other in main.py.

  SimulatedFeed  : random walk, no network, always available
  CcxtFeed       : live price from any ccxt-supported exchange
"""

import logging
import random
import time

from bot.exchanges.ccxt_client import CcxtClient, ExchangeError

logger = logging.getLogger(__name__)


class SimulatedFeed:
    def __init__(self, symbol: str, start_price: float = 100.0, volatility: float = 2.0):
        self.symbol = symbol
        self._price = start_price
        self._volatility = volatility

    def get_price(self) -> float:
        delta = random.uniform(-self._volatility, self._volatility)
        self._price = max(0.01, round(self._price + delta, 2))
        return self._price


_MAX_PRICE_AGE_S = 120.0  # refuse to trade on data older than 2 minutes


class CcxtFeed:
    """
    Live price feed backed by any ccxt-compatible exchange.

    Falls back to the last known price on transient errors, but only within
    _MAX_PRICE_AGE_S seconds. Beyond that, the error propagates so the main
    loop skips the tick rather than trading on stale data.
    """

    def __init__(self, exchange_id: str, symbol: str):
        self.symbol = symbol
        self._client = CcxtClient(exchange_id)
        self._last_price: float | None = None
        self._last_price_ts: float     = 0.0

    def get_price(self) -> float:
        try:
            price = self._client.fetch_price(self.symbol)
            self._last_price    = price
            self._last_price_ts = time.monotonic()
            return price
        except ExchangeError as exc:
            age = time.monotonic() - self._last_price_ts
            if self._last_price is not None and age < _MAX_PRICE_AGE_S:
                logger.warning(
                    "Price fetch failed (%s) — reusing last price %.2f (%.0fs old)",
                    exc, self._last_price, age,
                )
                return self._last_price
            raise
