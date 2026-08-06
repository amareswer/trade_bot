"""
Tests for unified_dashboard.py's shadow-match-rate parsing (bug found and
fixed 2026-08-05).

Bug: _read_gate_stats() used an unbounded regex (`Match rate[^0-9]*(...)%`)
to pull the fidelity percentage out of the latest logs/shadow_report_*.md.
When that day's audit found no comparable data (e.g. a Kraken fetch error —
the report shows "Match rate | N/A"), the regex's [^0-9]* wildcard skipped
straight past "N/A" and matched the next unrelated X.XX% pattern later in
the document — BACKTEST_FEE_PCT's "0.80%" — displaying a fabricated 0.8%
fidelity failure on the dashboard instead of "no data, audit failed".

Fix: the regex is now bounded to the "Match rate" table row only (stops at
the newline), and an explicit N/A case is detected and surfaced separately
(out["shadow_na"]) rather than silently falling through to a wrong number.
"""
import pytest

import unified_dashboard as ud


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    return tmp_path


def _write_report(sandbox, filename: str, match_rate_row: str) -> None:
    (sandbox / "logs" / filename).write_text(
        "# Shadow Signal Report\n\n"
        "| Metric | Value |\n"
        "|--------|-------|\n"
        "| Comparable candles | 100 |\n"
        f"| Match rate | {match_rate_row} |\n\n"
        "## Fill fidelity\n\n"
        "BACKTEST_FEE_PCT: **0.80%**  \n"
        "Live fee assumption: 0.40% maker BUY + 0.80% taker SELL = **1.20% round-trip**\n",
        encoding="utf-8",
    )


def test_passing_match_rate_parsed_correctly(sandbox):
    _write_report(sandbox, "shadow_report_20260804.md", "**100.0%** ✓ PASS (≥95%)")
    out = ud._read_gate_stats()
    assert out["shadow"] == pytest.approx(100.0)
    assert out["shadow_na"] is False


def test_failing_but_real_match_rate_parsed_correctly(sandbox):
    _write_report(sandbox, "shadow_report_20260804.md", "**72.5%** ✗ FAIL (<95%)")
    out = ud._read_gate_stats()
    assert out["shadow"] == pytest.approx(72.5)
    assert out["shadow_na"] is False


def test_na_row_does_not_bleed_into_unrelated_fee_percentage(sandbox):
    # This is the exact regression: a "N/A" row used to let the old regex
    # fall through and grab BACKTEST_FEE_PCT's "0.80%" from later in the
    # document, fabricating a fidelity reading that was never computed.
    _write_report(sandbox, "shadow_report_20260805.md", "N/A")
    out = ud._read_gate_stats()
    assert out["shadow"] is None
    assert out["shadow_na"] is True


def test_no_report_file_is_neither_shadow_nor_na(sandbox):
    out = ud._read_gate_stats()
    assert out["shadow"] is None
    assert out["shadow_na"] is False


def test_latest_report_by_filename_is_used_when_multiple_exist(sandbox):
    _write_report(sandbox, "shadow_report_20260803.md", "**100.0%** ✓ PASS (≥95%)")
    _write_report(sandbox, "shadow_report_20260805.md", "N/A")
    out = ud._read_gate_stats()
    assert out["shadow"] is None
    assert out["shadow_na"] is True


# ── Display layer: _gate_tracker_section() renders the right message ────────

def test_na_case_renders_distinct_message_from_never_run(sandbox):
    _write_report(sandbox, "shadow_report_20260805.md", "N/A")
    html_na = ud._gate_tracker_section()
    assert "N/A" in html_na
    assert "Kraken fetch error" in html_na
    assert "0.8%" not in html_na   # the fabricated reading must never appear

    (sandbox / "logs" / "shadow_report_20260805.md").unlink()
    html_never_run = ud._gate_tracker_section()
    assert "run python shadow_signal.py" in html_never_run


# ── _crypto_card: STALE badge vs "no fills, bot alive" (fixed 2026-08-06) ───
#
# Bug: the badge only looked at state-file age (saved_at, which only updates
# on a fill or a restart). BTC/CAD trades every 1-3 weeks, so a perfectly
# healthy bot with zero fills for a week showed the same red "STALE" alarm
# as an actually-hung bot. Fix: cross-check logs/trade_bot.log freshness (the
# same signal the "Crypto Bot" heartbeat card uses) — a fresh log means the
# scan loop is alive and this is just "no fills," not a stall.

def _old_state(hours_old: float) -> dict:
    from datetime import datetime, timedelta, timezone
    saved_at = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
    return {"symbol": "BTC/CAD", "cash": 77.0, "position": 0.0, "cost_basis": 0.0,
            "realized_pnl": 0.0, "fees_paid": 0.0, "saved_at": saved_at}


def test_old_state_with_fresh_log_shows_no_fills_not_stale(monkeypatch):
    monkeypatch.setattr(ud, "_fetch_crypto_price", lambda sym: "$90,000.00")
    monkeypatch.setattr(ud, "_file_age_h", lambda path: 0.5)   # log touched 30 min ago
    html = ud._crypto_card("BTC/CAD", _old_state(hours_old=7 * 24))
    assert "NO FILLS · 7d" in html
    assert "bot alive" in html
    assert "STALE" not in html


def test_old_state_with_stale_log_still_shows_stale(monkeypatch):
    monkeypatch.setattr(ud, "_fetch_crypto_price", lambda sym: "$90,000.00")
    monkeypatch.setattr(ud, "_file_age_h", lambda path: 72.0)   # log itself untouched 3 days
    html = ud._crypto_card("BTC/CAD", _old_state(hours_old=7 * 24))
    assert "STALE · 7d old" in html
    assert "check the bot" in html
    assert "NO FILLS" not in html


def test_fresh_state_shows_live_regardless_of_log_age(monkeypatch):
    monkeypatch.setattr(ud, "_fetch_crypto_price", lambda sym: "$90,000.00")
    monkeypatch.setattr(ud, "_file_age_h", lambda path: 72.0)
    html = ud._crypto_card("BTC/CAD", _old_state(hours_old=1))
    assert "LIVE · BTC/CAD" in html
    assert "STALE" not in html
    assert "NO FILLS" not in html
