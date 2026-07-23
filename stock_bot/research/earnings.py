"""
Earnings data fetcher via yfinance.

Returns next earnings date, last EPS actual vs estimate, and a human-readable note.
All fields are optional — yfinance earnings coverage varies by symbol and region.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd
import yfinance as yf

from stock_bot.data.yf_client import fetch_with_retry

# 2026-07-23: this module is the one of three yfinance call sites in the repo
# with NO lock — stock_bot/data/price_feed.py (_yf_download_lock) and
# stock_bot/fast_validator.py (_yf_lock) both already serialize their calls
# for this exact reason. main.py's research phase runs earnings fetches for
# up to 5 symbols concurrently (ThreadPoolExecutor max_workers=5); serializing
# them removes that self-inflicted concurrent load. Could not force a 100%
# reproduction of the intermittent "Fetch failed X:earnings: ['Earnings
# Date']" failures in isolated testing (consistent with a low, probabilistic
# per-call failure rate rather than a deterministic bug) — this closes the
# one structural gap found, matching the pattern already adopted elsewhere.
_yf_lock = threading.Lock()

_earnings_cache: dict[str, tuple[any, float, bool]] = {}   # (info, timestamp, was_success)
_EARNINGS_TTL         = 86400  # 24 hours — earnings dates don't change intra-day
_EARNINGS_FAILURE_TTL = 3600   # 1 hour — a fetch failure is usually transient (yfinance
                                # hiccup / contention), not a real "no earnings data"
                                # result; retry much sooner instead of silently disabling
                                # the earnings blackout safety feature for a full day
                                # (incident: 2026-07-23, NVDA/RY failed once, then fetched
                                # cleanly moments later on manual retest)

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
        info, ts, was_success = cached
        ttl = _EARNINGS_TTL if was_success else _EARNINGS_FAILURE_TTL
        if time.time() - ts < ttl:
            return info

    def _fetch_earnings():
        with _yf_lock:
            t   = yf.Ticker(symbol)
            cal = t.calendar        # force lazy network call — RL surfaces here
            ed  = t.earnings_dates  # same
            return t, cal, ed

    raw = fetch_with_retry(_fetch_earnings, label=f"{symbol}:earnings")
    if raw is None:
        fallback = EarningsInfo(earnings_note="No data")
        _earnings_cache[symbol] = (fallback, time.time(), False)
        return fallback

    ticker, cal, ed = raw

    try:
        # ── Next earnings date ──────────────────────────────────────────────
        next_date: Optional[date] = None
        try:
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
        _earnings_cache[symbol] = (result, time.time(), True)
        return result

    except Exception as exc:
        logger.warning("Earnings fetch failed for %s: %s", symbol, exc)
        fallback = EarningsInfo(earnings_note="No data")
        _earnings_cache[symbol] = (fallback, time.time(), False)
        return fallback
