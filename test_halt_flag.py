"""
Unit tests for the manual halt flag file helper (_check_halt_flag).

Operational kill-switch: `touch logs/HALT` engages the risk manager's manual
halt without a restart; removing the file lifts it. The helper only lifts a
halt it engaged itself (tracked via the returned halt_file_active bool).
"""
import os
import tempfile
from unittest.mock import MagicMock

from bot.main import _check_halt_flag
from bot.risk.risk_manager import RiskManager, RiskConfig


def _alerter():
    return MagicMock()


def _flag(tmp: str) -> str:
    return os.path.join(tmp, "HALT")


# ── Engage ────────────────────────────────────────────────────────────────

def test_flag_present_engages_halt():
    with tempfile.TemporaryDirectory() as tmp:
        risk, a = RiskManager(RiskConfig()), _alerter()
        open(_flag(tmp), "w").close()
        active = _check_halt_flag(risk, _flag(tmp), False, a)
        assert active is True
        assert risk.config.halt is True
        a.error.assert_called_once()


def test_flag_persisting_does_not_repeat_alert():
    with tempfile.TemporaryDirectory() as tmp:
        risk, a = RiskManager(RiskConfig()), _alerter()
        open(_flag(tmp), "w").close()
        active = _check_halt_flag(risk, _flag(tmp), False, a)
        active = _check_halt_flag(risk, _flag(tmp), active, a)
        assert active is True
        assert risk.config.halt is True
        a.error.assert_called_once()   # engage alert fired once, not per tick


# ── Lift ──────────────────────────────────────────────────────────────────

def test_flag_removed_lifts_halt():
    with tempfile.TemporaryDirectory() as tmp:
        risk, a = RiskManager(RiskConfig()), _alerter()
        open(_flag(tmp), "w").close()
        active = _check_halt_flag(risk, _flag(tmp), False, a)
        os.remove(_flag(tmp))
        active = _check_halt_flag(risk, _flag(tmp), active, a)
        assert active is False
        assert risk.config.halt is False
        assert a.error.call_count == 2   # engage + lift


def test_does_not_lift_halt_it_did_not_engage():
    with tempfile.TemporaryDirectory() as tmp:
        # Halt engaged elsewhere (e.g. future Telegram command) — no flag file
        risk, a = RiskManager(RiskConfig(halt=True)), _alerter()
        active = _check_halt_flag(risk, _flag(tmp), False, a)
        assert active is False
        assert risk.config.halt is True   # untouched
        a.error.assert_not_called()


# ── No-op ─────────────────────────────────────────────────────────────────

def test_no_flag_no_halt_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        risk, a = RiskManager(RiskConfig()), _alerter()
        active = _check_halt_flag(risk, _flag(tmp), False, a)
        assert active is False
        assert risk.config.halt is False
        a.error.assert_not_called()


if __name__ == "__main__":
    tests = [
        test_flag_present_engages_halt,
        test_flag_persisting_does_not_repeat_alert,
        test_flag_removed_lifts_halt,
        test_does_not_lift_halt_it_did_not_engage,
        test_no_flag_no_halt_is_noop,
    ]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
