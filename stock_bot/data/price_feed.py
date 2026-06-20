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

MINIMUM_VALID_PRICE = 1.00  # reject any candle set whose latest close is below this

# Module-level cache reset each scan cycle via reset_price_cache()
_last_prices: dict[str, float] = {}

# Sector cache — persists for the process lifetime (one yfinance call per symbol)
_sector_cache: dict[str, str] = {}


def get_sector(symbol: str) -> str:
    """
    Fetch the sector for a symbol from yfinance.
    Returns a normalized lowercase sector string.
    Falls back to "other" on any failure.
    Cached in _sector_cache to avoid repeat API calls.
    """
    sym = symbol.upper()
    if sym in _sector_cache:
        return _sector_cache[sym]
    try:
        info = yf.Ticker(sym).info
        sector = info.get("sector", "") or ""
        normalized = sector.lower().strip()
        if not normalized:
            normalized = "other"
        _sector_cache[sym] = normalized
    except Exception:
        _sector_cache[sym] = "other"
    return _sector_cache[sym]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Candle:
    timestamp:    datetime
    open:         float
    high:         float
    low:          float
    close:        float
    volume:       float
    volume_ratio: float | None = None  # today's volume ÷ 20-day average


# ---------------------------------------------------------------------------
# Price sanity validation
# ---------------------------------------------------------------------------

def reset_price_cache() -> None:
    """Clear the per-cycle duplicate price cache. Call once at the start of each scan."""
    global _last_prices
    _last_prices = {}


def _is_duplicate_price(symbol: str, price: float) -> bool:
    """
    Detect when yfinance returns the same price for multiple different symbols.
    This is the signature of holiday data corruption (one ticker's price bleeds
    into others). Returns True and logs a warning when corruption is detected.
    """
    for other_symbol, other_price in _last_prices.items():
        if other_symbol != symbol and abs(other_price - price) < 0.01:
            logger.warning(
                "%s price $%.2f matches %s — holiday data corruption, rejecting",
                symbol, price, other_symbol,
            )
            return True
    _last_prices[symbol] = price
    return False


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

    # Attach volume ratio (today vs 20-day average) to the latest candle
    volumes = [c.volume for c in candles if c.volume and c.volume > 0]
    avg_vol_20 = sum(volumes[-20:]) / len(volumes[-20:]) if len(volumes) >= 20 else None
    if avg_vol_20 and avg_vol_20 > 0:
        candles[-1].volume_ratio = round(candles[-1].volume / avg_vol_20, 2)

    if len(candles) < 26:
        logger.info("%s — only %d candles (new IPO or thin history)", symbol, len(candles))

    latest = candles[-1].close
    if latest < MINIMUM_VALID_PRICE:
        logger.warning(
            "%s price $%.4f is below $%.2f minimum — rejecting as corrupted data",
            symbol, latest, MINIMUM_VALID_PRICE,
        )
        return None

    if latest <= 0:
        logger.warning("%s price $%.2f ≤ 0 — rejecting", symbol, latest)
        return None

    if latest > 500_000:
        logger.warning("%s price $%.2f > $500k — rejecting", symbol, latest)
        return None

    if _is_duplicate_price(symbol, latest):
        return None

    logger.debug("Fetched %d candles for %s (interval=%s)", len(candles), symbol, interval)
    return candles


def latest_price(symbol: str) -> Optional[float]:
    """Quick single-price fetch — returns None on failure."""
    candles = fetch_candles(symbol, interval="1d", lookback_days=5)
    return candles[-1].close if candles else None
