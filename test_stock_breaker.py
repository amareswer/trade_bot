"""
Unit tests for the stock-bot daily-loss circuit breaker baseline.

Bug fixed 2026-07-04: after a restart with open positions, the session
baseline was cash-only while the drawdown calc used cash + position marks —
the breaker could never fire (current total always far above baseline).
Baseline now includes the mark value (avg_cost) of restored positions.

State/CSV paths are monkeypatched to a tmp dir so tests never touch the real
paper_state.json / paper_trades.csv (see .memory/core.md rule 10).
"""
import os

import pytest

import stock_bot.execution.paper as paper_mod
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


def test_fresh_session_baseline_is_cash(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    assert ex._session_start_value == 1000.0
    assert ex._open_position_value == 0.0


def test_breaker_fires_after_restart_with_open_positions(sandbox):
    # Session 1: buy and persist — ~$500 cash + 10 shares near $50
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.set_daily_loss_limit(0.03)
    order = ex.buy("TEST", 10, 50.0, reason="test")
    assert order.status.value == "FILLED"
    ex.save_state()

    # Session 2 (restart): baseline must include the restored position marks
    ex2 = StockPaperExecutor(starting_cash=1000.0)
    ex2.set_daily_loss_limit(0.03)
    assert ex2._session_start_value == pytest.approx(
        ex2._cash + ex2._open_position_value
    )
    assert ex2._open_position_value > 0   # position marked at avg_cost, not 0

    # Price drops ~14% → portfolio down ~7% from the ~$1000 baseline.
    # Under the old cash-only baseline (~$500) this could never trip.
    ex2._update_position_value({"TEST": 43.0})
    assert ex2._is_daily_loss_tripped()


def test_breaker_silent_within_limit_after_restart(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.buy("TEST", 10, 50.0, reason="test")
    ex.save_state()

    ex2 = StockPaperExecutor(starting_cash=1000.0)
    ex2.set_daily_loss_limit(0.03)
    ex2._update_position_value({"TEST": 48.5})   # ~1.6% portfolio drawdown
    assert not ex2._is_daily_loss_tripped()
