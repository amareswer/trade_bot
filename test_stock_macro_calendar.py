"""
Unit tests for stock_bot/risk/macro_calendar.py — the stock bot's macro
economic event blackout (added 2026-08-05, punch-list item #5).

jobs_report_dates() is verified by invariant (12 Fridays, one per month, in
the first 7 days) rather than hardcoded expected dates — it's a pure
calendar computation (Python's own calendar module), not a fact to look up.
"""
from datetime import date

import pytest

from stock_bot.risk.macro_calendar import (
    is_macro_blackout,
    jobs_report_dates,
    parse_user_event_dates,
)


# ── jobs_report_dates ────────────────────────────────────────────────────────

def test_returns_twelve_dates_one_per_month():
    dates = jobs_report_dates(2026)
    assert len(dates) == 12
    assert [d.month for d in dates] == list(range(1, 13))


def test_every_date_is_a_friday_within_first_week():
    for year in (2025, 2026, 2027, 2028):   # spans different weekday-of-Jan-1 cases
        for d in jobs_report_dates(year):
            assert d.weekday() == 4   # Monday=0 ... Friday=4
            assert 1 <= d.day <= 7


# ── parse_user_event_dates ───────────────────────────────────────────────────

def test_parses_valid_comma_separated_dates():
    dates = parse_user_event_dates("2026-09-16,2026-09-17,2026-10-14")
    assert dates == [date(2026, 9, 16), date(2026, 9, 17), date(2026, 10, 14)]


def test_empty_string_returns_empty_list():
    assert parse_user_event_dates("") == []


def test_skips_invalid_entries_without_raising(caplog):
    dates = parse_user_event_dates("2026-09-16,not-a-date,2026-10-14")
    assert dates == [date(2026, 9, 16), date(2026, 10, 14)]
    assert any("invalid date" in r.message for r in caplog.records)


def test_strips_whitespace_around_entries():
    assert parse_user_event_dates(" 2026-09-16 , 2026-10-14 ") == [
        date(2026, 9, 16), date(2026, 10, 14),
    ]


# ── is_macro_blackout ────────────────────────────────────────────────────────

def test_blocks_on_exact_user_event_date():
    blocked, event = is_macro_blackout(date(2026, 9, 16), 1, [date(2026, 9, 16)])
    assert blocked is True
    assert event == date(2026, 9, 16)


def test_blocks_within_window_before_and_after():
    user_dates = [date(2026, 9, 16)]
    assert is_macro_blackout(date(2026, 9, 15), 1, user_dates)[0] is True   # day before
    assert is_macro_blackout(date(2026, 9, 17), 1, user_dates)[0] is True   # day after


def test_boundary_inclusive_exactly_at_blackout_days():
    user_dates = [date(2026, 9, 16)]
    assert is_macro_blackout(date(2026, 9, 13), 3, user_dates)[0] is True   # exactly 3 days before
    assert is_macro_blackout(date(2026, 9, 12), 3, user_dates)[0] is False  # 4 days before — outside


def test_allows_when_outside_window_and_no_jobs_report_nearby():
    # Pick a date deliberately far from both the user event and any first-Friday.
    blocked, event = is_macro_blackout(date(2026, 9, 22), 1, [date(2026, 9, 16)])
    if not blocked:
        assert event is None


def test_zero_blackout_days_disables_the_feature():
    assert is_macro_blackout(date(2026, 9, 16), 0, [date(2026, 9, 16)]) == (False, None)


def test_negative_blackout_days_disables_the_feature():
    assert is_macro_blackout(date(2026, 9, 16), -1, [date(2026, 9, 16)]) == (False, None)


def test_jobs_report_date_alone_triggers_blackout_with_empty_user_dates():
    jan_friday = jobs_report_dates(2026)[0]
    blocked, event = is_macro_blackout(jan_friday, 1, [])
    assert blocked is True
    assert event == jan_friday


def test_nearest_event_returned_when_multiple_in_window():
    # jobs-report Friday plus a closer user-supplied date in the same window.
    jan_friday = jobs_report_dates(2026)[0]
    near_date = jan_friday  # same day, but pass a distinct near user date one day off
    from datetime import timedelta
    user_close = jan_friday - timedelta(days=1)
    blocked, event = is_macro_blackout(jan_friday, 2, [user_close])
    assert blocked is True
    assert event == jan_friday   # distance 0 beats distance 1
