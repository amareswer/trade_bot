"""
Unit tests for LiveExecutor — all exchange calls mocked, no network.

Run: python -m pytest tests/crypto/test_live_executor.py -v
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
    order_type:     str   = "market",
    native_stop_loss_enabled: bool = False,
    max_slippage_pct: float = 0.0,
    tmp_path        = None,
) -> tuple[LiveExecutor, MagicMock]:
    """
    Build a LiveExecutor with a fully mocked ccxt exchange.
    Returns (executor, mock_exchange).

    tmp_path is pytest's built-in per-test fixture (auto-cleaned) — pass your
    test's own tmp_path fixture through. Falls back to tempfile.mkdtemp() only
    when called outside pytest (e.g. the __main__ runner at the bottom of this
    file), which does not have a tmp_path fixture available.
    """
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS if markets is None else markets
    mock_ex.fetch_balance.return_value = (
        {"free": {"CAD": starting_cash}} if balance is None else balance
    )
    mock_ex.fetch_open_orders.return_value = []

    if state_path is None:
        # Use a temp path that doesn't exist — clean slate for each test
        if tmp_path is not None:
            state_path = str(tmp_path / "live_state.json")
        else:
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
            order_type    = order_type,
            native_stop_loss_enabled = native_stop_loss_enabled,
            max_slippage_pct = max_slippage_pct,
        )
    return ex, mock_ex


# ---------------------------------------------------------------------------
# Test 1: dry-run BUY fills portfolio without touching create_order
# ---------------------------------------------------------------------------

def test_dry_run_buy_fills_portfolio(tmp_path):
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)
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

def test_validation_rejects_below_min_amount(tmp_path):
    ex, _ = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)
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

def test_validation_rejects_below_min_cost(tmp_path):
    # Set only a cost minimum so the amount check doesn't fire first
    markets = {
        "BTC/CAD": {
            "limits": {
                "amount": {"min": None},
                "cost":   {"min": 10.0},  # $10 minimum
            }
        }
    }
    ex, _ = _make(dry_run=True, markets=markets, tmp_path=tmp_path)
    # 0.00005 BTC × $90k = $4.50 < $10 minimum
    order = ex.execute(Signal.BUY, 90_000.0, 0.00005)

    assert order is not None
    assert order.status == OrderStatus.REJECTED
    assert "min cost" in (order.reject_reason or "")


# ---------------------------------------------------------------------------
# Pre-trade minimum-size guard (2026-07-30): warns before a BUY whose
# computed qty is within MIN_SIZE_SAFETY_MARGIN of amt_min. _DEFAULT_MARKETS
# has amount.min=0.00005, cost.min=5.0; default margin is 1.5x -> threshold
# 0.000075.
# ---------------------------------------------------------------------------

def test_min_size_guard_fires_below_safety_margin(caplog, tmp_path):
    import logging
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)

    with patch.object(ex._alerter, "error") as mock_alert, \
         caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        # 0.00006 clears amt_min (0.00005) and cost_min ($5.4 >= $5) so the
        # order itself still fills — but sits inside the 1.5x margin
        # (threshold 0.000075), so the guard must fire.
        order = ex.execute(Signal.BUY, 90_000.0, 0.00006)

    assert order is not None
    assert order.status == OrderStatus.FILLED, "guard must not block the order"
    mock_alert.assert_called_once()
    alert_msg = mock_alert.call_args[0][0]
    assert "MIN-SIZE GUARD" in alert_msg
    assert "0.00006000" in alert_msg
    assert "0.00005000" in alert_msg
    assert any("MIN-SIZE GUARD" in r.message for r in caplog.records)


def test_min_size_guard_silent_above_safety_margin(tmp_path):
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)

    with patch.object(ex._alerter, "error") as mock_alert:
        # 0.001 BTC is well above the 0.000075 threshold.
        order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    mock_alert.assert_not_called()


def test_min_size_guard_never_alters_quantity_sent(tmp_path):
    """The guard is alert-only — it must never round the quantity up, even
    when it fires. Silently increasing size would break the ATR risk cap
    the sizing exists to enforce."""
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)

    requested_qty = 0.00006
    with patch.object(ex._alerter, "error"):
        order = ex.execute(Signal.BUY, 90_000.0, requested_qty)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.quantity == pytest.approx(requested_qty, abs=1e-12)
    assert abs(ex.position - requested_qty) < 1e-12


def test_min_size_guard_margin_is_env_configurable(tmp_path):
    """MIN_SIZE_SAFETY_MARGIN is read into a module-level constant — prove
    the guard's math actually uses it (amt_min * margin), not a hardcoded
    1.5, by overriding it to a value that changes the outcome for the same
    quantity."""
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)

    # 0.00008 is above the default 1.5x threshold (0.000075) but below a
    # tighter 1.0x-margin threshold would still pass — use a WIDER margin
    # (3.0x -> threshold 0.00015) so the same qty that was silent by default
    # now trips the guard.
    with patch.object(le_mod, "_MIN_SIZE_SAFETY_MARGIN", 3.0):
        with patch.object(ex._alerter, "error") as mock_alert:
            order = ex.execute(Signal.BUY, 90_000.0, 0.00008)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    mock_alert.assert_called_once()
    assert "3.00" in mock_alert.call_args[0][0]


# ---------------------------------------------------------------------------
# Test 4: live BUY updates portfolio correctly (using filled from response)
# ---------------------------------------------------------------------------

def test_live_buy_updates_portfolio(tmp_path):
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

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

def test_live_sell_updates_portfolio(tmp_path):
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

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
def test_fetch_order_polling_resolves_on_close(mock_cfg, mock_sleep, tmp_path):
    mock_cfg.exchange.limit_order_enabled = False  # force market-order path so range(1,10) poll loop runs
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

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
def test_fetch_order_polling_timeout_uses_partial_fill(mock_cfg, mock_sleep, caplog, tmp_path):
    import logging
    mock_cfg.exchange.limit_order_enabled = False  # force market-order path so range(1,10) poll loop runs
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

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

def test_fee_deducted_when_quote_currency(tmp_path):
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

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


def test_fee_wrong_currency_not_deducted(caplog, tmp_path):
    import logging
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

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


def test_fee_currency_mismatch_alerts_telegram(tmp_path):
    """A fee-currency mismatch must alert, not just log — silent cash drift
    is the whole risk this guards against."""
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    raw = {
        "id": "order-006", "status": "closed",
        "filled": 0.001, "average": 90_000.0,
        "fee": {"cost": 0.000001, "currency": "BTC"},  # fee in BTC — wrong currency
    }
    mock_ex.create_order.return_value = raw
    mock_ex.fetch_order.return_value  = raw

    with patch.object(ex._alerter, "error") as mock_alert:
        ex.execute(Signal.BUY, 90_000.0, 0.001)

    mock_alert.assert_called_once()
    alert_msg = mock_alert.call_args[0][0]
    assert "FEE CURRENCY MISMATCH" in alert_msg
    assert "BTC" in alert_msg
    assert "CAD" in alert_msg


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

def test_sync_cash_uses_exchange_free_balance(tmp_path):
    ex, mock_ex = _make(dry_run=False, starting_cash=100.0, balance={"free": {"CAD": 150.75}}, tmp_path=tmp_path)
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
        with patch.object(le_mod.ccxt, "kraken") as mock_cls, \
             patch("bot.exchanges.retry.time.sleep"):
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

def test_reset_restores_starting_cash(tmp_path):
    ex, mock_ex = _make(dry_run=True, starting_cash=500.0, tmp_path=tmp_path)
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
def test_limit_order_fills_on_first_attempt(mock_cfg, mock_sleep, tmp_path):
    """Limit order closed immediately by exchange — FILLED, maker fee deducted."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.fetch_order_book.return_value  = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"

    limit_raw = {
        "id":      "limit-001",
        "status":  "closed",
        "filled":  0.001,
        "average": 90009.0,
        "fee":     {"cost": 0.360, "currency": "CAD"},  # maker ~0.40% (confirmed Jun 14 fill; 0.001 BTC × 90009 × 0.004)
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
    assert abs(ex.fees_paid - 0.360) < 1e-6
    # No market-order fallback — fetch_order never needed
    mock_ex.fetch_order.assert_not_called()


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_limit_order_reprices_after_timeout(mock_cfg, mock_sleep, tmp_path):
    """First attempt times out (timeout=0 skips poll loop), cancel called, second attempt fills."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=0, max_retries=3)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.fetch_order_book.return_value   = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"

    open_raw = {"id": "limit-01", "status": "open",   "filled": 0.0,   "average": None,    "fee": {}}
    fill_raw = {"id": "limit-02", "status": "closed", "filled": 0.001, "average": 90009.0,
                "fee": {"cost": 0.0144, "currency": "CAD"}}
    mock_ex.create_order.side_effect = [open_raw, fill_raw]
    # Post-cancel race check: order cancelled clean, nothing filled → chase retries
    mock_ex.fetch_order.return_value = {
        "id": "limit-01", "status": "canceled", "type": "limit",
        "filled": 0.0, "amount": 0.001, "average": None, "fee": {},
    }

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
def test_limit_order_falls_back_to_market_after_max_retries(mock_cfg, mock_sleep, caplog, tmp_path):
    """All limit attempts time out → market order placed → WARNING containing 'falling back'."""
    import logging
    _limit_cfg(mock_cfg, enabled=True, timeout_s=0, max_retries=2)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

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
    # Post-cancel race check after each timeout: cancelled clean, nothing filled
    mock_ex.fetch_order.side_effect = lambda oid, _sym: {
        "id": oid, "status": "canceled", "type": "limit",
        "filled": 0.0, "amount": 0.001, "average": None, "fee": {},
    }

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
def test_limit_order_disabled_uses_market(mock_cfg, mock_sleep, tmp_path):
    """LIMIT_ORDER_ENABLED=false → existing market path used, create_order called with type='market'."""
    _limit_cfg(mock_cfg, enabled=False)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

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
def test_limit_order_po_rejection_retries_with_tighter_offset(mock_cfg, mock_sleep, tmp_path):
    """ccxt.InvalidOrder on first create_order → halves tick_pct, second attempt fills. No market fallback."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30, max_retries=3, tick_pct=0.0001)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

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


# ---------------------------------------------------------------------------
# Test: ORDER_TYPE=limit BUY uses post-only and bid-side price (0.2% below)
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_order_type_limit_buy_uses_post_only_and_bid_price(mock_cfg, mock_sleep, tmp_path):
    """BUY with order_type='limit' must use price*0.998 and timeInForce=PO."""
    mock_cfg.exchange.limit_order_enabled = False  # use simple path, not limit-chase
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, order_type="limit", tmp_path=tmp_path)

    fill_price = round(90_000.0 * 0.998, 2)  # 89_820.0
    raw = {
        "id": "lo-001", "status": "closed",
        "filled": 0.001, "average": fill_price,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = raw
    mock_ex.fetch_order.return_value  = raw

    order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is not None
    assert order.status == OrderStatus.FILLED

    call = mock_ex.create_order.call_args
    # positional: symbol, type, side, amount, price  /  keyword or positional params
    assert call[1].get("type") == "limit" or call[0][1] == "limit"
    assert call[1].get("side") == "buy" or call[0][2] == "buy"
    # price must be bid-side (below market)
    actual_price = call[1].get("price") or call[0][4]
    assert actual_price == fill_price, f"expected {fill_price}, got {actual_price}"
    # post-only param must be present — Kraken uses postOnly=True, not timeInForce=PO
    params = call[1].get("params") or (call[0][5] if len(call[0]) > 5 else {})
    assert params == {"postOnly": True}, f"missing post-only: {params}"


# ---------------------------------------------------------------------------
# Test: ORDER_TYPE=limit SELL falls through to market (guaranteed exit)
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_order_type_limit_sell_falls_through_to_market(mock_cfg, mock_sleep, tmp_path):
    """SELL must always be a market order even when order_type='limit'."""
    mock_cfg.exchange.limit_order_enabled = False
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, order_type="limit", tmp_path=tmp_path)

    # Seed a position
    buy_raw = {
        "id": "lo-buy", "status": "closed",
        "filled": 0.001, "average": round(90_000.0 * 0.998, 2),
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = buy_raw
    mock_ex.fetch_order.return_value  = buy_raw
    ex.execute(Signal.BUY, 90_000.0, 0.001)
    mock_ex.create_order.reset_mock()

    # Now SELL — must place market, not limit
    sell_raw = {
        "id": "lo-sell", "status": "closed",
        "filled": 0.001, "average": 91_000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = sell_raw
    mock_ex.fetch_order.return_value  = sell_raw
    order = ex.execute(Signal.SELL, 91_000.0, 0.001)

    assert order is not None
    assert order.status == OrderStatus.FILLED

    sell_call = mock_ex.create_order.call_args
    order_type_used = sell_call[1].get("type") or sell_call[0][1]
    assert order_type_used == "market", (
        f"SELL should use market order, got '{order_type_used}'"
    )


# ---------------------------------------------------------------------------
# Test: urgent=True bypasses the limit-chase — SL/TP exits are always market
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_urgent_sell_bypasses_limit_chase(mock_cfg, mock_sleep, tmp_path):
    """SL/TP exit path passes urgent=True: even with LIMIT_ORDER_ENABLED=true
    the order must be a plain market order — a stop must never sit in the
    chase repricing while price runs away."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    # Seed a bot-owned position directly
    ex._portfolio.position    = 0.001
    ex._portfolio._cost_basis = 90_000.0

    sell_raw = {
        "id": "urgent-001", "status": "closed",
        "filled": 0.001, "average": 88_650.0,   # −1.5% stop level
        "fee": {"cost": 0.709, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = sell_raw
    mock_ex.fetch_order.return_value  = sell_raw

    order = ex.execute(Signal.SELL, 88_650.0, 0.001, urgent=True)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    # Market order placed, limit-chase never touched
    call = mock_ex.create_order.call_args
    order_type_used = call[1].get("type") or call[0][1]
    assert order_type_used == "market", (
        f"urgent SELL should use market order, got '{order_type_used}'"
    )
    mock_ex.fetch_order_book.assert_not_called()


# ---------------------------------------------------------------------------
# Slippage guard (MAX_SLIPPAGE_PCT) — post-fill alert only, never blocks
# ---------------------------------------------------------------------------

def test_slippage_guard_alerts_on_unfavorable_buy_fill(tmp_path):
    """BUY filled worse (higher) than expected, past threshold — alerts."""
    ex, mock_ex = _make(
        dry_run=False, starting_cash=100_000.0,
        max_slippage_pct=0.01, tmp_path=tmp_path,
    )
    raw = {
        "id": "order-001", "status": "closed",
        "filled": 0.001, "average": 91_000.0,   # filled 1.11% above expected
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = raw
    mock_ex.fetch_order.return_value  = raw

    with patch.object(ex._alerter, "error") as mock_alert:
        order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is not None and order.status == OrderStatus.FILLED
    mock_alert.assert_called_once()
    assert "SLIPPAGE WARNING" in mock_alert.call_args[0][0]


def test_slippage_guard_alerts_on_unfavorable_sell_fill(tmp_path):
    """SELL filled worse (lower) than expected, past threshold — alerts."""
    ex, mock_ex = _make(
        dry_run=False, starting_cash=100_000.0,
        max_slippage_pct=0.01, tmp_path=tmp_path,
    )
    buy_raw = {
        "id": "order-buy", "status": "closed",
        "filled": 0.001, "average": 90_000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = buy_raw
    mock_ex.fetch_order.return_value  = buy_raw
    ex.execute(Signal.BUY, 90_000.0, 0.001)

    sell_raw = {
        "id": "order-sell", "status": "closed",
        "filled": 0.001, "average": 89_000.0,   # filled 1.11% below expected
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = sell_raw
    mock_ex.fetch_order.return_value  = sell_raw

    with patch.object(ex._alerter, "error") as mock_alert:
        order = ex.execute(Signal.SELL, 90_000.0, 0.001)

    assert order is not None and order.status == OrderStatus.FILLED
    mock_alert.assert_called_once()
    assert "SLIPPAGE WARNING" in mock_alert.call_args[0][0]


def test_slippage_within_threshold_no_alert(tmp_path):
    """Small deviation under the threshold — no alert."""
    ex, mock_ex = _make(
        dry_run=False, starting_cash=100_000.0,
        max_slippage_pct=0.01, tmp_path=tmp_path,
    )
    raw = {
        "id": "order-001", "status": "closed",
        "filled": 0.001, "average": 90_090.0,   # 0.1% above expected — within 1% threshold
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = raw
    mock_ex.fetch_order.return_value  = raw

    with patch.object(ex._alerter, "error") as mock_alert:
        ex.execute(Signal.BUY, 90_000.0, 0.001)

    mock_alert.assert_not_called()


def test_slippage_favorable_direction_never_alerts(tmp_path):
    """BUY filled CHEAPER than expected — favorable, must never alert
    regardless of how large the gap is."""
    ex, mock_ex = _make(
        dry_run=False, starting_cash=100_000.0,
        max_slippage_pct=0.01, tmp_path=tmp_path,
    )
    raw = {
        "id": "order-001", "status": "closed",
        "filled": 0.001, "average": 80_000.0,   # filled well BELOW expected — a good fill
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = raw
    mock_ex.fetch_order.return_value  = raw

    with patch.object(ex._alerter, "error") as mock_alert:
        ex.execute(Signal.BUY, 90_000.0, 0.001)

    mock_alert.assert_not_called()


def test_slippage_guard_disabled_via_zero_threshold(tmp_path):
    """max_slippage_pct=0.0 (the LiveExecutor default) disables the guard
    entirely — even a large unfavorable fill must not alert."""
    ex, mock_ex = _make(
        dry_run=False, starting_cash=100_000.0,
        max_slippage_pct=0.0, tmp_path=tmp_path,
    )
    raw = {
        "id": "order-001", "status": "closed",
        "filled": 0.001, "average": 99_000.0,   # 10% above expected
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = raw
    mock_ex.fetch_order.return_value  = raw

    with patch.object(ex._alerter, "error") as mock_alert:
        ex.execute(Signal.BUY, 90_000.0, 0.001)

    mock_alert.assert_not_called()


def test_slippage_guard_dry_run_never_alerts(tmp_path):
    """Dry-run always fills at exactly the requested price — the guard
    naturally never has anything to flag, but locked in explicitly."""
    ex, mock_ex = _make(dry_run=True, starting_cash=100_000.0, max_slippage_pct=0.01, tmp_path=tmp_path)

    with patch.object(ex._alerter, "error") as mock_alert:
        ex.execute(Signal.BUY, 90_000.0, 0.001)

    mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# Native stop-loss backstop (NATIVE_STOP_LOSS_ENABLED)
# ---------------------------------------------------------------------------

def test_native_stop_disabled_by_default_noop(tmp_path):
    """Feature flag off (the default) — sync_protective_stop never touches the exchange."""
    ex, mock_ex = _make(dry_run=False, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    ex.sync_protective_stop(88_000.0)
    mock_ex.create_order.assert_not_called()
    mock_ex.cancel_order.assert_not_called()
    assert not ex.has_resting_stop


def test_native_stop_dry_run_noop(tmp_path):
    """Feature enabled but dry_run=True — must never place a real order."""
    ex, mock_ex = _make(dry_run=True, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    ex.sync_protective_stop(88_000.0)
    mock_ex.create_order.assert_not_called()
    assert not ex.has_resting_stop


def test_native_stop_placed_with_stop_loss_price_param(tmp_path):
    """Enabled + live: places a market SELL with Kraken's stopLossPrice param,
    sized to the current position."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.price_to_precision.return_value = "88000.0"
    mock_ex.create_order.return_value = {"id": "stop-001"}

    ex.sync_protective_stop(88_000.0)

    assert ex.has_resting_stop
    call = mock_ex.create_order.call_args
    assert call[0][0] == "BTC/CAD"
    assert call[0][1] == "market"
    assert call[0][2] == "sell"
    assert abs(call[0][3] - 0.001) < 1e-9
    assert call[1]["params"]["stopLossPrice"] == "88000.0"


def test_native_stop_cancelled_when_position_closes(tmp_path):
    """sync_protective_stop(None) cancels an existing resting stop and doesn't replace it."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(88_000.0)
    assert ex.has_resting_stop

    ex._portfolio.position = 0.0   # position closed by the caller before this call
    ex.sync_protective_stop(None)

    mock_ex.cancel_order.assert_called_once_with("stop-001", "BTC/CAD")
    assert not ex.has_resting_stop


def test_native_stop_resync_replaces_existing_order(tmp_path):
    """A second sync_protective_stop call (e.g. after a partial fill changes
    quantity) cancels the old resting order before placing the new one."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(88_000.0)

    ex._portfolio.position = 0.0005
    mock_ex.create_order.return_value = {"id": "stop-002"}
    ex.sync_protective_stop(88_000.0)

    mock_ex.cancel_order.assert_called_once_with("stop-001", "BTC/CAD")
    assert ex._native_stop_order_id == "stop-002"


def test_native_stop_cancel_failure_is_swallowed(tmp_path):
    """Cancelling an already-filled/gone order raises on Kraken — must not
    propagate (the whole point: the position closed one way or another)."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(88_000.0)

    mock_ex.cancel_order.side_effect = ccxt.OrderNotFound("already filled")
    ex._portfolio.position = 0.0
    ex.sync_protective_stop(None)   # must not raise

    assert not ex.has_resting_stop


def test_native_stop_placement_failure_alerts_and_stays_unprotected(tmp_path):
    """create_order fails — must alert, not raise, and leave has_resting_stop False
    so the caller/next cycle knows the position is unprotected."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.create_order.side_effect = ccxt.BaseError("exchange rejected")

    with patch.object(ex._alerter, "error") as mock_alert:
        ex.sync_protective_stop(88_000.0)   # must not raise

    assert not ex.has_resting_stop
    mock_alert.assert_called_once()
    assert "NATIVE STOP FAILED" in mock_alert.call_args[0][0]


def test_native_stop_state_persists_and_restores_across_restart(tmp_path):
    """Order id/price survive a save/reload cycle, same as every other
    accounting field."""
    state_path = str(tmp_path / "state.json")
    ex, mock_ex = _make(
        dry_run=False, native_stop_loss_enabled=True,
        state_path=state_path, tmp_path=tmp_path,
    )
    ex._portfolio.position = 0.001
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(88_000.0)

    with open(state_path) as f:
        saved = json.load(f)
    assert saved["native_stop_order_id"] == "stop-001"
    assert saved["native_stop_price"]    == 88_000.0


def test_native_stop_startup_confirms_still_open_order(tmp_path):
    """Restart with a saved stop id that's still open on the exchange — kept, no re-placement."""
    state_path = str(tmp_path / "state.json")
    json.dump({
        "symbol": "BTC/CAD", "cash": 89.88, "position": 0.001,
        "cost_basis": 88_870.0, "realized_pnl": 0.0, "fees_paid": 0.0,
        "native_stop_order_id": "stop-001", "native_stop_price": 88_000.0,
        "saved_at": "2026-08-01T00:00:00+00:00",
    }, open(state_path, "w"))

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.return_value = {
        "free": {"CAD": 89.88, "BTC": 0.001}, "total": {"CAD": 89.88, "BTC": 0.001},
    }
    mock_ex.fetch_open_orders.return_value = [{"id": "stop-001"}]

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=100.0, dry_run=False, state_path=state_path,
            native_stop_loss_enabled=True,
        )

    assert ex.has_resting_stop
    assert ex._native_stop_order_id == "stop-001"
    mock_ex.create_order.assert_not_called()   # nothing re-placed — already confirmed live


def test_native_stop_startup_detects_gap_when_order_gone(tmp_path):
    """Restart with a saved stop id that's no longer open (cancelled somehow while
    the bot was down, position still held) — cleared so main.py's fallback can act."""
    state_path = str(tmp_path / "state.json")
    json.dump({
        "symbol": "BTC/CAD", "cash": 89.88, "position": 0.001,
        "cost_basis": 88_870.0, "realized_pnl": 0.0, "fees_paid": 0.0,
        "native_stop_order_id": "stop-001", "native_stop_price": 88_000.0,
        "saved_at": "2026-08-01T00:00:00+00:00",
    }, open(state_path, "w"))

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.return_value = {
        "free": {"CAD": 89.88, "BTC": 0.001}, "total": {"CAD": 89.88, "BTC": 0.001},
    }
    mock_ex.fetch_open_orders.return_value = []   # gone

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=100.0, dry_run=False, state_path=state_path,
            native_stop_loss_enabled=True,
        )

    assert not ex.has_resting_stop   # cleared — main.py's startup loop will re-place


def test_native_stop_startup_position_closed_externally_clears_stale_id(tmp_path):
    """Position closed while the bot was down (the native stop's whole job) —
    exchange shows 0, saved stop id is stale and gets cleared. No re-placement:
    there's no position left to protect."""
    state_path = str(tmp_path / "state.json")
    json.dump({
        "symbol": "BTC/CAD", "cash": 89.88, "position": 0.001,
        "cost_basis": 88_870.0, "realized_pnl": 0.0, "fees_paid": 0.0,
        "native_stop_order_id": "stop-001", "native_stop_price": 88_000.0,
        "saved_at": "2026-08-01T00:00:00+00:00",
    }, open(state_path, "w"))

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.return_value = {
        "free": {"CAD": 89.88, "BTC": 0.0}, "total": {"CAD": 89.88, "BTC": 0.0},
    }

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=100.0, dry_run=False, state_path=state_path,
            native_stop_loss_enabled=True,
        )

    assert ex.position == 0.0
    assert not ex.has_resting_stop
    mock_ex.cancel_order.assert_called_once_with("stop-001", "BTC/CAD")


# ---------------------------------------------------------------------------
# Native trailing-stop backstop (sync_protective_stop trailing_pct path —
# 2026-08-19: mirrors the software trailing stop when it's the level in
# control, i.e. ATR SL unavailable and TRAILING_STOP_PCT>0)
# ---------------------------------------------------------------------------

def test_native_trailing_stop_placed_with_trailing_percent_param(tmp_path):
    """trailing_pct>0 places a market SELL with Kraken's trailingPercent param
    (not stopLossPrice), sized to the current position."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.create_order.return_value = {"id": "trail-001"}

    ex.sync_protective_stop(None, trailing_pct=0.02)

    assert ex.has_resting_stop
    assert ex.native_stop_is_trailing
    call = mock_ex.create_order.call_args
    assert call[0][0] == "BTC/CAD"
    assert call[0][1] == "market"
    assert call[0][2] == "sell"
    assert abs(call[0][3] - 0.001) < 1e-9
    assert "stopLossPrice" not in call[1]["params"]
    assert call[1]["params"]["trailingPercent"] == "2.0000"


def test_native_trailing_stop_takes_priority_over_stop_price(tmp_path):
    """When both are given, trailing_pct wins — the static stop_price is ignored."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.create_order.return_value = {"id": "trail-001"}

    ex.sync_protective_stop(88_000.0, trailing_pct=0.02)

    call = mock_ex.create_order.call_args
    assert "trailingPercent" in call[1]["params"]
    assert "stopLossPrice" not in call[1]["params"]


def test_native_trailing_stop_dry_run_noop(tmp_path):
    """Feature enabled but dry_run=True — must never place a real order."""
    ex, mock_ex = _make(dry_run=True, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    ex.sync_protective_stop(None, trailing_pct=0.02)
    mock_ex.create_order.assert_not_called()
    assert not ex.has_resting_stop


def test_native_trailing_stop_cancelled_when_position_closes(tmp_path):
    """sync_protective_stop(None) with no trailing_pct cancels an existing
    resting trailing stop and doesn't replace it."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.create_order.return_value = {"id": "trail-001"}
    ex.sync_protective_stop(None, trailing_pct=0.02)
    assert ex.has_resting_stop

    ex._portfolio.position = 0.0
    ex.sync_protective_stop(None)

    mock_ex.cancel_order.assert_called_once_with("trail-001", "BTC/CAD")
    assert not ex.has_resting_stop
    assert not ex.native_stop_is_trailing


def test_native_trailing_stop_resync_on_quantity_change_replaces_order(tmp_path):
    """A quantity change (partial TP / partial fill) cancels the old resting
    trailing order and places a fresh one sized to the new quantity — same
    cancel/replace shape as the static path, since Kraken's create_order has
    no in-place volume amend."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.create_order.return_value = {"id": "trail-001"}
    ex.sync_protective_stop(None, trailing_pct=0.02)

    ex._portfolio.position = 0.0005
    mock_ex.create_order.return_value = {"id": "trail-002"}
    ex.sync_protective_stop(None, trailing_pct=0.02)

    mock_ex.cancel_order.assert_called_once_with("trail-001", "BTC/CAD")
    assert ex._native_stop_order_id == "trail-002"
    assert ex.native_stop_is_trailing
    call = mock_ex.create_order.call_args
    assert abs(call[0][3] - 0.0005) < 1e-9


def test_native_trailing_stop_placement_failure_alerts_and_stays_unprotected(tmp_path):
    """create_order fails — must alert, not raise, and leave has_resting_stop
    False and native_stop_is_trailing False."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.create_order.side_effect = ccxt.BaseError("exchange rejected")

    with patch.object(ex._alerter, "error") as mock_alert:
        ex.sync_protective_stop(None, trailing_pct=0.02)   # must not raise

    assert not ex.has_resting_stop
    assert not ex.native_stop_is_trailing
    mock_alert.assert_called_once()
    assert "NATIVE TRAILING STOP FAILED" in mock_alert.call_args[0][0]


def test_native_trailing_stop_state_persists_and_restores_across_restart(tmp_path):
    """Order id / trailing flag survive a save/reload cycle. native_stop_price
    stays None for a trailing order — there's no fixed price to restore."""
    state_path = str(tmp_path / "state.json")
    ex, mock_ex = _make(
        dry_run=False, native_stop_loss_enabled=True,
        state_path=state_path, tmp_path=tmp_path,
    )
    ex._portfolio.position = 0.001
    mock_ex.create_order.return_value = {"id": "trail-001"}
    ex.sync_protective_stop(None, trailing_pct=0.02)

    with open(state_path) as f:
        saved = json.load(f)
    assert saved["native_stop_order_id"]    == "trail-001"
    assert saved["native_stop_price"]       is None
    assert saved["native_stop_is_trailing"] is True

    # Fresh executor loading this state restores the trailing flag.
    mock_ex2 = MagicMock()
    mock_ex2.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex2.fetch_balance.return_value = {
        "free": {"CAD": ex.cash, "BTC": 0.001}, "total": {"CAD": ex.cash, "BTC": 0.001},
    }
    mock_ex2.fetch_open_orders.return_value = [{"id": "trail-001"}]
    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex2
        ex2 = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=100.0, dry_run=False, state_path=state_path,
            native_stop_loss_enabled=True,
        )
    assert ex2.has_resting_stop
    assert ex2.native_stop_is_trailing
    mock_ex2.create_order.assert_not_called()   # confirmed still open — nothing re-placed


if __name__ == "__main__":
    import pathlib
    import shutil
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
        # Standalone runner has no pytest tmp_path fixture — build an
        # equivalent per-test dir and clean it up manually.
        fake_tmp_path = pathlib.Path(tempfile.mkdtemp())
        try:
            # test_state_save_load_roundtrip manages its own TemporaryDirectory
            # internally and takes no tmp_path arg.
            if t is test_state_save_load_roundtrip:
                t()
            else:
                # Keyword, not positional — @patch-decorated tests append their
                # injected mocks after positional args, which would shift
                # fake_tmp_path into the wrong (mock_cfg) parameter slot.
                t(tmp_path=fake_tmp_path)
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failures += 1
        finally:
            shutil.rmtree(fake_tmp_path, ignore_errors=True)
    print(f"\n{len(tests) - failures}/{len(tests)} passed.")
    sys.exit(failures)
