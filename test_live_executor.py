"""
Unit tests for LiveExecutor — all exchange calls mocked, no network.

Run: python -m pytest test_live_executor.py -v
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

import bot.execution.live_executor as le_mod
from bot.execution.executor import OrderSide, OrderStatus
from bot.execution.live_executor import LiveExecutor
from bot.strategy.threshold_strategy import Signal


# ---------------------------------------------------------------------------
# Test fixture helpers
# ---------------------------------------------------------------------------

_DEFAULT_MARKETS = {
    "BTC/CAD": {
        "limits": {
            "amount": {"min": 0.00005},   # Kraken BTC minimum
            "cost":   {"min": 5.0},       # $5 CAD minimum order
        }
    }
}


def _make(
    *,
    dry_run:        bool  = True,
    starting_cash:  float = 1000.0,
    markets:        dict  = None,
    balance:        dict  = None,
    state_path:     str   = None,
) -> tuple[LiveExecutor, MagicMock]:
    """
    Build a LiveExecutor with a fully mocked ccxt exchange.
    Returns (executor, mock_exchange).
    """
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS if markets is None else markets
    mock_ex.fetch_balance.return_value = (
        {"free": {"CAD": starting_cash}} if balance is None else balance
    )

    if state_path is None:
        # Use a temp path that doesn't exist — clean slate for each test
        state_path = os.path.join(tempfile.mkdtemp(), "live_state.json")

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id   = "kraken",
            symbol        = "BTC/CAD",
            api_key       = "test_key",
            api_secret    = "test_secret",
            starting_cash = starting_cash,
            dry_run       = dry_run,
            state_path    = state_path,
        )
    return ex, mock_ex


# ---------------------------------------------------------------------------
# Test 1: dry-run BUY fills portfolio without touching create_order
# ---------------------------------------------------------------------------

def test_dry_run_buy_fills_portfolio():
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0)
    price = 90_000.0
    qty   = 0.001   # $90 — well above minimums

    order = ex.execute(Signal.BUY, price, qty)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.side   == OrderSide.BUY
    assert abs(order.quantity - qty) < 1e-9
    assert abs(order.price - price) < 0.01

    # Portfolio updated
    assert abs(ex.cash - (1000.0 - price * qty)) < 0.01
    assert abs(ex.position - qty) < 1e-9

    # No real order placed
    mock_ex.create_order.assert_not_called()

    # Appears in filled_orders()
    assert len(ex.filled_orders()) == 1
    assert ex.filled_orders()[0].status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# Test 2: validation rejects order below minimum amount
# ---------------------------------------------------------------------------

def test_validation_rejects_below_min_amount():
    ex, _ = _make(dry_run=True, starting_cash=1000.0)
    # Kraken minimum is 0.00005 BTC; send 0.00001
    order = ex.execute(Signal.BUY, 90_000.0, 0.00001)

    assert order is not None
    assert order.status == OrderStatus.REJECTED
    assert "Kraken minimum" in (order.reject_reason or "")
    assert "RISK_PER_TRADE_PCT" in (order.reject_reason or "")

    # Portfolio unchanged
    assert ex.cash     == 1000.0
    assert ex.position == 0.0

    # Appears in rejected_orders()
    assert len(ex.rejected_orders()) == 1


# ---------------------------------------------------------------------------
# Test 3: validation rejects order below minimum cost
# ---------------------------------------------------------------------------

def test_validation_rejects_below_min_cost():
    # Set only a cost minimum so the amount check doesn't fire first
    markets = {
        "BTC/CAD": {
            "limits": {
                "amount": {"min": None},
                "cost":   {"min": 10.0},  # $10 minimum
            }
        }
    }
    ex, _ = _make(dry_run=True, markets=markets)
    # 0.00005 BTC × $90k = $4.50 < $10 minimum
    order = ex.execute(Signal.BUY, 90_000.0, 0.00005)

    assert order is not None
    assert order.status == OrderStatus.REJECTED
    assert "min cost" in (order.reject_reason or "")


# ---------------------------------------------------------------------------
# Test 4: live BUY updates portfolio correctly (using filled from response)
# ---------------------------------------------------------------------------

def test_live_buy_updates_portfolio():
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    # Simulate exchange: create_order returns immediate close
    raw = {
        "id":      "order-001",
        "status":  "closed",
        "filled":  0.001,
        "average": 90_000.0,
        "fee":     {"cost": 0.09, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = raw
    mock_ex.fetch_order.return_value  = raw   # polls see same closed order

    order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is not None
    assert order.status   == OrderStatus.FILLED
    assert order.order_id == "order-001"
    assert abs(order.quantity - 0.001) < 1e-9
    assert abs(order.price - 90_000.0) < 0.01

    # Cash: 1000 − (90000×0.001) − 0.09 fee = 1000 − 90 − 0.09 = 909.91
    assert abs(ex.cash - 909.91) < 0.01
    assert abs(ex.position - 0.001) < 1e-9
    assert abs(ex.avg_entry - 90_000.0) < 0.01


# ---------------------------------------------------------------------------
# Test 5: live SELL updates portfolio and computes PnL
# ---------------------------------------------------------------------------

def test_live_sell_updates_portfolio():
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    # BUY setup
    buy_raw = {
        "id": "order-buy", "status": "closed",
        "filled": 0.001, "average": 90_000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = buy_raw
    mock_ex.fetch_order.return_value  = buy_raw
    ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert abs(ex.position - 0.001) < 1e-9

    # SELL at higher price
    sell_raw = {
        "id": "order-sell", "status": "closed",
        "filled": 0.001, "average": 91_000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = sell_raw
    mock_ex.fetch_order.return_value  = sell_raw
    order = ex.execute(Signal.SELL, 91_000.0, 0.001)

    assert order.status == OrderStatus.FILLED
    assert ex.position  == 0.0
    # Realized PnL = (91000 - 90000) * 0.001 = $1.00
    assert abs(ex.portfolio.realized_pnl - 1.0) < 0.01
    # Cash: 1000 − 90 + 91 = 1001
    assert abs(ex.cash - 1001.0) < 0.01
    assert len(ex.filled_orders()) == 2


# ---------------------------------------------------------------------------
# Test 6: fetch_order polling resolves when order reaches 'closed'
# ---------------------------------------------------------------------------

def test_fetch_order_polling_resolves_on_close():
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    # create_order returns 'open' (not yet filled)
    mock_ex.create_order.return_value = {
        "id": "order-002", "status": "open", "filled": 0.0,
        "average": None, "price": 90_000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    # Polls: open → open → closed
    mock_ex.fetch_order.side_effect = [
        {"id": "order-002", "status": "open",   "filled": 0.0,   "average": None,      "fee": {}},
        {"id": "order-002", "status": "open",   "filled": 0.0005,"average": None,      "fee": {}},
        {"id": "order-002", "status": "closed", "filled": 0.001, "average": 90_100.0,
         "fee": {"cost": 0.09, "currency": "CAD"}},
    ]

    order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order.status             == OrderStatus.FILLED
    assert mock_ex.fetch_order.call_count == 3
    assert abs(order.price - 90_100.0) < 0.01
    assert abs(order.quantity - 0.001) < 1e-9


# ---------------------------------------------------------------------------
# Test 7: 3 polls, never 'closed' — partial fill saved, warning logged
# ---------------------------------------------------------------------------

def test_fetch_order_polling_timeout_uses_partial_fill(caplog):
    import logging
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    mock_ex.create_order.return_value = {
        "id": "order-003", "status": "open", "filled": 0.0,
        "average": None, "price": 90_000.0, "fee": {},
    }
    # All 3 polls still 'open', partial fill accumulates
    mock_ex.fetch_order.side_effect = [
        {"id": "order-003", "status": "open", "filled": 0.0003, "average": None, "price": 90_000.0, "fee": {}},
        {"id": "order-003", "status": "open", "filled": 0.0006, "average": None, "price": 90_000.0, "fee": {}},
        {"id": "order-003", "status": "open", "filled": 0.0008, "average": None, "price": 90_000.0, "fee": {}},
    ]

    with caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order.status == OrderStatus.FILLED
    assert mock_ex.fetch_order.call_count == 3
    # Uses last reported filled amount
    assert abs(order.quantity - 0.0008) < 1e-9
    # Warning was logged
    assert any("NOT CLOSED" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 8: fee in quote currency is deducted; wrong-currency fee is not
# ---------------------------------------------------------------------------

def test_fee_deducted_when_quote_currency():
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    raw = {
        "id": "order-004", "status": "closed",
        "filled": 0.001, "average": 90_000.0,
        "fee": {"cost": 0.90, "currency": "CAD"},   # 0.90 CAD fee
    }
    mock_ex.create_order.return_value = raw
    mock_ex.fetch_order.return_value  = raw

    ex.execute(Signal.BUY, 90_000.0, 0.001)

    # Cash: 1000 − (90000×0.001) − 0.90 = 1000 − 90 − 0.90 = 909.10
    assert abs(ex.cash - 909.10) < 0.01


def test_fee_wrong_currency_not_deducted(caplog):
    import logging
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    raw = {
        "id": "order-005", "status": "closed",
        "filled": 0.001, "average": 90_000.0,
        "fee": {"cost": 0.000001, "currency": "BTC"},  # fee in BTC — wrong currency
    }
    mock_ex.create_order.return_value = raw
    mock_ex.fetch_order.return_value  = raw

    with caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        ex.execute(Signal.BUY, 90_000.0, 0.001)

    # Cash: no fee deducted — only fill cost
    assert abs(ex.cash - (1000.0 - 90.0)) < 0.01
    assert any("mismatch" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 9: state file saved after fill and loaded on new executor
# ---------------------------------------------------------------------------

def test_state_save_load_roundtrip():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = os.path.join(tmpdir, "state.json")
        ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, state_path=state_path)

        # Execute a BUY to trigger _save_state
        ex.execute(Signal.BUY, 90_000.0, 0.001)

        assert os.path.exists(state_path)

        # Load a second executor from same state_path
        with patch.object(le_mod.ccxt, "kraken") as mock_cls2:
            mock_cls2.return_value = mock_ex
            ex2 = LiveExecutor(
                exchange_id   = "kraken",
                symbol        = "BTC/CAD",
                api_key       = "k",
                api_secret    = "s",
                starting_cash = 1000.0,
                dry_run       = True,
                state_path    = state_path,
            )

        assert abs(ex2.cash     - ex.cash)     < 0.01
        assert abs(ex2.position - ex.position) < 1e-9
        assert abs(ex2.avg_entry - ex.avg_entry) < 0.01


# ---------------------------------------------------------------------------
# Test 10: _sync_cash returns exchange balance (live mode)
# ---------------------------------------------------------------------------

def test_sync_cash_uses_exchange_free_balance():
    ex, mock_ex = _make(dry_run=False, starting_cash=100.0, balance={"free": {"CAD": 150.75}})
    # After __init__, cash should be the exchange balance
    assert abs(ex.cash - 150.75) < 0.01
    mock_ex.fetch_balance.assert_called_once()


def test_sync_cash_falls_back_on_error(caplog):
    import logging
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.side_effect = Exception("API timeout")

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = os.path.join(tmpdir, "state.json")
        with patch.object(le_mod.ccxt, "kraken") as mock_cls:
            mock_cls.return_value = mock_ex
            with caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
                ex = LiveExecutor(
                    exchange_id   = "kraken",
                    symbol        = "BTC/CAD",
                    api_key       = "k",
                    api_secret    = "s",
                    starting_cash = 100.0,
                    dry_run       = False,
                    state_path    = state_path,
                )

    assert abs(ex.cash - 100.0) < 0.01
    assert any("_sync_cash failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 11: reset() restores starting_cash and clears history
# ---------------------------------------------------------------------------

def test_reset_restores_starting_cash():
    ex, mock_ex = _make(dry_run=True, starting_cash=500.0)
    ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert ex.cash     != 500.0
    assert ex.position != 0.0
    assert len(ex.filled_orders()) == 1

    ex.reset()

    assert abs(ex.cash - 500.0) < 0.01
    assert ex.position == 0.0
    assert ex.avg_entry == 0.0
    assert len(ex.filled_orders()) == 0


if __name__ == "__main__":
    import sys
    import traceback
    tests = [
        test_dry_run_buy_fills_portfolio,
        test_validation_rejects_below_min_amount,
        test_validation_rejects_below_min_cost,
        test_live_buy_updates_portfolio,
        test_live_sell_updates_portfolio,
        test_fetch_order_polling_resolves_on_close,
        test_reset_restores_starting_cash,
        test_state_save_load_roundtrip,
        test_sync_cash_uses_exchange_free_balance,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failures += 1
    print(f"\n{len(tests) - failures}/{len(tests)} passed.")
    sys.exit(failures)
