"""
Regression test for the 2026-09-12 concurrent-sell finding: the background
SL/TP watcher thread (stock_bot/main.py's _check_open_positions_sl_tp) and
the main strategy scan loop can both decide to exit the same symbol at
nearly the same moment. Neither StockPaperExecutor.sell() nor
IBKRExecutor.sell() had a lock around the full read-position -> validate ->
submit -> update sequence (only realized-P&L accumulation was ever
protected), so both threads could read the same held-shares figure, both
pass the "enough shares to sell" check, and both submit a sell — overselling
the real position.

The IBKR-side test lives in tests/stock/test_ibkr_executor.py (reuses its
FakeIB harness); this file covers StockPaperExecutor, whose fully in-memory
position book lets the test assert the actual business outcome (one FILLED,
one REJECTED), not just the overlap invariant.
"""
from __future__ import annotations

import threading
import time

import pytest

import stock_bot.execution.paper as paper_mod
from stock_bot.execution.paper import StockPaperExecutor
from stock_bot.data.price_feed import _sector_cache


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect all paper-state files to tmp — never touch the real
    paper_state.json / paper_trades.csv (see .memory/core.md rule 10)."""
    monkeypatch.setattr(paper_mod, "_STATE_JSON", str(tmp_path / "state.json"))
    monkeypatch.setattr(paper_mod, "_TRADES_CSV", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(paper_mod, "_RESET_FLAG", str(tmp_path / ".reset"))
    monkeypatch.setattr(paper_mod, "_SETTLEMENT_CSV", str(tmp_path / "settlement.csv"))
    _sector_cache["TEST"] = "other"
    return tmp_path


def test_concurrent_sell_calls_do_not_oversell(sandbox):
    """Two threads both try to sell the entire 10-share position at once.
    Proven two ways: (1) an overlap counter around _fill_price() — the
    first read/mutate inside the critical section — must never see both
    threads inside at once; (2) the actual outcome must be exactly one
    FILLED (closing the position) and one REJECTED (nothing left to sell),
    never both FILLED (which would mean overselling a 10-share position by
    10 phantom shares)."""
    ex = StockPaperExecutor(starting_cash=10_000.0)
    ex.set_slippage_bps(0)
    ex.buy("TEST", 10, 50.0, reason="setup")
    assert ex.position("TEST") == 10

    active = {"count": 0, "max": 0}
    guard = threading.Lock()
    real_fill_price = ex._fill_price

    def _slow_fill_price(price, side):
        with guard:
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
        time.sleep(0.15)
        result = real_fill_price(price, side)
        with guard:
            active["count"] -= 1
        return result

    ex._fill_price = _slow_fill_price

    results: list = []
    def _sell():
        results.append(ex.sell("TEST", 10, 55.0, reason="test"))

    t1 = threading.Thread(target=_sell)
    t2 = threading.Thread(target=_sell)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert active["max"] == 1, (
        "two sell() calls entered the critical section concurrently — the "
        "per-symbol lock did not serialize them"
    )
    statuses = sorted(r.status.value for r in results)
    assert statuses == ["FILLED", "REJECTED"], (
        "exactly one sell must close the 10-share position and the other "
        f"must be rejected as insufficient — got {statuses}"
    )
    assert ex.position("TEST") == 0, "position must be fully closed, not oversold negative"
