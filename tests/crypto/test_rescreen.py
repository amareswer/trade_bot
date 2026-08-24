"""Tests for rescreen.py's USD screening leg (added 2026-08-24).

Covers the automation gap fix: CLAUDE.md long claimed USD re-screening was
"automated monthly via rescreen.py" when the code never actually passed
SCREEN_QUOTE=USD anywhere. These tests cover the new leg running with the
correct env override, its results landing in the report correctly, and a
regression check that the existing CAD leg's behavior is unaffected.

Also covers the _alert() nested-config-attribute bug found and fixed in the
same pass (cfg.telegram_bot_token → cfg.alerts.telegram_bot_token etc.).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import rescreen
from config import cfg


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


# ── _crypto_usd_whitelist() ─────────────────────────────────────────────────

def test_crypto_usd_whitelist_empty_when_only_cad_whitelisted(monkeypatch):
    monkeypatch.setattr(cfg.universe, "universe_whitelist", "BTC/CAD")
    assert rescreen._crypto_usd_whitelist() == set()


def test_crypto_usd_whitelist_filters_usd_suffix(monkeypatch):
    monkeypatch.setattr(cfg.universe, "universe_whitelist", "BTC/CAD,SOL/USD")
    assert rescreen._crypto_usd_whitelist() == {"SOL/USD"}


def test_crypto_usd_whitelist_empty_string_is_empty_set(monkeypatch):
    monkeypatch.setattr(cfg.universe, "universe_whitelist", "")
    assert rescreen._crypto_usd_whitelist() == set()


def test_crypto_whitelist_unaffected_by_usd_filter_addition(monkeypatch):
    """Regression: _crypto_whitelist() (the pre-existing CAD comparison)
    still returns the whole whitelist unfiltered, not just the CAD subset —
    adding _crypto_usd_whitelist() must not have changed its behavior."""
    monkeypatch.setattr(cfg.universe, "universe_whitelist", "BTC/CAD,SOL/USD")
    assert rescreen._crypto_whitelist() == {"BTC/CAD", "SOL/USD"}


# ── run(): USD leg wiring ────────────────────────────────────────────────────

def _make_run_env(tmp_path, monkeypatch, whitelist="BTC/CAD"):
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(rescreen, "_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(cfg.universe, "universe_whitelist", whitelist)
    monkeypatch.setenv("RESCREEN_SKIP_STOCKS", "true")  # keep these tests focused on the crypto legs
    # _alert()'s real 5s post-send sleep (daemon-thread hand-off margin) has
    # no purpose in a test where TelegramAlerter._send is already a no-op
    # (conftest.py autouse fixture) — skip it so a run() test with any
    # attention-worthy result doesn't cost 5 real seconds.
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)


def test_usd_leg_runs_with_correct_env_override(tmp_path, monkeypatch):
    _make_run_env(tmp_path, monkeypatch)
    calls = []

    def _fake_run_gate(script, extra_env=None):
        calls.append((script, extra_env))
        return 0, "PASS (0): \n"

    monkeypatch.setattr(rescreen, "_run_gate", _fake_run_gate)
    rescreen.run()

    assert ("screen_universe.py", None) in calls              # CAD leg — unchanged
    assert ("screen_universe.py", {"SCREEN_QUOTE": "USD"}) in calls  # USD leg — new
    assert len(calls) == 2  # CAD + USD only, stocks skipped


def test_usd_leg_results_land_in_report_correctly(tmp_path, monkeypatch):
    _make_run_env(tmp_path, monkeypatch)

    def _fake_run_gate(script, extra_env=None):
        if extra_env and extra_env.get("SCREEN_QUOTE") == "USD":
            return 0, "some gate output\nPASS (1): SOL/USD\n"
        return 0, "PASS (0): \n"

    monkeypatch.setattr(rescreen, "_run_gate", _fake_run_gate)
    rescreen.run()

    report = (tmp_path / "logs" / f"rescreen_{_today()}.md").read_text()

    assert "## crypto-usd (screen_universe.py)" in report
    assert "## crypto (screen_universe.py)" in report          # CAD section still present, distinct header
    usd_section = report.split("## crypto-usd")[1].split("## stocks")[0] if "## stocks" in report else report.split("## crypto-usd")[1]
    assert "PASS: SOL/USD" in usd_section
    assert "🆕 NEW QUALIFIERS — passed but not whitelisted: SOL/USD" in usd_section
    assert "whitelist now: (none)" in usd_section  # nothing USD-whitelisted today


def test_cad_leg_unaffected_by_usd_addition_regression(tmp_path, monkeypatch):
    """The CAD leg's own behavior (whitelist comparison, no extra_env,
    section content) must be identical to before the USD leg existed."""
    _make_run_env(tmp_path, monkeypatch, whitelist="BTC/CAD")

    def _fake_run_gate(script, extra_env=None):
        if extra_env is None:
            return 0, "PASS (1): BTC/CAD\n"   # CAD leg: whitelist matches evidence
        return 0, "PASS (0): \n"

    monkeypatch.setattr(rescreen, "_run_gate", _fake_run_gate)
    rescreen.run()

    report = (tmp_path / "logs" / f"rescreen_{_today()}.md").read_text()
    cad_section = report.split("## crypto (screen_universe.py)")[1].split("## crypto-usd")[0]
    assert "whitelist now: BTC/CAD" in cad_section
    assert "✓ no changes — whitelist matches the evidence" in cad_section
    assert "🔻" not in cad_section
    assert "🆕" not in cad_section


def test_rescreen_skip_usd_env_var(tmp_path, monkeypatch):
    _make_run_env(tmp_path, monkeypatch)
    monkeypatch.setenv("RESCREEN_SKIP_USD", "true")
    calls = []

    def _fake_run_gate(script, extra_env=None):
        calls.append((script, extra_env))
        return 0, "PASS (0): \n"

    monkeypatch.setattr(rescreen, "_run_gate", _fake_run_gate)
    rescreen.run()
    assert calls == [("screen_universe.py", None)]   # only CAD ran, USD and stocks both skipped


def test_usd_leg_failure_reported_like_cad_failure(tmp_path, monkeypatch):
    """A gate-script failure on the USD leg surfaces the same way the
    existing rc!=0 handling already does for CAD/stocks — no special-casing
    needed, but worth locking in since it's new code exercising that path."""
    _make_run_env(tmp_path, monkeypatch)

    def _fake_run_gate(script, extra_env=None):
        if extra_env and extra_env.get("SCREEN_QUOTE") == "USD":
            return 1, "boom"
        return 0, "PASS (0): \n"

    monkeypatch.setattr(rescreen, "_run_gate", _fake_run_gate)
    rescreen.run()

    report = (tmp_path / "logs" / f"rescreen_{_today()}.md").read_text()
    usd_section = report.split("## crypto-usd")[1]
    assert "⚠ gate script exited rc=1 — result unusable" in usd_section


# ── _alert(): nested config attribute fix ───────────────────────────────────

def test_alert_uses_nested_alerts_config_not_flat_attrs(capsys, monkeypatch):
    """Regression for the AttributeError bug: cfg.telegram_bot_token etc.
    don't exist on AppConfig (they live under cfg.alerts.*). Before the fix,
    every call here printed '(Telegram alert failed: ...)' to stdout —
    TelegramAlerter._send is a no-op in tests (conftest.py autouse fixture),
    so this exercises the real construction path with no real network call."""
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    rescreen._alert(["test line"])
    captured = capsys.readouterr()
    assert "Telegram alert failed" not in captured.out


def test_alert_constructs_telegram_alerter_with_cfg_alerts_values(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    with patch("bot.alerts.telegram.TelegramAlerter") as MockAlerter:
        rescreen._alert(["test line"])
        MockAlerter.assert_called_once_with(
            cfg.alerts.telegram_bot_token,
            cfg.alerts.telegram_chat_id,
            cfg.alerts.telegram_enabled,
        )
