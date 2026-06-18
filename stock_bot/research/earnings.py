"""
Earnings data fetcher via yfinance.

Returns next earnings date, last EPS actual vs estimate, and a human-readable note.
All fields are optional — yfinance earnings coverage varies by symbol and region.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd
import yfinance as yf

_earnings_cache: dict[str, tuple[any, float]] = {}
_EARNINGS_TTL   = 86400  # 24 hours — earnings dates don't change intra-day

logger = logging.getLogger(__name__)


@dataclass
class EarningsInfo:
    next_earnings_date: Optional[date]  = None
    last_eps_actual:    Optional[float] = None
    last_eps_estimate:  Optional[float] = None
    eps_surprise_pct:   Optional[float] = None
    earnings_note:      str             = "No data"


def _surprise_note(actual: Optional[float], estimate: Optional[float]) -> str:
    if actual is None or estimate is None:
        return "No data"
    if abs(estimate) < 1e-9:
        return f"EPS actual: {actual:+.2f}"
    pct = (actual - estimate) / abs(estimate) * 100
    direction = "Beat" if pct >= 0 else "Missed"
    return f"{direction} by {abs(pct):.1f}%"


def fetch_earnings(symbol: str) -> EarningsInfo:
    """
    Fetch earnings data for `symbol` via yfinance. Cached for 24 hours.
    Returns EarningsInfo with all-None fields on failure.
    """
    cached = _earnings_cache.get(symbol)
    if cached is not None:
        info, ts = cached
        if time.time() - ts < _EARNINGS_TTL:
            return info

    try:
        ticker = yf.Ticker(symbol)

        # ── Next earnings date ──────────────────────────────────────────────
        next_date: Optional[date] = None
        try:
            cal = ticker.calendar
            if isinstance(cal, dict):
                raw_dates = cal.get("Earnings Date", [])
                if raw_dates:
                    d = raw_dates[0]
                    next_date = d.date() if hasattr(d, "date") else d
            elif isinstance(cal, pd.DataFrame) and not cal.empty:
                # Older yfinance returns a DataFrame transposed
                if "Earnings Date" in cal.index:
                    val = cal.loc["Earnings Date"].iloc[0]
                    next_date = val.date() if hasattr(val, "date") else val
        except Exception as exc:
            logger.debug("Calendar fetch failed for %s: %s", symbol, exc)

        # ── Last EPS actual vs estimate ─────────────────────────────────────
        actual: Optional[float]   = None
        estimate: Optional[float] = None
        try:
            ed = ticker.earnings_dates
            if ed is not None and isinstance(ed, pd.DataFrame) and not ed.empty:
                # Drop rows where Reported EPS is NaN (future dates)
                past = ed.dropna(subset=["Reported EPS"])
                if not past.empty:
                    row      = past.iloc[0]
                    actual   = float(row["Reported EPS"])
                    est_val  = row.get("EPS Estimate")
                    if est_val is not None and pd.notna(est_val):
                        estimate = float(est_val)
        except Exception as exc:
            logger.debug("Earnings dates fetch failed for %s: %s", symbol, exc)

        surprise: Optional[float] = None
        if actual is not None and estimate is not None and abs(estimate) > 1e-9:
            surprise = round((actual - estimate) / abs(estimate) * 100, 1)

        result = EarningsInfo(
            next_earnings_date = next_date,
            last_eps_actual    = actual,
            last_eps_estimate  = estimate,
            eps_surprise_pct   = surprise,
            earnings_note      = _surprise_note(actual, estimate),
        )
        _earnings_cache[symbol] = (result, time.time())
        return result

    except Exception as exc:
        logger.warning("Earnings fetch failed for %s: %s", symbol, exc)
        fallback = EarningsInfo(earnings_note="No data")
        _earnings_cache[symbol] = (fallback, time.time())
        return fallback
