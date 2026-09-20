"""
Tests for migrate_legacy_fills.py's pure file-safety helpers only
(_copy_db / _backup_live_db) — no network, no ccxt call, no KRAKEN_API_KEY
required. main() itself needs a live Kraken connection and is exercised
manually per deploy/PAPER_SHADOW_RUNBOOK.md, not here; the matching/linking
logic it delegates to (bot/accounting/migration.py) has its own full test
suite in test_accounting_migration.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import migrate_legacy_fills as cli  # noqa: E402


def _make_db(path: str, content: bytes = b"fake sqlite content") -> None:
    with open(path, "wb") as f:
        f.write(content)


def test_backup_live_db_copies_without_touching_the_original(tmp_path):
    live = str(tmp_path / "trades.db")
    _make_db(live, b"original-bytes")

    backup_path = cli._backup_live_db(live)

    assert os.path.exists(backup_path)
    assert backup_path != live
    with open(backup_path, "rb") as f:
        assert f.read() == b"original-bytes"
    with open(live, "rb") as f:
        assert f.read() == b"original-bytes"   # original untouched by taking a backup


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
    _make_db(live, b"live-data")

    copy_path = cli._copy_db(live)

    assert copy_path != live
    with open(live, "rb") as f:
        assert f.read() == b"live-data"
    with open(copy_path, "rb") as f:
        assert f.read() == b"live-data"
