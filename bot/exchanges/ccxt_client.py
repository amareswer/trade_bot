"""
Thin abstraction over ccxt.

Supports any ccxt-compatible exchange that exposes public ticker data.
No API keys required for read-only price fetching on most exchanges.

Supported exchanges (sample):
  kraken, binance, coinbase, bybit, okx, bitfinex, huobi, gate, kucoin

Usage:
    client = CcxtClient("kraken")
    price  = client.fetch_price("BTC/USDT")
"""

import logging
import ccxt

logger = logging.getLogger(__name__)

# ccxt exchanges that reliably expose public ticker without API keys
SUPPORTED_EXCHANGES = {
    "kraken", "binance", "coinbase", "bybit",
    "okx", "bitfinex", "gate", "kucoin", "huobi",
}


class ExchangeError(Exception):
    """Raised when the exchange returns an unexpected response."""


class CcxtClient:
    def __init__(self, exchange_id: str, timeout_ms: int = 10_000):
        exchange_id = exchange_id.lower()
        if exchange_id not in SUPPORTED_EXCHANGES:
            logger.warning(
                "Exchange '%s' is not in the tested list %s — proceeding anyway.",
                exchange_id, SUPPORTED_EXCHANGES,
            )

        if not hasattr(ccxt, exchange_id):
            raise ExchangeError(f"ccxt does not support exchange: '{exchange_id}'")

        exchange_class = getattr(ccxt, exchange_id)
        self._exchange = exchange_class({"timeout": timeout_ms})
        self._exchange_id = exchange_id
        logger.info("CcxtClient initialised | exchange=%s", exchange_id)

    def fetch_price(self, symbol: str) -> float:
        """
        Return the last traded price for *symbol* (e.g. 'BTC/USDT').
        Raises ExchangeError on network or data problems.
        """
        try:
            ticker = self._exchange.fetch_ticker(symbol)
        except ccxt.NetworkError as exc:
            raise ExchangeError(f"Network error on {self._exchange_id}: {exc}") from exc
        except ccxt.ExchangeError as exc:
            raise ExchangeError(f"Exchange error on {self._exchange_id}: {exc}") from exc

        price = ticker.get("last")
        if price is None:
            raise ExchangeError(
                f"'last' price missing in ticker response from {self._exchange_id}"
            )

        return float(price)

    @property
    def exchange_id(self) -> str:
        return self._exchange_id
