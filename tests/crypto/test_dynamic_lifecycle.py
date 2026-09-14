"""Unit tests for bot.dynamic.lifecycle.DynamicSymbolManager — dynamic
symbol admission/removal, warmup wiring, position retention across
delisting, and restart recovery. All fakes, no network/real executors."""
import json
import os

import pytest

from bot.dynamic.lifecycle import DynamicSymbolManager, state_path_for


class FakeStrategy:
    def __init__(self):
        self.warmed_up_with = None


class FakeExecutor:
    def __init__(self, symbol, state_path):
        self.symbol = symbol
        self.state_path = state_path
        self.position = 0.0
        self.cash = 100.0
        # If a prior test already wrote a state file with a position, load it —
        # mimics a real executor restoring state on construction.
        if os.path.exists(state_path):
            with open(state_path) as f:
                data = json.load(f)
            self.position = data.get("position", 0.0)

    def save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({"position": self.position}, f)


def _manager(tmp_path, warmup_calls=None):
    warmup_calls = warmup_calls if warmup_calls is not None else []

    def make_strategy():
        return FakeStrategy()

    def warmup(strat, exchange, timeframe, symbol):
        warmup_calls.append(symbol)
        strat.warmed_up_with = (exchange, timeframe, symbol)
        return 12345

    def make_sm():
        return object()

    def make_pm():
        return object()

    def make_executor(symbol, state_path):
        return FakeExecutor(symbol, state_path)

    return DynamicSymbolManager(
        make_strategy=make_strategy,
        warmup_strategy=warmup,
        make_state_machine=make_sm,
        make_position_manager=make_pm,
        make_executor=make_executor,
        manifest_path=str(tmp_path / "manifest.json"),
        state_dir=str(tmp_path / "state"),
    )


# ── Admission ────────────────────────────────────────────────────────────

def test_admit_initializes_strategy_and_warmup(tmp_path):
    calls = []
    mgr = _manager(tmp_path, warmup_calls=calls)

    handle = mgr.admit("ETH/CAD", exchange="fake-exchange", timeframe="4h")

    assert handle.symbol == "ETH/CAD"
    assert calls == ["ETH/CAD"]
    assert handle.strategy.warmed_up_with == ("fake-exchange", "4h", "ETH/CAD")
    assert mgr.is_admitted("ETH/CAD")
    assert mgr.active_symbols == ["ETH/CAD"]


def test_admit_is_idempotent_no_double_warmup(tmp_path):
    calls = []
    mgr = _manager(tmp_path, warmup_calls=calls)

    h1 = mgr.admit("ETH/CAD", "ex", "4h")
    h2 = mgr.admit("ETH/CAD", "ex", "4h")

    assert h1 is h2
    assert calls == ["ETH/CAD"]  # only warmed up once


def test_admit_writes_manifest(tmp_path):
    mgr = _manager(tmp_path)
    mgr.admit("ETH/CAD", "ex", "4h")

    manifest_path = tmp_path / "manifest.json"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert data["symbols"] == ["ETH/CAD"]


# ── Retirement / position retention ─────────────────────────────────────

def test_retire_if_flat_removes_symbol(tmp_path):
    mgr = _manager(tmp_path)
    mgr.admit("ETH/CAD", "ex", "4h")

    retired = mgr.retire_if_flat("ETH/CAD")

    assert retired is True
    assert not mgr.is_admitted("ETH/CAD")


def test_retire_if_flat_keeps_open_position(tmp_path):
    mgr = _manager(tmp_path)
    handle = mgr.admit("ETH/CAD", "ex", "4h")
    handle.executor.position = 1.5   # simulate an open position

    retired = mgr.retire_if_flat("ETH/CAD")

    assert retired is False
    assert mgr.is_admitted("ETH/CAD")


def test_sync_to_candidates_retires_dropped_flat_symbols_only(tmp_path):
    mgr = _manager(tmp_path)
    flat = mgr.admit("FLAT/CAD", "ex", "4h")
    held = mgr.admit("HELD/CAD", "ex", "4h")
    held.executor.position = 2.0

    retired = mgr.sync_to_candidates(eligible_symbols=set())  # neither is a candidate anymore

    assert retired == ["FLAT/CAD"]
    assert not mgr.is_admitted("FLAT/CAD")
    assert mgr.is_admitted("HELD/CAD")   # kept despite leaving the candidate list


def test_sync_to_candidates_keeps_symbols_still_eligible(tmp_path):
    mgr = _manager(tmp_path)
    mgr.admit("ETH/CAD", "ex", "4h")

    retired = mgr.sync_to_candidates(eligible_symbols={"ETH/CAD"})

    assert retired == []
    assert mgr.is_admitted("ETH/CAD")


# ── Restart recovery ─────────────────────────────────────────────────────

def test_restart_recovery_reloads_manifest_into_a_fresh_manager(tmp_path):
    calls = []
    mgr1 = _manager(tmp_path, warmup_calls=calls)
    mgr1.admit("ETH/CAD", "ex", "4h")
    mgr1.admit("SOL/CAD", "ex", "4h")

    # A brand-new process: fresh manager, no in-memory state.
    mgr2 = _manager(tmp_path, warmup_calls=calls)
    recovered = mgr2.restart_recovery(exchange="ex", timeframe="4h")

    assert set(recovered) == {"ETH/CAD", "SOL/CAD"}
    assert set(mgr2.active_symbols) == {"ETH/CAD", "SOL/CAD"}


def test_restart_recovery_finds_open_position_missing_from_manifest(tmp_path):
    """Defensive path: the manifest write raced a crash, but the symbol's
    own state file already shows an open position — restart_recovery must
    still pick it up rather than silently lose the position."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Write an orphaned state file for a symbol never recorded on the manifest.
    (state_dir / "ORPHAN_CAD.json").write_text(json.dumps({"position": 3.0}))

    mgr = _manager(tmp_path)
    recovered = mgr.restart_recovery(exchange="ex", timeframe="4h")

    assert "ORPHAN/CAD" in recovered
    assert mgr.is_admitted("ORPHAN/CAD")


def test_restart_recovery_ignores_flat_orphan_state_files(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "FLATORPHAN_CAD.json").write_text(json.dumps({"position": 0.0}))

    mgr = _manager(tmp_path)
    recovered = mgr.restart_recovery(exchange="ex", timeframe="4h")

    assert recovered == []
    assert not mgr.is_admitted("FLATORPHAN/CAD")


# ── Helpers ──────────────────────────────────────────────────────────────

def test_state_path_for_uses_underscore_naming_like_live_bot(tmp_path):
    path = state_path_for("BTC/CAD", state_dir=str(tmp_path))
    assert path == str(tmp_path / "BTC_CAD.json")


def test_open_position_symbols_reports_only_nonzero(tmp_path):
    mgr = _manager(tmp_path)
    a = mgr.admit("A/CAD", "ex", "4h")
    b = mgr.admit("B/CAD", "ex", "4h")
    a.executor.position = 1.0

    assert mgr.open_position_symbols() == ["A/CAD"]
    assert mgr.has_open_position("A/CAD")
    assert not mgr.has_open_position("B/CAD")
