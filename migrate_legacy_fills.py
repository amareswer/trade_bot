"""
CLI wrapper for bot/accounting/migration.py (execution-accounting design
item 8). Operates on a COPY of trades.db by default — never the live
database — and prints a report for human review before anyone points this
at the real file.

Usage:
    .venv/bin/python migrate_legacy_fills.py                  # dry run on a DB copy
    .venv/bin/python migrate_legacy_fills.py --apply-to-live   # explicit, separate step

Safety, gated-readiness review 2026-09-20:
  - `--apply-to-live` first copies the REAL logs/trades.db to a timestamped
    `logs/trades_pre_migration_backup_<ts>.db` — unconditionally, cannot be
    skipped — before touching it. Restore by copying that file back over
    logs/trades.db if a migration run needs to be undone.
  - Only exact-conservation matches are ever linked automatically
    (bot/accounting/engine.match_legacy_fill — quantity+fee sum must
    conserve within 1e-6, never a nearest-timestamp guess). Anything
    ambiguous or unmatched is BLOCKED and printed for manual review, never
    silently applied — this script cannot override that; it is enforced in
    bot/accounting/migration.py itself.
  - The recommended workflow is: run the default dry-run mode first, read
    the full report (every blocked row's reason + candidate trade ids,
    every proposed link's fill-id -> trade-id(s) mapping), and only pass
    --apply-to-live after a human has reviewed it.

Does NOT place any order, touch logs/HALT, or import bot/execution or
bot/main. Requires KRAKEN_API_KEY/KRAKEN_API_SECRET (read-only trade/
balance history calls only — Query Funds / Query Orders capability, no
Create/Cancel Orders needed for this script).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

import ccxt
from dotenv import load_dotenv

load_dotenv()

from bot.accounting import engine, migration
from bot.accounting.kraken_adapter import KrakenAccountingAdapter

_LIVE_DB = os.path.join(os.path.dirname(__file__), "logs", "trades.db")
_SYMBOLS = ["BTC/CAD", "SOL/CAD"]  # matches CLAUDE.md's UNIVERSE_WHITELIST at time of writing


def _sqlite_backup(src_path: str, dest_path: str) -> None:
    """A true consistent SQLite snapshot via the connection backup API
    (accounting review follow-up, 2026-09-20, P1) — NOT shutil.copyfile,
    which raw-copies the main database file only. That's unsafe in two
    ways: (1) in WAL mode, committed transactions can still live in the
    separate -wal file and never make it into a copy of the main file alone
    (reproduced: a WAL-mode DB with a committed row copied via copyfile came
    back missing the table entirely), and (2) even in the current rollback-
    journal mode, a copy racing a concurrent writer mid-transaction can read
    a torn/inconsistent file. sqlite3.Connection.backup() uses SQLite's own
    online backup API, which is safe against a live writer in either
    journal mode and captures anything already committed regardless of
    which journal mode is active. Runs a PRAGMA integrity_check on the
    result and raises if it doesn't come back clean — a snapshot that isn't
    verified is not a real safety net."""
    src = sqlite3.connect(src_path)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
            (result,) = dest.execute("PRAGMA integrity_check").fetchone()
            if result != "ok":
                raise RuntimeError(
                    f"sqlite backup of {src_path} -> {dest_path} failed integrity_check: {result}"
                )
        finally:
            dest.close()
    finally:
        src.close()


def _copy_db(live_path: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(os.path.dirname(live_path), f"trades_migration_copy_{ts}.db")
    _sqlite_backup(live_path, dest)
    return dest


def _backup_live_db(live_path: str) -> str:
    """Unconditional pre-apply backup — separate from _copy_db's dry-run
    working copy. Named distinctly (trades_pre_migration_backup_*, not
    trades_migration_copy_*) so it's unambiguous which files are safe-to-
    delete dry-run scratch copies and which are the one real restore point
    for an --apply-to-live run. Restore: copy this file back over
    logs/trades.db."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(
        os.path.dirname(live_path), f"trades_pre_migration_backup_{ts}.db",
    )
    _sqlite_backup(live_path, dest)
    return dest


def _fetch_full_history(adapter: KrakenAccountingAdapter) -> list:
    """Full-account-history paginated pull with a coverage proof — item 8's
    "migrate historical fills" precondition. Raises if coverage cannot be
    proven complete rather than migrating against a possibly-truncated set."""
    coverage = engine.retrieve_with_coverage_proof(adapter, None, since=None)
    if not coverage.complete:
        raise RuntimeError(
            f"full-history retrieval was NOT provably complete ({coverage.reason}) — "
            f"refusing to migrate against a possibly-truncated trade set"
        )
    return coverage.trades


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-to-live", action="store_true",
                         help="Operate on the REAL logs/trades.db instead of a copy. "
                              "Only use after reviewing a dry-run report.")
    parser.add_argument("--symbols", default=",".join(_SYMBOLS))
    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    api_key = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")
    if not api_key or not api_secret:
        print("FATAL: KRAKEN_API_KEY/KRAKEN_API_SECRET not set", file=sys.stderr)
        return 1

    ex = ccxt.kraken({"apiKey": api_key, "secret": api_secret})
    ex.load_markets()
    adapter = KrakenAccountingAdapter(ex)

    print("Fetching full account trade history (paginated, coverage-proven)...")
    try:
        all_trades = _fetch_full_history(adapter)
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    print(f"  {len(all_trades)} real trades retrieved across the whole account.")

    backup_path = None
    if args.apply_to_live:
        backup_path = _backup_live_db(_LIVE_DB)
        print(f"Backed up live DB to: {backup_path}  (restore by copying it back over trades.db)")
        db_path = _LIVE_DB
        print(f"OPERATING ON THE LIVE DATABASE: {db_path}")
    else:
        db_path = _copy_db(_LIVE_DB)
        print(f"Operating on a COPY: {db_path} (live trades.db untouched)")

    report = migration.run_migration(db_path, all_trades, symbols=symbols)

    print("\n=== Migration report ===")
    print(report.summary)
    print(
        "Matching is exact-conservation only (quantity+fee sum, tolerance 1e-6) — "
        "never a nearest-timestamp guess. Every entry below is either an unambiguous "
        "exact match or a block; nothing approximate is ever auto-linked."
    )
    if report.blocked:
        print("\nBLOCKED — needs manual review (no subset matched, or ambiguous):")
        for r in report.blocked:
            candidates = ", ".join(r.candidate_trade_ids) or "(none in window)"
            print(f"  fills.id={r.fill_id}: {r.reason}")
            print(f"    candidate trade id(s) considered: {candidates}")
    if report.orphan_trades_inserted:
        print(f"\nOrphan trades backfilled (no prior fills row existed): {len(report.orphan_trades_inserted)}")
        for tid in report.orphan_trades_inserted:
            print(f"  {tid}")
    if report.linked:
        print(f"\nLinked {len(report.linked)} existing fills row(s) to their real trade id(s) — review each:")
        for r in report.linked:
            print(f"  fills.id={r.fill_id}  <-  trade id(s): {', '.join(r.matched_trade_ids)}")

    if args.apply_to_live and report.blocked:
        print(
            f"\nNOTE: {len(report.blocked)} row(s) remain BLOCKED even after this live apply — "
            "those fills are still unlinked and need manual resolution (they were "
            "deliberately not guessed at). Nothing exact-and-unambiguous was skipped."
        )

    print(
        "\nlogs/HALT untouched. No order placed. "
        f"{'LIVE DB was modified' if args.apply_to_live else 'Live trades.db was NOT modified — copy only'}"
        f"{f'. Pre-migration backup: {backup_path}' if backup_path else '.'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
