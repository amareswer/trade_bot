"""
CLI wrapper for bot/accounting/migration.py (execution-accounting design
item 8). Operates on a COPY of trades.db by default — never the live
database — and prints a report for human review before anyone points this
at the real file.

Usage:
    .venv/bin/python migrate_legacy_fills.py                  # dry run on a DB copy
    .venv/bin/python migrate_legacy_fills.py --apply-to-live   # explicit, separate step

Does NOT place any order, touch logs/HALT, or import bot/execution or
bot/main. Requires KRAKEN_API_KEY/KRAKEN_API_SECRET (read-only trade/
balance history calls only — Query Funds / Query Orders capability, no
Create/Cancel Orders needed for this script).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone

import ccxt
from dotenv import load_dotenv

load_dotenv()

from bot.accounting import engine, migration
from bot.accounting.kraken_adapter import KrakenAccountingAdapter

_LIVE_DB = os.path.join(os.path.dirname(__file__), "logs", "trades.db")
_SYMBOLS = ["BTC/CAD", "SOL/CAD"]  # matches CLAUDE.md's UNIVERSE_WHITELIST at time of writing


def _copy_db(live_path: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(os.path.dirname(live_path), f"trades_migration_copy_{ts}.db")
    shutil.copyfile(live_path, dest)
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

    if args.apply_to_live:
        db_path = _LIVE_DB
        print(f"OPERATING ON THE LIVE DATABASE: {db_path}")
    else:
        db_path = _copy_db(_LIVE_DB)
        print(f"Operating on a COPY: {db_path} (live trades.db untouched)")

    report = migration.run_migration(db_path, all_trades, symbols=symbols)

    print("\n=== Migration report ===")
    print(report.summary)
    if report.blocked:
        print("\nBLOCKED — needs manual review (no subset matched, or ambiguous):")
        for r in report.blocked:
            print(f"  fills.id={r.fill_id}: {r.reason}")
    if report.orphan_trades_inserted:
        print(f"\nOrphan trades backfilled (no prior fills row existed): {len(report.orphan_trades_inserted)}")
        for tid in report.orphan_trades_inserted:
            print(f"  {tid}")
    if report.linked:
        print(f"\nLinked {len(report.linked)} existing fills row(s) to their real trade id(s).")

    print(
        "\nlogs/HALT untouched. No order placed. "
        f"{'LIVE DB was modified.' if args.apply_to_live else 'Live trades.db was NOT modified — copy only.'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
