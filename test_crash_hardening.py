"""
Crash-hardening (2026-07-17 late): atomic state writes, fatal-crash Telegram
alert, synchronous send. Hermetic — tmp dirs and mocks only, no network.
"""
import json
import os
from unittest.mock import MagicMock, patch

from bot.atomic_json import atomic_write_json


# ── atomic_write_json ────────────────────────────────────────────────────

def test_atomic_write_creates_valid_json(tmp_path):
    p = str(tmp_path / "state.json")
    atomic_write_json(p, {"cash": 77.0, "position": 0.0})
    with open(p) as f:
        assert json.load(f) == {"cash": 77.0, "position": 0.0}


def test_atomic_write_replaces_existing(tmp_path):
    p = str(tmp_path / "state.json")
    atomic_write_json(p, {"v": 1})
    atomic_write_json(p, {"v": 2})
    with open(p) as f:
        assert json.load(f)["v"] == 2


def test_atomic_write_leaves_no_tmp_file(tmp_path):
    p = str(tmp_path / "state.json")
    atomic_write_json(p, {"v": 1})
    assert os.listdir(tmp_path) == ["state.json"]


def test_atomic_write_creates_parent_dirs(tmp_path):
    p = str(tmp_path / "nested" / "dir" / "state.json")
    atomic_write_json(p, {"v": 1})
    assert os.path.exists(p)


def test_failed_write_preserves_old_file(tmp_path):
    p = str(tmp_path / "state.json")
    atomic_write_json(p, {"v": "good"})

    class Unserializable:
        pass

    try:
        atomic_write_json(p, {"v": Unserializable()})
    except TypeError:
        pass
    # the live file must still hold the last good state
    with open(p) as f:
        assert json.load(f)["v"] == "good"


# ── TelegramAlerter.send_now ─────────────────────────────────────────────

def test_send_now_respects_disabled():
    from bot.alerts.telegram import TelegramAlerter
    t = TelegramAlerter("", "", enabled=False)
    with patch.object(t, "_send") as mock_send:
        t.send_now("boom")
        mock_send.assert_not_called()


def test_send_now_is_synchronous_when_enabled():
    from bot.alerts.telegram import TelegramAlerter
    t = TelegramAlerter("123:fake", "42", enabled=True)
    with patch.object(t, "_send") as mock_send:
        t.send_now("boom")
        mock_send.assert_called_once_with("boom")


# ── fatal-crash alert helpers ────────────────────────────────────────────

def test_crypto_crash_alert_never_raises_when_disabled(monkeypatch):
    import bot.main as m
    monkeypatch.setattr(m.cfg.alerts, "telegram_enabled", False)
    m._send_crash_alert("Crypto bot", "Traceback: fake")   # must not raise


def test_stock_crash_alert_never_raises_when_channel_off():
    import stock_bot.main as m
    with patch("stock_bot.alerts.notifier._make_telegram", return_value=None):
        m._send_crash_alert("Traceback: fake")   # must not raise
