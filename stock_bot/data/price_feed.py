"""
Stock price feed via yfinance.

Fetches OHLCV candles for any symbol — TSX (.TO suffix) and US markets
are handled transparently by yfinance with no special casing needed.

fetch_candles() is the only public function.
"""
from __future__ import annotations

import logging
import math
import os
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import yfinance as yf
from yfinance.exceptions import YFRateLimitError

logger = logging.getLogger(__name__)

MINIMUM_VALID_PRICE = 1.00  # reject any candle set whose latest close is below this

# Serializes yf.download() calls across threads; 0.5s sleep follows each call
_yf_download_lock = threading.Lock()

# Module-level cache reset each scan cycle via reset_price_cache()
_last_prices: dict[str, float] = {}

# Sector cache — persists for the process lifetime (one yfinance call per symbol)
_sector_cache: dict[str, str] = {}

# TSX-specific price corruption counter — incremented per rejection, never reset
_tsx_corruption_warnings: int = 0

# Multiplier for the within-fetch outlier check; override via PRICE_OUTLIER_FACTOR in .env
_PRICE_OUTLIER_FACTOR: float = float(os.getenv("PRICE_OUTLIER_FACTOR", "10"))


def get_tsx_warnings() -> int:
    """Return the number of TSX price corruption rejections since process start."""
    return _tsx_corruption_warnings


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
        if other_symbol != symbol and abs(other_price - price) / max(other_price, 0.01) < 0.001:
            logger.debug(
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
    _rl_delays = [5, 15, 30]
    with _yf_download_lock:
        df = None
        for attempt, delay in enumerate(_rl_delays):
            try:
                df = yf.download(
                    symbol,
                    period=f"{lookback_days}d",
                    interval=interval,
                    auto_adjust=True,
                    actions=False,
                    progress=False,
                )
                break
            except YFRateLimitError:
                if attempt < len(_rl_delays) - 1:
                    logger.warning("Rate limited fetching %s, waiting %ds", symbol, delay)
                    time.sleep(delay)
                else:
                    logger.error(
                        "Rate limit: giving up on %s after 3 attempts", symbol
                    )
                    time.sleep(0.5)
                    return None
            except Exception as e:
                logger.warning("fetch failed %s: %s", symbol, e)
                time.sleep(0.5)
                return None
        time.sleep(0.5)

    if df is None or df.empty:
        return None

    # Flatten MultiIndex columns yfinance >= 0.2.38 returns for a single ticker
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

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

    # Outlier check: latest close vs median of this same download's candles.
    # Catches single-candle corruption without relying on any stored historical state.
    closes = [c.close for c in candles]
    median_close = statistics.median(closes)
    if median_close > 0 and latest > median_close * _PRICE_OUTLIER_FACTOR:
        logger.warning(
            "Price outlier detected: %s close $%.2f vs median $%.2f",
            symbol, latest, median_close,
        )
        return None

    # TSX-specific check: candle close vs live fast_info.last_price (5% tolerance).
    # Catches currency-mismatch corruption (e.g. USD price bled into a CAD ticker).
    # Non-.TO symbols are intentionally excluded — fast_info adds ~2s/symbol overhead.
    if symbol.upper().endswith(".TO"):
        tsx_last_price = None
        tsx_prev_close = None
        try:
            fi = yf.Ticker(symbol).fast_info
            tsx_last_price = getattr(fi, "last_price", None) or getattr(fi, "lastPrice", None)
            tsx_prev_close = getattr(fi, "previous_close", None) or getattr(fi, "previousClose", None)
        except Exception:
            pass
        if tsx_prev_close and tsx_prev_close > 0:
            prev_deviation = abs(latest - tsx_prev_close) / tsx_prev_close
            if prev_deviation > 0.20:
                logger.warning(
                    "%s — price mismatch: candle close $%.2f vs fast_info.previous_close $%.2f "
                    "(%.1f%% deviation) — rejecting as corrupted data",
                    symbol, latest, tsx_prev_close, prev_deviation * 100,
                )
                return None
        if tsx_last_price and tsx_last_price > 0:
            tsx_deviation = abs(latest - tsx_last_price) / tsx_last_price
            if tsx_deviation > 0.05:
                global _tsx_corruption_warnings
                _tsx_corruption_warnings += 1
                logger.warning(
                    "%s — TSX price mismatch: candle close $%.2f vs fast_info.last_price $%.2f "
                    "(%.1f%% deviation) — rejecting as corrupted data",
                    symbol, latest, tsx_last_price, tsx_deviation * 100,
                )
                return None

    logger.debug("Fetched %d candles for %s (interval=%s)", len(candles), symbol, interval)
    return candles


def latest_price(symbol: str) -> Optional[float]:
    """Quick single-price fetch — returns None on failure."""
    candles = fetch_candles(symbol, interval="1d", lookback_days=5)
    return candles[-1].close if candles else None
