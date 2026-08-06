"""
Macro economic event blackout — restricts new BUYs around major market-wide
volatility events (FOMC, CPI, GDP, employment reports). Closes punch-list
item #5 from the trading-spec gap review (see CLAUDE.md).

Two sources of event dates, deliberately kept separate:

  1. jobs_report_dates() — computed algorithmically. The U.S. Non-Farm
     Payrolls report is always released the first Friday of the month, so
     this is always correct with zero maintenance and no external data.

  2. User-maintained dates (MACRO_EVENT_DATES in stock_bot/.env) — FOMC
     meeting dates, CPI releases, and GDP releases do NOT follow a clean
     weekday rule; the Fed/BLS/BEA set exact dates on their own release
     calendars, typically months in advance. This module does not fabricate
     those dates — ships with an empty list. Add real ones as they're
     published:
       FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
       CPI:  https://www.bls.gov/schedule/news_release/cpi.htm
       GDP:  https://www.bea.gov/news/schedule
     Format: MACRO_EVENT_DATES=2026-09-16,2026-09-17,2026-10-14 (comma-
     separated ISO dates; a two-day FOMC meeting needs both days listed).

Blackout window is symmetric (days before AND after — a surprise print can
keep moving the market into the next session, not just up to the release).
"""
from __future__ import annotations

import calendar
import logging
from datetime import date

logger = logging.getLogger(__name__)


def jobs_report_dates(year: int) -> list[date]:
    """First Friday of each month in `year` — the Non-Farm Payrolls release day."""
    dates = []
    for month in range(1, 13):
        month_cal = calendar.monthcalendar(year, month)
        friday = month_cal[0][calendar.FRIDAY]
        if friday == 0:   # first week doesn't contain a Friday (0 = no such day)
            friday = month_cal[1][calendar.FRIDAY]
        dates.append(date(year, month, friday))
    return dates


def parse_user_event_dates(raw: str) -> list[date]:
    """
    Parse a comma-separated MACRO_EVENT_DATES string into date objects.
    A malformed entry is skipped with a warning rather than raising — a
    typo in .env shouldn't crash the bot or block all trading.
    """
    dates: list[date] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            dates.append(date.fromisoformat(token))
        except ValueError:
            logger.warning("MACRO_EVENT_DATES: skipping invalid date %r", token)
    return dates


def is_macro_blackout(
    today: date, blackout_days: int, user_dates: list[date],
) -> tuple[bool, date | None]:
    """
    Returns (blocked, event_date) — blocked=True if `today` falls within
    blackout_days (inclusive, symmetric) of a jobs-report date or a
    user-supplied macro event date. event_date is the nearest such date
    when blocked, else None.

    blackout_days <= 0 always returns (False, None) — the feature is
    effectively off (mirrors _is_earnings_blackout's own guard).
    """
    if blackout_days <= 0:
        return False, None

    candidates = (
        jobs_report_dates(today.year - 1)
        + jobs_report_dates(today.year)
        + jobs_report_dates(today.year + 1)
        + user_dates
    )
    best: date | None = None
    best_distance = blackout_days + 1
    for d in candidates:
        distance = abs((d - today).days)
        if distance <= blackout_days and distance < best_distance:
            best = d
            best_distance = distance
    return (best is not None), best
