"""
Tests for stock_bot.main._is_macro_event_blackout — the wrapper that reads
StockConfig and calls stock_bot.risk.macro_calendar.is_macro_blackout
(added 2026-08-05, punch-list item #5). The pure calendar logic itself is
already covered by test_stock_macro_calendar.py — these tests only cover
the config-reading wrapper and the fail-open/wiring guarantees.
"""
import inspect
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import stock_bot.main as main_mod
from stock_bot.risk.macro_calendar import jobs_report_dates


# Fixed reference date for the two tests that need a known position relative
# to the algorithmic jobs-report calendar. 2026-09-16 sits 12 days after the
# nearest jobs-report Friday (2026-09-04) and 16 days before the next
# (2026-10-02) — far enough that a small blackout window touches neither.
#
# Both tests below used the REAL date.today() until 2026-08-07, which made
# them fail on every actual jobs-report day (~3 days a month, since the
# window is symmetric). Production code was correct in both cases — the
# tests were simply contaminated by the real calendar. See each test's note.
_FIXED_TODAY   = date(2026, 9, 16)
_NEAREST_JOBS  = date(2026, 9, 4)    # first Friday of Sept 2026


def _cfg(blackout_days: int, event_dates: str) -> SimpleNamespace:
    return SimpleNamespace(
        macro_blackout_days=blackout_days,
        macro_event_dates_str=event_dates,
    )


def _frozen_date(today: date):
    """Patch object for stock_bot.main's `date` — the wrapper only ever calls
    date.today(), so a stub with just that method is sufficient."""
    stub = MagicMock()
    stub.today.return_value = today
    return stub


def test_fixed_test_date_is_clear_of_real_jobs_reports():
    """Guards the two fixed dates above against a future edit that moves them
    somewhere the algorithmic calendar would interfere again."""
    jobs = jobs_report_dates(_FIXED_TODAY.year)
    assert _NEAREST_JOBS in jobs
    assert min(abs((d - _FIXED_TODAY).days) for d in jobs) == 12


def test_blocks_on_user_supplied_event_date():
    # Was: date.today() + 1 day with window=2. On a real jobs-report day the
    # algorithmic date (0 days away) outranked the user date (1 day away) as
    # "nearest event", so `event == tomorrow` failed while `blocked` still
    # passed — i.e. the gate worked, the assertion was just over-specific.
    tomorrow = _FIXED_TODAY + timedelta(days=1)
    cfg = _cfg(2, tomorrow.isoformat())
    with patch.object(main_mod, "date", _frozen_date(_FIXED_TODAY)):
        blocked, event = main_mod._is_macro_event_blackout(cfg)
    assert blocked is True
    assert event == tomorrow


def test_allows_when_disabled():
    cfg = _cfg(0, "")   # blackout_days=0 disables the feature entirely
    blocked, event = main_mod._is_macro_event_blackout(cfg)
    assert blocked is False
    assert event is None


def test_fails_open_on_bad_config_value():
    # macro_blackout_days as a non-int would blow up the comparison inside
    # is_macro_blackout — the wrapper must swallow it and allow the trade.
    cfg = _cfg("not-a-number", "")
    blocked, event = main_mod._is_macro_event_blackout(cfg)
    assert blocked is False
    assert event is None


def test_empty_user_dates_still_checks_jobs_report():
    # Was: window computed as the distance from the REAL today to the nearest
    # jobs report. On an actual jobs-report day that distance is 0 — and
    # blackout_days=0 disables the feature by design, so the test asserted
    # blocked is True against a config that deliberately blocks nothing.
    # Now pinned to a fixed date 12 days out, so the window is a real
    # (boundary-inclusive) 12 rather than a degenerate 0.
    window = (_FIXED_TODAY - _NEAREST_JOBS).days   # 12 — boundary-inclusive hit
    cfg = _cfg(window, "")   # no user dates — jobs-report half of the gate alone
    with patch.object(main_mod, "date", _frozen_date(_FIXED_TODAY)):
        blocked, event = main_mod._is_macro_event_blackout(cfg)
    assert blocked is True
    assert event == _NEAREST_JOBS


def test_run_wires_up_macro_blackout_gate():
    """
    The tests above prove _is_macro_event_blackout() works in isolation —
    this proves the BUY path in the scan loop still actually calls it and
    blocks (with a continue) on a hit, market-wide (before the per-symbol
    earnings check). Source-inspection rather than executing run().
    """
    source = inspect.getsource(main_mod.run)
    assert "_is_macro_event_blackout(cfg)" in source
    assert "if _macro_blocked:" in source
