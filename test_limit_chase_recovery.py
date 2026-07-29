"""
Regression tests for the 2026-07-15 unrecorded-fill incident (limit-chase path).

Incident: _place_limit_order hit a NetworkError fetching the orderbook, fell
back to a market order (OFIPRK-N6JMC-IRHKMX, filled 0.000084 BTC / $7.73 CAD),
and returned the immediate create_order response (status=None, filled=0).
execute() trusted that dict without polling, and the qty=0 guard classified the
order as "limit" (from ORDER_TYPE config, not the actual market fallback) —
so it refused the amount inference and returned None. Real fill, no record.

Fixes under test:
  1. execute() polls fetch_order on unresolved orders returned by the chase.
  2. qty=0 recovery classifies by last_raw["type"] (actual), not config.
  3. Chase timeout-cancel verifies the order's fate before re-placing —
     a fill racing the cancel is recorded, never doubled.

Run: python -m pytest test_limit_chase_recovery.py -v
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

import bot.execution.live_executor as le_mod
from bot.execution.executor import OrderStatus
from bot.execution.live_executor import LiveExecutor
from bot.strategy.threshold_strategy import Signal


@pytest.fixture(autouse=True)
def _force_chase_path(monkeypatch):
    """Route execute() into the limit-chase regardless of the developer's .env,
    with a zero poll deadline so the chase never wall-clock spins under a
    mocked time.sleep."""
    monkeypatch.setattr(le_mod.cfg.exchange, "limit_order_enabled", True)
    monkeypatch.setattr(le_mod.cfg.exchange, "limit_chase_timeout_s", 0)
    monkeypatch.setattr(le_mod.cfg.exchange, "limit_chase_max_retries", 1)
    monkeypatch.setattr(le_mod.cfg.exchange, "limit_chase_tick_pct", 0.0001)


_DEFAULT_MARKETS = {
    "BTC/CAD": {
        "limits": {
            "amount": {"min": 0.00005},
            "cost":   {"min": 5.0},
        }
    }
}

_QTY   = 0.000084
_PRICE = 92002.90


def _make_live_executor(
    tmp_path,
    position: float = 0.0,
    starting_cash: float = 77.0,
) -> tuple[LiveExecutor, MagicMock]:
    """tmp_path is pytest's built-in per-test fixture (auto-cleaned) — pass
    your test's own tmp_path fixture through."""
    state_path = str(tmp_path / "live_state_BTC_CAD.json")
    with open(state_path, "w") as f:
        json.dump({
            "symbol":       "BTC/CAD",
            "cash":         starting_cash,
            "position":     position,
            "cost_basis":   0.0,
            "realized_pnl": 0.0,
            "fees_paid":    0.0,
            "bot_opened":   position > 0,
            "saved_at":     "2026-07-15T00:00:00+00:00",
        }, f)

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = _DEFAULT_MARKETS
    mock_ex.fetch_balance.return_value = {
        "free":  {"BTC": position, "CAD": starting_cash},
        "total": {"BTC": position, "CAD": starting_cash},
    }
    mock_ex.price_to_precision.side_effect = lambda _s, p: f"{float(p):.2f}"

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
            order_type    = "limit",   # matches live .env at incident time
        )
        exc._exchange = mock_ex
        exc._portfolio.position = position

    return exc, mock_ex


def _closed(order_id: str, filled: float = _QTY, otype: str = "market") -> dict:
    return {
        "id": order_id, "status": "closed", "type": otype,
        "filled": filled, "amount": _QTY,
        "average": _PRICE, "price": _PRICE,
        "fee": {"cost": 0.06, "currency": "CAD"},
    }


def _unresolved(order_id: str, otype: str = "market") -> dict:
    """Kraken's immediate create_order response before the fill propagates."""
    return {
        "id": order_id, "status": None, "type": otype,
        "filled": 0.0, "amount": _QTY,
        "average": None, "price": None,
        "fee": None,
    }


# ── 1. Incident regression: market fallback resolves via polling ─────────────

def test_market_fallback_unresolved_fill_recovered_by_polling(caplog, tmp_path):
    """Orderbook fetch fails → market fallback returns status=None/filled=0.
    execute() must poll fetch_order and record the real fill (the 2026-07-15
    incident returned None here and lost a $7.73 fill)."""
    exc, mock_ex = _make_live_executor(tmp_path)

    mock_ex.fetch_order_book.side_effect = le_mod.ccxt.NetworkError("Depth fetch failed")
    mock_ex.create_order.return_value = _unresolved("OFIPRK-N6JMC-IRHKMX")
    mock_ex.fetch_order.return_value = _closed("OFIPRK-N6JMC-IRHKMX")

    with patch("bot.execution.live_executor.time.sleep"):
        order = exc.execute(Signal.BUY, price=_PRICE, quantity=_QTY)

    assert order is not None, "filled market fallback must be recorded, not dropped"
    assert order.status == OrderStatus.FILLED
    assert order.quantity == pytest.approx(_QTY)
    assert order.price == pytest.approx(_PRICE)
    assert exc.position == pytest.approx(_QTY)
    assert "qty=0 GUARD" not in caplog.text


# ── 2. Actual order type drives the amount inference ─────────────────────────

def test_market_fallback_closed_filled_zero_infers_from_amount(caplog, tmp_path):
    """Fallback market order closes but every poll reports filled=0 (timing
    artifact). ORDER_TYPE=limit is configured, but the ACTUAL order was market
    — the amount inference must fire instead of the limit-order guard."""
    exc, mock_ex = _make_live_executor(tmp_path)

    mock_ex.fetch_order_book.side_effect = le_mod.ccxt.NetworkError("Depth fetch failed")
    mock_ex.create_order.return_value = _unresolved("ORD_MF")
    mock_ex.fetch_order.return_value = _closed("ORD_MF", filled=0.0)

    import logging
    with patch("bot.execution.live_executor.time.sleep"), \
         caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        order = exc.execute(Signal.BUY, price=_PRICE, quantity=_QTY)

    assert order is not None, "closed market fallback must infer qty from amount"
    assert order.quantity == pytest.approx(_QTY)
    assert "inferring fill qty from amount" in caplog.text


# ── 3. Genuinely unfilled fallback still returns None ────────────────────────

def test_market_fallback_never_fills_returns_none(caplog, tmp_path):
    """Fallback order stays unresolved through every poll — no phantom row."""
    exc, mock_ex = _make_live_executor(tmp_path)

    mock_ex.fetch_order_book.side_effect = le_mod.ccxt.NetworkError("Depth fetch failed")
    mock_ex.create_order.return_value = _unresolved("ORD_NF")
    mock_ex.fetch_order.return_value = _unresolved("ORD_NF")

    import logging
    with patch("bot.execution.live_executor.time.sleep"), \
         caplog.at_level(logging.ERROR, logger="bot.execution.live_executor"):
        order = exc.execute(Signal.BUY, price=_PRICE, quantity=_QTY)

    assert order is None
    assert "qty=0 GUARD" in caplog.text
    assert exc.position == pytest.approx(0.0)


# ── 4. Cancel race: fill during cancel is recorded, never re-placed ──────────

def test_chase_timeout_fill_during_cancel_race_recorded_not_replaced(tmp_path):
    """Chase order times out, cancel raises because the order just filled.
    The fill must be recorded and NO second order placed (double-fill guard)."""
    exc, mock_ex = _make_live_executor(tmp_path)

    mock_ex.fetch_order_book.return_value = {
        "bids": [[_PRICE - 10.0, 1.0]],
        "asks": [[_PRICE + 10.0, 1.0]],
    }
    mock_ex.create_order.return_value = {
        "id": "CHASE1", "status": "open", "type": "limit",
        "filled": 0.0, "amount": _QTY, "price": _PRICE,
        "fee": None,
    }
    mock_ex.cancel_order.side_effect = le_mod.ccxt.OrderNotFound("already closed")
    mock_ex.fetch_order.return_value = _closed("CHASE1", otype="limit")

    with patch("bot.execution.live_executor.time.sleep"):
        order = exc.execute(Signal.BUY, price=_PRICE, quantity=_QTY)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.quantity == pytest.approx(_QTY)
    assert mock_ex.create_order.call_count == 1, \
        "a fill racing the cancel must never trigger a second order"


# ── 5. Clean timeout-cancel with no fill still retries the chase ─────────────

def test_chase_timeout_clean_cancel_retries(tmp_path):
    """Cancel succeeds and the post-cancel check shows filled=0 — the chase
    must keep retrying as before (no behavior regression from the race guard)."""
    exc, mock_ex = _make_live_executor(tmp_path)

    mock_ex.fetch_order_book.return_value = {
        "bids": [[_PRICE - 10.0, 1.0]],
        "asks": [[_PRICE + 10.0, 1.0]],
    }
    first = {
        "id": "CHASE1", "status": "open", "type": "limit",
        "filled": 0.0, "amount": _QTY, "price": _PRICE,
        "fee": None,
    }
    second = _closed("CHASE2", otype="limit")
    second["status"] = "closed"
    mock_ex.create_order.side_effect = [first, second]
    mock_ex.cancel_order.return_value = {}
    # post-cancel verification fetch: order cancelled, nothing filled
    mock_ex.fetch_order.return_value = {
        "id": "CHASE1", "status": "canceled", "type": "limit",
        "filled": 0.0, "amount": _QTY, "price": _PRICE,
        "fee": None,
    }

    with patch("bot.execution.live_executor.time.sleep"):
        order = exc.execute(Signal.BUY, price=_PRICE, quantity=_QTY)

    assert mock_ex.create_order.call_count == 2, "clean cancel must re-place"
    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.quantity == pytest.approx(_QTY)


# ── 6. Cancel fails AND unverifiable → abort chase, resolve in execute() ─────

def test_chase_cancel_unverifiable_aborts_without_replacing(caplog, tmp_path):
    """Cancel fails (network) and the verification fetch also fails — the
    chase must NOT re-place (double-fill risk). execute()'s poll loop then
    resolves the order; here it turns out to have filled."""
    exc, mock_ex = _make_live_executor(tmp_path)

    mock_ex.fetch_order_book.return_value = {
        "bids": [[_PRICE - 10.0, 1.0]],
        "asks": [[_PRICE + 10.0, 1.0]],
    }
    mock_ex.create_order.return_value = {
        "id": "CHASE1", "status": "open", "type": "limit",
        "filled": 0.0, "amount": _QTY, "price": _PRICE,
        "fee": None,
    }
    mock_ex.cancel_order.side_effect = le_mod.ccxt.NetworkError("cancel failed")
    # 1st fetch_order = post-cancel verification (fails); rest = execute() polls
    mock_ex.fetch_order.side_effect = [
        le_mod.ccxt.NetworkError("fetch failed"),
        _closed("CHASE1", otype="limit"),
    ]

    with patch("bot.execution.live_executor.time.sleep"):
        order = exc.execute(Signal.BUY, price=_PRICE, quantity=_QTY)

    assert mock_ex.create_order.call_count == 1, \
        "unverifiable order state must never be followed by a re-place"
    assert order is not None
    assert order.quantity == pytest.approx(_QTY)
    assert "aborting chase without re-placing" in caplog.text
