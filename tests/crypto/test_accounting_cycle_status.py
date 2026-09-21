"""
Tests for bot/accounting/cycle_status.py — the persisted last-cycle-outcome
record scripts/accounting_shadow_report.py's verdict is built on
(accounting review, sixth + seventh passes, 2026-09-21, P1).
"""
from __future__ import annotations

from bot.accounting import cycle_status


def test_read_missing_file_returns_none(tmp_path):
    assert cycle_status.read(str(tmp_path / "does_not_exist.json")) is None


def test_write_then_read_roundtrips(tmp_path):
    path = str(tmp_path / "status.json")
    cycle_status.write(
        path, requested_symbols=["SOL/CAD", "BTC/CAD"], block_state_ok=True,
        block_state_explain="ok", four_way_ran=True, four_way_ready=True, four_way_explain="ok",
        db_identity="db-abc",
    )
    status = cycle_status.read(path)
    assert status is not None
    assert status.requested_symbols == ["BTC/CAD", "SOL/CAD"]   # sorted on write
    assert status.block_state_ok is True
    assert status.four_way_ran is True
    assert status.four_way_ready is True
    assert status.db_identity == "db-abc"
    assert status.in_progress is False
    assert status.ready is True


def test_ready_false_when_block_state_not_ok():
    status = cycle_status.CycleStatus(
        computed_at="2026-01-01T00:00:00Z", requested_symbols=["BTC/CAD"],
        block_state_ok=False, block_state_explain="account cash unreconciled",
        four_way_ran=False, four_way_ready=None, four_way_explain=None,
    )
    assert status.ready is False


def test_ready_false_when_four_way_never_ran_even_if_block_state_ok():
    """block_state_ok=True but four_way_ran=False is a real failure, not a
    skip — the four-way stage simply never got reached this cycle."""
    status = cycle_status.CycleStatus(
        computed_at="2026-01-01T00:00:00Z", requested_symbols=["BTC/CAD"],
        block_state_ok=True, block_state_explain="ok",
        four_way_ran=False, four_way_ready=None, four_way_explain=None,
    )
    assert status.ready is False


def test_ready_false_when_four_way_ran_but_not_ready():
    status = cycle_status.CycleStatus(
        computed_at="2026-01-01T00:00:00Z", requested_symbols=["BTC/CAD"],
        block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=False, four_way_explain="position-fold [BTC/CAD]: ...",
    )
    assert status.ready is False


def test_a_later_write_fully_overwrites_an_earlier_one(tmp_path):
    """Every cycle attempt overwrites the file with ITS OWN outcome — a
    later failure must not leave an earlier success's fields lingering."""
    path = str(tmp_path / "status.json")
    cycle_status.write(
        path, requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok",
    )
    cycle_status.write(
        path, requested_symbols=["BTC/CAD"], block_state_ok=False,
        block_state_explain="account cash unreconciled", four_way_ran=False,
    )
    status = cycle_status.read(path)
    assert status.block_state_ok is False
    assert status.four_way_ran is False
    assert status.four_way_ready is None
    assert status.ready is False


# ============================================================================
# Two-phase write (seventh pass, 2026-09-21, P1): "a failed status write
# preserves an earlier PASSED result"
# ============================================================================

def test_write_in_progress_marks_not_ready(tmp_path):
    path = str(tmp_path / "status.json")
    cycle_status.write_in_progress(path, requested_symbols=["BTC/CAD"], db_identity="db-abc")
    status = cycle_status.read(path)
    assert status.in_progress is True
    assert status.ready is False


def test_write_in_progress_invalidates_a_prior_passing_result(tmp_path):
    """The exact scenario this two-phase design closes: a cycle STARTS
    (invalidating the old PASSED result immediately) and then — simulating
    a crash before the final write() ever runs — the file is left showing
    in_progress=True, never the stale prior PASSED content."""
    path = str(tmp_path / "status.json")
    cycle_status.write(
        path, requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok", db_identity="db-abc",
    )
    assert cycle_status.read(path).ready is True   # sanity: it really was PASSED before

    cycle_status.write_in_progress(path, requested_symbols=["BTC/CAD"], db_identity="db-abc")
    # Simulated crash: the final write() that would normally follow never happens.

    status = cycle_status.read(path)
    assert status.in_progress is True
    assert status.ready is False   # never reads as the old PASSED result


def test_final_write_clears_in_progress(tmp_path):
    path = str(tmp_path / "status.json")
    cycle_status.write_in_progress(path, requested_symbols=["BTC/CAD"], db_identity="db-abc")
    cycle_status.write(
        path, requested_symbols=["BTC/CAD"], block_state_ok=True, block_state_explain="ok",
        four_way_ran=True, four_way_ready=True, four_way_explain="ok", db_identity="db-abc",
    )
    status = cycle_status.read(path)
    assert status.in_progress is False
    assert status.ready is True
