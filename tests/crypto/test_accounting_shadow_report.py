"""
Tests for scripts/accounting_shadow_report.py.

Sixth-pass finding (2026-09-21, P1): checkpoint existence/freshness alone
was still not enough evidence — a fresh account-wide CAD cash checkpoint
could exist while a REQUESTED symbol's own reconciliation never completed,
or a four-way verification never ran, or an earlier successful checkpoint
simply survived a LATER failed cycle unchanged. The script's verdict now
comes entirely from bot/accounting/cycle_status.py's persisted last-cycle
outcome — every test here proves a specific way that outcome record can
be missing, stale, symbol-mismatched, or failing, and that none of those
can produce PASSED.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "scripts"))
sys.path.insert(0, _PROJECT_ROOT)

import accounting_shadow_report as report  # noqa: E402
from bot.accounting import cycle_status, store  # noqa: E402
from bot.data.trade_log import TradeLog  # noqa: E402
from config import cfg  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(db_path: str, status_path: str, symbols: "list[str]") -> "tuple[int, str]":
    import io
    from contextlib import redirect_stdout
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["accounting_shadow_report.py", "--db", db_path, "--status", status_path, *symbols]
        with redirect_stdout(buf):
            exit_code = report.main()
    finally:
        sys.argv = old_argv
    return exit_code, buf.getvalue()


def _prep_db(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    TradeLog(db_path=db_path)   # creates the `fills` table this script also queries
    return db_path


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
    status_path = str(tmp_path / "status.json")
    exit_code, out = _run(missing_db, status_path, ["BTC/CAD"])
    assert exit_code == 3
    assert not os.path.exists(missing_db)
    assert "NOT_VERIFIED" in out


# ============================================================================
# Cycle-status-driven verdict
# ============================================================================

def test_no_cycle_status_file_is_not_verified_even_with_zero_unlinked(tmp_path):
    """The exact 'silence is not evidence' case: a clean, empty database
    with no persisted cycle outcome at all must not be reported PASSED."""
    db_path = _prep_db(tmp_path)
    status_path = str(tmp_path / "status.json")   # never written
    exit_code, out = _run(db_path, status_path, ["BTC/CAD"])
    assert exit_code == 4
    assert "NOT_VERIFIED" in out
    assert "PASSED" not in out


def test_stale_cycle_status_reports_stale(tmp_path):
    db_path = _prep_db(tmp_path)
    status_path = str(tmp_path / "status.json")
    cycle_status.write(
        status_path, requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    # Backdate it past the staleness window — cycle_status.write always
    # stamps "now", so age it directly the same way the four_way tests do.
    import json
    with open(status_path) as f:
        data = json.load(f)
    stale_at = datetime.now(timezone.utc) - timedelta(
        seconds=cfg.accounting.reconcile_interval_s + cfg.accounting.stale_grace_s + 3600)
    data["computed_at"] = stale_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(status_path, "w") as f:
        json.dump(data, f)

    exit_code, out = _run(db_path, status_path, ["BTC/CAD"])
    assert exit_code == 5
    assert "STALE" in out


def test_cycle_status_for_a_different_symbol_set_is_not_verified(tmp_path):
    """A fresh, fully-passing cycle for SOL/CAD alone says nothing about
    BTC/CAD — the exact 'account-wide checkpoint, no per-symbol evidence'
    shape from the sixth-pass finding."""
    db_path = _prep_db(tmp_path)
    status_path = str(tmp_path / "status.json")
    cycle_status.write(
        status_path, requested_symbols=["SOL/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    exit_code, out = _run(db_path, status_path, ["BTC/CAD"])
    assert exit_code == 4
    assert "NOT_VERIFIED" in out
    assert "PASSED" not in out


def test_block_state_failure_is_failed_not_passed(tmp_path):
    db_path = _prep_db(tmp_path)
    status_path = str(tmp_path / "status.json")
    cycle_status.write(
        status_path, requested_symbols=["BTC/CAD"], block_state_ok=False,
        block_state_explain="account cash unreconciled", four_way_ran=False,
    )
    exit_code, out = _run(db_path, status_path, ["BTC/CAD"])
    assert exit_code == 2
    assert "FAILED" in out


def test_four_way_never_ran_is_failed_not_passed(tmp_path):
    """block_state_ok=True but four_way_ran=False must not be read as
    'not applicable, therefore fine' — see cycle_status.CycleStatus.ready's
    own docstring for why this is a real failure, not a skip."""
    db_path = _prep_db(tmp_path)
    status_path = str(tmp_path / "status.json")
    cycle_status.write(
        status_path, requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=False,
    )
    exit_code, out = _run(db_path, status_path, ["BTC/CAD"])
    assert exit_code == 2
    assert "FAILED" in out


def test_four_way_not_ready_is_failed_not_passed(tmp_path):
    db_path = _prep_db(tmp_path)
    status_path = str(tmp_path / "status.json")
    cycle_status.write(
        status_path, requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=False, four_way_explain="position-fold [BTC/CAD]: qty diff",
    )
    exit_code, out = _run(db_path, status_path, ["BTC/CAD"])
    assert exit_code == 2
    assert "FAILED" in out


def test_fresh_fully_passing_cycle_status_with_zero_unlinked_is_passed(tmp_path):
    db_path = _prep_db(tmp_path)
    status_path = str(tmp_path / "status.json")
    cycle_status.write(
        status_path, requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    exit_code, out = _run(db_path, status_path, ["BTC/CAD"])
    assert exit_code == 0
    assert "PASSED" in out


def test_fresh_passing_cycle_status_with_unlinked_fill_is_failed(tmp_path):
    """A REAL, currently-relevant residual on top of an otherwise-passing
    cycle must still be FAILED, not PASSED."""
    db_path = _prep_db(tmp_path)
    tl = TradeLog(db_path=db_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-unlinked")
    status_path = str(tmp_path / "status.json")
    cycle_status.write(
        status_path, requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    exit_code, out = _run(db_path, status_path, ["BTC/CAD"])
    assert exit_code == 2
    assert "FAILED" in out
    assert "NOT_VERIFIED" not in out


def test_requesting_a_subset_of_the_cycles_symbols_still_passes(tmp_path):
    """Checking fewer symbols than the last cycle actually covered is
    fine — the missing-coverage check is one-directional (every symbol
    THIS run asks about must be in the cycle's own requested set)."""
    db_path = _prep_db(tmp_path)
    status_path = str(tmp_path / "status.json")
    cycle_status.write(
        status_path, requested_symbols=["BTC/CAD", "SOL/CAD"], block_state_ok=True,
        block_state_explain="ok", four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    exit_code, out = _run(db_path, status_path, ["BTC/CAD"])
    assert exit_code == 0
    assert "PASSED" in out
