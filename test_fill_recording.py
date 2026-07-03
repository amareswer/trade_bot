"""
Unit tests for BUG 1 — qty=0 fill recording.

Recovery priority in live_executor.execute() for SELL with quantity=0:
  1. last_raw["filled"] if non-zero — authoritative (exchange confirms it)
  2. last_raw["amount"] ONLY for market orders whose status is "closed" — safe
     inference (closed market SELL = fully executed). Never for limit orders.
  3. If neither recovers a positive qty → return None (no phantom row).

Tests:
  (a) closed market SELL, filled present  → uses filled (not amount)
  (b) closed limit order, filled=0.7*amt  → poll loop records 0.7*amt (partial fill)
  (c) filled absent on closed market SELL  → falls back to amount + warning
  (d) limit order closed, filled=0         → returns None (no amount inference)
  (e) market order, still open after polls → returns None
  (f) TradeLog.log_fill guard: qty <= 0 raises ValueError
  (g) TradeLog.log_fill guard: qty < 0 raises ValueError
  (h) TradeLog.log_fill: qty > 0 writes correctly (no regression)

Run: python -m pytest test_fill_recording.py -v
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
from bot.data.trade_log import TradeLog


_DEFAULT_MARKETS = {
    "BTC/CAD": {
        "limits": {
            "amount": {"min": 0.00005},
            "cost":   {"min": 5.0},
        }
    }
}


def _make_live_executor(
    position: float = 0.000378,
    starting_cash: float = 100.0,
    order_type: str = "market",
) -> tuple[LiveExecutor, MagicMock]:
    """Build a LiveExecutor with a pre-existing position; all exchange calls mocked."""
    tmpdir = tempfile.mkdtemp()
    state_path = os.path.join(tmpdir, "live_state_BTC_CAD.json")
    with open(state_path, "w") as f:
        json.dump({
            "symbol":       "BTC/CAD",
            "cash":         starting_cash,
            "position":     position,
            "cost_basis":   80000.0,
            "realized_pnl": 0.0,
            "fees_paid":    0.0,
            "bot_opened":   True,
            "saved_at":     "2026-06-27T00:00:00+00:00",
        }, f)

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.return_value = {
        "free":  {"BTC": position, "CAD": starting_cash},
        "total": {"BTC": position, "CAD": starting_cash},
    }

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        exc = LiveExecutor(
            exchange_id   = "kraken",
            symbol        = "BTC/CAD",
            api_key       = "key",
            api_secret    = "secret",
            starting_cash = starting_cash,
            dry_run       = False,
            state_path    = state_path,
            order_type    = order_type,
        )
        exc._exchange = mock_ex
        exc._portfolio.position = position

    return exc, mock_ex


# ── (a) closed market SELL, filled present → uses filled ─────────────────────

def test_sell_uses_filled_when_present(caplog):
    """
    Initial create_order response has filled=0, but fetch_order (poll) returns
    filled=0.000378.  The executor must use filled, not amount.
    """
    exc, mock_ex = _make_live_executor(position=0.000378, order_type="market")

    mock_ex.create_order.return_value = {
        "id": "ORD_A", "status": "open",
        "filled": 0.0, "amount": 0.000378,
        "average": 85000.0, "price": 85000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.fetch_order.return_value = {
        "id": "ORD_A", "status": "closed",
        "filled": 0.000378,   # <-- exchange now reports correct fill
        "amount": 0.000378,
        "average": 85000.0, "price": 85000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }

    import logging
    with patch("bot.execution.live_executor.time.sleep"), \
         caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        order = exc.execute(Signal.SELL, price=85000.0, quantity=0.0)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.quantity == pytest.approx(0.000378), \
        f"must use filled=0.000378, got {order.quantity}"
    # Should NOT say "inferring fill qty from amount"
    assert "inferring fill qty from amount" not in caplog.text
    assert exc.position == pytest.approx(0.0)


# ── (b) closed limit order, filled=0.7*amount → poll loop records partial ────

def test_sell_limit_partial_fill_recorded_correctly(caplog):
    """
    Limit order closes with a 70% partial fill.
    Poll loop sets filled_qty = 0.70 * amount; guard code is NOT reached (qty > 0).
    The amount (100%) must NOT be used.
    """
    exc, mock_ex = _make_live_executor(position=0.000378, order_type="limit")

    _partial = round(0.000378 * 0.70, 9)   # 0.0002646

    mock_ex.create_order.return_value = {
        "id": "ORD_B", "status": "open",
        "filled": 0.0, "amount": 0.000378,
        "price": 85000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.fetch_order.return_value = {
        "id": "ORD_B", "status": "closed",
        "filled": _partial,       # 70% fill
        "amount": 0.000378,       # requested quantity
        "average": 85000.0, "price": 85000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }

    import logging
    with patch("bot.execution.live_executor.time.sleep"), \
         caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        order = exc.execute(Signal.SELL, price=85000.0, quantity=0.0)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.quantity == pytest.approx(_partial), \
        f"must record partial fill {_partial}, not full amount 0.000378 — got {order.quantity}"
    # Guard (amount-inference) must NOT have been triggered
    assert "inferring fill qty from amount" not in caplog.text


# ── (c) filled absent on closed market order → falls back to amount + warning ─

def test_sell_market_closed_filled_absent_falls_back_to_amount(caplog):
    """
    Market SELL is closed but all polls report filled=0 (exchange timing artifact).
    Executor must fall back to amount with a logged warning; must NOT return None.
    """
    exc, mock_ex = _make_live_executor(position=0.000378, order_type="market")

    mock_ex.create_order.return_value = {
        "id": "ORD_C", "status": "open",
        "filled": 0.0, "amount": 0.000378,
        "average": 85000.0, "price": 85000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.fetch_order.return_value = {
        "id": "ORD_C", "status": "closed",
        "filled": 0.0,        # <-- still 0 even though closed (timing artifact)
        "amount": 0.000378,
        "average": 85000.0, "price": 85000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }

    import logging
    with patch("bot.execution.live_executor.time.sleep"), \
         caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        order = exc.execute(Signal.SELL, price=85000.0, quantity=0.0)

    assert order is not None, "market order closed → must recover fill from amount"
    assert order.status == OrderStatus.FILLED
    assert order.quantity == pytest.approx(0.000378), \
        f"must infer from amount=0.000378, got {order.quantity}"
    assert "inferring fill qty from amount" in caplog.text
    assert exc.position == pytest.approx(0.0)


# ── (d) limit order closed, filled=0 → returns None (no amount inference) ────

def test_sell_limit_closed_filled_zero_returns_none(caplog):
    """
    Limit order shows status=closed but filled=0.  Limit orders may cancel with
    0 fill, so we must NOT infer from amount.  Executor must return None.
    """
    exc, mock_ex = _make_live_executor(position=0.000378, order_type="limit")

    mock_ex.create_order.return_value = {
        "id": "ORD_D", "status": "open",
        "filled": 0.0, "amount": 0.000378,
        "price": 85000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.fetch_order.return_value = {
        "id": "ORD_D", "status": "closed",
        "filled": 0.0,
        "amount": 0.000378,
        "price": 85000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }

    import logging
    with patch("bot.execution.live_executor.time.sleep"), \
         caplog.at_level(logging.ERROR, logger="bot.execution.live_executor"):
        order = exc.execute(Signal.SELL, price=85000.0, quantity=0.0)

    assert order is None, "limit order with filled=0 must not write a phantom row"
    assert "SELL qty=0 GUARD" in caplog.text
    assert "inferring fill qty from amount" not in caplog.text


# ── (e) market order still open after all polls → returns None ───────────────

def test_sell_market_still_open_after_polls_returns_none(caplog):
    """
    Market SELL never reaches 'closed' status in 9 polls.
    Executor must return None — not infer from amount of an open order.
    """
    exc, mock_ex = _make_live_executor(position=0.000378, order_type="market")

    mock_ex.create_order.return_value = {
        "id": "ORD_E", "status": "open",
        "filled": 0.0, "amount": 0.000378,
        "price": 85000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.fetch_order.return_value = {
        "id": "ORD_E", "status": "open",   # never closes
        "filled": 0.0,
        "amount": 0.000378,
        "price": 85000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }

    import logging
    with patch("bot.execution.live_executor.time.sleep"), \
         caplog.at_level(logging.ERROR, logger="bot.execution.live_executor"):
        order = exc.execute(Signal.SELL, price=85000.0, quantity=0.0)

    assert order is None, "open market order with filled=0 must not write a phantom row"
    assert "SELL qty=0 GUARD" in caplog.text


# ── (f)(g)(h) TradeLog.log_fill guard ────────────────────────────────────────

def test_log_fill_guard_rejects_zero_quantity():
    """log_fill with quantity=0 must raise ValueError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tl = TradeLog(db_path=os.path.join(tmpdir, "trades.db"))
        with pytest.raises(ValueError, match="phantom row"):
            tl.log_fill(
                side="SELL", symbol="BTC/CAD",
                quantity=0.0, price=85000.0,
                pnl=-1.0, exchange="kraken",
            )


def test_log_fill_guard_rejects_negative_quantity():
    """log_fill with quantity < 0 must also raise ValueError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tl = TradeLog(db_path=os.path.join(tmpdir, "trades.db"))
        with pytest.raises(ValueError, match="phantom row"):
            tl.log_fill(
                side="BUY", symbol="BTC/CAD",
                quantity=-0.001, price=85000.0,
                exchange="kraken",
            )


def test_log_fill_normal_positive_quantity_writes_correctly():
    """log_fill with quantity > 0 must write successfully — no regression."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tl = TradeLog(db_path=os.path.join(tmpdir, "trades.db"))
        tl.log_fill(
            side="SELL", symbol="BTC/CAD",
            quantity=0.000378, price=85000.0,
            pnl=1.00, exchange="kraken",
        )
        rows = tl.recent(limit=1)
        assert len(rows) == 1
        assert rows[0]["quantity"] == pytest.approx(0.000378)
