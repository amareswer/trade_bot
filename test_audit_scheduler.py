"""In-bot scheduled audits (cron replacement, 2026-07-14).

Tests the REAL `_audit_due()` from bot.main — the pure due-check behind the
scheduled-audits daemon thread that replaced the macOS cron jobs (cron failed
twice: lid-close sleep, then TCC "Operation not permitted" on ~/Desktop).
"""
from datetime import datetime

from bot.main import _audit_due


def _dt(day: str, hhmm: str) -> datetime:
    return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")


# ── Daily job (shadow signal) ────────────────────────────────────────────────

def test_daily_not_due_before_run_time():
    # 2026-07-14 is a Tuesday
    assert _audit_due(None, _dt("2026-07-14", "09:00"), "12:05") is False


def test_daily_due_after_run_time_when_never_run():
    assert _audit_due(None, _dt("2026-07-14", "12:05"), "12:05") is True


def test_daily_catches_up_late_in_day():
    # Bot restarted at 15:00 — the missed 12:05 audit still fires.
    assert _audit_due("2026-07-13", _dt("2026-07-14", "15:00"), "12:05") is True


def test_daily_not_due_twice_same_day():
    assert _audit_due("2026-07-14", _dt("2026-07-14", "18:00"), "12:05") is False


# ── Weekly job (live comparison, Monday-anchored) ───────────────────────────

def test_weekly_not_due_when_run_this_week():
    # Ran Monday 2026-07-13; Wednesday same week → not due.
    assert _audit_due(
        "2026-07-13", _dt("2026-07-15", "18:00"), "12:10", weekly_monday=True
    ) is False


def test_weekly_due_monday_after_run_time():
    assert _audit_due(
        "2026-07-06", _dt("2026-07-13", "12:10"), "12:10", weekly_monday=True
    ) is True


def test_weekly_not_due_monday_before_run_time():
    assert _audit_due(
        "2026-07-06", _dt("2026-07-13", "08:00"), "12:10", weekly_monday=True
    ) is False


def test_weekly_catches_up_midweek_when_monday_missed():
    # Machine was off Monday; Thursday morning (before run_at) still fires.
    assert _audit_due(
        "2026-07-06", _dt("2026-07-16", "08:00"), "12:10", weekly_monday=True
    ) is True


# ── Monthly job (re-screen, 1st-of-month anchored) ──────────────────────────

def test_monthly_not_due_first_before_run_time():
    assert _audit_due(
        "2026-06-01", _dt("2026-07-01", "08:00"), "12:20", monthly_first=True
    ) is False


def test_monthly_due_first_after_run_time():
    assert _audit_due(
        "2026-06-01", _dt("2026-07-01", "12:20"), "12:20", monthly_first=True
    ) is True


def test_monthly_due_when_never_run():
    assert _audit_due(
        None, _dt("2026-07-16", "08:00"), "12:20", monthly_first=True
    ) is True


def test_monthly_catches_up_midmonth_when_first_missed():
    # Bot was down on the 1st; the 16th (even before run_at) still fires.
    assert _audit_due(
        "2026-06-02", _dt("2026-07-16", "08:00"), "12:20", monthly_first=True
    ) is True


def test_monthly_not_due_twice_same_month():
    # Ran on the 3rd (catch-up); the 20th of the same month → not due.
    assert _audit_due(
        "2026-07-03", _dt("2026-07-20", "18:00"), "12:20", monthly_first=True
    ) is False


def test_monthly_rearms_next_month():
    assert _audit_due(
        "2026-07-03", _dt("2026-08-01", "12:20"), "12:20", monthly_first=True
    ) is True
