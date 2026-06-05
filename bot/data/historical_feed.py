"""
Historical OHLCV data fetcher via ccxt.

fetch_candles()           — single API call, up to ~1000 candles
fetch_candles_paginated() — multiple calls via 'since', up to 5000+ candles

Both return list[Candle] ordered oldest → newest.
Uses only public endpoints — no API key required.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import ccxt

logger = logging.getLogger(__name__)

# Candle duration in milliseconds — used for paginated fetching
_TF_MS: dict[str, int] = {
    "1m":       60_000,
    "5m":      300_000,
    "15m":     900_000,
    "30m":   1_800_000,
    "1h":    3_600_000,
    "2h":    7_200_000,
    "4h":   14_400_000,
    "6h":   21_600_000,
    "12h":  43_200_000,
    "1d":   86_400_000,
    "1w":  604_800_000,
}

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


_PAGE_SIZE = 1000   # Binance hard cap per request


def fetch_candles_paginated(
    exchange_id:  str,
    symbol:       str,
    timeframe:    str = "4h",
    total_limit:  int = 5000,
) -> list[Candle]:
    """
    Fetch up to *total_limit* candles using multiple API calls.

    Makes ceil(total_limit / 1000) requests, each fetching 1000 candles
    via the 'since' parameter, then deduplicates and returns the full
    list ordered oldest → newest.

    A 0.5s sleep between pages keeps us inside Binance's rate limits.
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

    candle_ms = _TF_MS.get(timeframe)
    if candle_ms is None:
        raise ValueError(f"Unknown timeframe '{timeframe}' — cannot compute page offsets.")

    pages      = math.ceil(total_limit / _PAGE_SIZE)
    now_ms     = exchange.milliseconds()
    start_ms   = now_ms - total_limit * candle_ms

    logger.info(
        "Paginated fetch: %d candles × %s from %s | %d pages",
        total_limit, timeframe, exchange_id, pages,
    )
    print(f"  Fetching {total_limit} × {timeframe} candles ({pages} pages) …", flush=True)

    raw_by_ts: dict[int, list] = {}   # deduplicate by timestamp

    for page in range(pages):
        since = start_ms + page * _PAGE_SIZE * candle_ms
        try:
            rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe,
                                        since=since, limit=_PAGE_SIZE)
        except ccxt.NetworkError as exc:
            logger.warning("Page %d network error (%s) — skipping", page + 1, exc)
            rows = []
        except ccxt.ExchangeError as exc:
            logger.warning("Page %d exchange error (%s) — stopping", page + 1, exc)
            break

        for row in rows:
            raw_by_ts[row[0]] = row

        print(f"  page {page + 1}/{pages}  ({len(raw_by_ts)} candles so far)", flush=True)

        if page < pages - 1:
            time.sleep(0.5)

    if not raw_by_ts:
        raise ValueError(f"No OHLCV data returned for {symbol} on {exchange_id}.")

    sorted_rows = sorted(raw_by_ts.values(), key=lambda r: r[0])

    candles = [
        Candle(
            timestamp = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
            open      = float(row[1]),
            high      = float(row[2]),
            low       = float(row[3]),
            close     = float(row[4]),
            volume    = float(row[5]),
        )
        for row in sorted_rows
    ]

    logger.info(
        "Paginated fetch complete: %d candles | %s → %s",
        len(candles),
        candles[0].timestamp.strftime("%Y-%m-%d %H:%M"),
        candles[-1].timestamp.strftime("%Y-%m-%d %H:%M"),
    )
    return candles


def slice_candles(
    candles:    list[Candle],
    start_date: str | None = None,   # "YYYY-MM-DD"
    end_date:   str | None = None,   # "YYYY-MM-DD" (exclusive)
) -> list[Candle]:
    """
    Return candles within [start_date, end_date).
    Both bounds are optional. Dates are UTC.
    """
    from datetime import datetime, timezone
    result = candles
    if start_date:
        dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        result = [c for c in result if c.timestamp >= dt]
    if end_date:
        dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        result = [c for c in result if c.timestamp < dt]
    return result
