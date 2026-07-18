"""
Stock bot → Telegram relay (stock_bot/alerts/notifier.py, 2026-07-17).

Hermetic — no network, no .env reads (credentials injected via root_env /
mock factories). Covers: channel-off no-ops, ops_alert forwarding, fill
formatting, and the HIGH-priority-only filter on scan alerts.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from stock_bot.alerts.alert import Alert, AlertType
from stock_bot.alerts.notifier import AlertNotifier, _make_telegram


def _cfg():
    return SimpleNamespace(alert_email_enabled=False, alert_desktop_enabled=False)


def _alert(priority: str, symbol: str = "AMD") -> Alert:
    return Alert(
        alert_type=AlertType.PORTFOLIO_SELL, symbol=symbol, message="test",
        confidence=70, price=100.0, currency="USD",
        timestamp=datetime(2026, 7, 17), priority=priority, source="watchlist",
    )


def test_make_telegram_disabled_without_enabled_key(monkeypatch):
    for k in ("TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    assert _make_telegram(root_env={}) is None


def test_make_telegram_disabled_without_credentials(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    assert _make_telegram(root_env={"TELEGRAM_ENABLED": "true"}) is None


def test_make_telegram_builds_from_root_env(monkeypatch):
    for k in ("TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    alerter = _make_telegram(root_env={
        "TELEGRAM_ENABLED": "true",
        "TELEGRAM_BOT_TOKEN": "123:fake",
        "TELEGRAM_CHAT_ID": "42",
    })
    assert alerter is not None


def test_ops_alert_forwards_to_telegram():
    tg = MagicMock()
    n = AlertNotifier(_cfg(), telegram_factory=lambda: tg)
    n.ops_alert("TWS connection lost", "details here")
    assert tg.message.call_count == 1
    assert "TWS connection lost" in tg.message.call_args[0][0]


def test_fill_forwards_with_pnl_and_reason():
    tg = MagicMock()
    n = AlertNotifier(_cfg(), telegram_factory=lambda: tg)
    n.fill("SELL", "KO", 3, 81.50, 244.50, pnl=-2.10, reason="stop loss")
    text = tg.message.call_args[0][0]
    assert "SELL" in text and "KO" in text
    assert "-2.10" in text and "stop loss" in text


def test_fill_noop_when_channel_off():
    n = AlertNotifier(_cfg(), telegram_factory=lambda: None)
    n.fill("BUY", "KO", 3, 81.50, 244.50)   # must not raise


def test_notify_relays_high_priority_only():
    tg = MagicMock()
    n = AlertNotifier(_cfg(), telegram_factory=lambda: tg)
    n.notify([_alert("HIGH", "AMD"), _alert("MEDIUM", "KO")])
    assert tg.message.call_count == 1
    assert "AMD" in tg.message.call_args[0][0]
