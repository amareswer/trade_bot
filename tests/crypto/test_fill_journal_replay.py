"""
Unit tests for _replay_pending_journal_entries (bot/main.py).

2026-09-18 review finding (P1-3): LiveExecutor.execute() persists a fill's
accounting effect to its own state file before the caller separately writes
that fill to trade_log — a crash between the two writes leaves the fill
invisible to trade_log/reporting forever despite the portfolio already
reflecting it. This function replays any such recovered entry at startup,
before any new trading.

2026-09-18 FOLLOW-UP review finding (P1): the singular pending_journal_entry
was upgraded to a list (pending_journal_entries) — a second fill recorded
before the first was acked no longer silently overwrites it. Replay must
also be idempotent (exec_key) and preserve original pnl/timestamp.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from bot.data.trade_log import TradeLog
from bot.main import _replay_pending_journal_entries


def _fake_executor(entries: "list[dict] | None"):
    ex = MagicMock()
    ex.pending_journal_entries = entries or []
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
        "order_id": "order-123", "exec_key": "order-123#1",
        "side": "BUY", "symbol": "BTC/CAD",
        "quantity": 0.001, "price": 90_000.0, "pnl": None,
        "fee_cost": 0.36, "fee_currency": "CAD",
        "filled_at": "2026-09-18T00:00:00+00:00",
    }
    ex = _fake_executor([entry])
    executors = {"BTC/CAD": ex}

    recovered = _replay_pending_journal_entries(executors, tl, alerter)

    assert recovered == ["BTC/CAD"]
    rows = tl.recent()
    assert len(rows) == 1
    assert rows[0]["side"] == "BUY"
    assert rows[0]["symbol"] == "BTC/CAD"
    assert abs(rows[0]["quantity"] - 0.001) < 1e-9
    assert rows[0]["signal_reason"] == "recovered_from_crash"
    assert rows[0]["timestamp"] == "2026-09-18T00:00:00+00:00"   # original, not replay time
    ex.ack_journal_entry.assert_called_once_with("order-123")
    alerter.message.assert_called_once()


def test_recovered_sell_preserves_pnl(tmp_path):
    """2026-09-18 follow-up review finding: a recovered SELL used to lose
    its P&L (NULL), silently excluding it from live PF/win-rate."""
    tl = TradeLog(db_path=str(tmp_path / "trades.db"))
    alerter = MagicMock()
    entry = {
        "order_id": "native-stop:s1", "exec_key": "native-stop:s1#1",
        "side": "SELL", "symbol": "BTC/CAD",
        "quantity": 0.01, "price": 78_000.0, "pnl": -20.0,
        "fee_cost": 1.5, "fee_currency": "CAD",
        "filled_at": "2026-09-18T00:00:00+00:00",
    }
    ex = _fake_executor([entry])
    executors = {"BTC/CAD": ex}

    _replay_pending_journal_entries(executors, tl, alerter)

    rows = tl.recent()
    assert rows[0]["pnl"] == -20.0


def test_replay_uses_exec_key_for_idempotent_insert(tmp_path):
    """2026-09-18 follow-up review finding: a crash between the DB insert
    succeeding and the ack being persisted used to duplicate the row on the
    NEXT replay. Simulate exactly that: replay the SAME entry twice (as if
    ack never took effect) — must produce exactly one row."""
    tl = TradeLog(db_path=str(tmp_path / "trades.db"))
    alerter = MagicMock()
    entry = {
        "order_id": "order-123", "exec_key": "order-123#1",
        "side": "BUY", "symbol": "BTC/CAD",
        "quantity": 0.001, "price": 90_000.0, "pnl": None,
        "fee_cost": 0.0, "fee_currency": "",
        "filled_at": "2026-09-18T00:00:00+00:00",
    }
    executors = {"BTC/CAD": _fake_executor([entry])}

    _replay_pending_journal_entries(executors, tl, alerter)
    # Simulate: ack didn't survive a second crash — same entry replayed again.
    _replay_pending_journal_entries(executors, tl, alerter)

    assert len(tl.recent(limit=10)) == 1


def test_replay_failure_alerts_and_does_not_ack(tmp_path):
    """A trade_log write failure must not ack — the entry stays pending so
    the next startup retries it, rather than silently losing the fill."""
    tl = MagicMock()
    tl.log_fill.side_effect = Exception("disk full")
    alerter = MagicMock()
    entry = {
        "order_id": "order-456", "exec_key": "order-456#1",
        "side": "SELL", "symbol": "BTC/CAD",
        "quantity": 0.001, "price": 91_000.0, "pnl": None,
        "fee_cost": 0.5, "fee_currency": "CAD",
        "filled_at": "2026-09-18T00:00:00+00:00",
    }
    ex = _fake_executor([entry])
    executors = {"BTC/CAD": ex}

    recovered = _replay_pending_journal_entries(executors, tl, alerter)

    assert recovered == []
    ex.ack_journal_entry.assert_not_called()
    alerter.error.assert_called_once()


def test_multiple_symbols_each_replayed_independently(tmp_path):
    tl = TradeLog(db_path=str(tmp_path / "trades.db"))
    alerter = MagicMock()
    entry_btc = {
        "order_id": "o1", "exec_key": "o1#1",
        "side": "BUY", "symbol": "BTC/CAD",
        "quantity": 0.001, "price": 90_000.0, "pnl": None,
        "fee_cost": 0.0, "fee_currency": "",
        "filled_at": "2026-09-18T00:00:00+00:00",
    }
    executors = {
        "BTC/CAD": _fake_executor([entry_btc]),
        "SOL/CAD": _fake_executor(None),   # nothing pending — untouched
    }

    recovered = _replay_pending_journal_entries(executors, tl, alerter)

    assert recovered == ["BTC/CAD"]
    executors["SOL/CAD"].ack_journal_entry.assert_not_called()


def test_multiple_pending_entries_on_one_symbol_each_replayed_and_acked(tmp_path):
    """2026-09-18 follow-up review finding: multiple unacked entries (a
    second fill recorded before the first was acked) must each be replayed
    and acked independently, in order — not just the first, or a silent
    overwrite of one by the other."""
    tl = TradeLog(db_path=str(tmp_path / "trades.db"))
    alerter = MagicMock()
    entry1 = {
        "order_id": "o1", "exec_key": "o1#1", "side": "BUY", "symbol": "BTC/CAD",
        "quantity": 0.001, "price": 90_000.0, "pnl": None,
        "fee_cost": 0.0, "fee_currency": "", "filled_at": "2026-09-18T00:00:00+00:00",
    }
    entry2 = {
        "order_id": "o2", "exec_key": "o2#1", "side": "SELL", "symbol": "BTC/CAD",
        "quantity": 0.001, "price": 91_000.0, "pnl": 1.0,
        "fee_cost": 0.0, "fee_currency": "", "filled_at": "2026-09-18T01:00:00+00:00",
    }
    ex = _fake_executor([entry1, entry2])
    executors = {"BTC/CAD": ex}

    recovered = _replay_pending_journal_entries(executors, tl, alerter)

    assert recovered == ["BTC/CAD", "BTC/CAD"]
    rows = tl.recent(limit=10)
    assert len(rows) == 2
    assert ex.ack_journal_entry.call_count == 2
    ex.ack_journal_entry.assert_any_call("o1")
    ex.ack_journal_entry.assert_any_call("o2")
