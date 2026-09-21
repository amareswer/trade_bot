"""
Read-only accounting shadow-mode inspection.

Reports unlinked fills (residuals) and fee corrections from the trades.db
directly, and — the authoritative readiness signal — the LATEST persisted
reconciliation-cycle outcome (bot/accounting/cycle_status.py), written by
bot/main.py after every cycle attempt (success or failure). Makes NO
network call, places NO order, mutates NOTHING.

History (accounting review, 2026-09-21):
- P2, first pass: the original version called store.init_db() (a real
  write) and declared "PASS" from zero unlinked fills alone. Fixed: a
  genuine read-only SQLite connection; a verdict based on checkpoint
  existence/freshness.
- P1, sixth pass: checkpoint existence/freshness was STILL not enough — a
  fresh account-wide cash checkpoint could exist with no completed
  four-way verification and no per-symbol evidence, and an earlier
  success survives a later failure unchanged. Fixed: the verdict moved
  entirely to cycle_status.py's persisted outcome record, overwritten on
  every cycle attempt.
- P1, seventh pass, two more gaps in that same mechanism:
  (a) "a failed status write preserves an earlier PASSED result" — if
      reconciliation failed and PERSISTING that failure then also raised,
      the file was left holding the PREVIOUS successful write, which
      could still read as fresh and PASSED. Fixed in cycle_status.py
      itself: an in-progress marker is written before every attempt,
      unconditionally invalidating the prior result the instant a new
      attempt begins, so a failure partway through leaves in_progress=True
      on disk, never a stale success.
  (b) "status evidence is not tied to the inspected database" — --db and
      --status were independently settable, so an unrelated or empty
      database paired with a fresh, successful status file for matching
      symbol names still reported PASSED; overriding --db alone silently
      kept pointing --status at the REAL production file. Fixed: --status
      no longer exists as a separate argument — the status path is always
      derived from --db's own directory, AND the status file's db_identity
      field (bot/accounting/store.get_or_create_db_identity) is checked
      against the identity persisted IN that same database — a status
      record that doesn't actually correspond to the inspected database
      (however it got there) is rejected, not trusted by proximity alone.

Usage:
    .venv/bin/python scripts/accounting_shadow_report.py [SYMBOL ...]
    .venv/bin/python scripts/accounting_shadow_report.py --db logs/shadow/trades.db [SYMBOL ...]

With no SYMBOL args, uses UNIVERSE_WHITELIST from .env (comma-separated),
falling back to the single configured SYMBOL. --db defaults to the REAL
logs/trades.db — pass logs/shadow/trades.db explicitly for a shadow
session; the cycle-status file living alongside it is found automatically.
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


def _status_path_for_db(db_path: str) -> str:
    """The cycle-status file always lives alongside its database — deriving
    it this way (rather than accepting an independent --status argument)
    is what makes a --db/--status MISMATCH structurally impossible instead
    of merely discouraged."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "accounting_cycle_status.json")


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
                              "pass logs/shadow/trades.db explicitly for a shadow session). The "
                              "cycle-status file is always read from the SAME directory — there is "
                              "no separate --status argument, to make a --db/--status mismatch "
                              "structurally impossible.")
    args = parser.parse_args()
    status_path = _status_path_for_db(args.db)

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
    print(f"Cycle status: {status_path}")
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

    db_identity = accounting_store.read_db_identity(conn)   # pure SELECT — never creates one
    conn.close()

    # ── The authoritative verdict: the persisted last-cycle outcome ──────
    print("\n" + "=" * 70)
    status = cycle_status.read(status_path)
    max_age_s = cfg.accounting.reconcile_interval_s + cfg.accounting.stale_grace_s

    if status is None:
        verdict, exit_code = "NOT_VERIFIED", 4
        print("NOT_VERIFIED: no reconciliation cycle has EVER completed at this status path — "
              "zero unlinked fills above proves nothing by itself; there may simply have been "
              "no cycle to produce a real result yet.")
    elif status.in_progress:
        verdict, exit_code = "NOT_VERIFIED", 4
        print(f"NOT_VERIFIED: a reconciliation cycle attempt started at {status.computed_at} and "
              f"never confirmed completion (in_progress=True) — either it's still running, or it "
              f"crashed/raised before persisting a real outcome. Never read as a pass.")
    elif db_identity is None:
        verdict, exit_code = "NOT_VERIFIED", 4
        print("NOT_VERIFIED: this database has no db_identity recorded yet — it has never been "
              "used by a real reconciliation cycle (get_or_create_db_identity establishes this "
              "on first use), so no persisted status could genuinely correspond to it.")
    elif status.db_identity != db_identity:
        verdict, exit_code = "NOT_VERIFIED", 4
        print(f"NOT_VERIFIED: the cycle-status file's db_identity ({status.db_identity!r}) does "
              f"not match this database's own identity ({db_identity!r}) — this status record "
              f"does not genuinely correspond to the database being inspected, however it ended "
              f"up alongside it. Never trusted by directory proximity alone.")
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
                  f"{status.requested_symbols} against this exact database (db_identity confirmed), "
                  f"exchange-data reconciled, four-way verification ready, and zero unlinked fills.")

    print(f"\nVerdict: {verdict}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
