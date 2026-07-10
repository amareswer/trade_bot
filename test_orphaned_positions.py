"""
Unit tests for the startup orphaned-position check (_check_orphaned_positions).

An orphaned position is an open position in a logs/live_state_*.json file whose
symbol is NOT in this run's initialized symbol list (e.g. removed from
UNIVERSE_WHITELIST while holding). Such positions get no SL/TP or drift
monitoring — the check must alert loudly so a human intervenes.
"""
import json
import os
import tempfile
from unittest.mock import MagicMock

from bot.main import _check_orphaned_positions


def _alerter():
    return MagicMock()


def _write_state(tmp: str, symbol: str, position: float) -> str:
    path = os.path.join(tmp, f"live_state_{symbol.replace('/', '_')}.json")
    with open(path, "w") as fh:
        json.dump({"symbol": symbol, "position": position, "cash": 77.0}, fh)
    return path


def test_orphaned_position_detected_and_alerted():
    with tempfile.TemporaryDirectory() as tmp:
        _write_state(tmp, "XRP/CAD", 25.0)
        a = _alerter()
        orphans = _check_orphaned_positions({"BTC/CAD"}, a, log_dir=tmp)
        assert orphans == ["XRP/CAD"]
        a.error.assert_called_once()
        assert "XRP/CAD" in a.error.call_args[0][0]


def test_initialized_symbol_with_position_is_not_orphaned():
    with tempfile.TemporaryDirectory() as tmp:
        _write_state(tmp, "BTC/CAD", 0.001)
        a = _alerter()
        orphans = _check_orphaned_positions({"BTC/CAD"}, a, log_dir=tmp)
        assert orphans == []
        a.error.assert_not_called()


def test_flat_uninitialized_symbol_is_not_orphaned():
    with tempfile.TemporaryDirectory() as tmp:
        _write_state(tmp, "XRP/CAD", 0.0)
        _write_state(tmp, "DOGE/CAD", 0.0)
        a = _alerter()
        orphans = _check_orphaned_positions({"BTC/CAD"}, a, log_dir=tmp)
        assert orphans == []
        a.error.assert_not_called()


def test_malformed_state_file_is_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "live_state_BAD_CAD.json"), "w") as fh:
            fh.write("{not valid json")
        _write_state(tmp, "XRP/CAD", 10.0)
        a = _alerter()
        orphans = _check_orphaned_positions({"BTC/CAD"}, a, log_dir=tmp)
        assert orphans == ["XRP/CAD"]   # bad file skipped, good one still caught
        a.error.assert_called_once()


def test_empty_log_dir_returns_no_orphans():
    with tempfile.TemporaryDirectory() as tmp:
        a = _alerter()
        orphans = _check_orphaned_positions({"BTC/CAD"}, a, log_dir=tmp)
        assert orphans == []
        a.error.assert_not_called()
