"""
Unit tests for drift reconciliation — consecutive escalation + acknowledgment.

Tests the REAL `_evaluate_drift()` from bot.main (extracted 2026-07-10; the
old tests exercised a hand-mirrored copy of the inline logic).

Covers:
  (a) Drift below threshold fires only logger.warning, not alerter.error
  (b) Drift at/above threshold fires alerter.error exactly once, then resets
  (c) Drift resolves → counter + acknowledgment reset
  (d) Post-fill position convergence: after a SELL fill, executor.position == 0
  (e) Acknowledged (unchanged) drift never re-alerts — external-deposit spam fix
  (f) A CHANGED drift amount re-arms the counter and re-alerts

Run: python -m pytest test_drift_escalation.py -v
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
from bot.main import _evaluate_drift


def _ss() -> dict:
    """Fresh per-symbol state slice, as built in bot.main run()."""
    return {"drift_count": 0, "drift_acked": 0.0}


def _drift(ss, alerter, exchange_pos=0.0, bot_pos=0.000378, threshold=3):
    _evaluate_drift("BTC/CAD", "BTC", exchange_pos, bot_pos, ss, threshold, alerter)


# ── (a) Drift below threshold — warning only, no alert ───────────────────────

def test_drift_below_threshold_no_alert():
    alerter = MagicMock()
    ss = _ss()
    for _ in range(2):
        _drift(ss, alerter)
    alerter.error.assert_not_called()
    assert ss["drift_count"] == 2


# ── (b) At threshold → alerter.error fires once, counter resets ──────────────

def test_drift_at_threshold_escalates_once():
    alerter = MagicMock()
    ss = _ss()
    for _ in range(3):
        _drift(ss, alerter)
    alerter.error.assert_called_once()
    assert "PERSISTENT position drift" in alerter.error.call_args[0][0]
    assert ss["drift_count"] == 0            # reset after escalation
    assert ss["drift_acked"] == pytest.approx(0.000378)


def test_acknowledged_drift_does_not_realert():
    """External-deposit incident (0.000085 BTC, Jul 6-10 2026): the same
    unchanged drift re-alerted every `threshold` checks forever. After
    escalation the amount is acknowledged — 20 more identical checks must
    produce zero additional alerts."""
    alerter = MagicMock()
    ss = _ss()
    for _ in range(3):
        _drift(ss, alerter)                  # escalates once
    for _ in range(20):
        _drift(ss, alerter)                  # same drift, acknowledged
    alerter.error.assert_called_once()
    assert ss["drift_count"] == 0


def test_changed_drift_amount_realerts():
    """If the drift AMOUNT changes (deposit grew / partial withdrawal), the
    acknowledgment no longer matches — counter re-arms and a fresh escalation
    fires at the threshold."""
    alerter = MagicMock()
    ss = _ss()
    for _ in range(3):
        _drift(ss, alerter, bot_pos=0.000378)          # escalate + ack 0.000378
    for _ in range(3):
        _drift(ss, alerter, bot_pos=0.000800)          # different amount
    assert alerter.error.call_count == 2
    assert ss["drift_acked"] == pytest.approx(0.000800)


# ── (c) Drift resolves → counter and acknowledgment reset ────────────────────

def test_drift_resolves_resets_counter():
    alerter = MagicMock()
    ss = _ss()

    for _ in range(2):
        _drift(ss, alerter)
    assert ss["drift_count"] == 2

    # Drift resolves (exchange matches bot)
    _drift(ss, alerter, exchange_pos=0.000378, bot_pos=0.000378)
    assert ss["drift_count"] == 0

    # 2 more drifts — still below threshold
    for _ in range(2):
        _drift(ss, alerter)
    alerter.error.assert_not_called()


def test_resolution_clears_acknowledgment():
    """Ack must clear on resolution so a FUTURE drift of the same size
    (a genuinely new event) alerts again."""
    alerter = MagicMock()
    ss = _ss()
    for _ in range(3):
        _drift(ss, alerter)                                     # escalate + ack
    _drift(ss, alerter, exchange_pos=0.000378, bot_pos=0.000378)  # resolved
    assert ss["drift_acked"] == 0.0
    for _ in range(3):
        _drift(ss, alerter)                                     # same size, new event
    assert alerter.error.call_count == 2


# ── (d) Post-fill convergence: executor position == 0 after SELL fill ─────────

def _make_live_executor_with_position(position: float = 0.000378) -> tuple[LiveExecutor, MagicMock]:
    tmpdir = tempfile.mkdtemp()
    state_path = os.path.join(tmpdir, "live_state_BTC_CAD.json")
    with open(state_path, "w") as f:
        json.dump({
            "symbol": "BTC/CAD", "cash": 100.0, "position": position,
            "cost_basis": 80000.0, "realized_pnl": 0.0,
            "fees_paid": 0.0, "bot_opened": True,
            "saved_at": "2026-06-27T00:00:00+00:00",
        }, f)

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = {
        "BTC/CAD": {"limits": {"amount": {"min": 0.00005}, "cost": {"min": 5.0}}}
    }
    mock_ex.fetch_balance.return_value = {
        "free":  {"BTC": position, "CAD": 100.0},
        "total": {"BTC": position, "CAD": 100.0},
    }

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        exc = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD",
            api_key="key", api_secret="secret",
            starting_cash=100.0, dry_run=False,
            state_path=state_path,
        )
        exc._exchange = mock_ex
        exc._portfolio.position = position

    return exc, mock_ex


def test_post_fill_executor_position_converges():
    """
    After a SELL fills with correct quantity, executor.position must be 0.
    This verifies the BUG 1 + BUG 2 combined fix: correct fill qty leads to
    correct position update, so the drift check fires 0 drift.
    """
    exc, mock_ex = _make_live_executor_with_position(position=0.000378)

    # Exchange returns filled=0.000378 on the closed order (normal happy path)
    mock_ex.create_order.return_value = {
        "id":      "ORDER789",
        "status":  "open",
        "filled":  0.0,
        "amount":  0.000378,
        "average": 85000.0,
        "price":   85000.0,
        "fee":     {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.fetch_order.return_value = {
        "id":      "ORDER789",
        "status":  "closed",
        "filled":  0.000378,  # Exchange correctly reports filled amount
        "amount":  0.000378,
        "average": 85000.0,
        "price":   85000.0,
        "fee":     {"cost": 0.0, "currency": "CAD"},
    }

    with patch("bot.execution.live_executor.time.sleep"):
        order = exc.execute(Signal.SELL, price=85000.0, quantity=0.0)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.quantity == pytest.approx(0.000378)
    assert exc.position == pytest.approx(0.0), (
        f"Position must be 0 after SELL fill, got {exc.position}"
    )


def test_post_fill_fallback_executor_position_converges():
    """
    After a SELL fills with filled=0 (but status=closed, amount=0.000378),
    the fallback recovers the correct qty and executor.position converges to 0.
    """
    exc, mock_ex = _make_live_executor_with_position(position=0.000378)

    mock_ex.create_order.return_value = {
        "id":      "ORDER_FALLBACK",
        "status":  "open",
        "filled":  0.0,
        "amount":  0.000378,
        "average": 85000.0,
        "price":   85000.0,
        "fee":     {"cost": 0.0, "currency": "CAD"},
    }
    mock_ex.fetch_order.return_value = {
        "id":      "ORDER_FALLBACK",
        "status":  "closed",
        "filled":  0.0,       # <- bug scenario: filled=0 on closed
        "amount":  0.000378,  # <- fallback
        "average": 85000.0,
        "price":   85000.0,
        "fee":     {"cost": 0.0, "currency": "CAD"},
    }

    with patch("bot.execution.live_executor.time.sleep"):
        order = exc.execute(Signal.SELL, price=85000.0, quantity=0.0)

    assert order is not None, "fallback must recover the fill"
    assert order.quantity == pytest.approx(0.000378)
    assert exc.position == pytest.approx(0.0), (
        f"Position must converge to 0 after fallback SELL, got {exc.position}"
    )
