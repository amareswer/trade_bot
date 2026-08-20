"""
Tests for the 2026-08-20 LiveTradingGate gate-repair pass
(stock_bot/analysis/accuracy_tracker.py).

Gate 1 — rewired to read logs/stock_backtest_latest.json (written by the
CURRENT stock_backtest.py tool) against the current RULE_WHITELIST, instead
of the old hardcoded AAPL/SPY pair reading the dead stock_bot/backtest.py
tool's stale output.

Gate 2 — repurposed from the retired fast/swing book (fast_trades.csv,
FAST_ENABLED=false, frozen since 2026-07-22) to AI confidence-band edge:
does MED/HIGH-confidence AI signal actually predict a winning trade,
independent of whether the rules engine itself has edge.

Gate 3 — threshold raised from 5 round-trips to the documented "Stock
Phase A gate" / "IBKR live go-live" bar: >=30 round-trips, PF>=1.2,
win rate>=30%, all three required.
"""
from __future__ import annotations

import csv
import json
import os

import pytest

import stock_bot.analysis.accuracy_tracker as at_mod
from stock_bot.analysis.accuracy_tracker import LiveTradingGate, _COLS

_WHITELIST = "MRNA,AMD,RY,PLTR,GLD,TD,CM,CSCO,KO,T,CAT,GOOGL,WMT,MSFT,GM,CVX"


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Every test gets its own trades CSVs + backtest JSON — never touches
    the real stock_bot/paper_trades.csv, ibkr_trades.csv, or
    logs/stock_backtest_latest.json."""
    monkeypatch.setattr(at_mod, "_TRADES_CSV", str(tmp_path / "paper_trades.csv"))
    monkeypatch.setattr(at_mod, "_IBKR_CSV", str(tmp_path / "ibkr_trades.csv"))
    monkeypatch.setattr(at_mod, "_LATEST_BACKTEST_JSON", str(tmp_path / "stock_backtest_latest.json"))
    monkeypatch.setenv("RULE_WHITELIST", _WHITELIST)
    yield


def _write_json(path: str, results: list[dict], run_at: str = "2026-08-20T12:00:00+00:00") -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"run_at": run_at, "windows": [0, 750, 500, 250], "results": results}, f)


def _sym_result(symbol: str, verdict: str) -> dict:
    return {"symbol": symbol, "verdict": verdict, "windows": []}


def _write_round_trips(path: str, trips: list[tuple], start_cash: float = 10_000.0) -> None:
    """trips: list of (symbol, entry_price, exit_price, confidence, day_offset).
    Writes a BUY then a SELL row per trip, 1 share each, distinct timestamps."""
    rows = []
    cash = start_cash
    for i, (sym, entry, exit_, conf, day) in enumerate(trips):
        buy_ts  = f"2026-01-{day:02d} 09:30:00"
        sell_ts = f"2026-01-{day:02d} 15:59:00"
        cash -= entry
        rows.append([buy_ts, sym, "BUY", "1.0", f"{entry}", f"{entry}", f"{cash}", "AI:BUY", str(conf)])
        cash += exit_
        rows.append([sell_ts, sym, "SELL", "1.0", f"{exit_}", f"{exit_}", f"{cash}", "SIGNAL", str(conf)])

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(_COLS)
        w.writerows(rows)


def _trips(n_win: int, n_loss: int, confidence: int, win_amt: float = 100.0, loss_amt: float = 50.0):
    """Build n_win winning + n_loss losing round trips at a fixed confidence,
    each on a distinct day (Jan 1..28, wraps if >28 needed — fine for our
    test sizes) so BUY/SELL pairing stays clean per symbol."""
    trips = []
    day = 1
    for _ in range(n_win):
        trips.append(("XYZ", 100.0, 100.0 + win_amt, confidence, day))
        day = day % 28 + 1
    for _ in range(n_loss):
        trips.append(("XYZ", 100.0, 100.0 - loss_amt, confidence, day))
        day = day % 28 + 1
    return trips


# ---------------------------------------------------------------------------
# Gate 1 — backtest walk-forward (current strategy)
# ---------------------------------------------------------------------------

def test_gate1_not_run_when_json_missing():
    result = LiveTradingGate().check_gate1()
    assert result["status"] == "NOT_RUN"
    assert "stock_backtest_latest.json" in result["detail"]


def test_gate1_malformed_json_fails_safely(tmp_path):
    with open(at_mod._LATEST_BACKTEST_JSON, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    result = LiveTradingGate().check_gate1()   # must not raise
    assert result["status"] == "FAIL"
    assert "unreadable" in result["detail"]


def test_gate1_pass_when_all_whitelist_symbols_pass():
    syms = _WHITELIST.split(",")
    _write_json(at_mod._LATEST_BACKTEST_JSON, [_sym_result(s, "PASS") for s in syms])
    result = LiveTradingGate().check_gate1()
    assert result["status"] == "PASS"
    assert result["passing_count"] == len(syms)
    assert result["total_count"] == len(syms)


def test_gate1_fail_when_one_whitelist_symbol_fails():
    syms = _WHITELIST.split(",")
    results = [_sym_result(s, "PASS") for s in syms]
    results[0] = _sym_result(syms[0], "FAIL")
    _write_json(at_mod._LATEST_BACKTEST_JSON, results)
    result = LiveTradingGate().check_gate1()
    assert result["status"] == "FAIL"
    assert syms[0] in result["detail"]


def test_gate1_fail_when_whitelist_symbol_missing_from_run():
    syms = _WHITELIST.split(",")
    # Only run the first symbol — the rest were never validated this run.
    _write_json(at_mod._LATEST_BACKTEST_JSON, [_sym_result(syms[0], "PASS")])
    result = LiveTradingGate().check_gate1()
    assert result["status"] == "FAIL"
    assert "not in latest run" in result["detail"]
    assert result["passing_count"] == 1


def test_gate1_ignores_non_whitelist_symbols_in_json():
    """A symbol that passed but isn't on RULE_WHITELIST (e.g. AAPL, from the
    default WATCHLIST run) must not count toward or against the gate."""
    syms = _WHITELIST.split(",")
    results = [_sym_result(s, "PASS") for s in syms]
    results.append(_sym_result("AAPL", "FAIL"))   # not whitelisted — irrelevant
    _write_json(at_mod._LATEST_BACKTEST_JSON, results)
    result = LiveTradingGate().check_gate1()
    assert result["status"] == "PASS"
    assert result["total_count"] == len(syms)   # AAPL not counted in total


def test_gate1_fail_when_whitelist_empty(monkeypatch):
    monkeypatch.setenv("RULE_WHITELIST", "")
    _write_json(at_mod._LATEST_BACKTEST_JSON, [])
    result = LiveTradingGate().check_gate1()
    assert result["status"] == "FAIL"
    assert "empty" in result["detail"]


# ---------------------------------------------------------------------------
# Gate 2 — AI confidence-band edge
# ---------------------------------------------------------------------------

def test_gate2_pending_below_min_trades():
    _write_round_trips(at_mod._TRADES_CSV, _trips(3, 2, confidence=85))   # 5 < 10
    result = LiveTradingGate().check_gate2()
    assert result["status"] == "PENDING"
    assert result["trades"] == 5


def test_gate2_pass_when_win_rate_meets_threshold():
    # 6 wins / 4 losses = 60% >= 55%, n=10 >= min
    _write_round_trips(at_mod._TRADES_CSV, _trips(6, 4, confidence=85))
    result = LiveTradingGate().check_gate2()
    assert result["status"] == "PASS"
    assert result["trades"] == 10
    assert result["win_pct"] == pytest.approx(60.0)


def test_gate2_fail_when_win_rate_below_threshold():
    # 4 wins / 6 losses = 40% < 55%, n=10 >= min
    _write_round_trips(at_mod._TRADES_CSV, _trips(4, 6, confidence=85))
    result = LiveTradingGate().check_gate2()
    assert result["status"] == "FAIL"
    assert result["win_pct"] == pytest.approx(40.0)


def test_gate2_ignores_low_and_pre_confidence_trades():
    """LOW (70-79) and PRE (<70) confidence trades must not count toward
    Gate 2's MED/HIGH tally, even though they're in the same book Gate 3
    reads in full."""
    med_high = _trips(6, 4, confidence=85)       # 10 MED trades, 60% win
    noise    = _trips(0, 20, confidence=50)       # 20 PRE-band losing trades
    _write_round_trips(at_mod._TRADES_CSV, med_high + noise)
    result = LiveTradingGate().check_gate2()
    assert result["status"] == "PASS"
    assert result["trades"] == 10   # the 20 PRE-band trades excluded entirely
    assert result["win_pct"] == pytest.approx(60.0)


def test_gate2_reads_active_book_not_retired_fast_book():
    """Structural guard: the retired fast/swing book has no wiring left in
    this file at all — confirms Gate 2 was actually repurposed, not just
    relabeled."""
    assert not hasattr(at_mod, "_FAST_TRADES_CSV")


# ---------------------------------------------------------------------------
# Gate 3 — position book (live), raised threshold
# ---------------------------------------------------------------------------

def test_gate3_pending_below_30_trades():
    _write_round_trips(at_mod._TRADES_CSV, _trips(3, 2, confidence=85))   # 5 < 30
    result = LiveTradingGate().check_gate3()
    assert result["status"] == "PENDING"
    assert result["pairs"] == 5


def test_gate3_pass_when_all_three_criteria_met():
    # 12 wins @ +$100, 18 losses @ -$50 -> PF = 1200/900 = 1.33, win rate 40%
    trips = _trips(12, 18, confidence=85, win_amt=100.0, loss_amt=50.0)
    _write_round_trips(at_mod._TRADES_CSV, trips)
    result = LiveTradingGate().check_gate3()
    assert result["status"] == "PASS"
    assert result["pairs"] == 30
    assert result["pf"] >= 1.2
    assert result["win_pct"] >= 30.0


def test_gate3_fails_on_pf_even_with_enough_trades_and_win_rate():
    """2 of 3 criteria pass (trade count >=30, win rate 40% >=30%) but PF
    is under 1.2 — must report FAIL, not PASS."""
    # 12 wins @ +$10, 18 losses @ -$10 -> PF = 120/180 = 0.667 < 1.2
    trips = _trips(12, 18, confidence=85, win_amt=10.0, loss_amt=10.0)
    _write_round_trips(at_mod._TRADES_CSV, trips)
    result = LiveTradingGate().check_gate3()
    assert result["status"] == "FAIL"
    assert result["pairs"] == 30
    assert result["pf"] < 1.2
    assert "PF" in result["detail"]


def test_gate3_fails_on_win_rate_even_with_enough_trades_and_pf():
    """2 of 3 criteria pass (trade count >=30, PF >=1.2) but win rate is
    under 30% — must report FAIL, not PASS."""
    # 8 wins @ +$100, 22 losses @ -$30 -> PF = 800/660 = 1.21 >= 1.2, win rate 26.7% < 30%
    trips = _trips(8, 22, confidence=85, win_amt=100.0, loss_amt=30.0)
    _write_round_trips(at_mod._TRADES_CSV, trips)
    result = LiveTradingGate().check_gate3()
    assert result["status"] == "FAIL"
    assert result["pairs"] == 30
    assert result["pf"] >= 1.2
    assert result["win_pct"] < 30.0
    assert "win rate" in result["detail"]


def test_gate3_label_no_longer_references_swing_book():
    """The old 'Swing paper (daily)' label was stale — this gate reads the
    active Mode A/B position book, not the retired swing/fast book."""
    descriptions = [g["description"] for g in LiveTradingGate().evaluate()]
    gate3_desc = descriptions[2]
    assert "swing" not in gate3_desc.lower()


# ---------------------------------------------------------------------------
# evaluate() / get_gate_status() — end-to-end wiring
# ---------------------------------------------------------------------------

def test_get_gate_status_reports_all_four_gates_and_thresholds():
    status = LiveTradingGate().get_gate_status()
    assert len(status["gates"]) == 4
    assert status["thresholds"]["gate3_min_trades"] == 30
    assert status["thresholds"]["gate3_min_pf"] == 1.2
    assert status["thresholds"]["gate3_min_win_pct"] == 30.0
    assert "ready" in status and "remaining" in status
