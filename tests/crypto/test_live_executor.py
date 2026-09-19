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
from bot.alerts.telegram import TelegramAlerter
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
    # Real ccxt always returns a string from price_to_precision(); an
    # unconfigured MagicMock returns another MagicMock, which is not JSON
    # serializable — harmless when only passed through to create_order(),
    # but 2026-09-18's persisted pending-submission marker now stores this
    # value via _save_state(), so tests that exercise the limit-chase path
    # without their own explicit price_to_precision mock need a real
    # string default. Plain return_value (not side_effect) so a test that
    # sets its own mock_ex.price_to_precision.return_value afterward still
    # cleanly overrides this default.
    mock_ex.price_to_precision.return_value = "0.0"

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
# 2026-09-18 review finding: a partial SELL request used to be silently
# overwritten with a full-position SELL, unconditionally.
# ---------------------------------------------------------------------------

def test_partial_sell_honors_requested_quantity_leaves_residual(tmp_path):
    """Reproduced by the review: buy 0.002 BTC, request SELL 0.001 BTC —
    must fill 0.001 and leave 0.001 held, not liquidate the whole position."""
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    buy_raw = {
        "id": "order-buy", "status": "closed",
        "filled": 0.002, "average": 90_000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = buy_raw
    mock_ex.fetch_order.return_value  = buy_raw
    ex.execute(Signal.BUY, 90_000.0, 0.002)
    assert abs(ex.position - 0.002) < 1e-9

    sell_raw = {
        "id": "order-sell", "status": "closed",
        "filled": 0.001, "average": 91_000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = sell_raw
    mock_ex.fetch_order.return_value  = sell_raw
    order = ex.execute(Signal.SELL, 91_000.0, quantity=0.001)

    assert order is not None and order.status == OrderStatus.FILLED
    assert abs(order.quantity - 0.001) < 1e-9
    assert abs(ex.position - 0.001) < 1e-9          # residual half still held, not zero


def test_sell_request_exceeding_position_is_capped_not_overselling(tmp_path):
    """An overlarge (stale/buggy caller) SELL request must be capped at
    what's actually held — never oversell."""
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)
    ex._portfolio.position    = 0.001
    ex._portfolio._cost_basis = 90_000.0

    sell_raw = {
        "id": "order-sell", "status": "closed",
        "filled": 0.001, "average": 91_000.0,
        "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = sell_raw
    mock_ex.fetch_order.return_value  = sell_raw

    order = ex.execute(Signal.SELL, 91_000.0, quantity=0.01)   # 10x what's held

    # The limit-chase path (default LIMIT_ORDER_ENABLED=true) calls
    # create_order positionally: (symbol, "limit", side, quantity, price, params).
    call = mock_ex.create_order.call_args
    assert abs(call[0][3] - 0.001) < 1e-9   # capped, never asked the exchange for 0.01
    assert order.quantity <= 0.001


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
# 2026-09-18 review finding (P1-3): durable fill journal — a fill's
# accounting is persisted separately from (and before) the caller's own
# trade_log write; pending_journal_entries / ack_journal_entry close that
# gap. 2026-09-18 FOLLOW-UP review: upgraded from a single dict (a second
# fill recorded before the first was acked silently overwrote it) to a
# list — every unacked fill survives independently.
# ---------------------------------------------------------------------------

def test_fill_sets_pending_journal_entry(tmp_path):
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)
    assert ex.pending_journal_entries == []

    order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    entries = ex.pending_journal_entries
    assert len(entries) == 1
    entry = entries[0]
    assert entry["order_id"] == order.order_id
    assert entry["side"] == "BUY"
    assert abs(entry["quantity"] - 0.001) < 1e-9
    assert entry["price"] == 90_000.0
    assert "exec_key" in entry and entry["exec_key"]


def test_ack_journal_entry_clears_it(tmp_path):
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)
    order = ex.execute(Signal.BUY, 90_000.0, 0.001)
    assert len(ex.pending_journal_entries) == 1

    ex.ack_journal_entry(order.order_id)

    assert ex.pending_journal_entries == []


def test_ack_journal_entry_ignores_mismatched_order_id(tmp_path):
    """Defensive: acking the WRONG order id must never clear a still-
    genuinely-pending entry for a different fill."""
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)
    ex.execute(Signal.BUY, 90_000.0, 0.001)
    assert len(ex.pending_journal_entries) == 1

    ex.ack_journal_entry("some-other-order-id")

    assert len(ex.pending_journal_entries) == 1   # NOT cleared


def test_pending_journal_entry_survives_restart(tmp_path):
    """The exact crash scenario: a fill's accounting is persisted, but the
    process dies before the caller (bot/main.py) gets to ack it. A restart
    must still see the pending entry so it can be replayed into trade_log."""
    state_path = str(tmp_path / "state.json")
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, state_path=state_path, tmp_path=tmp_path)
    ex.execute(Signal.BUY, 90_000.0, 0.001)
    assert len(ex.pending_journal_entries) == 1

    mock_ex.fetch_balance.return_value = {
        "free":  {"CAD": ex.cash, "BTC": ex.position},
        "total": {"CAD": ex.cash, "BTC": ex.position},
    }
    mock_ex.fetch_ticker.return_value = {"last": ex.avg_entry}

    with patch.object(le_mod.ccxt, "kraken") as mock_cls2:
        mock_cls2.return_value = mock_ex
        ex2 = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=1000.0, dry_run=False, state_path=state_path,
        )

    assert len(ex2.pending_journal_entries) == 1


def test_second_fill_before_first_ack_does_not_overwrite_it(tmp_path):
    """2026-09-18 follow-up review finding: a single-dict pending marker
    meant a second fill recorded before the first was acked silently lost
    the first's recovery record. Both must survive independently."""
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)
    order1 = ex.execute(Signal.BUY, 90_000.0, 0.001)
    order2 = ex.execute(Signal.BUY, 91_000.0, 0.001)   # neither acked yet

    entries = ex.pending_journal_entries
    assert len(entries) == 2
    assert {e["order_id"] for e in entries} == {order1.order_id, order2.order_id}

    ex.ack_journal_entry(order1.order_id)
    remaining = ex.pending_journal_entries
    assert len(remaining) == 1
    assert remaining[0]["order_id"] == order2.order_id


def test_exec_key_disambiguates_same_order_id_entries(tmp_path):
    """A native stop's order_id is identical across each of its own
    partial-fill deltas — exec_key must still differ per entry."""
    from bot.execution.executor import Order as _Order, OrderSide, OrderStatus
    from datetime import datetime, timezone

    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)
    # Manually craft two entries sharing an order_id, as
    # _record_pending_journal_entry would for two stop-fill deltas.
    shared_id = "native-stop:stop-001"
    o1 = _Order(order_id=shared_id, symbol="BTC/CAD", side=OrderSide.SELL,
                 quantity=0.001, price=78_000.0, status=OrderStatus.FILLED,
                 created_at=datetime.now(timezone.utc), filled_at=datetime.now(timezone.utc))
    o2 = _Order(order_id=shared_id, symbol="BTC/CAD", side=OrderSide.SELL,
                 quantity=0.001, price=77_500.0, status=OrderStatus.FILLED,
                 created_at=datetime.now(timezone.utc), filled_at=datetime.now(timezone.utc))
    ex._record_pending_journal_entry(o1)
    ex._record_pending_journal_entry(o2)

    entries = ex.pending_journal_entries
    assert len(entries) == 2
    assert entries[0]["exec_key"] != entries[1]["exec_key"]
    assert all(e["order_id"] == shared_id for e in entries)


def test_state_save_failure_blocks_new_buys_not_sells(tmp_path):
    """2026-09-18 review finding: opening a new position on top of
    accounting that isn't confirmed durably persisted risks losing it on a
    crash. SELL/exits must stay unblocked — reducing risk is always safe."""
    ex, mock_ex = _make(dry_run=True, starting_cash=1000.0, tmp_path=tmp_path)
    ex.execute(Signal.BUY, 90_000.0, 0.001)   # succeeds, establishes a position
    assert ex.state_write_healthy is True

    with patch("bot.atomic_json.atomic_write_json", side_effect=OSError("disk full")):
        with patch.object(ex._alerter, "error"):
            order = ex.execute(Signal.BUY, 90_000.0, 0.001)   # the save inside this fails

    assert ex.state_write_healthy is False

    blocked = ex.execute(Signal.BUY, 91_000.0, 0.001)
    assert blocked is None   # new BUY refused while state writes are unhealthy

    sell_order = ex.execute(Signal.SELL, 92_000.0, quantity=0.001)
    assert sell_order is not None and sell_order.status == OrderStatus.FILLED   # SELL still works


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
# 2026-09-18 review finding (P1-7): startup sync failure must expose a
# persistent readiness flag and block new BUYs, not just fall back silently.
# ---------------------------------------------------------------------------

def test_startup_sync_healthy_true_on_clean_start(tmp_path):
    ex, mock_ex = _make(dry_run=False, starting_cash=100.0, tmp_path=tmp_path)
    assert ex.startup_sync_healthy is True


def test_startup_cash_sync_failure_marks_unhealthy_and_blocks_buy(tmp_path):
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.side_effect = Exception("API timeout")

    with patch.object(le_mod.ccxt, "kraken") as mock_cls, \
         patch("bot.exchanges.retry.time.sleep"):
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=100.0, dry_run=False,
            state_path=str(tmp_path / "state.json"),
        )

    assert ex.startup_sync_healthy is False
    order = ex.execute(Signal.BUY, 90_000.0, 0.001)
    assert order is None   # BUY refused — never even attempted create_order
    mock_ex.create_order.assert_not_called()


def test_startup_sync_healthy_does_not_block_sell(tmp_path):
    """A degraded startup sync must not also disable exits — a bot that
    can't confirm its balance can still get OUT of a position it already
    knows (from saved state) it holds."""
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.side_effect = Exception("API timeout")

    with patch.object(le_mod.ccxt, "kraken") as mock_cls, \
         patch("bot.exchanges.retry.time.sleep"):
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=100.0, dry_run=False,
            state_path=str(tmp_path / "state.json"),
        )
    assert ex.startup_sync_healthy is False
    ex._portfolio.position    = 0.001
    ex._portfolio._cost_basis = 90_000.0

    sell_raw = {"id": "s1", "status": "closed", "filled": 0.001, "average": 91_000.0,
                "fee": {"cost": 0.0, "currency": "CAD"}}
    mock_ex.create_order.return_value = sell_raw
    mock_ex.fetch_order.return_value  = sell_raw
    order = ex.execute(Signal.SELL, 91_000.0, quantity=0.001, urgent=True)
    assert order is not None and order.status == OrderStatus.FILLED


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
    # Post-only flag sent — postOnly=True, NOT timeInForce="PO" (found
    # 2026-08-26: the latter is invalid on Kraken's real API and was
    # silently falling back to market every time since 2026-06-22; this
    # test had been locking in the buggy value along with the code).
    # clientOrderId (2026-09-12) is a fresh UUID each call, checked for
    # presence/shape rather than an exact value.
    sent_params = mock_ex.create_order.call_args[0][5]
    assert sent_params["postOnly"] is True
    assert isinstance(sent_params.get("clientOrderId"), str) and sent_params["clientOrderId"]
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
def test_maker_fallback_fires_telegram_alert(mock_cfg, mock_sleep, tmp_path):
    """A post-only limit that degrades to a market order fires a MAKER FALLBACK
    Telegram alert — the post-only fee bug hid for 2 months because this path
    was logger.warning only (2026-08-27)."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=0, max_retries=1)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.fetch_order_book.return_value   = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"

    open_raw   = {"id": "limit-0X", "status": "open", "filled": 0.0, "average": None, "fee": {}}
    market_raw = {"id": "mkt-001", "status": "closed", "filled": 0.001, "type": "market",
                  "average": 90000.0, "fee": {"cost": 0.72, "currency": "CAD"}}
    mock_ex.create_order.side_effect = [
        {**open_raw, "id": "limit-01"},
        {**open_raw, "id": "limit-02"},
        market_raw,
    ]
    mock_ex.fetch_order.side_effect = lambda oid, _sym: {
        "id": oid, "status": "canceled", "type": "limit",
        "filled": 0.0, "amount": 0.001, "average": None, "fee": {},
    }

    with patch.object(ex._alerter, "error") as mock_alert:
        order = ex.execute(Signal.BUY, 90000.0, 0.001)

    assert order is not None and order.status == OrderStatus.FILLED
    assert mock_alert.called, "MAKER FALLBACK alert must fire on maker→taker degradation"
    assert "MAKER FALLBACK" in mock_alert.call_args[0][0]
    assert "timed out" in mock_alert.call_args[0][0]


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_maker_fallback_no_alert_on_clean_limit_fill(mock_cfg, mock_sleep, tmp_path):
    """A post-only limit that fills normally must NOT fire the MAKER FALLBACK alert."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.fetch_order_book.return_value  = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"
    mock_ex.create_order.return_value = {
        "id": "limit-001", "status": "closed", "filled": 0.001, "type": "limit",
        "average": 90009.0, "fee": {"cost": 0.360, "currency": "CAD"},
    }

    with patch.object(ex._alerter, "error") as mock_alert:
        order = ex.execute(Signal.BUY, 90000.0, 0.001)

    assert order is not None and order.status == OrderStatus.FILLED
    assert not any(
        "MAKER FALLBACK" in str(c) for c in mock_alert.call_args_list
    ), "clean limit fill must not trigger the maker-fallback alert"


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_submission_exception_adopts_untracked_order_instead_of_market(mock_cfg, mock_sleep, tmp_path):
    """create_order raises after Kraken may have already accepted the
    request (e.g. the response itself was lost to a network error) — the
    exchange might still have the order resting. The chase must check
    fetch_open_orders before assuming failure and adopt a matching resting
    order instead of placing a market order on top of it (duplicate-order
    finding)."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.fetch_order_book.return_value   = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"
    mock_ex.create_order.side_effect = ccxt.NetworkError("response lost")

    # Adoption now matches by clientOrderId (2026-09-12), not just "one
    # untracked same-side order" — pin the UUID this attempt generates so
    # the mocked resting order can carry a matching clientOrderId.
    fixed_coid = "11111111-1111-1111-1111-111111111111"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(le_mod.uuid, "uuid4", lambda: fixed_coid)

    resting = {
        "id": "limit-ghost-1", "status": "open", "side": "buy",
        "filled": 0.0, "amount": 0.001, "average": None, "fee": {},
        "clientOrderId": fixed_coid,
    }
    mock_ex.fetch_open_orders.return_value = [resting]
    mock_ex.fetch_order.return_value = {
        **resting, "status": "closed", "filled": 0.001, "average": 90009.0,
        "fee": {"cost": 0.36, "currency": "CAD"},
    }

    order = ex.execute(Signal.BUY, 90000.0, 0.001)
    monkeypatch.undo()

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.quantity == pytest.approx(0.001)
    # Only the one (failed-response) limit attempt — no market order placed
    assert mock_ex.create_order.call_count == 1
    mock_ex.fetch_open_orders.assert_called()


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_submission_exception_no_untracked_order_falls_back_to_market(mock_cfg, mock_sleep, tmp_path):
    """create_order raises and fetch_open_orders confirms nothing landed —
    the market-order fallback must still happen (no regression from before,
    for the case where the order genuinely never reached the exchange)."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.fetch_order_book.return_value   = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"
    mock_ex.fetch_open_orders.return_value  = []   # nothing resting — genuinely failed

    market_raw = {
        "id": "mkt-999", "status": "closed", "filled": 0.001,
        "average": 90000.0, "fee": {"cost": 0.72, "currency": "CAD"},
    }
    mock_ex.create_order.side_effect = [ccxt.NetworkError("response lost"), market_raw]

    order = ex.execute(Signal.BUY, 90000.0, 0.001)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert mock_ex.create_order.call_count == 2
    last_call = mock_ex.create_order.call_args_list[-1]
    assert last_call[0][1] == "market"


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_submission_exception_already_filled_and_closed_is_not_re_ordered(mock_cfg, mock_sleep, tmp_path):
    """Reviewer-reproduced gap: the original order didn't just rest — it
    fully filled and closed before the exception-recovery check ran, so it
    was ALREADY GONE from fetch_open_orders(). The old heuristic (open
    orders only) read this as 'nothing to adopt' and placed a second market
    order on top of a real, already-filled position. clientOrderId lets the
    check find it in fetch_closed_orders() too."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.fetch_order_book.return_value   = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"
    mock_ex.create_order.side_effect = ccxt.NetworkError("response lost")
    mock_ex.fetch_open_orders.return_value  = []   # already closed — not resting anymore

    fixed_coid = "22222222-2222-2222-2222-222222222222"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(le_mod.uuid, "uuid4", lambda: fixed_coid)

    mock_ex.fetch_closed_orders.return_value = [{
        "id": "limit-already-filled", "status": "closed", "side": "buy",
        "filled": 0.001, "amount": 0.001, "average": 90009.0,
        "fee": {"cost": 0.36, "currency": "CAD"}, "clientOrderId": fixed_coid,
    }]

    order = ex.execute(Signal.BUY, 90000.0, 0.001)
    monkeypatch.undo()

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.quantity == pytest.approx(0.001)
    # The already-filled order was adopted — no second (market) order placed.
    assert mock_ex.create_order.call_count == 1
    mock_ex.fetch_closed_orders.assert_called()


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_cancel_but_order_still_open_does_not_retry(mock_cfg, mock_sleep, caplog, tmp_path):
    """Timeout → cancel_order() reports 'success', but the post-cancel check
    shows the order still status='open' with filled=0 (eventual consistency
    or a silently ignored cancel). The chase must NOT place a second order
    on top of a possibly-still-resting first one."""
    import logging
    _limit_cfg(mock_cfg, enabled=True, timeout_s=0, max_retries=3)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.fetch_order_book.return_value   = _ob()
    mock_ex.price_to_precision.return_value = "90009.0"

    open_raw = {"id": "limit-01", "status": "open", "filled": 0.0, "average": None, "fee": {}}
    mock_ex.create_order.return_value = open_raw
    mock_ex.cancel_order.return_value = {}   # reports success
    # Post-cancel verification still reads the order as open — never confirmed cancelled
    mock_ex.fetch_order.return_value = {
        "id": "limit-01", "status": "open", "filled": 0.0, "average": None, "fee": {},
    }

    with caplog.at_level(logging.ERROR, logger="bot.execution.live_executor"):
        ex.execute(Signal.BUY, 90000.0, 0.001)

    # Exactly one limit attempt and one cancel — must not loop back and
    # place a second order while the first may still be resting.
    assert mock_ex.create_order.call_count == 1
    assert mock_ex.cancel_order.call_count == 1
    assert "aborting chase without re-placing" in caplog.text


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
    """BUY with order_type='limit' must use price*0.998 and postOnly=True."""
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
    # postOnly must be present; clientOrderId (2026-09-18 reconciliation
    # fix) is also expected now — no longer asserting an exact dict.
    assert params.get("postOnly") is True, f"missing post-only: {params}"
    assert "clientOrderId" in params


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
# 2026-09-18 review finding: direct market/limit-BUY submission exceptions
# must be reconciled, not assumed failed — same protection the limit-chase
# path already had (2026-09-11), extended to the two paths that lacked it.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_direct_market_submission_exception_adopts_untracked_order(mock_cfg, mock_sleep, tmp_path):
    """create_order raises on the direct/urgent market path, but the order
    actually reached Kraken (found resting via client_order_id) — adopt it
    instead of placing a second market order on top."""
    mock_cfg.exchange.limit_order_enabled = False
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    resting = {
        "id": "mkt-001", "status": "closed", "filled": 0.001,
        "average": 90_100.0, "clientOrderId": "will-be-overwritten",
        "fee": {"cost": 0.5, "currency": "CAD"},
    }

    def _create_order_side_effect(**kwargs):
        resting["clientOrderId"] = kwargs["params"]["clientOrderId"]
        raise ccxt.RequestTimeout("response lost")

    mock_ex.create_order.side_effect = _create_order_side_effect
    mock_ex.fetch_open_orders.return_value = []
    mock_ex.fetch_closed_orders.return_value = [resting]

    order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is not None and order.status == OrderStatus.FILLED
    assert order.order_id == "mkt-001"
    assert mock_ex.create_order.call_count == 1   # never placed a second order


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_direct_market_submission_exception_confirmed_empty_is_rejected(mock_cfg, mock_sleep, tmp_path):
    """create_order raises and reconciliation confirms (both open and
    closed orders checked) that nothing was placed — a genuine, safe
    rejection, same REJECTED order as before this fix."""
    mock_cfg.exchange.limit_order_enabled = False
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.create_order.side_effect = ccxt.ExchangeError("kraken rejected")
    mock_ex.fetch_open_orders.return_value = []
    mock_ex.fetch_closed_orders.return_value = []

    order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is not None and order.status == OrderStatus.REJECTED
    assert mock_ex.create_order.call_count == 1


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_direct_market_submission_exception_unconfirmed_holds_back(mock_cfg, mock_sleep, tmp_path):
    """2026-09-18 review finding (the core P0): create_order raises AND the
    reconciliation lookup itself fails — the outcome is genuinely unknown.
    Must NOT fall back to a second market order, and must NOT record a
    REJECTED order either (that would wrongly license a same-tick retry) —
    execute() returns None and leaves it for manual/next-cycle resolution."""
    mock_cfg.exchange.limit_order_enabled = False
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.create_order.side_effect = ccxt.RequestTimeout("response lost")
    mock_ex.fetch_open_orders.side_effect = ccxt.NetworkError("still unreachable")

    with patch.object(ex._alerter, "error") as mock_alert:
        order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is None
    assert mock_ex.create_order.call_count == 1   # exactly one submission attempt, ever
    assert ex.rejected_orders() == []              # not recorded as a confirmed rejection either
    mock_alert.assert_called_once()
    assert "OUTCOME UNKNOWN" in mock_alert.call_args[0][0]


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_direct_limit_buy_submission_exception_unconfirmed_holds_back(mock_cfg, mock_sleep, tmp_path):
    """Same as above, for the direct passive-limit BUY path (ORDER_TYPE=limit,
    LIMIT_ORDER_ENABLED=false — the non-chased limit path)."""
    mock_cfg.exchange.limit_order_enabled = False
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, order_type="limit", tmp_path=tmp_path)

    mock_ex.create_order.side_effect = ccxt.RequestTimeout("response lost")
    mock_ex.fetch_open_orders.side_effect = ccxt.NetworkError("still unreachable")

    order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is None
    assert mock_ex.create_order.call_count == 1


# ---------------------------------------------------------------------------
# 2026-09-18 FOLLOW-UP review finding (P0): an unresolved submission left no
# persisted trace — a SECOND execute() call (same tick retry, or next tick,
# or after a restart) generated a fresh client_order_id and submitted again.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_two_consecutive_calls_with_unresolved_outcome_submit_only_once(mock_cfg, mock_sleep, tmp_path):
    """Reproduced by the follow-up review: two consecutive execute(BUY, ...)
    calls, both hitting a submission timeout with reconciliation itself
    unavailable, used to produce TWO create_order calls. Must produce
    exactly one — the second call must refuse to submit a fresh order
    while the first's outcome is still unresolved."""
    mock_cfg.exchange.limit_order_enabled = False
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.create_order.side_effect = ccxt.RequestTimeout("response lost")
    mock_ex.fetch_open_orders.side_effect = ccxt.NetworkError("still unreachable")

    order1 = ex.execute(Signal.BUY, 90_000.0, 0.001)
    order2 = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order1 is None
    assert order2 is None
    assert mock_ex.create_order.call_count == 1   # NOT 2
    assert "buy" in ex.pending_submissions
    assert ex.pending_submissions["buy"]["side"] == "buy"


def test_pending_submission_resolves_once_reconciliation_recovers(tmp_path):
    """Once the exchange becomes reachable again, the NEXT execute() call
    must first resolve the OLD pending submission (via its own
    client_order_id) before deciding whether to place a new one — here it
    discovers the old one actually filled, and adopts it instead of
    submitting fresh."""
    with patch("bot.execution.live_executor.cfg") as mock_cfg, patch("time.sleep"):
        mock_cfg.exchange.limit_order_enabled = False
        ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

        mock_ex.create_order.side_effect = ccxt.RequestTimeout("response lost")
        mock_ex.fetch_open_orders.side_effect = ccxt.NetworkError("still unreachable")
        ex.execute(Signal.BUY, 90_000.0, 0.001)
        assert "buy" in ex.pending_submissions
        _cid = ex.pending_submissions["buy"]["client_order_id"]

        # Exchange reachable again — the stale submission is discovered
        # already closed (filled) when reconciliation is retried.
        old_order = {
            "id": "recovered-001", "status": "closed", "filled": 0.001,
            "average": 90_050.0, "clientOrderId": _cid,
            "fee": {"cost": 0.36, "currency": "CAD"},
        }
        mock_ex.fetch_open_orders.side_effect = None
        mock_ex.fetch_open_orders.return_value = []
        mock_ex.fetch_closed_orders.return_value = [old_order]

        order = ex.execute(Signal.BUY, 91_000.0, 0.001)

    assert order is not None and order.status == OrderStatus.FILLED
    assert order.order_id == "recovered-001"
    assert mock_ex.create_order.call_count == 1   # never placed a second, fresh order
    assert "buy" not in ex.pending_submissions


def test_pending_submission_survives_restart(tmp_path):
    """The exact crash scenario: process dies with a submission genuinely
    unresolved. A restart must still see the pending submission so it can
    be reconciled before any new trading, rather than starting fresh and
    blind to it."""
    state_path = str(tmp_path / "state.json")
    with patch("bot.execution.live_executor.cfg") as mock_cfg, patch("time.sleep"):
        mock_cfg.exchange.limit_order_enabled = False
        ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, state_path=state_path, tmp_path=tmp_path)
        mock_ex.create_order.side_effect = ccxt.RequestTimeout("response lost")
        mock_ex.fetch_open_orders.side_effect = ccxt.NetworkError("still unreachable")
        ex.execute(Signal.BUY, 90_000.0, 0.001)
        assert "buy" in ex.pending_submissions
        _pending = ex.pending_submissions["buy"]

    mock_ex.fetch_balance.return_value = {
        "free": {"CAD": ex.cash, "BTC": ex.position}, "total": {"CAD": ex.cash, "BTC": ex.position},
    }
    with patch.object(le_mod.ccxt, "kraken") as mock_cls2:
        mock_cls2.return_value = mock_ex
        ex2 = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=1000.0, dry_run=False, state_path=state_path,
        )

    assert "buy" in ex2.pending_submissions
    assert ex2.pending_submissions["buy"]["client_order_id"] == _pending["client_order_id"]


# ---------------------------------------------------------------------------
# 2026-09-18 PASS-3 review findings — the submission lifecycle must stay
# unresolved until a CONFIRMED TERMINAL outcome, not merely a successful
# (but possibly still-open) network round-trip.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_accepted_but_still_open_order_across_two_calls_submits_once(mock_cfg, mock_sleep, tmp_path):
    """Reproduced by PASS-3: create_order (and every subsequent poll)
    returns an ACCEPTED order that stays status="open"/filled=0 — no
    exception anywhere. Two consecutive execute(BUY, ...) calls must still
    submit only once; the order remaining genuinely unresolved (not a
    submission exception) must keep the pending-submission slot occupied.
    fetch_open_orders reflects the real (still-resting) order on the
    second call's reconciliation check, exactly as the real exchange
    would — a genuinely still-open order IS found there, unlike a
    confirmed-empty lookup."""
    mock_cfg.exchange.limit_order_enabled = False
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    open_order = {"id": "x1", "status": "open", "filled": 0.0}
    mock_ex.create_order.return_value = open_order
    mock_ex.fetch_order.return_value = open_order

    order1 = ex.execute(Signal.BUY, 90_000.0, 0.001)
    _cid = ex.pending_submissions["buy"]["client_order_id"]
    mock_ex.fetch_open_orders.return_value = [{**open_order, "clientOrderId": _cid}]

    order2 = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order1 is None
    assert order2 is None
    assert mock_ex.create_order.call_count == 1   # NOT 2
    assert "buy" in ex.pending_submissions
    assert ex.pending_submissions["buy"]["order_id"] == "x1"


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_accepted_order_resolves_once_it_actually_closes(mock_cfg, mock_sleep, tmp_path):
    """Once the SAME order the previous test left pending is discovered to
    have actually closed, the pending-submission slot must clear and a
    genuinely new BUY must be allowed afterward."""
    mock_cfg.exchange.limit_order_enabled = False
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    open_order = {"id": "x1", "status": "open", "filled": 0.0}
    mock_ex.create_order.return_value = open_order
    mock_ex.fetch_order.return_value = open_order
    ex.execute(Signal.BUY, 90_000.0, 0.001)
    _cid = ex.pending_submissions["buy"]["client_order_id"]
    assert "buy" in ex.pending_submissions

    closed_order = {
        "id": "x1", "status": "closed", "filled": 0.001,
        "average": 90_000.0, "fee": {"cost": 0.36, "currency": "CAD"},
        "clientOrderId": _cid,
    }
    mock_ex.fetch_open_orders.return_value = [closed_order]

    order = ex.execute(Signal.BUY, 91_000.0, 0.001)

    assert order is not None and order.status == OrderStatus.FILLED
    assert "buy" not in ex.pending_submissions
    assert mock_ex.create_order.call_count == 1   # never placed a second order


def test_opposite_side_submission_does_not_overwrite_pending_slot(tmp_path):
    """PASS-3 review finding: an opposite-side submission must never
    overwrite the only tracked entry — each side gets its own slot."""
    with patch("bot.execution.live_executor.cfg") as mock_cfg, patch("time.sleep"):
        mock_cfg.exchange.limit_order_enabled = False
        ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

        open_buy = {"id": "buy-1", "status": "open", "filled": 0.0}
        mock_ex.create_order.return_value = open_buy
        mock_ex.fetch_order.return_value = open_buy
        ex.execute(Signal.BUY, 90_000.0, 0.001)
        assert "buy" in ex.pending_submissions

        # An unrelated SELL now happens (e.g. an urgent exit on an
        # existing position) — must not disturb the still-pending BUY.
        ex._portfolio.position    = 0.002
        ex._portfolio._cost_basis = 85_000.0
        sell_raw = {"id": "sell-1", "status": "closed", "filled": 0.002,
                    "average": 91_000.0, "fee": {"cost": 0.5, "currency": "CAD"}}
        mock_ex.create_order.return_value = sell_raw
        mock_ex.fetch_order.return_value = sell_raw
        sell_order = ex.execute(Signal.SELL, 91_000.0, quantity=0.002, urgent=True)

    assert sell_order is not None and sell_order.status == OrderStatus.FILLED
    assert "buy" in ex.pending_submissions        # untouched by the SELL
    assert ex.pending_submissions["buy"]["order_id"] == "buy-1"
    assert "sell" not in ex.pending_submissions   # SELL resolved and cleared


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_failed_pre_submit_persistence_causes_zero_submissions(mock_cfg, mock_sleep, tmp_path):
    """PASS-3 review finding, reproduced exactly: a durable pre-submit
    persistence failure (simulated disk-full) must cause ZERO entry
    submissions — the old code proceeded to contact the exchange anyway."""
    mock_cfg.exchange.limit_order_enabled = False
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    with patch("bot.atomic_json.atomic_write_json", side_effect=OSError("disk full")):
        with patch.object(ex._alerter, "error"):
            order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is not None and order.status == OrderStatus.REJECTED
    mock_ex.create_order.assert_not_called()   # exchange was NEVER contacted
    assert "buy" not in ex.pending_submissions  # nothing to recover — it never happened


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_failed_pre_submit_persistence_after_healthy_start_still_aborts(mock_cfg, mock_sleep, tmp_path):
    """PASS-3 review finding: the entry-health check (state_write_healthy)
    happens BEFORE execute()'s own submission attempt — a write failure
    that occurs DURING this specific submission (not a pre-existing false
    health flag) must still be caught, not just an already-false flag."""
    mock_cfg.exchange.limit_order_enabled = False
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)
    assert ex.state_write_healthy is True   # healthy going in — not a pre-existing flag

    call_count = {"n": 0}
    real_write = None
    from bot.atomic_json import atomic_write_json as _real_write
    def _flaky_write(path, data, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("disk full")
        return _real_write(path, data, **kwargs)

    with patch("bot.atomic_json.atomic_write_json", side_effect=_flaky_write):
        order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is not None and order.status == OrderStatus.REJECTED
    mock_ex.create_order.assert_not_called()


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_market_fallback_submission_timeout_holds_back_not_rejected(mock_cfg, mock_sleep, tmp_path):
    """Reproduced by the follow-up review: order-book fetch fails -> limit
    chase falls back to market -> THAT market submission itself times out.
    The old code let this exception propagate uncaught by any
    reconciliation, landing in execute()'s generic ccxt.BaseError handler
    and marking REJECTED even though the market order might have gone
    through. Must hold back (None), not confirm a rejection, when
    reconciliation itself is unavailable."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.fetch_order_book.side_effect = ccxt.NetworkError("book fetch failed")
    mock_ex.create_order.side_effect = ccxt.RequestTimeout("response lost")
    mock_ex.fetch_open_orders.side_effect = ccxt.NetworkError("still unreachable")

    with patch.object(ex._alerter, "error") as mock_alert:
        order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is None
    assert ex.rejected_orders() == []
    assert mock_ex.create_order.call_count == 1
    assert any("OUTCOME UNKNOWN" in c.args[0] for c in mock_alert.call_args_list)


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_market_fallback_reconciles_and_adopts_on_confirmed_fill(mock_cfg, mock_sleep, tmp_path):
    """Same market-fallback-after-book-fetch-failure path, but this time
    reconciliation succeeds and finds the market order actually landed —
    must adopt it, not place a second market order on top."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, starting_cash=1000.0, tmp_path=tmp_path)

    mock_ex.fetch_order_book.side_effect = ccxt.NetworkError("book fetch failed")

    def _create_order_side_effect(*args, **kwargs):
        raise ccxt.RequestTimeout("response lost")
    mock_ex.create_order.side_effect = _create_order_side_effect
    mock_ex.fetch_open_orders.return_value = []

    def _closed_orders_side_effect(symbol, limit=10):
        _cid = mock_ex.create_order.call_args
        return [{
            "id": "mkt-recovered", "status": "closed", "filled": 0.001,
            "average": 90_000.0,
            "clientOrderId": mock_ex.create_order.call_args[0][-1].get("clientOrderId")
                if mock_ex.create_order.call_args and isinstance(mock_ex.create_order.call_args[0][-1], dict)
                else None,
            "fee": {"cost": 0.5, "currency": "CAD"},
        }]
    mock_ex.fetch_closed_orders.side_effect = _closed_orders_side_effect

    order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is not None and order.status == OrderStatus.FILLED
    assert order.order_id == "mkt-recovered"
    assert mock_ex.create_order.call_count == 1
    assert "buy" not in ex.pending_submissions


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
    mock_ex.fetch_order.return_value = {"id": "stop-001", "status": "canceled", "filled": 0.0}
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
    mock_ex.fetch_order.return_value = {"id": "stop-001", "status": "canceled", "filled": 0.0}
    mock_ex.create_order.return_value = {"id": "stop-002"}
    ex.sync_protective_stop(88_000.0)

    mock_ex.cancel_order.assert_called_once_with("stop-001", "BTC/CAD")
    assert ex._native_stop_order_id == "stop-002"


def test_native_stop_cancel_failure_confirmed_gone_clears_tracking(tmp_path):
    """Cancelling raises on Kraken (e.g. OrderNotFound), but the follow-up
    verification confirms the order is genuinely canceled/gone with nothing
    filled — tracked protection clears and a replacement may be placed."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(88_000.0)

    mock_ex.cancel_order.side_effect = ccxt.OrderNotFound("already filled")
    mock_ex.fetch_order.return_value = {"id": "stop-001", "status": "canceled", "filled": 0.0}
    ex._portfolio.position = 0.0
    ex.sync_protective_stop(None)   # must not raise

    assert not ex.has_resting_stop


def test_native_stop_cancel_unconfirmed_leaves_tracking_intact(tmp_path):
    """2026-09-18 review finding: cancel_order raises AND the follow-up
    verification also fails (or is itself unreachable) — the old code
    cleared the tracked stop id anyway, which could leave the bot believing
    a real resting stop was gone. The tracked id/price must survive
    untouched so nothing downstream assumes protection was removed."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.001
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(88_000.0)
    assert ex.has_resting_stop

    mock_ex.cancel_order.side_effect = ccxt.NetworkError("timeout")
    mock_ex.fetch_order.side_effect = ccxt.NetworkError("timeout")

    with patch.object(ex._alerter, "error") as mock_alert:
        outcome, fill_order = ex._cancel_native_stop()

    assert outcome == "unknown"
    assert fill_order is None
    assert ex.has_resting_stop                       # NOT cleared
    assert ex._native_stop_order_id == "stop-001"
    assert ex._native_stop_price == 88_000.0
    mock_alert.assert_called_once()
    assert "UNCONFIRMED" in mock_alert.call_args[0][0]


def test_native_stop_cancel_race_fill_records_exit_once(tmp_path):
    """2026-09-18 review finding: the stop fires (fills) in the race between
    our cancel attempt and its own trigger. The old code discarded this
    silently (no fill record, no P&L, no CSV row). Now it must be recorded
    through the same accounting a normal SELL fill gets, exactly once."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=2000.0, tmp_path=tmp_path)
    ex._portfolio.position    = 0.01
    ex._portfolio._cost_basis = 80_000.0
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(78_000.0)
    assert ex.has_resting_stop

    mock_ex.cancel_order.side_effect = ccxt.InvalidOrder("order already filled")
    mock_ex.fetch_order.return_value = {
        "id": "stop-001", "status": "closed", "filled": 0.01,
        "average": 78_000.0, "fee": {"cost": 1.5, "currency": "CAD"},
    }

    outcome, fill_order = ex._cancel_native_stop()

    assert outcome == "filled"
    assert fill_order is not None
    assert fill_order.status == OrderStatus.FILLED
    assert abs(fill_order.quantity - 0.01) < 1e-9
    assert fill_order.price == 78_000.0
    assert ex._portfolio.position == 0.0              # position closed
    assert abs(ex._portfolio.realized_pnl - (-20.0)) < 1e-6   # (78000-80000)*0.01
    assert not ex.has_resting_stop                     # stop is genuinely gone
    assert fill_order in ex.filled_orders()             # recorded exactly once
    assert ex.filled_orders().count(fill_order) == 1


# ---------------------------------------------------------------------------
# 2026-09-18 FOLLOW-UP review finding (P0): a PARTIAL fill on a STILL-OPEN
# native stop was treated as a completed/terminal exit purely because
# filled > 0, ignoring status — clearing the tracked id while the order was
# genuinely still resting on the exchange.
# ---------------------------------------------------------------------------

def test_native_stop_partial_fill_still_open_retains_tracking(tmp_path):
    """Reproduced by the follow-up review: starting position 0.002 BTC,
    cancel times out, fetch_order confirms the stop is STILL OPEN with a
    cumulative fill of 0.001 (half). Must record only the new delta,
    retain the tracked id (still genuinely resting), and return "partial"
    — not "filled"."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=2000.0, tmp_path=tmp_path)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 80_000.0
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(78_000.0)
    assert ex.has_resting_stop

    mock_ex.cancel_order.side_effect = ccxt.NetworkError("cancel timeout")
    mock_ex.fetch_order.return_value = {
        "id": "stop-001", "status": "open", "filled": 0.001,
        "average": 78_000.0, "fee": {"cost": 0.75, "currency": "CAD"},
    }

    outcome, fill_order = ex._cancel_native_stop()

    assert outcome == "partial"
    assert fill_order is not None
    assert abs(fill_order.quantity - 0.001) < 1e-9   # only the delta, not the full position
    assert ex._portfolio.position == pytest.approx(0.001)   # half closed, half remains
    assert ex.has_resting_stop                        # STILL tracked — order is genuinely open
    assert ex._native_stop_order_id == "stop-001"      # id retained, not cleared


def test_native_stop_repeated_poll_of_same_cumulative_fill_does_not_double_record(tmp_path):
    """Polling the SAME cumulative filled amount twice (no new fill between
    checks) must not record a second fill or change the outcome."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=2000.0, tmp_path=tmp_path)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 80_000.0
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(78_000.0)

    mock_ex.cancel_order.side_effect = ccxt.NetworkError("cancel timeout")
    mock_ex.fetch_order.return_value = {
        "id": "stop-001", "status": "open", "filled": 0.001,
        "average": 78_000.0, "fee": {"cost": 0.75, "currency": "CAD"},
    }

    outcome1, fill1 = ex._cancel_native_stop()
    assert outcome1 == "partial" and fill1 is not None
    n_fills_after_first = len(ex.filled_orders())

    # Same cumulative filled amount reported again — nothing NEW happened.
    outcome2, fill2 = ex._cancel_native_stop()

    assert outcome2 == "unknown"   # no new delta, status still non-terminal
    assert fill2 is None
    assert len(ex.filled_orders()) == n_fills_after_first   # no duplicate record
    assert ex._portfolio.position == pytest.approx(0.001)    # unchanged


def test_native_stop_additional_fill_after_partial_records_only_new_delta(tmp_path):
    """A later poll showing MORE cumulative fill must record only the
    incremental delta, not the full new cumulative amount again."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=2000.0, tmp_path=tmp_path)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 80_000.0
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(78_000.0)

    mock_ex.cancel_order.side_effect = ccxt.NetworkError("cancel timeout")
    mock_ex.fetch_order.return_value = {
        "id": "stop-001", "status": "open", "filled": 0.001,
        "average": 78_000.0, "fee": {"cost": 0.75, "currency": "CAD"},
    }
    outcome1, fill1 = ex._cancel_native_stop()
    assert abs(fill1.quantity - 0.001) < 1e-9

    # The remainder fills and the order now reads fully closed.
    mock_ex.fetch_order.return_value = {
        "id": "stop-001", "status": "closed", "filled": 0.002,
        "average": 77_900.0, "fee": {"cost": 0.75, "currency": "CAD"},
    }
    outcome2, fill2 = ex._cancel_native_stop()

    assert outcome2 == "filled"
    assert fill2 is not None
    assert abs(fill2.quantity - 0.001) < 1e-9   # only the NEW delta (0.002-0.001), not 0.002 again
    assert ex._portfolio.position == pytest.approx(0.0)
    assert not ex.has_resting_stop
    assert len(ex.filled_orders()) == 2   # one row per real fill event, not duplicated


def test_native_stop_partial_fills_use_delta_price_and_fee_not_cumulative(tmp_path):
    """2026-09-18 PASS-3 review finding (P1), reproduced exactly: fill 1 is
    0.001 @ average 90000 with cumulative fee $0.36; fill 2's snapshot
    shows cumulative 0.002 @ average 95000 with cumulative fee $0.76. The
    correct total proceeds are $190 (0.002 * true average cost) minus
    $0.76 total fee = $189.24. The old code applied fill 2's CUMULATIVE
    average ($95,000, not its own true price of $100,000) to only its
    delta quantity, and re-charged the FULL cumulative fee on top of fill
    1's already-deducted fee — producing $183.88 instead."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=1000.0, tmp_path=tmp_path)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 80_000.0
    starting_cash = ex._portfolio.cash
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(78_000.0)

    mock_ex.cancel_order.side_effect = ccxt.NetworkError("cancel timeout")
    # Fill 1: cumulative 0.001 @ average 90000 (cost $90), fee $0.36.
    mock_ex.fetch_order.return_value = {
        "id": "stop-001", "status": "open", "filled": 0.001,
        "average": 90_000.0, "cost": 90.0,
        "fee": {"cost": 0.36, "currency": "CAD"},
    }
    outcome1, fill1 = ex._cancel_native_stop()
    assert outcome1 == "partial"
    assert fill1.price == 90_000.0
    assert fill1.fee_cost == pytest.approx(0.36)

    # Fill 2 snapshot: cumulative 0.002 @ average 95000 (cost $190), fee
    # $0.76 cumulative. Fill 2's OWN price is therefore $100,000
    # ((190-90)/0.001) and its OWN fee is $0.40 (0.76-0.36).
    mock_ex.fetch_order.return_value = {
        "id": "stop-001", "status": "closed", "filled": 0.002,
        "average": 95_000.0, "cost": 190.0,
        "fee": {"cost": 0.76, "currency": "CAD"},
    }
    outcome2, fill2 = ex._cancel_native_stop()

    assert outcome2 == "filled"
    assert fill2.price == pytest.approx(100_000.0)
    assert fill2.fee_cost == pytest.approx(0.40)
    # Total cash increase across both fills: (90 - 0.36) + (100 - 0.40) = 189.24
    assert (ex._portfolio.cash - starting_cash) == pytest.approx(189.24)
    assert ex._fees_paid == pytest.approx(0.76)   # total fee, not 1.12 (double-counted)


def test_sync_protective_stop_does_not_replace_when_partial_fill_still_open(tmp_path):
    """2026-09-18 follow-up review finding: sync_protective_stop must not
    place a REPLACEMENT stop when the existing one is confirmed still
    resting (a "partial" outcome) — that would duplicate a live order."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=2000.0, tmp_path=tmp_path)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 80_000.0
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(78_000.0)
    mock_ex.reset_mock()

    mock_ex.cancel_order.side_effect = ccxt.NetworkError("cancel timeout")
    mock_ex.fetch_order.return_value = {
        "id": "stop-001", "status": "open", "filled": 0.001,
        "average": 78_000.0, "fee": {"cost": 0.75, "currency": "CAD"},
    }

    ex.sync_protective_stop(78_000.0)

    mock_ex.create_order.assert_not_called()   # no replacement placed
    assert ex._native_stop_order_id == "stop-001"   # original still tracked


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


# ---------------------------------------------------------------------------
# 2026-09-18 PASS-3 review finding (P0): native protective-stop placement
# bypassed persisted submission tracking entirely — a response-lost timeout
# on two sync calls placed TWO real stops, with pending_submissions left
# empty either way (no recovery record at all).
# ---------------------------------------------------------------------------

def test_native_stop_response_lost_timeout_across_two_sync_calls_places_one(tmp_path):
    """Reproduced exactly: position 0.002 BTC, both sync_protective_stop(89000)
    calls hit a create_order timeout with reconciliation unavailable. Must
    place AT MOST ONE stop, not two."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.002

    mock_ex.create_order.side_effect = ccxt.RequestTimeout("response lost")
    mock_ex.fetch_open_orders.side_effect = ccxt.NetworkError("still unreachable")

    with patch.object(ex._alerter, "error"):
        ex.sync_protective_stop(89_000.0)
        ex.sync_protective_stop(89_000.0)

    assert mock_ex.create_order.call_count == 1   # NOT 2
    assert "protect" in ex.pending_submissions
    assert not ex.has_resting_stop   # genuinely unresolved — not fabricated as tracked


def test_native_stop_response_lost_timeout_recovers_on_reconciliation(tmp_path):
    """Once the exchange becomes reachable again, sync_protective_stop must
    resolve the STALE pending submission (adopt the stop that actually
    landed) rather than placing a second one on top."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.002

    mock_ex.create_order.side_effect = ccxt.RequestTimeout("response lost")
    mock_ex.fetch_open_orders.side_effect = ccxt.NetworkError("still unreachable")
    with patch.object(ex._alerter, "error"):
        ex.sync_protective_stop(89_000.0)
    _cid = ex.pending_submissions["protect"]["client_order_id"]

    landed_stop = {
        "id": "stop-landed", "clientOrderId": _cid,
        "info": {"descr": {"ordertype": "stop-loss"}},
    }
    mock_ex.fetch_open_orders.side_effect = None
    mock_ex.fetch_open_orders.return_value = [landed_stop]

    ex.sync_protective_stop(89_000.0)

    assert mock_ex.create_order.call_count == 1   # never placed a second stop
    assert "protect" not in ex.pending_submissions
    assert ex._native_stop_order_id == "stop-landed"


def test_native_trailing_stop_response_lost_timeout_places_one(tmp_path):
    """Same fix, trailing-stop variant."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    ex._portfolio.position = 0.002

    mock_ex.create_order.side_effect = ccxt.RequestTimeout("response lost")
    mock_ex.fetch_open_orders.side_effect = ccxt.NetworkError("still unreachable")

    with patch.object(ex._alerter, "error"):
        ex.sync_protective_stop(None, trailing_pct=0.02)
        ex.sync_protective_stop(None, trailing_pct=0.02)

    assert mock_ex.create_order.call_count == 1
    assert "protect" in ex.pending_submissions


def test_protective_stop_pending_does_not_block_ordinary_buy_sell(tmp_path):
    """A protective-stop submission stuck pending must never block an
    ordinary BUY/SELL — role-keyed slots keep them fully independent."""
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=1000.0, tmp_path=tmp_path)
    ex._portfolio.position = 0.002

    mock_ex.create_order.side_effect = ccxt.RequestTimeout("response lost")
    mock_ex.fetch_open_orders.side_effect = ccxt.NetworkError("still unreachable")
    with patch.object(ex._alerter, "error"):
        ex.sync_protective_stop(89_000.0)
    assert "protect" in ex.pending_submissions

    # Now an ordinary BUY, on the same executor — must not be blocked by
    # the still-pending protective-stop submission.
    mock_ex.create_order.side_effect = None
    mock_ex.create_order.return_value = {
        "id": "buy-1", "status": "closed", "filled": 0.001,
        "average": 90_000.0, "fee": {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.fetch_order.return_value = mock_ex.create_order.return_value
    with patch("bot.execution.live_executor.cfg") as mock_cfg, patch("time.sleep"):
        mock_cfg.exchange.limit_order_enabled = False
        order = ex.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is not None and order.status == OrderStatus.FILLED


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
    # Realistic outcome for "closed while the bot was down": the stop
    # itself is what closed it — confirmed terminal (closed) with the
    # full saved position filled, not an unconfigured mock default.
    mock_ex.fetch_order.return_value = {
        "id": "stop-001", "status": "closed", "filled": 0.001,
        "average": 88_000.0, "fee": {"cost": 0.0, "currency": "CAD"},
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


def test_native_stop_startup_detects_multiple_stop_orders_and_alerts(tmp_path):
    """Two stop-type orders (raw Kraken descr.ordertype in {stop-loss,
    trailing-stop}) found resting simultaneously — this bot's own cancel-
    then-place logic should never produce this, so it must not be silently
    resolved by picking one. Alerts loudly; existing tracked-id confirm
    logic still runs unchanged underneath (the tracked id here IS still
    open, so has_resting_stop stays True either way — the alert is the
    thing under test, not a behavior change to the confirm path)."""
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
    mock_ex.fetch_open_orders.return_value = [
        {"id": "stop-001", "info": {"descr": {"ordertype": "stop-loss"}}},
        {"id": "stop-002", "info": {"descr": {"ordertype": "trailing-stop"}}},
    ]

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=100.0, dry_run=False, state_path=state_path,
            native_stop_loss_enabled=True,
        )
        with patch.object(ex._alerter, "error") as mock_alert:
            ex._verify_resting_stop_on_startup()

    mock_alert.assert_called_once()
    assert "NATIVE STOP AMBIGUOUS" in mock_alert.call_args[0][0]
    assert "stop-001" in mock_alert.call_args[0][0]
    assert "stop-002" in mock_alert.call_args[0][0]
    # Existing tracked-id logic untouched — our own id is among the two
    # found and still open, so it's still correctly confirmed as resting.
    assert ex.has_resting_stop
    mock_ex.create_order.assert_not_called()   # no auto-adoption, no third order placed


def test_native_stop_startup_ignores_unrelated_open_orders(tmp_path):
    """A resting native stop PLUS an unrelated open order (e.g. a stray
    limit-chase BUY interrupted by the same crash) must not be misread as
    'multiple stop orders' — only orders with a stop-type descr.ordertype
    count. Negative case for the ambiguity check above."""
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
    mock_ex.fetch_open_orders.return_value = [
        {"id": "stop-001", "info": {"descr": {"ordertype": "stop-loss"}}},
        {"id": "buy-999", "info": {"descr": {"ordertype": "limit"}}},
    ]

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=100.0, dry_run=False, state_path=state_path,
            native_stop_loss_enabled=True,
        )
        with patch.object(ex._alerter, "error") as mock_alert:
            ex._verify_resting_stop_on_startup()

    mock_alert.assert_not_called()
    assert ex.has_resting_stop


def test_native_stop_price_property_reflects_private_field(tmp_path):
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True, tmp_path=tmp_path)
    assert ex.native_stop_price is None
    ex._native_stop_price = 88_000.0
    assert ex.native_stop_price == 88_000.0


# ---------------------------------------------------------------------------
# Gap A — resting stop quantity mismatch after fills-while-down
# (2026-08-20 follow-up pass; see .memory/execution_layer.md)
# ---------------------------------------------------------------------------

def _startup_with_tracked_stop(tmp_path, *, resting_order: dict, position: float = 0.001):
    """Shared setup: a saved state with a tracked static stop id, restarted
    against a mocked exchange whose fetch_open_orders returns exactly one
    matching order (the caller supplies its shape — id/amount/remaining/
    info)."""
    state_path = str(tmp_path / "state.json")
    json.dump({
        "symbol": "BTC/CAD", "cash": 89.88, "position": position,
        "cost_basis": 88_870.0, "realized_pnl": 0.0, "fees_paid": 0.0,
        "native_stop_order_id": "stop-001", "native_stop_price": 88_000.0,
        "native_stop_is_trailing": False,
        "saved_at": "2026-08-01T00:00:00+00:00",
    }, open(state_path, "w"))

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.return_value = {
        "free": {"CAD": 89.88, "BTC": position}, "total": {"CAD": 89.88, "BTC": position},
    }
    mock_ex.fetch_open_orders.return_value = [resting_order]
    mock_ex.price_to_precision.return_value = "87000.0"
    # Only exercised by tests that reach the actual cancel (the under-sized
    # resize path) — confirms a clean cancel with nothing filled so the
    # resize proceeds exactly as before this method started verifying.
    mock_ex.fetch_order.return_value = {"id": "stop-001", "status": "canceled", "filled": 0.0}

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=100.0, dry_run=False, state_path=state_path,
            native_stop_loss_enabled=True,
        )
    return ex, mock_ex


def test_native_stop_startup_resizes_under_sized_static_stop(tmp_path):
    """Resting stop covers less than the actual position (a Kraken-side
    partial fill happened while the bot was down) — always alerts, and
    since this is the under-protected direction, cancels + re-places a
    fresh static stop at the SAME price, sized to the real position."""
    resting_order = {
        "id": "stop-001", "amount": 0.0005, "remaining": 0.0005,
        "info": {"descr": {"ordertype": "stop-loss", "price": "87000.0"},
                  "stopprice": "87000.0"},
    }
    ex, mock_ex = _startup_with_tracked_stop(tmp_path, resting_order=resting_order, position=0.001)

    mock_ex.cancel_order.assert_called_once_with("stop-001", "BTC/CAD")
    mock_ex.create_order.assert_called_once()
    call = mock_ex.create_order.call_args
    assert abs(call[0][3] - 0.001) < 1e-9        # resized to real position
    assert call[1]["params"]["stopLossPrice"] == "87000.0"   # same price


def test_native_stop_startup_leaves_over_sized_static_stop(tmp_path):
    """Resting stop covers MORE than the actual position — benign (Kraken
    can't oversell), left untouched. No cancel, no re-place."""
    resting_order = {
        "id": "stop-001", "amount": 0.002, "remaining": 0.002,
        "info": {"descr": {"ordertype": "stop-loss", "price": "87000.0"},
                  "stopprice": "87000.0"},
    }
    ex, mock_ex = _startup_with_tracked_stop(tmp_path, resting_order=resting_order, position=0.001)

    mock_ex.cancel_order.assert_not_called()
    mock_ex.create_order.assert_not_called()
    assert ex._native_stop_order_id == "stop-001"   # unchanged


def test_native_stop_startup_no_alert_when_quantity_matches(tmp_path):
    """Resting stop quantity matches the position within tolerance —
    no alert, nothing touched."""
    resting_order = {
        "id": "stop-001", "amount": 0.001, "remaining": 0.001,
        "info": {"descr": {"ordertype": "stop-loss", "price": "87000.0"},
                  "stopprice": "87000.0"},
    }
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
    mock_ex.fetch_open_orders.return_value = [resting_order]

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        with patch.object(TelegramAlerter, "error") as mock_alert:
            ex = LiveExecutor(
                exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
                starting_cash=100.0, dry_run=False, state_path=state_path,
                native_stop_loss_enabled=True,
            )

    mock_alert.assert_not_called()
    mock_ex.cancel_order.assert_not_called()
    mock_ex.create_order.assert_not_called()


def test_native_stop_startup_qty_check_skips_when_amount_unknown(tmp_path):
    """A resting order with no amount/remaining field at all (e.g. a bare
    test double, or a genuinely malformed response) — the quantity check
    must degrade to a no-op, not a false-positive alert or a crash."""
    resting_order = {"id": "stop-001", "info": {"descr": {"ordertype": "stop-loss"}}}
    ex, mock_ex = _startup_with_tracked_stop(tmp_path, resting_order=resting_order, position=0.001)

    mock_ex.cancel_order.assert_not_called()
    mock_ex.create_order.assert_not_called()


def test_native_stop_startup_resizes_under_sized_trailing_stop(tmp_path):
    """Same under-sized case, but the resting order is a native TRAILING
    stop — the replacement must also be trailing, at the SAME percent read
    back from the resting order's own descr.price field (there is no
    numeric trailing_pct stored anywhere in memory to fall back on)."""
    state_path = str(tmp_path / "state.json")
    json.dump({
        "symbol": "BTC/CAD", "cash": 89.88, "position": 0.001,
        "cost_basis": 88_870.0, "realized_pnl": 0.0, "fees_paid": 0.0,
        "native_stop_order_id": "trail-001", "native_stop_price": None,
        "native_stop_is_trailing": True,
        "saved_at": "2026-08-01T00:00:00+00:00",
    }, open(state_path, "w"))
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.return_value = {
        "free": {"CAD": 89.88, "BTC": 0.001}, "total": {"CAD": 89.88, "BTC": 0.001},
    }
    mock_ex.fetch_open_orders.return_value = [{
        "id": "trail-001", "amount": 0.0005, "remaining": 0.0005,
        "info": {"descr": {"ordertype": "trailing-stop", "price": "+2.5000%"}},
    }]
    mock_ex.fetch_order.return_value = {"id": "trail-001", "status": "canceled", "filled": 0.0}

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=100.0, dry_run=False, state_path=state_path,
            native_stop_loss_enabled=True,
        )

    mock_ex.cancel_order.assert_called_once_with("trail-001", "BTC/CAD")
    mock_ex.create_order.assert_called_once()
    call = mock_ex.create_order.call_args
    assert abs(call[0][3] - 0.001) < 1e-9
    assert call[1]["params"]["trailingPercent"] == "2.5000"
    assert ex.native_stop_is_trailing


# ---------------------------------------------------------------------------
# Gap B — untracked-but-real resting order not adopted
# (2026-08-20 follow-up pass; see .memory/execution_layer.md)
# ---------------------------------------------------------------------------

def test_native_stop_startup_adopts_untracked_single_static_stop(tmp_path):
    """State file's tracked id is missing entirely (lost to a crash before
    save), but a real static stop order is actually resting — adopted
    verbatim instead of main.py placing a duplicate."""
    state_path = str(tmp_path / "state.json")
    json.dump({
        "symbol": "BTC/CAD", "cash": 89.88, "position": 0.001,
        "cost_basis": 88_870.0, "realized_pnl": 0.0, "fees_paid": 0.0,
        "saved_at": "2026-08-01T00:00:00+00:00",
    }, open(state_path, "w"))
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.return_value = {
        "free": {"CAD": 89.88, "BTC": 0.001}, "total": {"CAD": 89.88, "BTC": 0.001},
    }
    mock_ex.fetch_open_orders.return_value = [{
        "id": "orphan-001",
        "info": {"descr": {"ordertype": "stop-loss", "price": "87500.0"},
                  "stopprice": "87500.0"},
    }]

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        with patch.object(TelegramAlerter, "message") as mock_msg:
            ex = LiveExecutor(
                exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
                starting_cash=100.0, dry_run=False, state_path=state_path,
                native_stop_loss_enabled=True,
            )

    assert ex.has_resting_stop
    assert ex._native_stop_order_id == "orphan-001"
    assert ex._native_stop_price == 87_500.0
    assert not ex.native_stop_is_trailing
    mock_ex.create_order.assert_not_called()   # adopted, not duplicated
    mock_msg.assert_called_once()
    assert "ADOPTED" in mock_msg.call_args[0][0]


def test_native_stop_startup_adopts_untracked_trailing_stop(tmp_path):
    """Same adoption path, but the untracked resting order is a trailing
    stop — native_stop_is_trailing must be set True and native_stop_price
    stays None (no fixed price for a trailing order, same convention as a
    freshly-placed one)."""
    state_path = str(tmp_path / "state.json")
    json.dump({
        "symbol": "BTC/CAD", "cash": 89.88, "position": 0.001,
        "cost_basis": 88_870.0, "realized_pnl": 0.0, "fees_paid": 0.0,
        "saved_at": "2026-08-01T00:00:00+00:00",
    }, open(state_path, "w"))
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.return_value = {
        "free": {"CAD": 89.88, "BTC": 0.001}, "total": {"CAD": 89.88, "BTC": 0.001},
    }
    mock_ex.fetch_open_orders.return_value = [{
        "id": "orphan-002",
        "info": {"descr": {"ordertype": "trailing-stop", "price": "+2.0000%"}},
    }]

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=100.0, dry_run=False, state_path=state_path,
            native_stop_loss_enabled=True,
        )

    assert ex.has_resting_stop
    assert ex._native_stop_order_id == "orphan-002"
    assert ex.native_stop_price is None
    assert ex.native_stop_is_trailing
    mock_ex.create_order.assert_not_called()


def test_native_stop_startup_multiple_untracked_stops_not_adopted(tmp_path):
    """No tracked id AND 2+ real stop-type orders resting — the same
    ambiguous situation as the tracked-id case, must alert and adopt
    nothing (never guess which one is 'ours')."""
    state_path = str(tmp_path / "state.json")
    json.dump({
        "symbol": "BTC/CAD", "cash": 89.88, "position": 0.001,
        "cost_basis": 88_870.0, "realized_pnl": 0.0, "fees_paid": 0.0,
        "saved_at": "2026-08-01T00:00:00+00:00",
    }, open(state_path, "w"))
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.return_value = {
        "free": {"CAD": 89.88, "BTC": 0.001}, "total": {"CAD": 89.88, "BTC": 0.001},
    }
    mock_ex.fetch_open_orders.return_value = [
        {"id": "orphan-001", "info": {"descr": {"ordertype": "stop-loss"}}},
        {"id": "orphan-002", "info": {"descr": {"ordertype": "trailing-stop"}}},
    ]

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        with patch.object(TelegramAlerter, "error") as mock_alert:
            ex = LiveExecutor(
                exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
                starting_cash=100.0, dry_run=False, state_path=state_path,
                native_stop_loss_enabled=True,
            )

    assert not ex.has_resting_stop   # nothing adopted
    mock_ex.create_order.assert_not_called()
    mock_alert.assert_called_once()
    assert "NATIVE STOP AMBIGUOUS" in mock_alert.call_args[0][0]


def test_native_stop_startup_no_untracked_stops_unchanged_gap_behavior(tmp_path):
    """No tracked id and nothing resting on the exchange either — unchanged
    pre-existing behavior: logged as a gap, nothing adopted, no ambiguity
    alert (there's nothing ambiguous about zero orders)."""
    state_path = str(tmp_path / "state.json")
    json.dump({
        "symbol": "BTC/CAD", "cash": 89.88, "position": 0.001,
        "cost_basis": 88_870.0, "realized_pnl": 0.0, "fees_paid": 0.0,
        "saved_at": "2026-08-01T00:00:00+00:00",
    }, open(state_path, "w"))
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.return_value = {
        "free": {"CAD": 89.88, "BTC": 0.001}, "total": {"CAD": 89.88, "BTC": 0.001},
    }
    mock_ex.fetch_open_orders.return_value = []

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        with patch.object(TelegramAlerter, "error") as mock_alert, \
             patch.object(TelegramAlerter, "message") as mock_msg:
            ex = LiveExecutor(
                exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
                starting_cash=100.0, dry_run=False, state_path=state_path,
                native_stop_loss_enabled=True,
            )

    assert not ex.has_resting_stop
    mock_ex.create_order.assert_not_called()
    mock_alert.assert_not_called()
    mock_msg.assert_not_called()


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
    mock_ex.fetch_order.return_value = {"id": "trail-001", "status": "canceled", "filled": 0.0}
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
    mock_ex.fetch_order.return_value = {"id": "trail-001", "status": "canceled", "filled": 0.0}
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


# ---------------------------------------------------------------------------
# Native stop pre-cancel on SELL (SOL/CAD incident 2026-08-27): a resting
# native stop reserves 100% of the base asset, so every SELL for the position
# failed "Insufficient funds" until it was cancelled — and the cancel only ran
# AFTER a successful fill, which could never happen. execute() now cancels the
# stop BEFORE the sell, and re-arms it if the sell is rejected.
# ---------------------------------------------------------------------------

# BTC-realistic numbers (the _make harness hard-codes symbol=BTC/CAD, whose
# test market has a $5 min cost) — the native-stop logic itself is symbol-agnostic.
def _seed_position_and_stop(ex, mock_ex, *, qty=0.01, entry=80_000.0, stop=78_000.0):
    ex._portfolio.position    = qty
    ex._portfolio._cost_basis = entry
    mock_ex.price_to_precision.return_value = str(stop)
    mock_ex.create_order.return_value = {"id": "stop-001"}
    ex.sync_protective_stop(stop)
    assert ex._native_stop_order_id == "stop-001"


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_sell_cancels_resting_native_stop_before_placing_order(mock_cfg, _s, tmp_path):
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=2000.0, tmp_path=tmp_path)
    _seed_position_and_stop(ex, mock_ex)
    mock_ex.reset_mock()   # drop the setup's place-stop create_order call

    sell_raw = {"id": "sell-001", "status": "closed", "filled": 0.01,
                "average": 88_000.0, "type": "market", "fee": {"cost": 3.5, "currency": "CAD"}}
    mock_ex.create_order.return_value = sell_raw

    def _fetch_order_side_effect(order_id, symbol):
        if order_id == "stop-001":
            # Cancellation of the native stop is confirmed clean — nothing
            # filled — so execute() proceeds to place its own SELL below.
            return {"id": "stop-001", "status": "canceled", "filled": 0.0}
        return sell_raw
    mock_ex.fetch_order.side_effect = _fetch_order_side_effect

    order = ex.execute(Signal.SELL, 88_000.0, 0.01, urgent=True)

    assert order is not None and order.status == OrderStatus.FILLED
    assert order.order_id == "sell-001"       # the bot's own SELL, not the stop
    # The resting stop was cancelled...
    mock_ex.cancel_order.assert_called_once_with("stop-001", "BTC/CAD")
    # ...BEFORE the sell order was placed.
    kinds = [c[0] for c in mock_ex.method_calls if c[0] in ("cancel_order", "create_order")]
    assert kinds[0] == "cancel_order" and "create_order" in kinds[1:]
    assert ex._native_stop_order_id is None   # full close — stays gone


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_rejected_sell_rearms_the_native_stop(mock_cfg, _s, tmp_path):
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=2000.0, tmp_path=tmp_path)
    _seed_position_and_stop(ex, mock_ex, stop=78_000.0)

    # Native stop cancellation confirms clean (nothing filled) before the
    # SELL attempt itself is rejected.
    mock_ex.fetch_order.return_value = {"id": "stop-001", "status": "canceled", "filled": 0.0}

    # SELL create_order raises; the follow-up _place_native_stop create_order succeeds.
    mock_ex.create_order.side_effect = [
        ccxt.InsufficientFunds("kraken EOrder:Insufficient funds"),
        {"id": "stop-restored"},
    ]

    order = ex.execute(Signal.SELL, 88_000.0, 0.01, urgent=True)

    assert order is not None and order.status == OrderStatus.REJECTED
    assert ex._native_stop_order_id == "stop-restored"   # put back
    assert ex._native_stop_price == 78_000.0             # at the prior level
    assert ex._portfolio.position == 0.01                # still held


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_sell_aborted_when_native_stop_cancellation_unconfirmed(mock_cfg, _s, tmp_path):
    """2026-09-18 review finding: if the pre-SELL cancel of the native stop
    can't be confirmed, execute() must refuse to place the SELL at all —
    not fall back to a plain market order, which could double-sell against
    a stop that's still resting, or against one that already filled."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=2000.0, tmp_path=tmp_path)
    _seed_position_and_stop(ex, mock_ex, stop=78_000.0)
    mock_ex.reset_mock()   # drop the setup's place-stop create_order call

    mock_ex.cancel_order.side_effect = ccxt.NetworkError("timeout")
    mock_ex.fetch_order.side_effect = ccxt.NetworkError("timeout")

    with patch.object(ex._alerter, "error") as mock_alert:
        order = ex.execute(Signal.SELL, 88_000.0, 0.01, urgent=True)

    assert order is None                       # no order placed, no phantom row
    mock_ex.create_order.assert_not_called()    # never attempted a SELL
    assert ex._native_stop_order_id == "stop-001"   # tracked protection untouched
    assert ex._portfolio.position == 0.01
    assert mock_alert.call_args[0][0].startswith("SELL HELD BACK")


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_sell_uses_stop_own_fill_when_it_wins_the_cancel_race(mock_cfg, _s, tmp_path):
    """2026-09-18 review finding: the native stop fires during the cancel
    attempt itself (wins the race). execute() must not also place its own
    SELL on top — the stop's fill IS the exit, recorded exactly once."""
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=2000.0, tmp_path=tmp_path)
    _seed_position_and_stop(ex, mock_ex, stop=78_000.0)
    mock_ex.reset_mock()   # drop the setup's place-stop create_order call

    mock_ex.cancel_order.side_effect = ccxt.InvalidOrder("order already filled")
    mock_ex.fetch_order.return_value = {
        "id": "stop-001", "status": "closed", "filled": 0.01,
        "average": 77_500.0, "fee": {"cost": 1.2, "currency": "CAD"},
    }

    order = ex.execute(Signal.SELL, 88_000.0, 0.01, urgent=True)

    assert order is not None and order.status == OrderStatus.FILLED
    assert order.order_id == "native-stop:stop-001"
    assert order.price == 77_500.0                 # the stop's own fill price
    mock_ex.create_order.assert_not_called()        # no second SELL placed
    assert ex._portfolio.position == 0.0
    assert not ex.has_resting_stop
    assert ex.filled_orders().count(order) == 1


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_sell_with_no_resting_stop_does_not_cancel(mock_cfg, _s, tmp_path):
    _limit_cfg(mock_cfg, enabled=True, timeout_s=30)
    ex, mock_ex = _make(dry_run=False, native_stop_loss_enabled=True,
                        starting_cash=2000.0, tmp_path=tmp_path)
    ex._portfolio.position    = 0.01
    ex._portfolio._cost_basis = 80_000.0
    assert ex._native_stop_order_id is None

    sell_raw = {"id": "sell-001", "status": "closed", "filled": 0.01,
                "average": 88_000.0, "type": "market", "fee": {"cost": 3.5, "currency": "CAD"}}
    mock_ex.create_order.return_value = sell_raw
    mock_ex.fetch_order.return_value  = sell_raw

    order = ex.execute(Signal.SELL, 88_000.0, 0.01, urgent=True)
    assert order.status == OrderStatus.FILLED
    mock_ex.cancel_order.assert_not_called()


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
