"""
Tests for config.py's execution/config fingerprint drift guard, added to
AppConfig.log_startup() alongside the existing strategy-hash check.

2026-09-18 follow-up review finding (P2): compute_execution_hash() /
compute_full_run_fingerprint() existed in bot/strategy/fingerprint.py with
no production caller — defining the functions alone strengthens nothing.
Wired into config.py's log_startup(), the one place both bots' config
validation already runs at every real startup.
"""
from __future__ import annotations

import logging

from bot.strategy.fingerprint import compute_execution_hash
from config import cfg


def _current_exec_snapshot() -> dict:
    """Mirrors exactly the snapshot config.py's log_startup() builds —
    kept in sync deliberately (same values, read the same way)."""
    return {
        "order_type":                 cfg.exchange.order_type,
        "limit_order_enabled":        cfg.exchange.limit_order_enabled,
        "native_stop_loss_enabled":   cfg.exchange.native_stop_loss_enabled,
        "max_slippage_pct":           cfg.exchange.max_slippage_pct,
        "atr_sizing_enabled":         cfg.strategy.atr_sizing_enabled,
        "atr_sl_mult":                cfg.strategy.atr_sl_mult,
        "risk_max_position_pct":      cfg.risk.max_position_pct,
        "risk_daily_loss_limit_pct":  cfg.risk.daily_loss_limit_pct,
        "risk_max_drawdown_pct":      cfg.risk.max_drawdown_pct,
        "risk_max_trades_per_day":    cfg.risk.max_trades_per_day,
        "risk_weekly_loss_limit_pct": cfg.risk.weekly_loss_limit_pct,
        "risk_kill_switch_pct":       cfg.risk.kill_switch_pct,
    }


def test_log_startup_logs_execution_hash(caplog, monkeypatch, tmp_path):
    monkeypatch.setenv("EXECUTION_HASH_FILE", str(tmp_path / "no_such_file"))
    with caplog.at_level(logging.INFO, logger="config"):
        cfg.log_startup()
    assert any("execution_hash=" in r.message for r in caplog.records)


def test_log_startup_no_warning_when_hash_matches(caplog, monkeypatch, tmp_path):
    hash_file = tmp_path / "validated_execution_hash"
    hash_file.write_text(compute_execution_hash(_current_exec_snapshot()) + "\n")
    monkeypatch.setenv("EXECUTION_HASH_FILE", str(hash_file))

    with caplog.at_level(logging.WARNING, logger="config"):
        cfg.log_startup()

    assert not any("EXECUTION/CONFIG DIFFERS" in r.message for r in caplog.records)


def test_log_startup_warns_when_hash_mismatches(caplog, monkeypatch, tmp_path):
    hash_file = tmp_path / "validated_execution_hash"
    hash_file.write_text("deadbeefdeadbeef\n")
    monkeypatch.setenv("EXECUTION_HASH_FILE", str(hash_file))

    with caplog.at_level(logging.WARNING, logger="config"):
        cfg.log_startup()

    assert any("EXECUTION/CONFIG DIFFERS" in r.message for r in caplog.records)


def test_log_startup_silent_when_no_hash_file_exists(caplog, monkeypatch, tmp_path):
    """No stamp yet (first run, or stamp_strategy.py never run) — no
    warning, same as the existing strategy-hash check's own behavior."""
    monkeypatch.setenv("EXECUTION_HASH_FILE", str(tmp_path / "does_not_exist"))

    with caplog.at_level(logging.WARNING, logger="config"):
        cfg.log_startup()

    assert not any("EXECUTION/CONFIG DIFFERS" in r.message for r in caplog.records)


def test_execution_snapshot_reflects_real_config_values():
    """Sanity: the snapshot this test (and config.py) builds is not empty
    and reads real values off the live cfg singleton, not placeholders."""
    snap = _current_exec_snapshot()
    assert snap["order_type"] == cfg.exchange.order_type
    assert isinstance(snap["risk_max_position_pct"], float)
