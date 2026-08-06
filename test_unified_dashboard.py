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
