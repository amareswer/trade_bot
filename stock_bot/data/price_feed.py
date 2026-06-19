"""
Stock price feed via yfinance.

Fetches OHLCV candles for any symbol — TSX (.TO suffix) and US markets
are handled transparently by yfinance with no special casing needed.

fetch_candles() is the only public function.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Candle:
    timestamp: datetime
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_candles(
    symbol:        str,
    interval:      str = "1d",
    lookback_days: int = 200,
) -> list[Candle] | None:
    """
    Fetch up to `lookback_days` of OHLCV candles for `symbol`.

    Works for:
      - US equities:  "AAPL", "NVDA", "MSFT"
      - TSX equities: "SHOP.TO", "RY.TO", "AC.TO"
    """
    try:
        df = yf.download(
            symbol,
            period=f"{lookback_days}d",
            interval=interval,
            auto_adjust=True,
            progress=False,
        )
        if df is None or df.empty:
            return None

        # Flatten MultiIndex columns yfinance >= 0.2.38 returns for a single ticker
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

    except Exception as e:
        logger.warning("fetch failed %s: %s", symbol, e)
        return None

    candles: list[Candle] = []
    for ts, row in df.iterrows():
        try:
            close = float(row["Close"])
            if math.isnan(close) or close <= 0 or close > 100_000:
                continue
            candles.append(Candle(
                timestamp = ts.to_pydatetime(),
                open      = float(row["Open"]),
                high      = float(row["High"]),
                low       = float(row["Low"]),
                close     = close,
                volume    = float(row["Volume"]),
            ))
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("Skipping malformed row for %s at %s: %s", symbol, ts, exc)

    if not candles:
        logger.warning("All rows were NaN or malformed for %s", symbol)
        return None

    if len(candles) < 26:
        logger.info("%s — only %d candles (new IPO or thin history)", symbol, len(candles))

    logger.debug("Fetched %d candles for %s (interval=%s)", len(candles), symbol, interval)
    return candles


def latest_price(symbol: str) -> Optional[float]:
    """Quick single-price fetch — returns None on failure."""
    candles = fetch_candles(symbol, interval="1d", lookback_days=5)
    return candles[-1].close if candles else None
