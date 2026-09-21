"""
Tests for scripts/accounting_shadow_report.py.

Sixth-pass finding (2026-09-21, P1): checkpoint existence/freshness alone
was not enough evidence — the verdict now comes entirely from
bot/accounting/cycle_status.py's persisted last-cycle outcome.

Seventh-pass findings (2026-09-21, P1), both covered explicitly here:
1. "A failed status write preserves an earlier PASSED result" — proven at
   the cycle_status.py level (test_accounting_cycle_status.py); here we
   prove the REPORT's own reaction to in_progress=True.
2. "Status evidence is not tied to the inspected database" — --status no
   longer exists as an independent argument (removed entirely — the path
   is always derived from --db's directory), and a status file's
   db_identity must match the database's own persisted identity or the
   report refuses to call it PASSED, however the mismatch happened.
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "scripts"))
sys.path.insert(0, _PROJECT_ROOT)

import accounting_shadow_report as report  # noqa: E402
from bot.accounting import cycle_status, store  # noqa: E402
from bot.data.trade_log import TradeLog  # noqa: E402
from config import cfg  # noqa: E402


def _prep_db(tmp_path) -> "tuple[str, str]":
    """Real db + fills table + an established db_identity, matching what a
    real reconciliation cycle would have produced. Returns (db_path, identity)."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    TradeLog(db_path=db_path)   # creates the `fills` table this script also queries
    conn = store.connect(db_path)
    identity = store.get_or_create_db_identity(conn)
    conn.close()
    return db_path, identity


def _run(db_path: str, symbols: "list[str]") -> "tuple[int, str]":
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["accounting_shadow_report.py", "--db", db_path, *symbols]
        with redirect_stdout(buf):
            exit_code = report.main()
    finally:
        sys.argv = old_argv
    return exit_code, buf.getvalue()


def _write_status(db_path: str, identity: str, **kwargs) -> None:
    status_path = report._status_path_for_db(db_path)
    cycle_status.write(status_path, db_identity=identity, **kwargs)


# ============================================================================
# Read-only enforcement
# ============================================================================

def test_readonly_connect_never_creates_a_missing_database(tmp_path):
    missing = str(tmp_path / "does_not_exist.db")
    assert not os.path.exists(missing)
    try:
        conn = report._readonly_connect(missing)
        conn.execute("SELECT 1 FROM observed_trades LIMIT 1")
        assert False, "expected a read-only-open failure against a missing database"
    except sqlite3.Error:
        pass
    assert not os.path.exists(missing)


def test_main_reports_not_verified_on_missing_db(tmp_path):
    missing_db = str(tmp_path / "trades.db")
    exit_code, out = _run(missing_db, ["BTC/CAD"])
    assert exit_code == 3
    assert not os.path.exists(missing_db)
    assert "NOT_VERIFIED" in out


# ============================================================================
# No --status argument — the path is always derived from --db
# ============================================================================

def test_status_path_is_derived_from_db_directory(tmp_path):
    db_path = str(tmp_path / "sub" / "trades.db")
    expected = str(tmp_path / "sub" / "accounting_cycle_status.json")
    assert report._status_path_for_db(db_path) == expected


def test_status_cli_argument_no_longer_exists(tmp_path):
    """The seventh-pass fix for finding 2 removed --status entirely — a
    caller can no longer point it at an unrelated file even by accident."""
    import argparse
    db_path, _ = _prep_db(tmp_path)
    old_argv = sys.argv
    try:
        sys.argv = ["accounting_shadow_report.py", "--db", db_path, "--status", "/tmp/somewhere.json"]
        try:
            report.main()
            assert False, "expected argparse to reject the removed --status flag"
        except SystemExit:
            pass
    finally:
        sys.argv = old_argv


# ============================================================================
# db_identity validation (seventh pass, P1, finding 2)
# ============================================================================

def test_database_with_no_identity_yet_is_not_verified(tmp_path):
    """A database no real reconciliation cycle has ever touched has no
    db_identity row at all — even a status file sitting alongside it (by
    whatever means) cannot be trusted to correspond to it."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    TradeLog(db_path=db_path)
    # No get_or_create_db_identity() call — this db has never been used by
    # a real cycle.
    status_path = report._status_path_for_db(db_path)
    cycle_status.write(
        status_path, requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok", db_identity="whatever",
    )
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 4
    assert "NOT_VERIFIED" in out
    assert "PASSED" not in out


def test_mismatched_db_identity_is_not_verified_not_passed(tmp_path):
    """The exact reproduction: an (effectively) unrelated database paired
    with a fresh, otherwise-fully-passing status for matching symbol names
    must not report PASSED — the identities don't match."""
    db_path, real_identity = _prep_db(tmp_path)
    assert real_identity  # sanity
    _write_status(
        db_path, "a-completely-different-identity",
        requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 4
    assert "NOT_VERIFIED" in out
    assert "PASSED" not in out


def test_matching_db_identity_can_pass(tmp_path):
    db_path, identity = _prep_db(tmp_path)
    _write_status(
        db_path, identity,
        requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 0
    assert "PASSED" in out


# ============================================================================
# in_progress / interrupted publication (seventh pass, P1, finding 1)
# ============================================================================

def test_in_progress_status_is_not_verified_even_after_a_prior_pass(tmp_path):
    """Interrupted-publication reproduction: a fully passing cycle
    persisted successfully, then a NEW cycle attempt started (writing the
    in-progress marker) and never completed (simulated crash). The report
    must show NOT_VERIFIED, never the old PASSED result."""
    db_path, identity = _prep_db(tmp_path)
    _write_status(
        db_path, identity,
        requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 0 and "PASSED" in out   # sanity: it really was passing

    status_path = report._status_path_for_db(db_path)
    cycle_status.write_in_progress(status_path, requested_symbols=["BTC/CAD"], db_identity=identity)

    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 4
    assert "NOT_VERIFIED" in out
    assert "PASSED" not in out


# ============================================================================
# The rest of the verdict chain (staleness / coverage / failures) —
# unchanged in spirit from the sixth pass, re-verified against the
# now-identity-checked interface
# ============================================================================

def test_no_cycle_status_file_is_not_verified_even_with_zero_unlinked(tmp_path):
    db_path, _ = _prep_db(tmp_path)   # status file never written
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 4
    assert "NOT_VERIFIED" in out
    assert "PASSED" not in out


def test_stale_cycle_status_reports_stale(tmp_path):
    db_path, identity = _prep_db(tmp_path)
    _write_status(
        db_path, identity,
        requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    import json
    status_path = report._status_path_for_db(db_path)
    with open(status_path) as f:
        data = json.load(f)
    stale_at = datetime.now(timezone.utc) - timedelta(
        seconds=cfg.accounting.reconcile_interval_s + cfg.accounting.stale_grace_s + 3600)
    data["computed_at"] = stale_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(status_path, "w") as f:
        json.dump(data, f)

    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 5
    assert "STALE" in out


def test_cycle_status_for_a_different_symbol_set_is_not_verified(tmp_path):
    db_path, identity = _prep_db(tmp_path)
    _write_status(
        db_path, identity,
        requested_symbols=["SOL/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 4
    assert "NOT_VERIFIED" in out
    assert "PASSED" not in out


def test_block_state_failure_is_failed_not_passed(tmp_path):
    db_path, identity = _prep_db(tmp_path)
    _write_status(
        db_path, identity,
        requested_symbols=["BTC/CAD"], block_state_ok=False,
        block_state_explain="account cash unreconciled", four_way_ran=False,
    )
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 2
    assert "FAILED" in out


def test_four_way_never_ran_is_failed_not_passed(tmp_path):
    db_path, identity = _prep_db(tmp_path)
    _write_status(
        db_path, identity,
        requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=False,
    )
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 2
    assert "FAILED" in out


def test_four_way_not_ready_is_failed_not_passed(tmp_path):
    db_path, identity = _prep_db(tmp_path)
    _write_status(
        db_path, identity,
        requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=False, four_way_explain="position-fold [BTC/CAD]: qty diff",
    )
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 2
    assert "FAILED" in out


def test_fresh_passing_status_with_unlinked_fill_is_failed(tmp_path):
    db_path, identity = _prep_db(tmp_path)
    tl = TradeLog(db_path=db_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-unlinked")
    _write_status(
        db_path, identity,
        requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 2
    assert "FAILED" in out
    assert "NOT_VERIFIED" not in out


def test_requesting_a_subset_of_the_cycles_symbols_still_passes(tmp_path):
    db_path, identity = _prep_db(tmp_path)
    _write_status(
        db_path, identity,
        requested_symbols=["BTC/CAD", "SOL/CAD"], block_state_ok=True,
        block_state_explain="ok", four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 0
    assert "PASSED" in out


# ============================================================================
# --pid process-liveness check (eighth pass, 2026-09-21, P1: "ensure the
# acceptance runner checks process failure as well as the status file")
# ============================================================================

def test_process_alive_true_for_current_process():
    assert report._process_alive(os.getpid()) is True


def test_process_alive_false_for_a_pid_that_does_not_exist():
    # A PID far beyond any real process table entry, extremely unlikely to
    # collide with a real running process during the test.
    assert report._process_alive(2**30) is False


def test_stopped_process_overrides_a_passing_status_file(tmp_path):
    """The exact property this check exists for: even a fresh, fully
    passing status file must not report PASSED if the process it belongs
    to has already stopped — proven by pairing a real PASSED status with
    a deliberately-nonexistent pid."""
    db_path, identity = _prep_db(tmp_path)
    _write_status(
        db_path, identity,
        requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    # Sanity: without --pid, this genuinely passes.
    exit_code, out = _run(db_path, ["BTC/CAD"])
    assert exit_code == 0 and "PASSED" in out

    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["accounting_shadow_report.py", "--db", db_path, "--pid", str(2**30), "BTC/CAD"]
        with redirect_stdout(buf):
            exit_code = report.main()
    finally:
        sys.argv = old_argv
    out = buf.getvalue()
    assert exit_code == 6
    assert "PROCESS_STOPPED" in out
    assert "Verdict: PROCESS_STOPPED" in out


def test_alive_process_does_not_change_a_passing_verdict(tmp_path):
    db_path, identity = _prep_db(tmp_path)
    _write_status(
        db_path, identity,
        requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["accounting_shadow_report.py", "--db", db_path, "--pid", str(os.getpid()), "BTC/CAD"]
        with redirect_stdout(buf):
            exit_code = report.main()
    finally:
        sys.argv = old_argv
    out = buf.getvalue()
    assert exit_code == 0
    assert "PASSED" in out
