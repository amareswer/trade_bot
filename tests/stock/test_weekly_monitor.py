"""
Tests for the weekly stock-bot progress monitor (stock_bot/analysis/weekly_monitor.py).

Report-only tool: verdict logic, log scan bucketing, and the render/run plumbing.
No network, no Telegram (--send is never exercised here).

Run: python -m pytest tests/stock/test_weekly_monitor.py -v
"""
from __future__ import annotations

import json

import stock_bot.analysis.weekly_monitor as wm


# ─────────────────────────── verdict logic ───────────────────────────────────

def _snap(n=7, net_pf=0.8, per_week=1.0, faults=None, dd=2.0,
         kill=False, max_notional=700.0):
    return {
        "generated": "2026-09-07 14:00 UTC",
        "gate3": {"status": "PENDING", "detail": f"{n}/30", "pairs": n,
                  "pf_gross": None, "win_pct": None},
        "net_of_cost": {"n": n, "net_pf": net_pf, "net_win_rate": 50.0,
                        "expectancy_usd": -1.0, "trades_per_week": per_week},
        "open_positions": {"count": 3, "notional": {"T": max_notional},
                           "max_notional": max_notional, "avg_notional": max_notional},
        "account": {"peak_equity": 5000.0, "day_open_equity": 5000.0 * (1 - dd / 100),
                    "cash": 500.0, "starting_cash": 5000.0, "realized_pnl": -30.0,
                    "kill_switch_tripped": kill,
                    "drawdown_from_peak_pct": dd},
        "log": {"faults": faults or {}, "info": {}, "blocked_buys": {},
                "lines_scanned": 10, "window_days": 7, "available": True},
    }


def test_early_when_sample_small():
    head, reasons = wm.verdict(_snap(n=7), prev=None)
    assert head == "EARLY"
    assert any("too small" in r for r in reasons)


def test_early_suppresses_throughput_stalled():
    # low pace, but n<10 and no prior run -> must not be THROUGHPUT_STALLED
    head, _ = wm.verdict(_snap(n=6, per_week=0.3), prev=None)
    assert head == "EARLY"


def test_edge_failing_when_pf_below_one():
    head, reasons = wm.verdict(_snap(n=12, net_pf=0.7), prev=None)
    assert head == "EDGE_FAILING"
    assert any("losing money" in r for r in reasons)


def test_edge_weak_between_one_and_bar():
    head, _ = wm.verdict(_snap(n=20, net_pf=1.1), prev=None)
    assert head == "EDGE_WEAK"


def test_on_track_when_healthy():
    head, _ = wm.verdict(_snap(n=20, net_pf=1.4, per_week=1.5), prev=None)
    assert head == "ON_TRACK"


def test_throughput_stalled_needs_prior_run():
    cur = _snap(n=12, net_pf=1.4, per_week=0.5)
    # no prior run -> just context, no verdict
    head1, _ = wm.verdict(cur, prev=None)
    assert head1 == "ON_TRACK"
    # prior run with same n -> stalled
    prev = _snap(n=12, net_pf=1.4, per_week=0.5)
    head2, reasons = wm.verdict(cur, prev=prev)
    assert head2 == "THROUGHPUT_STALLED"
    assert any("not lifting pace" in r for r in reasons)


def test_throughput_ok_when_moved_this_week():
    cur = _snap(n=15, net_pf=1.4, per_week=0.6)
    prev = _snap(n=11, net_pf=1.4, per_week=0.6)
    head, _ = wm.verdict(cur, prev=prev)
    assert head == "ON_TRACK"


def test_faults_drive_needs_attention():
    head, reasons = wm.verdict(_snap(n=20, net_pf=1.5, faults={"Stuck loop detected": 3}), prev=None)
    assert head == "NEEDS_ATTENTION"
    assert any("Stuck loop" in r for r in reasons)


def test_kill_switch_is_needs_attention():
    head, reasons = wm.verdict(_snap(n=20, net_pf=1.5, kill=True), prev=None)
    assert head == "NEEDS_ATTENTION"
    assert any("kill-switch" in r.lower() for r in reasons)


def test_drawdown_breach_is_needs_attention():
    head, _ = wm.verdict(_snap(n=20, net_pf=1.5, dd=6.0), prev=None)
    assert head == "NEEDS_ATTENTION"


def test_severity_ordering_worst_wins():
    # edge failing AND a fault -> NEEDS_ATTENTION outranks EDGE_FAILING
    head, _ = wm.verdict(
        _snap(n=15, net_pf=0.5, faults={"Order rejected": 2}), prev=None
    )
    assert head == "NEEDS_ATTENTION"


# ─────────────────────────── log scan ────────────────────────────────────────

def test_scan_log_buckets_faults_vs_noise(tmp_path):
    log = tmp_path / "stock_bot.log"
    log.write_text(
        "2026-09-07 09:00:00,000 x INFO API connection failed: ConnectionRefusedError\n"
        "2026-09-07 09:01:00,000 x ERROR STUCK LOOP: buy:PLTR\n"
        "2026-09-07 09:02:00,000 x WARNING CORRELATION GATE: AMD blocked\n"
        "2026-09-07 09:03:00,000 x ERROR nvidia_nim FULL ERROR for HOOD: APITimeoutError\n"
    )
    out = wm._scan_log(days=7, log_file=str(log))
    assert out["available"] is True
    assert "Stuck loop detected" in out["faults"]
    assert any("TWS connection refused" in k for k in out["info"])
    assert any("AI provider timeout" in k for k in out["info"])
    assert "CORRELATION_GATE" in out["blocked_buys"]


def test_scan_log_respects_time_window(tmp_path):
    log = tmp_path / "stock_bot.log"
    log.write_text(
        "2000-01-01 00:00:00,000 x ERROR STUCK LOOP: ancient\n"
        "2026-09-07 09:00:00,000 x ERROR STUCK LOOP: recent\n"
    )
    out = wm._scan_log(days=7, log_file=str(log))
    assert out["faults"].get("Stuck loop detected") == 1


def test_scan_log_missing_file():
    out = wm._scan_log(days=7, log_file="/nonexistent/stock_bot.log")
    assert out["available"] is False
    assert out["faults"] == {}


# ─────────────────────────── render / run ────────────────────────────────────

def test_render_contains_key_sections():
    txt = wm.render(_snap(n=20, net_pf=1.4), prev=None, head="ON_TRACK", reasons=["edge: fine"])
    assert "Gate 3" in txt
    assert "Open positions" in txt
    assert "Log scan" in txt
    assert "never trades" in txt


def test_run_writes_report_and_baseline(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setattr(wm, "_STATE_FILE", str(state))
    monkeypatch.setattr(wm, "snapshot", lambda: _snap(n=8, net_pf=0.9))
    monkeypatch.setattr(wm, "_REPO_DIR", str(tmp_path))
    (tmp_path / "logs").mkdir()

    res = wm.run(send=False, quiet=True, update_baseline=True)
    assert res["verdict"] == "EARLY"
    assert state.exists()
    saved = json.loads(state.read_text())
    assert saved["net_of_cost"]["n"] == 8


def test_run_quiet_suppresses_on_track(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(wm, "_STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(wm, "snapshot", lambda: _snap(n=25, net_pf=1.5, per_week=1.6))
    monkeypatch.setattr(wm, "_REPO_DIR", str(tmp_path))
    (tmp_path / "logs").mkdir()

    res = wm.run(send=False, quiet=True, update_baseline=False)
    assert res["verdict"] == "ON_TRACK"
    assert res["emitted"] is False


def test_main_exit_code_on_fault(tmp_path, monkeypatch):
    monkeypatch.setattr(wm, "_STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(wm, "_REPO_DIR", str(tmp_path))
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(wm, "snapshot",
                        lambda: _snap(n=20, net_pf=1.5, faults={"Order rejected": 3}))
    rc = wm.main(["--no-baseline-update"])
    assert rc == 1
