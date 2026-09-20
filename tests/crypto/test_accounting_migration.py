"""
Tests for bot/accounting/migration.py — item 8 (historical fill migration)
and item 9's "legacy migration ambiguity" scenario. Operates on a tmp_path
sqlite db (never the live trades.db) with a real bot.data.trade_log.TradeLog
seeding pre-existing `fills` rows the way the ACTUAL historical rows look
(synthetic UUID exec_key, no trade_id linkage) — then runs the real
migration matcher against a hand-built real-trade list.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _accounting_fake_exchange import iso  # noqa: E402

from bot.accounting import migration, store  # noqa: E402
from bot.accounting.engine import ObservedTrade  # noqa: E402
from bot.data.trade_log import TradeLog  # noqa: E402


def _t(trade_id, order_id, symbol, side, price, amount, ts_ms, fee=0.0, fee_ccy="CAD"):
    return ObservedTrade(
        trade_id=trade_id, order_id=order_id, symbol=symbol, side=side, price=price,
        amount=amount, cost=price * amount, fee_cost=fee, fee_currency=fee_ccy,
        exchange_timestamp=iso(ts_ms), source="live",
    )


def _seed_legacy_fill(db_path, *, side, symbol, qty, price, fee, ts_ms):
    tl = TradeLog(db_path=db_path)
    tl.log_fill(side=side, symbol=symbol, quantity=qty, price=price, exchange="kraken",
                fee_cost=fee, fee_currency="CAD", timestamp=iso(ts_ms))


def test_migration_links_exact_single_match(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    _seed_legacy_fill(db_path, side="BUY", symbol="BTC/CAD", qty=0.001, price=90_000.0,
                       fee=0.09, ts_ms=1_000_000)
    real_trade = _t("T1", "O1", "BTC/CAD", "buy", 90_000.0, 0.001, 1_000_000, fee=0.09)

    report = migration.run_migration(db_path, [real_trade], symbols=["BTC/CAD"])
    assert len(report.linked) == 1
    assert not report.blocked
    assert report.linked[0].matched_trade_ids == ["T1"]

    conn = store.connect(db_path)
    assert store.is_ledger_represented(conn, "T1")
    obs = store.get_observed_trade(conn, "T1")
    assert obs.source == "live"  # migration LINKED an existing live-recorded row, didn't reclassify it


def test_migration_blocks_on_ambiguous_legacy_row(tmp_path):
    """Two real trades that BOTH exactly conserve the legacy row's totals —
    never guessed, always blocked for manual review (item 8's explicit
    requirement)."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    _seed_legacy_fill(db_path, side="BUY", symbol="BTC/CAD", qty=0.001, price=90_000.0,
                       fee=0.09, ts_ms=1_000_000)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 90_000.0, 0.001, 1_000_000, fee=0.09)
    t2 = _t("T2", "O2", "BTC/CAD", "buy", 88_000.0, 0.001, 1_000_050, fee=0.09)  # same qty/fee, different price

    report = migration.run_migration(db_path, [t1, t2], symbols=["BTC/CAD"])
    assert len(report.linked) == 0
    assert len(report.blocked) == 1
    assert "ambiguous" in report.blocked[0].reason

    conn = store.connect(db_path)
    assert not store.is_ledger_represented(conn, "T1")
    assert not store.is_ledger_represented(conn, "T2")


def test_migration_backfills_a_genuine_orphan_trade_with_no_prior_fills_row(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    orphan = _t("T9", "O9", "BTC/CAD", "buy", 90_000.0, 0.002, 1_000_000, fee=0.18)

    report = migration.run_migration(db_path, [orphan], symbols=["BTC/CAD"])
    assert report.orphan_trades_inserted == ["T9"]

    conn = store.connect(db_path)
    assert store.is_ledger_represented(conn, "T9")
    row = store.fills_row_by_exec_key(conn, "migration:T9")
    assert row is not None
    assert row["quantity"] == 0.002
    assert "migration_backfill" in (row["notes"] or "")


def test_migration_never_double_links_across_two_runs(tmp_path):
    """Running migration twice against the same data must not create a
    second link or a second fills row — idempotent, since a real operator
    might re-run this after reviewing a blocked-rows report."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    _seed_legacy_fill(db_path, side="BUY", symbol="BTC/CAD", qty=0.001, price=90_000.0,
                       fee=0.09, ts_ms=1_000_000)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 90_000.0, 0.001, 1_000_000, fee=0.09)

    migration.run_migration(db_path, [t1], symbols=["BTC/CAD"])
    report2 = migration.run_migration(db_path, [t1], symbols=["BTC/CAD"])
    # Second run: T1 is already linked, so it's excluded from candidate
    # matching (already_linked) — nothing new to link or block.
    assert len(report2.linked) == 0
    assert len(report2.blocked) == 0
    conn = store.connect(db_path)
    row = conn.execute("SELECT COUNT(*) FROM trade_fill_links WHERE trade_id='T1'").fetchone()
    assert row[0] == 1


def test_migration_many_to_one_legacy_row(tmp_path):
    """A single historical fills row that was actually TWO real fills
    (e.g. a partial-fill order journaled as one aggregate row historically)
    — matched as a group, not forced onto one trade."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    _seed_legacy_fill(db_path, side="BUY", symbol="BTC/CAD", qty=0.002, price=90_000.0,
                       fee=0.18, ts_ms=1_000_000)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 90_000.0, 0.001, 999_990, fee=0.09)
    t2 = _t("T2", "O1", "BTC/CAD", "buy", 90_000.0, 0.001, 1_000_010, fee=0.09)

    report = migration.run_migration(db_path, [t1, t2], symbols=["BTC/CAD"])
    assert len(report.linked) == 1
    assert sorted(report.linked[0].matched_trade_ids) == ["T1", "T2"]
