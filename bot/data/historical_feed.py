"""
Historical OHLCV data fetcher via ccxt.

fetch_candles() returns a list of Candle objects ordered oldest → newest.
Uses only public endpoints — no API key required.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import ccxt

logger = logging.getLogger(__name__)

ANNUALISATION = {
    "1m":  525_600,
    "5m":  105_120,
    "15m":  35_040,
    "30m":  17_520,
    "1h":    8_760,
    "2h":    4_380,
    "4h":    2_190,
    "6h":    1_460,
    "12h":     730,
    "1d":      365,
    "1w":       52,
}


@dataclass
class Candle:
    timestamp: datetime
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float


def fetch_candles(
    exchange_id: str,
    symbol:      str,
    timeframe:   str = "1h",
    limit:       int = 500,
) -> list[Candle]:
    """
    Fetch up to *limit* historical OHLCV candles for *symbol* from *exchange_id*.
    Returns candles ordered oldest → newest.
    Raises ValueError / ccxt exceptions on failure.
    """
    exchange_id = exchange_id.lower()
    if not hasattr(ccxt, exchange_id):
        raise ValueError(f"ccxt does not support exchange: '{exchange_id}'")

    exchange_cls = getattr(ccxt, exchange_id)
    exchange     = exchange_cls({"timeout": 15_000})

    if not exchange.has.get("fetchOHLCV"):
        raise ValueError(f"Exchange '{exchange_id}' does not support OHLCV data.")

    if timeframe not in exchange.timeframes:
        available = ", ".join(sorted(exchange.timeframes.keys()))
        raise ValueError(
            f"Timeframe '{timeframe}' not supported by {exchange_id}. "
            f"Available: {available}"
        )

    logger.info(
        "Fetching %d × %s candles for %s from %s …",
        limit, timeframe, symbol, exchange_id,
    )

    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if not raw:
        raise ValueError(f"No OHLCV data returned for {symbol} on {exchange_id}.")

    candles = [
        Candle(
            timestamp = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
            open      = float(row[1]),
            high      = float(row[2]),
            low       = float(row[3]),
            close     = float(row[4]),
            volume    = float(row[5]),
        )
        for row in raw
    ]

    logger.info(
        "Fetched %d candles | %s → %s",
        len(candles),
        candles[0].timestamp.strftime("%Y-%m-%d %H:%M"),
        candles[-1].timestamp.strftime("%Y-%m-%d %H:%M"),
    )
    return candles
