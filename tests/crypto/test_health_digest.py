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


def _fake_executor(**overrides):
    """A minimal MagicMock with the specific attributes
    _maybe_send_health_digest's new (2026-09-18) checks read, explicitly
    set — avoids the classic MagicMock trap where an unset attribute reads
    back as a truthy auto-created Mock instead of a real default."""
    exc = MagicMock()
    exc.state_write_healthy    = overrides.get("state_write_healthy", True)
    exc.startup_sync_healthy   = overrides.get("startup_sync_healthy", True)
    exc.pending_journal_entries = overrides.get("pending_journal_entries", [])
    exc.position               = overrides.get("position", 0.0)
    exc.has_resting_stop       = overrides.get("has_resting_stop", True)
    return exc


def test_digest_flags_price_feed_stale(monkeypatch, tmp_path):
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    ss = {"BTC/CAD": {"price_feed_stale": True, "executor": _fake_executor()}}
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    bot_main._maybe_send_health_digest(
        execs, ss, risk, alerter, True, False, datetime(2026, 8, 27, 9, 0),
    )
    body = alerter.message.call_args[0][0]
    assert "NEEDS ATTENTION" in body
    assert "BTC/CAD: live price feed stale" in body


def test_digest_flags_state_write_and_startup_sync_unhealthy(monkeypatch, tmp_path):
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    ss = {
        "BTC/CAD": {"executor": _fake_executor(
            state_write_healthy=False, startup_sync_healthy=False,
        )},
    }
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    bot_main._maybe_send_health_digest(
        execs, ss, risk, alerter, True, False, datetime(2026, 8, 27, 9, 0),
    )
    body = alerter.message.call_args[0][0]
    assert "BTC/CAD: last state save failed" in body
    assert "BTC/CAD: startup balance/position sync failed" in body


def test_digest_flags_unacked_journal_entry(monkeypatch, tmp_path):
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    ss = {"BTC/CAD": {"executor": _fake_executor(
        pending_journal_entries=[{"order_id": "o1", "side": "BUY"}],
    )}}
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    bot_main._maybe_send_health_digest(
        execs, ss, risk, alerter, True, False, datetime(2026, 8, 27, 9, 0),
    )
    body = alerter.message.call_args[0][0]
    assert "BTC/CAD: 1 unacked fill journal entry" in body


def test_digest_flags_multiple_unacked_journal_entries_plural(monkeypatch, tmp_path):
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    ss = {"BTC/CAD": {"executor": _fake_executor(
        pending_journal_entries=[
            {"order_id": "o1", "side": "BUY"}, {"order_id": "o2", "side": "SELL"},
        ],
    )}}
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    bot_main._maybe_send_health_digest(
        execs, ss, risk, alerter, True, False, datetime(2026, 8, 27, 9, 0),
    )
    body = alerter.message.call_args[0][0]
    assert "BTC/CAD: 2 unacked fill journal entries" in body


def test_digest_flags_unprotected_open_position(monkeypatch, tmp_path):
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    ss = {"BTC/CAD": {"executor": _fake_executor(position=0.001, has_resting_stop=False)}}
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    monkeypatch.setattr(bot_main.cfg.exchange, "native_stop_loss_enabled", True)
    bot_main._maybe_send_health_digest(
        execs, ss, risk, alerter, True, False, datetime(2026, 8, 27, 9, 0),
    )
    body = alerter.message.call_args[0][0]
    assert "BTC/CAD: holding a position with no resting native stop" in body


def test_digest_healthy_executor_flags_nothing(monkeypatch, tmp_path):
    """A fully healthy executor must not add any spurious attention items."""
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    ss = {"BTC/CAD": {"executor": _fake_executor()}}
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    bot_main._maybe_send_health_digest(
        execs, ss, risk, alerter, True, False, datetime(2026, 8, 27, 9, 0),
    )
    body = alerter.message.call_args[0][0]
    assert "✅ all systems normal" in body


def test_wired_into_run_loop():
    src = inspect.getsource(bot_main.run)
    assert "_maybe_send_health_digest(" in src
    # generic stuck-loop watchdog is created and fed from the execute path.
    # stuck_detector.record() itself moved into _execute_approved_signal()
    # (2026-09-13, extracted for dynamic-universe testability — see that
    # function's own docstring) — run() now feeds it by calling that
    # function with stuck_detector= passed through, rather than recording
    # inline. Check both ends of that wiring rather than the old inline text.
    assert "StuckLoopDetector(alerter.error)" in src
    assert "_execute_approved_signal(" in src
    assert "stuck_detector=stuck_detector" in src   # passed into _execute_approved_signal AND the digest
    exec_src = inspect.getsource(bot_main._execute_approved_signal)
    assert "stuck_detector.record(" in exec_src


# ── 2026-09-26 review findings: failures must never read as "normal" ─────

def test_digest_open_orders_query_failure_is_unknown_not_none(monkeypatch, tmp_path):
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    execs["BTC/CAD"]._exchange.fetch_open_orders.side_effect = RuntimeError("kraken timeout")
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    bot_main._maybe_send_health_digest(
        execs, {}, risk, alerter, True, False, datetime(2026, 8, 27, 9, 0),
    )
    body = alerter.message.call_args[0][0]
    assert "NEEDS ATTENTION" in body
    assert "Open exchange orders: UNKNOWN" in body
    assert "Open exchange orders: none" not in body
    assert "open-order query failed (kraken timeout)" in body


def test_digest_text_distinguishes_unknown_from_empty():
    assert "UNKNOWN" in bot_main._health_digest_text(_NOW, "c", "s", None, 0, 0, ["x"])
    assert "Open exchange orders: none" in bot_main._health_digest_text(_NOW, "c", "s", [], 0, 0, [])


from datetime import timezone as _utc_tz
from bot.accounting import cycle_status as _cs

_UTC_NOW = datetime(2026, 9, 26, 14, 0, tzinfo=_utc_tz.utc)


def _status(tmp_path, **kw):
    p = str(tmp_path / "accounting_cycle_status.json")
    fields = dict(requested_symbols=["BTC/CAD"], block_state_ok=True,
                  block_state_explain="ok", four_way_ran=True, four_way_ready=True)
    fields.update(kw)
    _cs.write(p, **fields)
    return p


def test_accounting_attention_off_when_disabled(tmp_path):
    assert bot_main._digest_accounting_attention(False, str(tmp_path / "x.json"), _UTC_NOW, 3_600_000) == []


def test_accounting_attention_missing_status_is_flagged(tmp_path):
    items = bot_main._digest_accounting_attention(True, str(tmp_path / "none.json"), _UTC_NOW, 3_600_000)
    assert items and "no reconciliation cycle has completed" in items[0]


def test_accounting_attention_unreconciled_is_flagged(tmp_path):
    p = _status(tmp_path, block_state_ok=False,
                block_state_explain="coverage incomplete (observation phase raised)",
                four_way_ran=False, four_way_ready=None)
    items = bot_main._digest_accounting_attention(True, p, datetime.now(_utc_tz.utc), 3_600_000)
    assert any("NOT reconciled" in i and "coverage incomplete" in i for i in items)


def test_accounting_attention_four_way_not_ready_is_flagged(tmp_path):
    p = _status(tmp_path, four_way_ready=False, four_way_explain="cash mismatch")
    items = bot_main._digest_accounting_attention(True, p, datetime.now(_utc_tz.utc), 3_600_000)
    assert any("cash mismatch" in i for i in items)


def test_accounting_attention_stale_status_is_flagged(tmp_path):
    p = _status(tmp_path)          # reconciled — but judged 10h later
    later = datetime.now(_utc_tz.utc) + timedelta(hours=10)
    items = bot_main._digest_accounting_attention(True, p, later, 3_600_000)
    assert any("stale" in i for i in items)


def test_accounting_attention_in_progress_is_flagged(tmp_path):
    p = str(tmp_path / "s.json")
    _cs.write_in_progress(p, requested_symbols=["BTC/CAD"])
    items = bot_main._digest_accounting_attention(True, p, datetime.now(_utc_tz.utc), 3_600_000)
    assert any("never completed" in i for i in items)


def test_accounting_attention_healthy_and_fresh_is_silent(tmp_path):
    p = _status(tmp_path)
    assert bot_main._digest_accounting_attention(True, p, datetime.now(_utc_tz.utc), 3_600_000) == []


def test_digest_reports_accounting_failure_in_the_real_message(monkeypatch, tmp_path):
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_main, "_STATE_LOG_DIR", str(tmp_path))
    _status(tmp_path, block_state_ok=False, block_state_explain="cash unreconciled",
            four_way_ran=False, four_way_ready=None)
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")
    bot_main._maybe_send_health_digest(
        execs, {}, risk, alerter, True, False, datetime(2026, 8, 27, 9, 0),
        accounting_enabled=True,
    )
    body = alerter.message.call_args[0][0]
    assert "NEEDS ATTENTION" in body and "cash unreconciled" in body


def test_run_passes_accounting_state_to_digest():
    src = inspect.getsource(bot_main.run)
    assert "accounting_enabled=_accounting_enabled" in src


# ── Shared freshness policy: digest must agree with the BUY gate (2026-09-26) ──

import json as _json
from types import SimpleNamespace as _NS

from bot.accounting.reconciliation import BlockState as _BlockState

_ACCT_CFG = _NS(reconcile_interval_s=3600.0, stale_grace_s=300.0)
_T0 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=_utc_tz.utc)


def _ok_status_at(tmp_path, when: datetime) -> str:
    p = tmp_path / "accounting_cycle_status.json"
    p.write_text(_json.dumps({
        "computed_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "requested_symbols": ["BTC/CAD"],
        "block_state_ok": True, "block_state_explain": "ok", "four_way_ran": True,
        "four_way_ready": True, "four_way_explain": None, "in_progress": False,
    }))
    return str(p)


def _gate_is_stale(age_s: float) -> bool:
    st = _BlockState(reconciled=True, computed_at_ms=int(_T0.timestamp() * 1000))
    return st.is_stale(now_ms=int((_T0.timestamp() + age_s) * 1000),
                       max_age_ms=bot_main._accounting_max_age_ms_for(_ACCT_CFG))


def _digest_is_stale(tmp_path, age_s: float) -> bool:
    items = bot_main._digest_accounting_attention(
        True, _ok_status_at(tmp_path, _T0), _T0 + timedelta(seconds=age_s),
        bot_main._accounting_max_age_ms_for(_ACCT_CFG),
    )
    return any("stale" in i for i in items)


def test_shared_max_age_is_interval_plus_grace():
    assert bot_main._accounting_max_age_ms_for(_ACCT_CFG) == 3_900_000


def test_review_repro_3960s_is_stale_for_both_gate_and_digest(tmp_path):
    assert _gate_is_stale(3960)
    assert _digest_is_stale(tmp_path, 3960), "digest must not report healthy evidence the BUY gate rejects"


def test_exactly_at_the_deadline_both_treat_evidence_as_fresh(tmp_path):
    assert not _gate_is_stale(3900)
    assert not _digest_is_stale(tmp_path, 3900)


def test_just_after_the_deadline_both_treat_evidence_as_stale(tmp_path):
    assert _gate_is_stale(3901)
    assert _digest_is_stale(tmp_path, 3901)


def test_gate_and_digest_both_use_the_shared_helper():
    src = inspect.getsource(bot_main)
    assert "_accounting_max_age_ms  = _accounting_max_age_ms_for(cfg.accounting)" in src
    digest_src = inspect.getsource(bot_main._maybe_send_health_digest)
    assert "_accounting_max_age_ms_for(cfg.accounting)" in digest_src
    assert "2 * cfg.accounting.reconcile_interval_s" not in digest_src


def test_real_digest_reports_3960s_old_evidence_as_stale(monkeypatch, tmp_path):
    """End-to-end review reproduction through the REAL composition path
    (_maybe_send_health_digest → message text), using only interfaces that
    existed before the fix — so it fails on the old 2×interval+grace policy
    by behavior, not because a helper is missing. interval=3600s, grace=300s:
    the BUY gate expires evidence after 3900s, so a successful status 3960s
    old must produce NEEDS ATTENTION, not "all systems normal"."""
    risk, alerter, execs = _mk(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_main, "_STATE_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(bot_main.cfg.accounting, "reconcile_interval_s", 3600.0)
    monkeypatch.setattr(bot_main.cfg.accounting, "stale_grace_s", 300.0)
    written = datetime.now(_utc_tz.utc) - timedelta(seconds=3960)
    (tmp_path / "accounting_cycle_status.json").write_text(_json.dumps({
        "computed_at": written.strftime("%Y-%m-%dT%H:%M:%SZ"), "requested_symbols": ["BTC/CAD"],
        "block_state_ok": True, "block_state_explain": "ok", "four_way_ran": True,
        "four_way_ready": True, "four_way_explain": None, "in_progress": False,
    }))
    monkeypatch.setenv("HEALTH_DIGEST_TIME", "08:00")

    bot_main._maybe_send_health_digest(
        execs, {}, risk, alerter, True, False, datetime(2026, 8, 27, 9, 0),
        accounting_enabled=True,
    )

    body = alerter.message.call_args[0][0]
    assert "NEEDS ATTENTION" in body
    assert "accounting status is stale" in body
    assert "all systems normal" not in body
