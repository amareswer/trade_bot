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
from datetime import datetime, timedelta, timezone as _tz
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)

_name_cache: dict[str, str] = {}


def _cache_name_from_ticker(symbol: str, ticker: yf.Ticker) -> None:
    if symbol in _name_cache:
        return
    clean = symbol.replace(".TO", "")
    try:
        meta  = getattr(ticker, "history_metadata", {}) or {}
        short = meta.get("shortName", "")
        _name_cache[symbol] = short if short and len(short) <= 25 else clean
    except Exception:
        _name_cache[symbol] = clean


def get_cached_name(symbol: str) -> str:
    return _name_cache.get(symbol, symbol.replace(".TO", ""))


@dataclass
class Candle:
    timestamp: datetime
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float


def _has_valid_data(df) -> bool:
    """Return True if df contains at least one non-NaN Close value."""
    if df is None or df.empty:
        return False
    return "Close" in df.columns and not df["Close"].isna().all()


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

    Tries the requested interval first; if data is empty or all NaN,
    retries once with period="5d" interval="1d".  Returns None (not [])
    when both attempts yield no usable data.
    """
    start = datetime.now(_tz.utc) - timedelta(days=lookback_days)
    df = None

    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start.strftime("%Y-%m-%d"), interval=interval)
    except Exception as exc:
        logger.debug("yfinance primary fetch failed for %s: %s", symbol, exc)

    if not _has_valid_data(df):
        logger.debug(
            "%s — %s interval returned no data; retrying with period=5d interval=1d",
            symbol, interval,
        )
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="5d", interval="1d")
        except Exception as exc:
            logger.debug("yfinance fallback fetch failed for %s: %s", symbol, exc)
            df = None

    if not _has_valid_data(df):
        logger.warning(
            "No data for %s after retry — market may be closed or symbol unknown",
            symbol,
        )
        return None

    candles: list[Candle] = []
    for ts, row in df.iterrows():
        try:
            close = float(row["Close"])
            if math.isnan(close):
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
        logger.info("%s — only %d candles of data (new IPO)", symbol, len(candles))

    _cache_name_from_ticker(symbol, ticker)
    logger.debug("Fetched %d candles for %s (interval=%s)", len(candles), symbol, interval)
    return candles


def latest_price(symbol: str) -> Optional[float]:
    """Quick single-price fetch — returns None on failure."""
    candles = fetch_candles(symbol, interval="1d", lookback_days=5)
    return candles[-1].close if candles else None
