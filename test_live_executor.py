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

import ccxt
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

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_fetch_order_polling_resolves_on_close(mock_cfg, mock_sleep):
    mock_cfg.exchange.limit_order_enabled = False  # force market-order path so range(1,10) poll loop runs
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

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_fetch_order_polling_timeout_uses_partial_fill(mock_cfg, mock_sleep, caplog):
    import logging
    mock_cfg.exchange.limit_order_enabled = False  # force market-order path so range(1,10) poll loop runs
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    mock_ex.create_order.return_value = {
        "id": "order-003", "status": "open", "filled": 0.0,
        "average": None, "price": 90_000.0, "fee": {},
    }
    # All 9 polls (range(1,10)) still 'open'; partial fill accumulates through first 3 then holds
    _open = lambda filled: {"id": "order-003", "status": "open", "filled": filled,
                             "average": None, "price": 90_000.0, "fee": {}}
    mock_ex.fetch_order.side_effect = [
        _open(0.0003), _open(0.0006), _open(0.0008),
        _open(0.0008), _open(0.0008), _open(0.0008),
        _open(0.0008), _open(0.0008), _open(0.0008),
    ]

    with caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order.status == OrderStatus.FILLED
    assert mock_ex.fetch_order.call_count == 9  # range(1,10) exhausted before 'closed'
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

        # Configure the mock exchange to report the values that match the saved state.
        # _sync_cash needs free.CAD; _sync_position needs free+total.BTC;
        # fetch_ticker is called to reseed cost_basis if prev_position was 0 on load.
        mock_ex.fetch_balance.return_value = {
            "free":  {"CAD": ex.cash, "BTC": ex.position},
            "total": {"CAD": ex.cash, "BTC": ex.position},
        }
        mock_ex.fetch_ticker.return_value = {"last": ex.avg_entry}

        # Second executor simulates a live restart: dry_run=False so _sync_cash and
        # _sync_position run and pull cash/position from the mocked exchange.
        with patch.object(le_mod.ccxt, "kraken") as mock_cls2:
            mock_cls2.return_value = mock_ex
            ex2 = LiveExecutor(
                exchange_id   = "kraken",
                symbol        = "BTC/CAD",
                api_key       = "k",
                api_secret    = "s",
                starting_cash = 1000.0,
                dry_run       = False,
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
    # After __init__, cash should be the exchange balance (_sync_cash + _sync_position both call fetch_balance)
    assert abs(ex.cash - 150.75) < 0.01
    assert mock_ex.fetch_balance.call_count >= 1


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


# ---------------------------------------------------------------------------
# Test 12: restart recovery — executor, position_manager, state_machine consistent
# ---------------------------------------------------------------------------

def test_restart_recovery_seeds_position_manager_and_state_machine():
    """
    After a restart with a persisted position:
    - executor.position > 0  (loaded from state file)
    - position_manager.has_position is True, qty and avg_entry match executor
    - state_machine.state is LONG
    - intra-candle SL/TP gate (has_position check) would fire correctly
    - state machine would allow SELL and block BUY
    """
    from bot.portfolio.position_manager import PositionManager
    from bot.state.trade_state import TradingStateMachine, TradingState
    from bot.strategy.threshold_strategy import Signal

    # Simulate executor loaded from state file with an open position
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = os.path.join(tmpdir, "state.json")

        # Write a state file as if a BUY was previously filled
        import json
        json.dump({
            "symbol":       "BTC/CAD",
            "cash":         89.88,
            "position":     0.000113,
            "cost_basis":   88870.20,
            "realized_pnl": 0.0,
            "fees_paid":    0.0803,
            "saved_at":     "2026-06-11T20:00:08+00:00",
        }, open(state_path, "w"))

        mock_ex = MagicMock()
        mock_ex.load_markets.return_value = _DEFAULT_MARKETS
        mock_ex.fetch_balance.return_value = {
            "free":  {"CAD": 89.88, "BTC": 0.000113},
            "total": {"CAD": 89.88, "BTC": 0.000113},
        }

        with patch.object(le_mod.ccxt, "kraken") as mock_cls:
            mock_cls.return_value = mock_ex
            executor = LiveExecutor(
                exchange_id="kraken", symbol="BTC/CAD",
                api_key="k", api_secret="s",
                starting_cash=100.0, dry_run=False,
                state_path=state_path,
            )

    # Verify executor loaded the position from state file
    assert abs(executor.position - 0.000113) < 1e-9
    assert abs(executor.avg_entry - 88870.20) < 0.01

    # Simulate what main.py's recovery block does
    position_manager = PositionManager()
    state_machine    = TradingStateMachine(cooldown_ticks=6)

    assert not position_manager.has_position        # fresh — not yet seeded
    assert state_machine.state == TradingState.IDLE # fresh — not yet recovered

    position_manager.seed(
        quantity     = executor.position,
        avg_entry    = executor.avg_entry,
        realized_pnl = executor.portfolio.realized_pnl,
    )
    state_machine.recover_long(executor.avg_entry)

    # Post-recovery assertions — all three components are consistent
    assert position_manager.has_position
    assert abs(position_manager.quantity  - executor.position)  < 1e-9
    assert abs(position_manager.avg_entry - executor.avg_entry) < 0.01
    assert abs(position_manager.realized_pnl - 0.0)             < 0.01

    assert state_machine.state          == TradingState.LONG
    assert state_machine.last_action    == Signal.BUY
    assert abs(state_machine.last_trade_price - executor.avg_entry) < 0.01
    assert state_machine.cooldown_remaining == 0

    # Signal filtering: SELL passes, BUY blocked (position already open)
    sell_sig, reason = state_machine.filter_signal(Signal.SELL)
    assert sell_sig == Signal.SELL,  f"SELL should pass LONG state, got: {reason}"

    buy_sig, reason = state_machine.filter_signal(Signal.BUY)
    assert buy_sig == Signal.HOLD,   f"BUY should be blocked in LONG state, got: {reason}"

    # history is empty — seed/recover_long create no fake trade records
    assert len(state_machine.history)  == 0
    assert len(position_manager.history) == 0


# ---------------------------------------------------------------------------
# Limit order tests — cfg and time.sleep mocked, no network
# ---------------------------------------------------------------------------

def _limit_cfg(mock_cfg, *, enabled=True, timeout_s=30, max_retries=3, tick_pct=0.0001):
    """Configure mock cfg for limit order tests."""
    mock_cfg.exchange.limit_order_enabled     = enabled
    mock_cfg.exchange.limit_chase_timeout_s   = timeout_s
    mock_cfg.exchange.limit_chase_max_retries = max_retries
    mock_cfg.exchange.limit_chase_tick_pct    = tick_pct


def _ob():
    """Standard orderbook mock: bid=90000, ask=90100."""
    return {"bids": [[90000.0, 1.0]], "asks": [[90100.0, 1.0]]}


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_limit_order_fills_on_first_attempt(mock_cfg, mock_sleep):
    """Limit order closed immediately by exchange — FILLED, maker fee deducted."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    mock_ex.fetch_order_book.return_value  = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"

    limit_raw = {
        "id":      "limit-001",
        "status":  "closed",
        "filled":  0.001,
        "average": 90009.0,
        "fee":     {"cost": 0.0144, "currency": "CAD"},  # maker ~0.16%
    }
    mock_ex.create_order.return_value = limit_raw

    order = ex.execute(Signal.BUY, 90000.0, 0.001)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    # create_order called exactly once with type='limit'
    mock_ex.create_order.assert_called_once()
    assert mock_ex.create_order.call_args[0][1] == "limit"
    # Post-only flag sent
    assert mock_ex.create_order.call_args[0][5] == {"timeInForce": "PO"}
    # Maker fee deducted
    assert abs(ex.fees_paid - 0.0144) < 1e-6
    # No market-order fallback — fetch_order never needed
    mock_ex.fetch_order.assert_not_called()


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_limit_order_reprices_after_timeout(mock_cfg, mock_sleep):
    """First attempt times out (timeout=0 skips poll loop), cancel called, second attempt fills."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=0, max_retries=3)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    mock_ex.fetch_order_book.return_value   = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"

    open_raw = {"id": "limit-01", "status": "open",   "filled": 0.0,   "average": None,    "fee": {}}
    fill_raw = {"id": "limit-02", "status": "closed", "filled": 0.001, "average": 90009.0,
                "fee": {"cost": 0.0144, "currency": "CAD"}}
    mock_ex.create_order.side_effect = [open_raw, fill_raw]

    order = ex.execute(Signal.BUY, 90000.0, 0.001)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    # First limit order was cancelled
    mock_ex.cancel_order.assert_called_once_with("limit-01", "BTC/CAD")
    # Two limit order placements total
    assert mock_ex.create_order.call_count == 2
    assert all(c[0][1] == "limit" for c in mock_ex.create_order.call_args_list)


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_limit_order_falls_back_to_market_after_max_retries(mock_cfg, mock_sleep, caplog):
    """All limit attempts time out → market order placed → WARNING containing 'falling back'."""
    import logging
    _limit_cfg(mock_cfg, enabled=True, timeout_s=0, max_retries=2)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    mock_ex.fetch_order_book.return_value   = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"

    open_raw   = {"id": "limit-0X", "status": "open",   "filled": 0.0,   "average": None,    "fee": {}}
    market_raw = {"id": "mkt-001",  "status": "closed", "filled": 0.001, "average": 90000.0,
                  "fee": {"cost": 0.72, "currency": "CAD"}}
    # 3 limit attempts (max_retries=2 → range(3)), then market fallback
    mock_ex.create_order.side_effect = [
        {**open_raw, "id": "limit-01"},
        {**open_raw, "id": "limit-02"},
        {**open_raw, "id": "limit-03"},
        market_raw,
    ]

    with caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        order = ex.execute(Signal.BUY, 90000.0, 0.001)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    # Final create_order call must be a market order
    last_call = mock_ex.create_order.call_args_list[-1]
    assert last_call[0][1] == "market"
    assert mock_ex.create_order.call_count == 4  # 3 limit + 1 market
    assert any("falling back" in r.message for r in caplog.records)


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_limit_order_disabled_uses_market(mock_cfg, mock_sleep):
    """LIMIT_ORDER_ENABLED=false → existing market path used, create_order called with type='market'."""
    _limit_cfg(mock_cfg, enabled=False)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    raw = {
        "id":      "mkt-002",
        "status":  "closed",
        "filled":  0.001,
        "average": 90000.0,
        "fee":     {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = raw
    mock_ex.fetch_order.return_value  = raw  # first poll sees closed → breaks

    order = ex.execute(Signal.BUY, 90000.0, 0.001)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    mock_ex.create_order.assert_called_once()
    assert mock_ex.create_order.call_args[1]["type"] == "market"
    # _place_limit_order never called — no orderbook fetch
    mock_ex.fetch_order_book.assert_not_called()


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_limit_order_po_rejection_retries_with_tighter_offset(mock_cfg, mock_sleep):
    """ccxt.InvalidOrder on first create_order → halves tick_pct, second attempt fills. No market fallback."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30, max_retries=3, tick_pct=0.0001)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0)

    mock_ex.fetch_order_book.return_value   = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"

    fill_raw = {
        "id":      "limit-002",
        "status":  "closed",
        "filled":  0.001,
        "average": 90009.0,
        "fee":     {"cost": 0.0144, "currency": "CAD"},
    }
    mock_ex.create_order.side_effect = [
        ccxt.InvalidOrder("would be filled immediately"),  # PO rejected on 1st attempt
        fill_raw,                                           # 2nd attempt fills
    ]

    order = ex.execute(Signal.BUY, 90000.0, 0.001)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    # Exactly two limit order placements — no market fallback
    assert mock_ex.create_order.call_count == 2
    assert all(c[0][1] == "limit" for c in mock_ex.create_order.call_args_list)


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
