"""
Unit tests for the external-holdings guard in LiveExecutor._sync_position().

These tests mock the exchange balance to simulate scenarios where the Kraken
account holds assets the bot did not open.
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from bot.execution.live_executor import LiveExecutor
from bot.strategy.threshold_strategy import Signal


def _make_executor(
    state_position: float = 0.0,
    state_bot_opened: bool = False,
    adopt: bool = False,
) -> tuple[LiveExecutor, str]:
    """
    Build a LiveExecutor with a pre-written state file in a temp directory.
    Returns (executor, state_path).
    """
    tmpdir = tempfile.mkdtemp()
    state_path = os.path.join(tmpdir, "live_state_BTC_CAD.json")

    if state_position > 0 or state_bot_opened:
        with open(state_path, "w") as f:
            json.dump(
                {
                    "symbol":       "BTC/CAD",
                    "cash":         100.0,
                    "position":     state_position,
                    "cost_basis":   90000.0,
                    "realized_pnl": 0.0,
                    "fees_paid":    0.0,
                    "bot_opened":   state_bot_opened,
                    "saved_at":     "2026-06-27T00:00:00+00:00",
                },
                f,
            )

    with (
        patch("ccxt.kraken") as mock_ccxt,
        patch("bot.execution.live_executor.ccxt") as mock_mod,
    ):
        mock_exchange = MagicMock()
        mock_exchange.load_markets.return_value = {}
        mock_ccxt.return_value  = mock_exchange
        mock_mod.kraken.return_value = mock_exchange

        exc = LiveExecutor(
            exchange_id             = "kraken",
            symbol                  = "BTC/CAD",
            api_key                 = "key",
            api_secret              = "secret",
            starting_cash           = 100.0,
            dry_run                 = True,   # skip _sync_cash / _sync_position at init
            state_path              = state_path,
            adopt_external_holdings = adopt,
        )
        exc._exchange = mock_exchange   # replace after init

    return exc, state_path


# ── Test (a): balance > state-file → managed qty = state qty + warning ────────

def test_external_holdings_caps_at_state_qty(caplog):
    """
    Exchange holds 0.000378 BTC, state-file shows 0.000000.
    Managed position must stay at 0. Warning must be logged.
    """
    exc, _ = _make_executor(state_position=0.0, state_bot_opened=False, adopt=False)

    exc._exchange.fetch_balance.return_value = {
        "free":  {"BTC": 0.000378, "CAD": 100.0},
        "total": {"BTC": 0.000378, "CAD": 100.0},
    }
    import logging
    with caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        exc._sync_position("BTC/CAD")

    assert exc.position == pytest.approx(0.0), "managed position must be 0 (state-file value)"
    assert "EXTERNAL HOLDINGS DETECTED" in caplog.text


def test_external_excess_over_existing_state_position(caplog):
    """
    Exchange holds 0.000931 BTC; state-file shows 0.000553.
    Managed qty should be capped at 0.000553.
    """
    exc, _ = _make_executor(state_position=0.000553, state_bot_opened=True, adopt=False)

    exc._exchange.fetch_balance.return_value = {
        "free":  {"BTC": 0.000931, "CAD": 100.0},
        "total": {"BTC": 0.000931, "CAD": 100.0},
    }
    import logging
    with caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        exc._sync_position("BTC/CAD")

    assert exc.position == pytest.approx(0.000553), "managed position capped at state-file value"
    assert "EXTERNAL HOLDINGS DETECTED" in caplog.text


# ── Test (b): state absent + balance present → no position adopted ────────────

def test_no_state_file_and_balance_present_no_adoption(caplog, tmp_path):
    """
    No state file exists. Exchange has BTC. With adopt=False the position
    must remain 0 — the bot should not manage assets it has no record of.
    """
    state_path = str(tmp_path / "live_state_BTC_CAD.json")
    # Do NOT write the state file.

    with (
        patch("ccxt.kraken") as mock_ccxt,
        patch("bot.execution.live_executor.ccxt") as mock_mod,
    ):
        mock_exchange = MagicMock()
        mock_exchange.load_markets.return_value = {}
        mock_ccxt.return_value       = mock_exchange
        mock_mod.kraken.return_value = mock_exchange

        exc = LiveExecutor(
            exchange_id             = "kraken",
            symbol                  = "BTC/CAD",
            api_key                 = "key",
            api_secret              = "secret",
            starting_cash           = 100.0,
            dry_run                 = True,
            state_path              = state_path,
            adopt_external_holdings = False,
        )
        exc._exchange = mock_exchange

    exc._exchange.fetch_balance.return_value = {
        "free":  {"BTC": 0.000378, "CAD": 100.0},
        "total": {"BTC": 0.000378, "CAD": 100.0},
    }
    import logging
    with caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        exc._sync_position("BTC/CAD")

    assert exc.position == pytest.approx(0.0), "no state file → no position adopted"
    assert "EXTERNAL HOLDINGS DETECTED" in caplog.text


# ── Test (c): SELL is sized to managed quantity only ─────────────────────────

def test_sell_uses_managed_qty_not_exchange_total(caplog):
    """
    After external holdings detection (state=0, exchange=0.000378),
    a SELL signal must not attempt to sell the external balance.
    execute(SELL) should raise or return quantity=0 (position is 0).
    """
    exc, _ = _make_executor(state_position=0.0, state_bot_opened=False, adopt=False)

    exc._exchange.fetch_balance.return_value = {
        "free":  {"BTC": 0.000378, "CAD": 100.0},
        "total": {"BTC": 0.000378, "CAD": 100.0},
    }
    import logging
    with caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        exc._sync_position("BTC/CAD")

    assert exc.position == pytest.approx(0.0)

    # Now trigger SELL — executor must reject it (position == 0, nothing to sell).
    # execute(signal, price, quantity) — no cfg param; dry_run=True skips real orders.
    order = exc.execute(Signal.SELL, price=85000.0, quantity=0.0)
    assert order is None, "SELL must not execute when managed position is 0"


# ── Test (d): adopt=True restores old behavior ───────────────────────────────

def test_adopt_external_holdings_true_adopts_balance(caplog):
    """
    With ADOPT_EXTERNAL_HOLDINGS=true, the exchange balance is fully adopted
    even if the state file shows 0 and bot_opened=False.
    """
    exc, _ = _make_executor(state_position=0.0, state_bot_opened=True, adopt=True)

    exc._exchange.fetch_balance.return_value = {
        "free":  {"BTC": 0.000378, "CAD": 100.0},
        "total": {"BTC": 0.000378, "CAD": 100.0},
    }
    exc._exchange.fetch_ticker.return_value = {"last": 85000.0}

    import logging
    with caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        exc._sync_position("BTC/CAD")

    # With adopt=True, position should be set from exchange (0.000378)
    assert exc.position == pytest.approx(0.000378), "adopt=True should take exchange qty"
    assert "EXTERNAL HOLDINGS DETECTED" not in caplog.text


# ── Test (e): no warning when exchange matches state exactly ─────────────────

def test_no_external_warning_when_exchange_matches_state(caplog):
    """
    Exchange holds exactly what the state file recorded — no external holdings.
    """
    exc, _ = _make_executor(state_position=0.000553, state_bot_opened=True, adopt=False)

    exc._exchange.fetch_balance.return_value = {
        "free":  {"BTC": 0.000553, "CAD": 60.0},
        "total": {"BTC": 0.000553, "CAD": 60.0},
    }
    import logging
    with caplog.at_level(logging.WARNING, logger="bot.execution.live_executor"):
        exc._sync_position("BTC/CAD")

    assert exc.position == pytest.approx(0.000553)
    assert "EXTERNAL HOLDINGS DETECTED" not in caplog.text
