"""Daily health digest — bot.main._maybe_send_health_digest / _health_digest_text /
_recent_error_count (added 2026-08-27).

A proactive once-a-day both-bots status push to Telegram, so a VPS deployment
gives a "yes it's fine" every morning (and its absence is a signal) instead of
only reactive alerts — built after the native-stop deadlock ran 8 min invisibly.
"""
import inspect
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import bot.main as bot_main


# ── _recent_error_count ────────────────────────────────────────────────────

def test_recent_error_count_windows_by_timestamp(tmp_path):
    ref = datetime(2026, 8, 27, 12, 0, 0)
    log = tmp_path / "trade_bot.log"
    log.write_text(
        "2026-08-25 09:00:00,000 x ERROR old, outside 24h\n"
        "2026-08-27 06:00:00,000 x ERROR recent one\n"
        "2026-08-27 07:00:00,000 x WARNING not an error\n"
        "2026-08-27 08:00:00,000 x ERROR recent two\n"
        "garbage line with ERROR but no timestamp\n"
    )
    assert bot_main._recent_error_count(str(log), ref, hours=24) == 2


def test_recent_error_count_missing_file_is_zero(tmp_path):
    assert bot_main._recent_error_count(str(tmp_path / "nope.log"), datetime.now()) == 0


# ── _health_digest_text ───────────────────────────────────────────────────

_NOW = datetime(2026, 8, 27, 8, 0, 0)


def test_digest_all_normal_header_when_no_attention():
    txt = bot_main._health_digest_text(
        _NOW, "📊 Crypto bot — LIVE", "📈 Stock bot — IBKR PAPER", [], 0, 1, [],
    )
    assert "✅ all systems normal" in txt
    assert "Crypto bot" in txt and "Stock bot" in txt
    assert "Open exchange orders: none" in txt
    assert "Errors last 24h — crypto: 0  stock: 1" in txt


def test_digest_attention_header_and_items():
    txt = bot_main._health_digest_text(
        _NOW, "c", "s", [], 30, 0,
        ["manual HALT is engaged", "SOL/CAD: 3 failed SL/TP exits"],
    )
    assert "⚠️ NEEDS ATTENTION" in txt
    assert "manual HALT is engaged" in txt and "3 failed SL/TP exits" in txt


def test_digest_lists_open_orders():
    orders = [{"symbol": "BTC/CAD", "type": "stop-loss", "side": "sell", "amount": 0.08}]
    txt = bot_main._health_digest_text(_NOW, "c", "s", orders, 0, 0, [])
    assert "Open exchange orders (1):" in txt
    assert "BTC/CAD stop-loss sell 0.08" in txt


# ── _maybe_send_health_digest scheduling ──────────────────────────────────

def _mk(monkeypatch, tmp_path):
    monkeypatch.setattr(bot_main, "_AUDIT_STATE_PATH", str(tmp_path / "audit_state.json"))
    monkeypatch.setattr(bot_main, "_status_crypto_text", lambda *a, **k: "CRYPTO")
    monkeypatch.setattr(bot_main, "_status_stock_text", lambda *a, **k: "STOCK")
    monkeypatch.setattr(bot_main, "_recent_error_count", lambda *a, **k: 0)
    risk = MagicMock()
    risk.config.halt = False
    risk.kill_switch_tripped = False
    alerter = MagicMock()
    execs = {"BTC/CAD": MagicMock()}
    execs["BTC/CAD"]._exchange.fetch_open_orders.return_value = []
    return risk, alerter, execs


def test_digest_sends_when_due_and_records_date_first(monkeypatch, tmp_path):
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    now = datetime(2026, 8, 27, 9, 0, 0)   # past the 08:00 default
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    bot_main._maybe_send_health_digest(execs, {}, risk, alerter, True, False, now)
    alerter.message.assert_called_once()
    assert "DAILY HEALTH DIGEST" in alerter.message.call_args[0][0]

    # second call same day — not due again
    alerter.message.reset_mock()
    bot_main._maybe_send_health_digest(execs, {}, risk, alerter, True, False, now)
    alerter.message.assert_not_called()


def test_digest_not_sent_before_scheduled_time(monkeypatch, tmp_path):
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    now = datetime(2026, 8, 27, 6, 0, 0)   # before 08:00
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    bot_main._maybe_send_health_digest(execs, {}, risk, alerter, True, False, now)
    alerter.message.assert_not_called()


def test_digest_disabled_with_off(monkeypatch, tmp_path):
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "off")
    bot_main._maybe_send_health_digest(
        execs, {}, risk, alerter, True, False, datetime(2026, 8, 27, 12, 0),
    )
    alerter.message.assert_not_called()


def test_digest_flags_halt_and_exit_failures(monkeypatch, tmp_path):
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    risk.config.halt = True
    ss = {"SOL/CAD": {"exit_fail_count": 4, "candle_feed_stale": False}}
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    bot_main._maybe_send_health_digest(
        execs, ss, risk, alerter, True, False, datetime(2026, 8, 27, 9, 0),
    )
    body = alerter.message.call_args[0][0]
    assert "NEEDS ATTENTION" in body
    assert "HALT is engaged" in body and "4 failed SL/TP exits" in body


def test_digest_flags_stuck_loop(monkeypatch, tmp_path):
    from bot.alerts.stuck_loop import StuckLoopDetector
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    det = StuckLoopDetector(lambda _m: None, threshold=3)
    for _ in range(3):
        det.record("execute:BTC/CAD:SELL", ok=False, detail="Insufficient funds")
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    bot_main._maybe_send_health_digest(
        execs, {}, risk, alerter, True, False, datetime(2026, 8, 27, 9, 0),
        stuck_detector=det,
    )
    body = alerter.message.call_args[0][0]
    assert "NEEDS ATTENTION" in body
    assert "stuck loop: execute:BTC/CAD:SELL (3 consecutive failures)" in body


def test_wired_into_run_loop():
    src = inspect.getsource(bot_main.run)
    assert "_maybe_send_health_digest(" in src
    # generic stuck-loop watchdog is created and fed from the execute path
    assert "StuckLoopDetector(alerter.error)" in src
    assert "stuck_detector.record(" in src
    assert "stuck_detector=stuck_detector" in src   # passed to the digest
