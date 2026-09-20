"""
Tests for migrate_legacy_fills.py's pure file-safety helpers only
(_copy_db / _backup_live_db) — no network, no ccxt call, no KRAKEN_API_KEY
required. main() itself needs a live Kraken connection and is exercised
manually per deploy/PAPER_SHADOW_RUNBOOK.md, not here; the matching/linking
logic it delegates to (bot/accounting/migration.py) has its own full test
suite in test_accounting_migration.py.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import migrate_legacy_fills as cli  # noqa: E402


def _make_db(path: str, rows=(("original", 1),)) -> None:
    """A real, minimal SQLite database — NOT raw bytes. The backup helpers
    under test (accounting review follow-up, 2026-09-20, P1: replaced
    shutil.copyfile with sqlite3.Connection.backup()) require an actual
    SQLite file to operate on; a byte-identical copy is no longer even the
    contract being tested (backup() doesn't promise byte-for-byte output,
    only that every committed row is present) — see _read_rows below."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (k TEXT, v INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def _read_rows(path: str):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT k, v FROM t ORDER BY k").fetchall()
    finally:
        conn.close()


def test_backup_live_db_copies_without_touching_the_original(tmp_path):
    live = str(tmp_path / "trades.db")
    _make_db(live, rows=[("original", 1)])

    backup_path = cli._backup_live_db(live)

    assert os.path.exists(backup_path)
    assert backup_path != live
    assert _read_rows(backup_path) == [("original", 1)]
    assert _read_rows(live) == [("original", 1)]   # original untouched by taking a backup


def test_backup_live_db_filename_is_distinct_from_dry_run_copy_naming(tmp_path):
    """The pre-apply backup and the dry-run working copy must be trivially
    distinguishable on disk (an operator scanning logs/ for 'is this safe
    to delete' should never have to open a file to find out)."""
    live = str(tmp_path / "trades.db")
    _make_db(live)

    backup_path = cli._backup_live_db(live)
    copy_path = cli._copy_db(live)

    assert "trades_pre_migration_backup_" in os.path.basename(backup_path)
    assert "trades_migration_copy_" in os.path.basename(copy_path)
    assert backup_path != copy_path


def test_copy_db_leaves_the_live_file_untouched(tmp_path):
    live = str(tmp_path / "trades.db")
    _make_db(live, rows=[("live-data", 42)])

    copy_path = cli._copy_db(live)

    assert copy_path != live
    assert _read_rows(live) == [("live-data", 42)]
    assert _read_rows(copy_path) == [("live-data", 42)]


def test_copy_db_captures_committed_wal_data_not_yet_in_the_main_file(tmp_path):
    """Accounting review follow-up, 2026-09-20, P1 reproduction: a plain
    shutil.copyfile of the main database file alone can miss transactions
    already COMMITTED but still sitting only in the separate -wal sidecar
    file (reproduced against the pre-fix code: the copy came back missing
    the table entirely). sqlite3.Connection.backup() reads through SQLite's
    own online-backup machinery instead of raw-copying one file, so it sees
    committed WAL contents even with the writer connection still open and
    no checkpoint having happened."""
    live = str(tmp_path / "trades.db")
    writer = sqlite3.connect(live)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (k TEXT, v INTEGER)")
    writer.commit()
    writer.execute("INSERT INTO t VALUES ('committed-in-wal', 1)")
    writer.commit()
    try:
        copy_path = cli._copy_db(live)
        assert _read_rows(copy_path) == [("committed-in-wal", 1)]
    finally:
        writer.close()


def test_backup_raises_on_a_non_database_file(tmp_path):
    """A file that isn't a real SQLite database must fail loudly, not
    silently 'succeed' by raw-copying garbage bytes and calling it a
    backup — the whole point of switching to the backup API."""
    live = str(tmp_path / "trades.db")
    with open(live, "wb") as f:
        f.write(b"not a sqlite file")
    with pytest.raises(sqlite3.DatabaseError):
        cli._backup_live_db(live)
