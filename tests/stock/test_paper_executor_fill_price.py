"""
Regression test for the 2026-09-12 finding: StockPaperExecutor.buy()/sell()
set order.status = FILLED but never updated order.price/order.quantity/
order.total_value away from the pre-slippage REQUESTED values passed into
_new_order() — a caller reading the returned order object after a fill (as
stock_bot/main.py's notifier/print/logging code does) got the request, not
what actually happened. IBKRExecutor already did this correctly; paper.py
did not.
"""
from __future__ import annotations

import pytest

import stock_bot.execution.paper as paper_mod
from stock_bot.execution.paper import StockPaperExecutor
from stock_bot.data.price_feed import _sector_cache


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_mod, "_STATE_JSON", str(tmp_path / "state.json"))
    monkeypatch.setattr(paper_mod, "_TRADES_CSV", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(paper_mod, "_RESET_FLAG", str(tmp_path / ".reset"))
    monkeypatch.setattr(paper_mod, "_SETTLEMENT_CSV", str(tmp_path / "settlement.csv"))
    _sector_cache["TEST"] = "other"
    return tmp_path


def test_buy_order_reflects_actual_slippage_adjusted_fill(sandbox):
    ex = StockPaperExecutor(starting_cash=10_000.0)
    ex.set_slippage_bps(100)   # 1% — BUY pays more than the requested price
    order = ex.buy("TEST", 10, 50.0, reason="test")
    assert order.status.value == "FILLED"
    assert order.price == pytest.approx(50.50)          # 50 * 1.01, NOT the requested 50.0
    assert order.quantity == 10
    assert order.total_value == pytest.approx(505.0)    # price * quantity, recomputed


def test_sell_order_reflects_actual_slippage_adjusted_fill(sandbox):
    ex = StockPaperExecutor(starting_cash=10_000.0)
    ex.set_slippage_bps(0)
    ex.buy("TEST", 10, 50.0, reason="setup")
    ex.set_slippage_bps(100)   # 1% — SELL receives less than the requested price
    order = ex.sell("TEST", 10, 55.0, reason="test")
    assert order.status.value == "FILLED"
    assert order.price == pytest.approx(54.45)          # 55 * 0.99, NOT the requested 55.0
    assert order.quantity == 10
    assert order.total_value == pytest.approx(544.5)
