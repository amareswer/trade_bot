"""
Unit tests for _replay_pending_journal_entries (bot/main.py).

2026-09-18 review finding (P1-3): LiveExecutor.execute() persists a fill's
accounting effect to its own state file before the caller separately writes
that fill to trade_log — a crash between the two writes leaves the fill
invisible to trade_log/reporting forever despite the portfolio already
reflecting it. This function replays any such recovered entry at startup,
before any new trading.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from bot.data.trade_log import TradeLog
from bot.main import _replay_pending_journal_entries


def _fake_executor(pending_entry: dict | None):
    ex = MagicMock()
    ex.pending_journal_entry = pending_entry
    return ex


def test_no_pending_entries_replays_nothing(tmp_path):
    tl = TradeLog(db_path=str(tmp_path / "trades.db"))
    alerter = MagicMock()
    executors = {"BTC/CAD": _fake_executor(None)}

    recovered = _replay_pending_journal_entries(executors, tl, alerter)

    assert recovered == []
    assert tl.recent() == []
    alerter.message.assert_not_called()


def test_pending_entry_is_replayed_and_acked(tmp_path):
    tl = TradeLog(db_path=str(tmp_path / "trades.db"))
    alerter = MagicMock()
    entry = {
        "order_id": "order-123", "side": "BUY", "symbol": "BTC/CAD",
        "quantity": 0.001, "price": 90_000.0,
        "fee_cost": 0.36, "fee_currency": "CAD",
        "filled_at": "2026-09-18T00:00:00+00:00",
    }
    ex = _fake_executor(entry)
    executors = {"BTC/CAD": ex}

    recovered = _replay_pending_journal_entries(executors, tl, alerter)

    assert recovered == ["BTC/CAD"]
    rows = tl.recent()
    assert len(rows) == 1
    assert rows[0]["side"] == "BUY"
    assert rows[0]["symbol"] == "BTC/CAD"
    assert abs(rows[0]["quantity"] - 0.001) < 1e-9
    assert rows[0]["signal_reason"] == "recovered_from_crash"
    ex.ack_journal_entry.assert_called_once_with("order-123")
    alerter.message.assert_called_once()


def test_replay_failure_alerts_and_does_not_ack(tmp_path):
    """A trade_log write failure must not ack — the entry stays pending so
    the next startup retries it, rather than silently losing the fill."""
    tl = MagicMock()
    tl.log_fill.side_effect = Exception("disk full")
    alerter = MagicMock()
    entry = {
        "order_id": "order-456", "side": "SELL", "symbol": "BTC/CAD",
        "quantity": 0.001, "price": 91_000.0,
        "fee_cost": 0.5, "fee_currency": "CAD",
        "filled_at": "2026-09-18T00:00:00+00:00",
    }
    ex = _fake_executor(entry)
    executors = {"BTC/CAD": ex}

    recovered = _replay_pending_journal_entries(executors, tl, alerter)

    assert recovered == []
    ex.ack_journal_entry.assert_not_called()
    alerter.error.assert_called_once()


def test_multiple_symbols_each_replayed_independently(tmp_path):
    tl = TradeLog(db_path=str(tmp_path / "trades.db"))
    alerter = MagicMock()
    entry_btc = {
        "order_id": "o1", "side": "BUY", "symbol": "BTC/CAD",
        "quantity": 0.001, "price": 90_000.0,
        "fee_cost": 0.0, "fee_currency": "",
        "filled_at": "2026-09-18T00:00:00+00:00",
    }
    executors = {
        "BTC/CAD": _fake_executor(entry_btc),
        "SOL/CAD": _fake_executor(None),   # nothing pending — untouched
    }

    recovered = _replay_pending_journal_entries(executors, tl, alerter)

    assert recovered == ["BTC/CAD"]
    executors["SOL/CAD"].ack_journal_entry.assert_not_called()
