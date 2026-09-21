"""
Read-only accounting shadow-mode inspection.

Reports unlinked fills (residuals) and fee corrections from the trades.db
directly, and — the authoritative readiness signal — the LATEST persisted
reconciliation-cycle outcome (bot/accounting/cycle_status.py), written by
bot/main.py after every cycle attempt (success or failure). Makes NO
network call, places NO order, mutates NOTHING.

History (accounting review, 2026-09-21):
- P2, first pass: the original version called store.init_db() (a real
  write — CREATEs the schema if missing) and declared "PASS" from zero
  unlinked fills alone, which a database with NO reconciliation cycle ever
  run also satisfies trivially. Fixed then: a genuine read-only SQLite
  connection, and a verdict based on checkpoint existence/freshness.
- P1, sixth pass: checkpoint existence/freshness was STILL not enough —
  reproduced exactly: a database with a fresh account-wide CAD cash
  checkpoint, but NO completed four-way verification and no checkpoint
  for a REQUESTED symbol's own scope, reported PASSED. Checkpoints can
  also survive a LATER failed cycle completely unchanged (an earlier
  success doesn't get retracted). Fixed by moving the source of truth
  entirely to cycle_status.py's persisted outcome record, which
  bot/main.py overwrites on every single cycle attempt with what THAT
  attempt actually concluded — there is no longer any "silence is
  evidence" step in this script; an absent or stale status file is
  reported as exactly that, never inferred as healthy.

Usage:
    .venv/bin/python scripts/accounting_shadow_report.py [SYMBOL ...]
    .venv/bin/python scripts/accounting_shadow_report.py --db logs/shadow/trades.db --status logs/shadow/accounting_cycle_status.json [SYMBOL ...]

With no SYMBOL args, uses UNIVERSE_WHITELIST from .env (comma-separated),
falling back to the single configured SYMBOL. --db/--status default to the
REAL logs/ paths — pass the logs/shadow/ equivalents explicitly to check a
shadow session (see bot/main.py's _STATE_LOG_DIR / deploy/PAPER_SHADOW_RUNBOOK.md).
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from bot.accounting import cycle_status  # noqa: E402
from bot.accounting import store as accounting_store  # noqa: E402
from config import cfg  # noqa: E402

load_dotenv()

_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "logs", "trades.db")
_DEFAULT_STATUS = os.path.join(_PROJECT_ROOT, "logs", "accounting_cycle_status.json")


def _symbols_from_args_or_env(explicit: "list[str]") -> "list[str]":
    if explicit:
        return explicit
    whitelist = os.getenv("UNIVERSE_WHITELIST", "")
    if whitelist.strip():
        return [s.strip() for s in whitelist.split(",") if s.strip()]
    sym = os.getenv("SYMBOL", "")
    return [sym] if sym else []


def _readonly_connect(db_path: str) -> sqlite3.Connection:
    """Opens SQLite in its own read-only URI mode — mode=ro raises
    OperationalError immediately for a missing file or one with no schema,
    rather than accounting_store.init_db()/connect(), which would silently
    CREATE the accounting tables in whatever file happened to be at this
    path. A read-only inspection tool must never be able to bring a
    database into existence."""
    abs_path = os.path.abspath(db_path)
    uri = f"file:{abs_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _status_age_s(status: "cycle_status.CycleStatus") -> "float | None":
    try:
        computed_dt = datetime.fromisoformat(status.computed_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - computed_dt).total_seconds()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", help="Symbols to check (default: UNIVERSE_WHITELIST/SYMBOL from .env)")
    parser.add_argument("--db", default=_DEFAULT_DB,
                         help="Path to the trades.db to inspect (default: the REAL logs/trades.db — "
                              "pass logs/shadow/trades.db explicitly for a shadow session)")
    parser.add_argument("--status", default=_DEFAULT_STATUS,
                         help="Path to the persisted cycle-status JSON (default: the REAL "
                              "logs/accounting_cycle_status.json — pass logs/shadow/accounting_cycle_status.json "
                              "explicitly for a shadow session)")
    args = parser.parse_args()

    symbols = _symbols_from_args_or_env(args.symbols)
    if not symbols:
        print("ERROR: no symbols found — pass them as args or set UNIVERSE_WHITELIST/SYMBOL in .env")
        return 1

    try:
        conn = _readonly_connect(args.db)
        conn.execute("SELECT 1 FROM observed_trades LIMIT 1")
    except sqlite3.Error as exc:
        print(f"ERROR: cannot open {args.db!r} read-only, or it has no accounting schema yet ({exc}).")
        print("This is NOT_VERIFIED, not a pass — no reconciliation cycle can have run against "
              "a database this tool cannot even read.")
        return 3

    print("Accounting shadow report (read-only, local SQLite only — no network call, no writes)")
    print(f"Database: {os.path.abspath(args.db)}")
    print(f"Cycle status: {os.path.abspath(args.status)}")
    print("=" * 70)

    total_unlinked = 0
    for sym in symbols:
        unlinked = accounting_store.unlinked_fills(conn, sym)
        total_unlinked += len(unlinked)
        print(f"\n[{sym}]")
        print(f"  Unlinked fills (residuals): {len(unlinked)}")
        for row in unlinked[:10]:
            print(f"    id={row.get('id')}  ts={row.get('timestamp')}  "
                  f"side={row.get('side')}  qty={row.get('quantity')}  "
                  f"notes={row.get('notes')!r}")
        if len(unlinked) > 10:
            print(f"    ... and {len(unlinked) - 10} more")

    fee_corrections = accounting_store.all_fee_correction_adjustment_ids(conn)
    print(f"\nFee corrections applied (all-time, all symbols): {len(fee_corrections)}")
    conn.close()

    # ── The authoritative verdict: the persisted last-cycle outcome ──────
    print("\n" + "=" * 70)
    status = cycle_status.read(args.status)
    max_age_s = cfg.accounting.reconcile_interval_s + cfg.accounting.stale_grace_s

    if status is None:
        verdict, exit_code = "NOT_VERIFIED", 4
        print("NOT_VERIFIED: no reconciliation cycle has EVER completed at this status path — "
              "zero unlinked fills above proves nothing by itself; there may simply have been "
              "no cycle to produce a real result yet.")
    else:
        age_s = _status_age_s(status)
        missing_symbols = sorted(set(symbols) - set(status.requested_symbols))
        if age_s is None:
            verdict, exit_code = "NOT_VERIFIED", 4
            print("NOT_VERIFIED: the persisted cycle-status file has an unparseable timestamp.")
        elif age_s > max_age_s:
            verdict, exit_code = "STALE", 5
            print(f"STALE: last cycle outcome is {age_s:.0f}s old (max {max_age_s:.0f}s = "
                  f"reconcile_interval_s + stale_grace_s) — the live bot's own BlockState.is_stale "
                  f"would no longer treat this as authorizing BUYs either.")
        elif missing_symbols:
            verdict, exit_code = "NOT_VERIFIED", 4
            print(f"NOT_VERIFIED: the latest cycle did not cover {missing_symbols} — it was requested "
                  f"for {status.requested_symbols}. A fresh, successful cycle for a DIFFERENT symbol "
                  f"set says nothing about these.")
        elif not status.block_state_ok:
            verdict, exit_code = "FAILED", 2
            print(f"FAILED: last cycle's exchange-data reconciliation itself failed: "
                  f"{status.block_state_explain}")
        elif not status.four_way_ran:
            verdict, exit_code = "FAILED", 2
            print("FAILED: last cycle's exchange-data reconciliation passed, but four-way "
                  "verification never ran this cycle (see bot/main.py — it only runs once "
                  "block_state.reconciled is True; something else must be blocking first).")
        elif not status.four_way_ready:
            verdict, exit_code = "FAILED", 2
            print(f"FAILED: four-way verification did not pass: {status.four_way_explain}")
        elif total_unlinked > 0:
            verdict, exit_code = "FAILED", 2
            print(f"FAILED: {total_unlinked} unlinked fill(s) alongside an otherwise-passing "
                  f"cycle — a real, currently-relevant residual.")
        else:
            verdict, exit_code = "PASSED", 0
            print(f"PASSED: last cycle ({status.computed_at}, {age_s:.0f}s ago) covered exactly "
                  f"{status.requested_symbols}, exchange-data reconciled, four-way verification "
                  f"ready, and zero unlinked fills.")

    print(f"\nVerdict: {verdict}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
