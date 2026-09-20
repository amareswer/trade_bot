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

import pytest  # noqa: E402


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


# ── Gated-readiness review 2026-09-20: idempotency, restart recovery, ─────
# duplicate prevention, rollback ────────────────────────────────────────────

def test_migration_orphan_backfill_idempotent_across_two_runs(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    orphan = _t("T9", "O9", "BTC/CAD", "buy", 90_000.0, 0.002, 1_000_000, fee=0.18)

    report1 = migration.run_migration(db_path, [orphan], symbols=["BTC/CAD"])
    assert report1.orphan_trades_inserted == ["T9"]
    report2 = migration.run_migration(db_path, [orphan], symbols=["BTC/CAD"])
    assert report2.orphan_trades_inserted == []   # already linked — nothing new to do

    conn = store.connect(db_path)
    fills_count = conn.execute(
        "SELECT COUNT(*) FROM fills WHERE exec_key='migration:T9'"
    ).fetchone()[0]
    assert fills_count == 1   # no duplicate fills row across the two runs
    links_count = conn.execute(
        "SELECT COUNT(*) FROM trade_fill_links WHERE trade_id='T9'"
    ).fetchone()[0]
    assert links_count == 1


def test_migration_recovers_an_orphan_stranded_after_observe_but_before_fills_row(tmp_path):
    """Simulates a crash between upsert_observed_trade succeeding and
    log_fill/link_trade_to_fill ever running — the exact window the
    2026-09-20 restart-recovery fix targets. Before the fix, `run_migration`
    would see the observed_trades row on every future run and skip the
    trade forever, permanently stranding it with no fills row and no link."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    TradeLog(db_path=db_path)   # ensure the (separate) `fills` table schema exists
    orphan = _t("T9", "O9", "BTC/CAD", "buy", 90_000.0, 0.002, 1_000_000, fee=0.18)

    conn = store.connect(db_path)
    store.upsert_observed_trade(conn, ObservedTrade(
        trade_id="T9", order_id="O9", symbol="BTC/CAD", side="buy",
        price=90_000.0, amount=0.002, cost=180.0, fee_cost=0.18,
        fee_currency="CAD", exchange_timestamp=iso(1_000_000), source="migration",
    ))
    assert store.fills_row_by_exec_key(conn, "migration:T9") is None   # crash before this step
    conn.close()

    report = migration.run_migration(db_path, [orphan], symbols=["BTC/CAD"])
    assert report.orphan_trades_inserted == ["T9"]

    conn = store.connect(db_path)
    assert store.is_ledger_represented(conn, "T9")
    row = store.fills_row_by_exec_key(conn, "migration:T9")
    assert row is not None


def test_migration_recovers_an_orphan_stranded_after_fills_row_but_before_link(tmp_path):
    """A crash one step later than the test above — log_fill already
    succeeded (its own exec_key idempotency made that safe to have
    happened), but link_trade_to_fill never ran. Recovery happens via the
    GENERAL exact-match linking loop, not the orphan-specific recovery
    branch — once a real `fills` row exists (linked or not), it is
    indistinguishable from any other historical unlinked fills row, and
    the ordinary conservation matcher (which runs before the orphan loop)
    finds and links it on its own. This is arguably the more elegant
    outcome: the orphan-specific recovery code added for the test above is
    only reached when NO fills row exists yet at all."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    orphan = _t("T9", "O9", "BTC/CAD", "buy", 90_000.0, 0.002, 1_000_000, fee=0.18)

    conn = store.connect(db_path)
    store.upsert_observed_trade(conn, ObservedTrade(
        trade_id="T9", order_id="O9", symbol="BTC/CAD", side="buy",
        price=90_000.0, amount=0.002, cost=180.0, fee_cost=0.18,
        fee_currency="CAD", exchange_timestamp=iso(1_000_000), source="migration",
    ))
    conn.close()
    tl = TradeLog(db_path=db_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.002, price=90_000.0,
                exchange="kraken", signal_reason="migration_backfill", risk_decision="n/a",
                fee_cost=0.18, fee_currency="CAD", source="migration_backfill",
                exec_key="migration:T9", timestamp=iso(1_000_000), order_id="O9")

    conn = store.connect(db_path)
    assert not store.is_ledger_represented(conn, "T9")   # fills row exists, not linked yet
    pre_row = store.fills_row_by_exec_key(conn, "migration:T9")
    assert pre_row is not None
    conn.close()

    report = migration.run_migration(db_path, [orphan], symbols=["BTC/CAD"])
    assert report.orphan_trades_inserted == []
    assert len(report.linked) == 1
    assert report.linked[0].matched_trade_ids == ["T9"]

    conn = store.connect(db_path)
    assert store.is_ledger_represented(conn, "T9")
    row = store.fills_row_by_exec_key(conn, "migration:T9")
    assert row is not None
    assert row["id"] == pre_row["id"]   # the SAME fills row, reused — not duplicated
    fills_count = conn.execute(
        "SELECT COUNT(*) FROM fills WHERE exec_key='migration:T9'"
    ).fetchone()[0]
    assert fills_count == 1


def test_migration_leaves_a_live_observed_unlinked_trade_alone(tmp_path):
    """Duplicate-prevention: a trade already present in observed_trades via
    the LIVE path (source='live'), still unlinked for its own reasons, must
    NOT be backfilled with a synthetic migration fills row — that would be
    a genuine double-count of a trade the live path already knows about,
    not a legitimate migration recovery (only source='migration' rows are
    ever resumed — see the restart-recovery tests above)."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    store.upsert_observed_trade(conn, ObservedTrade(
        trade_id="T5", order_id="O5", symbol="BTC/CAD", side="buy",
        price=90_000.0, amount=0.001, cost=90.0, fee_cost=0.09,
        fee_currency="CAD", exchange_timestamp=iso(1_000_000), source="live",
    ))
    conn.close()

    live_trade = _t("T5", "O5", "BTC/CAD", "buy", 90_000.0, 0.001, 1_000_000, fee=0.09)
    report = migration.run_migration(db_path, [live_trade], symbols=["BTC/CAD"])

    assert report.orphan_trades_inserted == []
    conn = store.connect(db_path)
    assert not store.is_ledger_represented(conn, "T5")
    assert store.fills_row_by_exec_key(conn, "migration:T5") is None


def test_migration_rolls_back_a_failed_multi_trade_link(tmp_path, monkeypatch):
    """A crash partway through linking a multi-trade match must not leave a
    partially-linked fills row: the whole match is committed inside one
    `with conn:` block, so SQLite rolls back the entire group on an
    uncaught exception. Re-running afterward (without the injected fault)
    must then complete cleanly from scratch — nothing was left half-done."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    _seed_legacy_fill(db_path, side="BUY", symbol="BTC/CAD", qty=0.002, price=90_000.0,
                       fee=0.18, ts_ms=1_000_000)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 90_000.0, 0.001, 999_990, fee=0.09)
    t2 = _t("T2", "O1", "BTC/CAD", "buy", 90_000.0, 0.001, 1_000_010, fee=0.09)

    original_upsert = store.upsert_observed_trade
    calls = {"n": 0}

    def _flaky_upsert(conn, trade):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash mid-transaction")
        return original_upsert(conn, trade)

    monkeypatch.setattr(migration.store, "upsert_observed_trade", _flaky_upsert)
    with pytest.raises(RuntimeError):
        migration.run_migration(db_path, [t1, t2], symbols=["BTC/CAD"])

    conn = store.connect(db_path)
    assert not store.is_ledger_represented(conn, "T1")
    assert not store.is_ledger_represented(conn, "T2")
    links = conn.execute("SELECT COUNT(*) FROM trade_fill_links").fetchone()[0]
    assert links == 0   # rollback proven — T1's upsert didn't survive on its own
    conn.close()

    monkeypatch.setattr(migration.store, "upsert_observed_trade", original_upsert)
    report2 = migration.run_migration(db_path, [t1, t2], symbols=["BTC/CAD"])
    assert sorted(report2.linked[0].matched_trade_ids) == ["T1", "T2"]
