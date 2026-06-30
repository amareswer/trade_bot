"""
Hot IPO / New Listing tracker.

WATCH_LIST is manually curated — add new symbols here as they happen.
get_recent_ipos() validates each symbol against yfinance before returning it,
so stale entries that haven't started trading yet are silently skipped.
"""
from __future__ import annotations

import logging

import yfinance as yf

from stock_bot.data.yf_client import fetch_with_retry

logger = logging.getLogger(__name__)


class IPOTracker:
    WATCH_LIST: list[str] = [
        "SPCX",   # SpaceX IPO — June 12 2026
        # Add new IPOs here as they happen
    ]

    def get_recent_ipos(self) -> list[str]:
        """Return WATCH_LIST symbols that yfinance can currently fetch."""
        valid: list[str] = []
        for s in self.WATCH_LIST:
            def _fetch_ipo(sym=s):
                fi = yf.Ticker(sym).fast_info
                lp = getattr(fi, "last_price", None)  # force lazy eval — RL surfaces here
                return fi, lp

            result = fetch_with_retry(_fetch_ipo, label=f"{s}:ipo_check")
            if result is None:
                logger.debug("IPO tracker: %s fetch failed — skipping", s)
                continue

            info, last_price = result
            if info and last_price is not None:
                valid.append(s)
                logger.info("IPO tracker: %s is live", s)
            else:
                logger.debug("IPO tracker: %s has no price data yet — skipping", s)
        return valid
