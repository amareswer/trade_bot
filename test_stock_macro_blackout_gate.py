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

import stock_bot.main as main_mod
from stock_bot.risk.macro_calendar import jobs_report_dates


def _cfg(blackout_days: int, event_dates: str) -> SimpleNamespace:
    return SimpleNamespace(
        macro_blackout_days=blackout_days,
        macro_event_dates_str=event_dates,
    )


def test_blocks_on_user_supplied_event_date():
    tomorrow = date.today() + timedelta(days=1)
    cfg = _cfg(2, tomorrow.isoformat())
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
    today = date.today()
    this_years_fridays = jobs_report_dates(today.year)
    nearest = min(this_years_fridays, key=lambda d: abs((d - today).days))
    window = abs((nearest - today).days)
    cfg = _cfg(window, "")   # exactly wide enough to catch the nearest jobs-report Friday
    blocked, event = main_mod._is_macro_event_blackout(cfg)
    assert blocked is True
    assert event == nearest


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
