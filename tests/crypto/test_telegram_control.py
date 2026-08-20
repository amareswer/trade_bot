"""
Unit tests for two-way Telegram control (2026-08-20):
  - bot/alerts/telegram_control.py: TelegramCommandPoller transport
    (auth, dispatch, offset handling, backlog draining) — no network.
  - bot/main.py: the extracted, testable command-body functions
    (_status_crypto_text, _pause_crypto_flag, _resume_crypto_flag,
    _status_stock_text, _help_crypto_text, _format_symbol_status).

Structural guard: no handler body may call any LiveExecutor trading
method or RiskManager.halt()/resume() directly — /pause and /resume must
only ever touch the logs/HALT flag file (the SAME mechanism
_check_halt_flag() already polls every tick), never a parallel path.
"""
from __future__ import annotations

import inspect
import os
import re
import tempfile
import types
from unittest.mock import MagicMock, patch

import bot.alerts.telegram_control as tc_mod
from bot.alerts.telegram_control import TelegramCommandPoller, start_telegram_control_thread
from bot.execution.executor import Portfolio
from bot.portfolio.position_manager import PositionManager
from bot.risk.risk_manager import RiskConfig, RiskManager

import bot.main as bot_main


CHAT_ID = "197926612"
TOKEN   = "test-token"


def _fake_response(payload: dict, ok: bool = True, status_code: int = 200):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.ok = ok
    resp.status_code = status_code
    resp.text = ""
    resp.raise_for_status.side_effect = None
    return resp


def _update(update_id: int, chat_id: str, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id},
                    "text": text},
    }


# ---------------------------------------------------------------------------
# TelegramCommandPoller — auth
# ---------------------------------------------------------------------------

def test_poll_once_dispatches_authorized_command_and_replies():
    handler = MagicMock(return_value="OK reply")
    poller = TelegramCommandPoller(TOKEN, CHAT_ID, {"/status_crypto": handler})

    with patch("requests.get") as mock_get, patch("requests.post") as mock_post:
        mock_get.return_value = _fake_response({
            "ok": True, "result": [_update(100, CHAT_ID, "/status_crypto")],
        })
        mock_post.return_value = _fake_response({"ok": True})
        poller.poll_once()

    handler.assert_called_once()
    mock_post.assert_called_once()
    sent = mock_post.call_args.kwargs["json"]
    assert sent["chat_id"] == CHAT_ID
    assert sent["text"] == "OK reply"


def test_poll_once_ignores_unauthorized_chat_id():
    handler = MagicMock(return_value="should not run")
    poller = TelegramCommandPoller(TOKEN, CHAT_ID, {"/status_crypto": handler})

    with patch("requests.get") as mock_get, patch("requests.post") as mock_post:
        mock_get.return_value = _fake_response({
            "ok": True, "result": [_update(100, "999999999", "/status_crypto")],
        })
        poller.poll_once()

    handler.assert_not_called()
    mock_post.assert_not_called()


def test_poll_once_ignores_unrecognized_command():
    """Authorized chat, but the command isn't in this process's handler
    dict (e.g. a stray /pause_stock, or a typo) — same silent-ignore
    treatment as bad auth, not an error."""
    handler = MagicMock()
    poller = TelegramCommandPoller(TOKEN, CHAT_ID, {"/status_crypto": handler})

    with patch("requests.get") as mock_get, patch("requests.post") as mock_post:
        mock_get.return_value = _fake_response({
            "ok": True, "result": [_update(100, CHAT_ID, "/pause_stock")],
        })
        poller.poll_once()

    handler.assert_not_called()
    mock_post.assert_not_called()


def test_poll_once_handler_exception_does_not_raise():
    def _boom():
        raise ValueError("kaboom")
    poller = TelegramCommandPoller(TOKEN, CHAT_ID, {"/status_crypto": _boom})

    with patch("requests.get") as mock_get, patch("requests.post") as mock_post:
        mock_get.return_value = _fake_response({
            "ok": True, "result": [_update(100, CHAT_ID, "/status_crypto")],
        })
        mock_post.return_value = _fake_response({"ok": True})
        poller.poll_once()   # must not raise

    mock_post.assert_called_once()
    assert "failed" in mock_post.call_args.kwargs["json"]["text"]


# ---------------------------------------------------------------------------
# TelegramCommandPoller — offset handling
# ---------------------------------------------------------------------------

def test_poll_once_advances_offset_to_max_plus_one():
    poller = TelegramCommandPoller(TOKEN, CHAT_ID, {"/help_crypto": lambda: "hi"})
    with patch("requests.get") as mock_get, patch("requests.post") as mock_post:
        mock_get.return_value = _fake_response({
            "ok": True, "result": [
                _update(100, CHAT_ID, "/help_crypto"),
                _update(105, "999999999", "/help_crypto"),   # unauthorized, still advances offset
            ],
        })
        mock_post.return_value = _fake_response({"ok": True})
        poller.poll_once()

    assert poller._offset == 106

    with patch("requests.get") as mock_get2:
        mock_get2.return_value = _fake_response({"ok": True, "result": []})
        poller.poll_once()
    assert mock_get2.call_args.kwargs["params"]["offset"] == 106


def test_prime_offset_drains_backlog_without_dispatching():
    handler = MagicMock()
    poller = TelegramCommandPoller(TOKEN, CHAT_ID, {"/pause_crypto": handler})

    with patch("requests.get") as mock_get:
        mock_get.return_value = _fake_response({
            "ok": True, "result": [
                _update(50, CHAT_ID, "/pause_crypto"),
                _update(51, CHAT_ID, "/pause_crypto"),
            ],
        })
        poller.prime_offset()

    handler.assert_not_called()   # backlog discarded unread, not acted on
    assert poller._offset == 52


def test_getupdates_failure_does_not_raise_and_keeps_offset():
    poller = TelegramCommandPoller(TOKEN, CHAT_ID, {})
    poller._offset = 10
    with patch("requests.get", side_effect=ConnectionError("network down")):
        poller.poll_once()   # must not raise
    assert poller._offset == 10


# ---------------------------------------------------------------------------
# TelegramCommandPoller — disabled (no credentials)
# ---------------------------------------------------------------------------

def test_poller_disabled_without_token_or_chat_id():
    poller = TelegramCommandPoller("", "", {})
    assert not poller.enabled
    with patch("requests.get") as mock_get:
        poller.poll_once()
        poller.prime_offset()
    mock_get.assert_not_called()


def test_start_thread_returns_none_when_disabled():
    poller = TelegramCommandPoller("", CHAT_ID, {})
    assert start_telegram_control_thread(poller) is None


# ---------------------------------------------------------------------------
# Structural guard — no handler may touch trading internals
# ---------------------------------------------------------------------------

_FORBIDDEN_CALL_PATTERNS = [
    ".execute(", ".sync_protective_stop(", ".cancel_order(", ".cancel(",
    "risk.halt(", "risk.resume(", ".create_order(", "LiveExecutor(",
]


def _strip_docstrings_and_comments(src: str) -> str:
    """Remove triple-quoted docstrings and '#' comments before scanning for
    forbidden call patterns — this guard checks actual CODE, not prose that
    happens to mention the pattern it's warning against (this file's own
    docstrings/comments do, deliberately, explain what must never appear)."""
    src = re.sub(r'"""[\s\S]*?"""', '', src)
    src = re.sub(r"'''[\s\S]*?'''", '', src)
    src = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    return src


def test_command_bodies_never_call_trading_methods():
    """Source-inspection guard (same idiom as run()'s other source-inspection
    tests elsewhere in this suite): every /status_crypto, /pause_crypto,
    /resume_crypto, /status_stock, /help_crypto command body must be free
    of any call into order placement/modification/cancellation or a direct
    RiskManager.halt()/resume() bypass of the shared logs/HALT mechanism."""
    sources = "\n".join(inspect.getsource(fn) for fn in (
        bot_main._status_crypto_text,
        bot_main._format_symbol_status,
        bot_main._pause_crypto_flag,
        bot_main._resume_crypto_flag,
        bot_main._status_stock_text,
        bot_main._help_crypto_text,
    ))
    code_only = _strip_docstrings_and_comments(sources)
    for forbidden in _FORBIDDEN_CALL_PATTERNS:
        assert forbidden not in code_only, f"forbidden pattern found: {forbidden!r}"


def test_telegram_control_module_has_no_trading_imports():
    """The poller module itself must carry no reference to trading
    internals — it only knows how to talk to Telegram and dispatch by
    exact command string to whatever bot/main.py injects."""
    code_only = _strip_docstrings_and_comments(inspect.getsource(tc_mod))
    for forbidden in ("LiveExecutor", "RiskManager", "PositionManager", "import ccxt"):
        assert forbidden not in code_only


# ---------------------------------------------------------------------------
# _pause_crypto_flag / _resume_crypto_flag — reuse logs/HALT, no parallel path
# ---------------------------------------------------------------------------

def test_pause_writes_halt_flag_file():
    with tempfile.TemporaryDirectory() as tmp:
        flag_path = os.path.join(tmp, "HALT")
        assert not os.path.exists(flag_path)
        reply = bot_main._pause_crypto_flag(flag_path, 30)
        assert os.path.exists(flag_path)
        assert "Halt flag written" in reply


def test_pause_is_idempotent_when_already_engaged():
    with tempfile.TemporaryDirectory() as tmp:
        flag_path = os.path.join(tmp, "HALT")
        open(flag_path, "w").close()
        reply = bot_main._pause_crypto_flag(flag_path, 30)   # must not raise/duplicate
        assert os.path.exists(flag_path)
        assert "Halt flag written" in reply


def test_resume_removes_halt_flag_file():
    with tempfile.TemporaryDirectory() as tmp:
        flag_path = os.path.join(tmp, "HALT")
        open(flag_path, "w").close()
        reply = bot_main._resume_crypto_flag(flag_path, 30)
        assert not os.path.exists(flag_path)
        assert "Halt flag removed" in reply


def test_resume_when_not_paused_is_a_clean_noop():
    with tempfile.TemporaryDirectory() as tmp:
        flag_path = os.path.join(tmp, "HALT")
        reply = bot_main._resume_crypto_flag(flag_path, 30)
        assert not os.path.exists(flag_path)
        assert "not set" in reply


def test_pause_then_check_halt_flag_actually_engages_risk():
    """End-to-end proof that /pause_crypto really does reuse the SAME
    mechanism _check_halt_flag() polls every tick — not a second,
    independent halt path."""
    with tempfile.TemporaryDirectory() as tmp:
        flag_path = os.path.join(tmp, "HALT")
        risk = RiskManager(RiskConfig())
        alerter = MagicMock()

        bot_main._pause_crypto_flag(flag_path, 30)
        active = bot_main._check_halt_flag(risk, flag_path, False, alerter)
        assert active is True
        assert risk.config.halt is True

        bot_main._resume_crypto_flag(flag_path, 30)
        active = bot_main._check_halt_flag(risk, flag_path, active, alerter)
        assert active is False
        assert risk.config.halt is False


# ---------------------------------------------------------------------------
# _status_crypto_text / _format_symbol_status
# ---------------------------------------------------------------------------

def _fake_executor(position=0.001, avg_entry=88_000.0, cash=50.0):
    exc = types.SimpleNamespace()
    exc.position   = position
    exc.avg_entry  = avg_entry
    exc.cash       = cash
    exc.portfolio  = Portfolio(cash=cash)
    exc.portfolio.position    = position
    exc.portfolio._cost_basis = avg_entry
    return exc


def test_status_crypto_shows_halt_state():
    risk = RiskManager(RiskConfig(halt=True))
    text = bot_main._status_crypto_text({}, {}, risk, live_trading=True, dry_run=False)
    assert "LIVE" in text
    assert "ENGAGED" in text


def test_status_crypto_shows_clear_when_not_halted():
    risk = RiskManager(RiskConfig(halt=False))
    text = bot_main._status_crypto_text({}, {}, risk, live_trading=False, dry_run=True)
    assert "DRY RUN" in text
    assert "clear" in text


def test_status_crypto_includes_per_symbol_block():
    risk = RiskManager(RiskConfig())
    exc = _fake_executor()
    pm  = PositionManager()
    pm.seed(quantity=0.001, avg_entry=88_000.0)
    ss = {"BTC/CAD": {"pm": pm, "last_price": 90_000.0, "strategy": None}}
    text = bot_main._status_crypto_text({"BTC/CAD": exc}, ss, risk, True, False)
    assert "BTC/CAD" in text
    assert "Position: 0.001000" in text


def test_format_symbol_status_pf_no_closed_trades():
    exc = _fake_executor()
    pm  = PositionManager()
    ss  = {"pm": pm, "last_price": 90_000.0, "strategy": None}
    text = bot_main._format_symbol_status("BTC/CAD", exc, ss)
    assert "n/a (no closed trades)" in text


def test_format_symbol_status_pf_no_losses_yet():
    exc = _fake_executor(position=0.0)
    pm  = PositionManager()
    pm.on_buy(price=80_000.0, quantity=0.001)
    pm.on_sell(price=90_000.0, quantity=0.001)   # winning trade only
    ss  = {"pm": pm, "last_price": 90_000.0, "strategy": None}
    text = bot_main._format_symbol_status("BTC/CAD", exc, ss)
    assert "inf (no losses yet)" in text


def test_format_symbol_status_pf_computed_from_wins_and_losses():
    exc = _fake_executor(position=0.0)
    pm  = PositionManager()
    pm.on_buy(price=80_000.0, quantity=0.001)
    pm.on_sell(price=90_000.0, quantity=0.001)   # full round-trip win: +10
    pm.on_buy(price=90_000.0, quantity=0.001)
    pm.on_sell(price=85_000.0, quantity=0.001)   # full round-trip loss: -5
    ss  = {"pm": pm, "last_price": 85_000.0, "strategy": None}
    text = bot_main._format_symbol_status("BTC/CAD", exc, ss)
    assert "PF: 2.00" in text   # 10 / 5


def test_format_symbol_status_reads_regime_from_strategy():
    exc = _fake_executor()
    strat = types.SimpleNamespace(last_regime="TRENDING")
    ss = {"pm": PositionManager(), "last_price": 90_000.0, "strategy": strat}
    text = bot_main._format_symbol_status("BTC/CAD", exc, ss)
    assert "Regime: TRENDING" in text


# ---------------------------------------------------------------------------
# _status_stock_text — read-only, injectable loader
# ---------------------------------------------------------------------------

def test_status_stock_no_state_file():
    text = bot_main._status_stock_text(load_stock_state=lambda: None)
    assert "no state file found" in text


def test_status_stock_formats_paper_state():
    state = {
        "cash": 500.0, "starting_cash": 1000.0, "realized_pnl": 25.5,
        "positions": {"RY": {"shares": 3, "avg_cost": 100.0}},
    }
    text = bot_main._status_stock_text(load_stock_state=lambda: state)
    assert "PAPER" in text
    assert "Open positions: 1" in text
    assert "$500.00" in text


def test_status_stock_formats_ibkr_state():
    state = {
        "cash": 200.0, "starting_cash": 1000.0, "realized_pnl": -5.0,
        "positions": {}, "executor": "ibkr",
    }
    text = bot_main._status_stock_text(load_stock_state=lambda: state)
    assert "IBKR PAPER" in text


def test_status_stock_loader_exception_does_not_raise():
    def _boom():
        raise RuntimeError("disk error")
    text = bot_main._status_stock_text(load_stock_state=_boom)
    assert "Could not read stock bot state" in text


# ---------------------------------------------------------------------------
# _help_crypto_text
# ---------------------------------------------------------------------------

def test_help_crypto_lists_all_commands():
    text = bot_main._help_crypto_text()
    for cmd in ("/status_crypto", "/pause_crypto", "/resume_crypto", "/status_stock"):
        assert cmd in text
