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


# ---------------------------------------------------------------------------
# 2026-09-18 PASS-3 review finding (P1): a NORMAL trade_log write and a
# journal replay of the SAME fill must be mutually idempotent — not just
# replay-to-replay. Integration tests using a REAL LiveExecutor and REAL
# TradeLog (temporary SQLite), not mocks, per the review's own request.
# ---------------------------------------------------------------------------

def test_normal_write_then_crash_before_ack_then_replay_leaves_one_row(tmp_path):
    """Reproduced by PASS-3 exactly: record a fill normally (as
    bot/main.py's _execute_approved_signal does — trade_log.log_fill with
    the order's own exec_key), leave its journal entry UNACKNOWLEDGED
    (simulating a crash between the normal write and the ack), then run
    replay. Must produce exactly ONE row, not two — normal-write-to-replay
    idempotency, not just replay-to-replay."""
    from unittest.mock import MagicMock, patch

    import bot.execution.live_executor as le_mod
    from bot.data.trade_log import TradeLog
    from bot.execution.live_executor import LiveExecutor
    from bot.main import _replay_pending_journal_entries
    from bot.strategy.threshold_strategy import Signal

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = {
        "BTC/CAD": {"limits": {"amount": {"min": 0.00005}, "cost": {"min": 5.0}}}
    }
    mock_ex.fetch_balance.return_value = {"free": {"CAD": 1000.0}}
    mock_ex.fetch_open_orders.return_value = []
    mock_ex.price_to_precision.return_value = "0.0"
    raw = {
        "id": "buy-1", "status": "closed", "filled": 0.001,
        "average": 90_000.0, "fee": {"cost": 0.36, "currency": "CAD"},
    }
    mock_ex.create_order.return_value = raw
    mock_ex.fetch_order.return_value = raw

    with patch.object(le_mod.ccxt, "kraken") as mock_cls, patch("time.sleep"), \
         patch("bot.execution.live_executor.cfg") as mock_cfg:
        mock_cfg.exchange.limit_order_enabled = False
        mock_cls.return_value = mock_ex
        executor = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=1000.0, dry_run=False,
            state_path=str(tmp_path / "state.json"),
        )
        order = executor.execute(Signal.BUY, 90_000.0, 0.001)

    assert order is not None and order.status.value == "FILLED"
    assert len(executor.pending_journal_entries) == 1   # not yet acked

    tl = TradeLog(db_path=str(tmp_path / "trades.db"))
    # The NORMAL write path (bot/main.py's _execute_approved_signal),
    # using the order's own exec_key — deliberately NOT followed by
    # ack_journal_entry(), simulating a crash right after this DB commit.
    tl.log_fill(
        side=order.side.value, symbol="BTC/CAD", quantity=order.quantity,
        price=order.price, pnl=order.pnl, exchange="kraken",
        fee_cost=order.fee_cost, fee_currency=order.fee_currency,
        exec_key=order.exec_key,
    )
    assert len(tl.recent(limit=10)) == 1

    # Restart-equivalent: replay runs against the (still-unacked) journal.
    alerter = MagicMock()
    _replay_pending_journal_entries({"BTC/CAD": executor}, tl, alerter)

    rows = tl.recent(limit=10)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"


def test_restart_delta_does_not_collide_with_pre_restart_exec_key(tmp_path):
    """Reproduced by PASS-3 exactly: a monotonic in-memory counter reset to
    0 on restart, so a native stop's SECOND partial-fill delta (recorded
    after a restart) could reuse the SAME exec_key ("order_id#1") as its
    FIRST delta (recorded and acked before the restart) — a genuinely new
    fill silently discarded as "already recorded". Fixed via Order.exec_key
    (a fresh UUID per Order, not a counter) — verify two deltas of the same
    native-stop order_id, separated by a restart, never share a key."""
    import bot.execution.live_executor as le_mod
    from bot.execution.executor import Order, OrderSide, OrderStatus
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch

    from bot.execution.live_executor import LiveExecutor

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = {
        "BTC/CAD": {"limits": {"amount": {"min": 0.00005}, "cost": {"min": 5.0}}}
    }
    mock_ex.fetch_balance.return_value = {"free": {"CAD": 1000.0}}
    state_path = str(tmp_path / "state.json")

    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=1000.0, dry_run=False, state_path=state_path,
        )
    shared_order_id = "native-stop:persisted-stop"
    o1 = Order(order_id=shared_order_id, symbol="BTC/CAD", side=OrderSide.SELL,
               quantity=0.001, price=78_000.0, status=OrderStatus.FILLED,
               created_at=datetime.now(timezone.utc), filled_at=datetime.now(timezone.utc))
    ex._record_pending_journal_entry(o1)
    first_key = ex.pending_journal_entries[0]["exec_key"]
    ex.ack_journal_entry(shared_order_id)   # acked BEFORE the restart

    # Restart: a fresh LiveExecutor loads the SAME state file.
    with patch.object(le_mod.ccxt, "kraken") as mock_cls2:
        mock_cls2.return_value = mock_ex
        ex2 = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD", api_key="k", api_secret="s",
            starting_cash=1000.0, dry_run=False, state_path=state_path,
        )
    o2 = Order(order_id=shared_order_id, symbol="BTC/CAD", side=OrderSide.SELL,
               quantity=0.001, price=77_500.0, status=OrderStatus.FILLED,
               created_at=datetime.now(timezone.utc), filled_at=datetime.now(timezone.utc))
    ex2._record_pending_journal_entry(o2)
    second_key = ex2.pending_journal_entries[0]["exec_key"]

    assert first_key != second_key, (
        "a post-restart delta must never reuse a pre-restart exec_key for "
        "the same order_id"
    )
