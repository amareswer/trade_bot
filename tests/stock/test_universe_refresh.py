"""
Top-movers universe refresh — persistence helpers + the 2026-08-27 bugfix.

The bug: run()'s universe refresh was gated on `now_et.hour == _UNIVERSE_REFRESH_HOUR`
(16). That block only runs in LIVE mode (market open), but at 16:00 ET the market
is closed and the loop is already in AFTER_HOURS mode and has `continue`d — so the
refresh could never fire. Across 3 days of logs: 179 "Universe: waiting for 16:00
ET refresh" lines, 0 "Universe refreshed". The bot scanned watchlist-only forever.

Fix: refresh on the first LIVE cycle of each day, and persist the movers list so a
restart (frequent on this project) doesn't drop back to watchlist-only until the
next daily refresh. run() needs a full live stack, so the trigger change is a
source-inspection guard; the persistence helpers are tested behaviorally.
"""
import inspect
import json

import pytest

import stock_bot.main as main_mod
from stock_bot.main import _load_persisted_movers, _persist_movers


@pytest.fixture
def movers_file(tmp_path, monkeypatch):
    p = tmp_path / "universe_movers.json"
    monkeypatch.setattr(main_mod, "_MOVERS_STATE_FILE", str(p))
    return p


# ── persistence helpers ──────────────────────────────────────────────────────

def test_persist_then_load_round_trips_same_day(movers_file):
    _persist_movers("2026-08-27", ["NVDA", "SMCI", "AVGO"])
    assert _load_persisted_movers("2026-08-27") == ["NVDA", "SMCI", "AVGO"]


def test_load_returns_empty_for_a_different_date(movers_file):
    _persist_movers("2026-08-27", ["NVDA", "SMCI"])
    assert _load_persisted_movers("2026-08-28") == []


def test_load_returns_empty_when_file_missing(movers_file):
    assert not movers_file.exists()
    assert _load_persisted_movers("2026-08-27") == []


def test_load_returns_empty_on_corrupt_json(movers_file):
    movers_file.write_text("{not valid json", encoding="utf-8")
    assert _load_persisted_movers("2026-08-27") == []


def test_load_returns_empty_when_movers_key_not_a_list(movers_file):
    movers_file.write_text(json.dumps({"date": "2026-08-27", "movers": "NVDA"}),
                           encoding="utf-8")
    assert _load_persisted_movers("2026-08-27") == []


def test_load_preserves_order(movers_file):
    ordered = ["D", "C", "B", "A"]
    _persist_movers("2026-08-27", ordered)
    assert _load_persisted_movers("2026-08-27") == ordered


def test_persist_never_raises_on_bad_path(monkeypatch):
    monkeypatch.setattr(main_mod, "_MOVERS_STATE_FILE", "/nonexistent-dir/x/y.json")
    _persist_movers("2026-08-27", ["NVDA"])   # must not raise


# ── the bugfix — source-inspection guards on run() ───────────────────────────

def test_refresh_no_longer_gated_on_the_unreachable_hour():
    src = inspect.getsource(main_mod.run)
    assert "now_et.hour == _UNIVERSE_REFRESH_HOUR" not in src, (
        "The universe refresh must not be gated on now_et.hour == 16 — that "
        "branch is unreachable (market closed at 16:00 → AFTER_HOURS → continue "
        "before this code). It ran watchlist-only for the bot's entire history."
    )


def test_refresh_triggers_on_first_live_cycle_of_the_day():
    src = inspect.getsource(main_mod.run)
    assert "_needs_refresh" in src and "_last_universe_refresh.date() != now_et.date()" in src, (
        "Expected a once-per-day trigger (first LIVE cycle) instead of a fixed hour."
    )


def test_failed_refresh_does_not_wipe_existing_movers():
    src = inspect.getsource(main_mod.run)
    # the fresh result lands in a temp name and only replaces top_movers on success
    assert "_fresh = _universe.pre_filter(" in src
    idx = src.index("_fresh = _universe.pre_filter(")
    tail = src[idx:idx + 900]
    assert "if _fresh and _fresh != _fallback_slice:" in tail
    assert "top_movers  = _fresh" in tail


def test_run_restores_persisted_movers_at_startup():
    src = inspect.getsource(main_mod.run)
    assert "_load_persisted_movers(" in src, (
        "Startup must reload today's persisted movers so a restart doesn't drop "
        "back to watchlist-only until the next daily refresh."
    )


def test_successful_refresh_persists():
    src = inspect.getsource(main_mod.run)
    assert "_persist_movers(now_et.date().isoformat(), top_movers)" in src
