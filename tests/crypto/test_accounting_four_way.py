"""
Tests for bot/accounting/four_way.py — the joint verification across
exchange data, executor/PositionManager state, and SQLite (design §9,
implementation item 7). Every check must be reported separately and by
name; `ready`/`explain()` are checked to reflect exactly that.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _accounting_fake_exchange import FakeExchangeAdapter, iso  # noqa: E402

from bot.accounting import four_way, reconciliation, store  # noqa: E402
from bot.data.trade_log import TradeLog  # noqa: E402

T0 = 1_700_000_000_000


def _setup(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    tl = TradeLog(db_path=db_path)
    return conn, tl


def test_ready_true_when_everything_agrees(tmp_path):
    conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001, timestamp_ms=T0 + 1000)
    state = reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 2000)

    report = four_way.run_four_way_verification(
        conn, state, symbols=["BTC/CAD"],
        live_positions={"BTC/CAD": {"qty": 0.001, "avg_cost": 90_000.0, "realized_pnl": 0.0}},
    )
    assert report.ready, report.explain()


def test_not_ready_when_block_state_has_a_blocked_symbol(tmp_path):
    conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                      timestamp_ms=T0 + 1000, visible=False)
    state = reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 2000)
    assert state.blocked_for_buy("BTC/CAD")

    report = four_way.run_four_way_verification(
        conn, state, symbols=["BTC/CAD"],
        live_positions={"BTC/CAD": {"qty": 0.0, "avg_cost": 0.0, "realized_pnl": 0.0}},
    )
    assert not report.ready
    assert "exchange-data" in report.explain()


def test_position_fold_diff_detects_a_wiring_bug_not_exchange_problem(tmp_path):
    """PositionManager reports a DIFFERENT qty than the persisted
    observed_trades fold implies — a wiring bug (recovery didn't actually
    read from the ledger), reported distinctly from any exchange-data
    block."""
    conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001, timestamp_ms=T0 + 1000)
    state = reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 2000)
    assert state.explain() == "ok"

    report = four_way.run_four_way_verification(
        conn, state, symbols=["BTC/CAD"],
        live_positions={"BTC/CAD": {"qty": 999.0, "avg_cost": 1.0, "realized_pnl": 0.0}},
    )
    assert not report.ready
    assert not report.position_diff["BTC/CAD"].ok
    assert "position-fold" in report.explain()


def test_position_diff_ok_when_no_observed_trades_yet_pre_migration_honesty(tmp_path):
    """Before item 8's migration has run for a symbol, observed_trades may
    be empty even though the live position is real — this is explicitly
    NOT treated as a diff (see diff_position_against_fold's docstring)."""
    conn, _ = _setup(tmp_path)
    diff = four_way.diff_position_against_fold(
        conn, "BTC/CAD", live_qty=0.05, live_avg_cost=90_000.0, live_realized_pnl=0.0,
    )
    assert diff.ok
    assert "nothing to diff" in diff.reason


def test_ledger_delivery_consistency_ok_after_a_clean_link(tmp_path):
    conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001, timestamp_ms=T0 + 1000)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 2000)

    # No links were created yet in this flow (reconciliation.py only
    # OBSERVES trades — live_observe.py / migration.py are what create
    # trade_fill_links) — so ledger-delivery is trivially ok (nothing
    # claims to be ledger_written yet).
    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert result.ok


def test_ledger_delivery_detects_orphaned_marker(tmp_path):
    conn, _ = _setup(tmp_path)
    t = store.ObservedTrade(
        trade_id="T1", order_id="O1", symbol="BTC/CAD", side="buy", price=90_000.0,
        amount=0.001, cost=90.0, fee_cost=0.0, fee_currency="CAD",
        exchange_timestamp=iso(T0), source="live",
    )
    store.upsert_observed_trade(conn, t)
    # Corrupt state directly: ledger_written_at set with NO trade_fill_links row.
    with conn:
        conn.execute("UPDATE observed_trades SET ledger_written_at = ? WHERE trade_id = 'T1'", (iso(T0),))
    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert not result.ok
    assert result.orphaned_marker_trade_ids == ["T1"]
