"""
Regression test for the stale-mark daily-loss breaker gap fixed 2026-07-28.

Bug: StockPaperExecutor._open_position_value was only refreshed inside
buy()/sell() at fill time. Between fills — which can be days apart — the
daily-loss breaker (_is_daily_loss_tripped) checked drawdown against a
stale mark, so a held position that moved significantly with no new trade
was invisible to the breaker until the next fill (which might never come).

Fix: stock_bot/main.py's scan loop now calls the module-level
_mark_positions_to_market(executor, price_data) once per cycle, right
after Phase 1 prices are fetched and before any buy/sell decision runs.

This test imports and calls _mark_positions_to_market directly from
stock_bot.main — the exact function object run() calls — and drives it
through a mocked _fetch_symbol_data (the scan loop's price fetch), not
through executor internals. It proves the wiring in the real cycle path,
not just that StockPaperExecutor's own methods work in isolation
(test_stock_breaker.py already covers that).

State/CSV paths are monkeypatched to a tmp dir so tests never touch the
real paper_state.json / paper_trades.csv (see .memory/core.md rule 10).
"""
import inspect

import pytest

import stock_bot.execution.paper as paper_mod
import stock_bot.main as main_mod
from stock_bot.execution.paper import StockPaperExecutor
from stock_bot.data.price_feed import _sector_cache


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect all paper-state files to tmp and stub the sector lookup."""
    monkeypatch.setattr(paper_mod, "_STATE_JSON", str(tmp_path / "state.json"))
    monkeypatch.setattr(paper_mod, "_TRADES_CSV", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(paper_mod, "_RESET_FLAG", str(tmp_path / ".reset"))
    _sector_cache["TEST"] = "other"
    return tmp_path


def _fetch_scan_cycle_price_data(monkeypatch, prices: dict[str, float]) -> dict:
    """
    Mock stock_bot.main._fetch_symbol_data — the scan loop's price fetch —
    and build the price_data map exactly as run()'s Phase 1 does (minus the
    ThreadPoolExecutor, which is a concurrency detail, not semantics): call
    the module's own _fetch_symbol_data reference once per symbol.
    """
    def _fake_fetch_symbol_data(symbol, cfg, screener, watchlist_set, market_status=None):
        return {"price": prices[symbol], "screened": False}

    monkeypatch.setattr(main_mod, "_fetch_symbol_data", _fake_fetch_symbol_data)

    return {
        sym: main_mod._fetch_symbol_data(sym, None, None, set(), None)
        for sym in prices
    }


def test_mark_positions_to_market_trips_breaker_without_a_fill(sandbox, monkeypatch):
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.set_daily_loss_limit(0.03)
    order = ex.buy("TEST", 10, 50.0, reason="test")
    assert order.status.value == "FILLED"

    # Before this cycle's refresh: the mark is still the fill-time price
    # (slippage-adjusted, per _fill_price — not the raw requested price).
    # This is the exact bug — a price crash with no new fill is invisible.
    stale_mark = ex._open_position_value
    assert stale_mark == pytest.approx(10 * ex.avg_cost("TEST"))
    assert not ex._is_daily_loss_tripped()

    # Simulate a scan cycle where TEST crashed ~14% and nothing else traded —
    # no buy()/sell() call anywhere in this test after the initial fill.
    price_data = _fetch_scan_cycle_price_data(monkeypatch, {"TEST": 43.0})
    main_mod._mark_positions_to_market(ex, price_data)

    assert ex._open_position_value == pytest.approx(430.0)
    assert ex._open_position_value != stale_mark
    assert ex._is_daily_loss_tripped(), (
        "breaker should trip from the price move alone, with no fill this cycle"
    )


def test_mark_positions_to_market_silent_within_limit(sandbox, monkeypatch):
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.set_daily_loss_limit(0.03)
    ex.buy("TEST", 10, 50.0, reason="test")

    price_data = _fetch_scan_cycle_price_data(monkeypatch, {"TEST": 49.5})
    main_mod._mark_positions_to_market(ex, price_data)

    assert not ex._is_daily_loss_tripped()


def test_mark_positions_to_market_noop_when_executor_none(monkeypatch):
    price_data = _fetch_scan_cycle_price_data(monkeypatch, {"TEST": 43.0})
    main_mod._mark_positions_to_market(None, price_data)   # must not raise


def test_run_wires_up_mark_positions_to_market():
    """
    The three tests above prove _mark_positions_to_market() works — this
    proves run() still actually calls it. Without this, someone could
    delete the call site from run() (leaving the helper itself intact and
    passing) and reintroduce the exact stale-mark bug with a fully green
    suite. Source-inspection rather than executing run() (which needs a
    live IBKR/yfinance/screener/dashboard stack to get past setup) — this
    is a wiring guard, not a substitute for the behavioral tests above.
    """
    source = inspect.getsource(main_mod.run)
    assert "_mark_positions_to_market(executor, price_data)" in source
