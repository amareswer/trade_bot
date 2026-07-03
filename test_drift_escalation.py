"""
Unit tests for BUG 2 — fill-confirmation drift (consecutive escalation logic).

Tests cover:
  (a) Drift below threshold fires only logger.warning, not alerter.error
  (b) Drift at/above threshold fires alerter.error exactly once, then resets
  (c) Drift resolves → counter resets → next N detections trigger a fresh escalation
  (d) Post-fill position convergence: after a SELL fill with correct qty, executor.position == 0

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


# ---------------------------------------------------------------------------
# Helper: simulate the drift-check logic extracted from bot/main.py
#
# The logic is inline in main.py but we mirror it here as a pure function
# so we can unit-test it without starting the full bot loop.
# ---------------------------------------------------------------------------

def _run_drift_check(
    exchange_pos: float,
    bot_pos: float,
    base: str,
    drift_count: int,
    drift_threshold: int,
    alerter,
    logger_mock,
) -> int:
    """
    One iteration of the drift-check block from bot/main.py.
    Returns the updated _drift_consecutive_count.
    """
    _DRIFT_MIN = 0.000010
    drift = abs(exchange_pos - bot_pos)
    if drift > _DRIFT_MIN:
        drift_count += 1
        logger_mock.warning(
            "POSITION DRIFT [%d/%d]: exchange=%.6f bot=%.6f drift=%.6f %s",
            drift_count, drift_threshold, exchange_pos, bot_pos, drift, base,
        )
        if drift_count >= drift_threshold:
            alerter.error(
                f"PERSISTENT position drift after"
                f" {drift_count} consecutive checks:"
                f" exchange={exchange_pos:.6f}"
                f" bot={bot_pos:.6f} {base}"
                f" — check logs/live_state.json"
            )
            drift_count = 0
    else:
        if drift_count > 0:
            logger_mock.info(
                "Position drift resolved: exchange=%.6f bot=%.6f %s",
                exchange_pos, bot_pos, base,
            )
        drift_count = 0
    return drift_count


# ── (a) Drift below threshold — warning only, no alert ───────────────────────

def test_drift_below_threshold_no_alert():
    """
    With threshold=3, first 2 consecutive drifts must NOT call alerter.error.
    """
    alerter = MagicMock()
    log     = MagicMock()
    count   = 0
    for _ in range(2):
        count = _run_drift_check(
            exchange_pos=0.0, bot_pos=0.000378,
            base="BTC", drift_count=count,
            drift_threshold=3, alerter=alerter, logger_mock=log,
        )
    alerter.error.assert_not_called()
    assert log.warning.call_count == 2
    assert count == 2


# ── (b) At threshold → alerter.error fires once, counter resets ──────────────

def test_drift_at_threshold_escalates_once():
    """
    3rd consecutive drift (threshold=3) → alerter.error called once, count resets to 0.
    """
    alerter = MagicMock()
    log     = MagicMock()
    count   = 0
    for _ in range(3):
        count = _run_drift_check(
            exchange_pos=0.0, bot_pos=0.000378,
            base="BTC", drift_count=count,
            drift_threshold=3, alerter=alerter, logger_mock=log,
        )
    alerter.error.assert_called_once()
    assert "PERSISTENT position drift" in alerter.error.call_args[0][0]
    assert count == 0  # reset after escalation


def test_drift_N_plus_1_does_not_double_alert():
    """
    4th consecutive drift after reset should be back to warning only.
    """
    alerter = MagicMock()
    log     = MagicMock()
    count   = 0
    for _ in range(4):
        count = _run_drift_check(
            exchange_pos=0.0, bot_pos=0.000378,
            base="BTC", drift_count=count,
            drift_threshold=3, alerter=alerter, logger_mock=log,
        )
    # Alert fires at 3 and resets; 4th is count=1 → no second alert
    alerter.error.assert_called_once()


# ── (c) Drift resolves → counter resets ──────────────────────────────────────

def test_drift_resolves_resets_counter():
    """
    Two drifts then resolution: counter goes to 0. Next two drifts again:
    still no alert (< threshold).
    """
    alerter = MagicMock()
    log     = MagicMock()
    count   = 0

    # 2 drifts
    for _ in range(2):
        count = _run_drift_check(
            exchange_pos=0.0, bot_pos=0.000378,
            base="BTC", drift_count=count,
            drift_threshold=3, alerter=alerter, logger_mock=log,
        )
    assert count == 2

    # Drift resolves (exchange matches bot)
    count = _run_drift_check(
        exchange_pos=0.000378, bot_pos=0.000378,
        base="BTC", drift_count=count,
        drift_threshold=3, alerter=alerter, logger_mock=log,
    )
    assert count == 0
    log.info.assert_called()  # "Position drift resolved"

    # 2 more drifts — still below threshold
    for _ in range(2):
        count = _run_drift_check(
            exchange_pos=0.0, bot_pos=0.000378,
            base="BTC", drift_count=count,
            drift_threshold=3, alerter=alerter, logger_mock=log,
        )
    alerter.error.assert_not_called()


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
