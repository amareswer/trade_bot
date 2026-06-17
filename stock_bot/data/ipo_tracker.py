"""
Hot IPO / New Listing tracker.

WATCH_LIST is manually curated — add new symbols here as they happen.
get_recent_ipos() validates each symbol against yfinance before returning it,
so stale entries that haven't started trading yet are silently skipped.
"""
from __future__ import annotations

import logging

import yfinance as yf

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
            try:
                info = yf.Ticker(s).fast_info
                if info and getattr(info, "last_price", None) is not None:
                    valid.append(s)
                    logger.info("IPO tracker: %s is live", s)
                else:
                    logger.debug("IPO tracker: %s has no price data yet — skipping", s)
            except Exception as exc:
                logger.debug("IPO tracker: %s fast_info failed: %s", s, exc)
        return valid
