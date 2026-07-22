"""
Unit tests for the paper-report expectancy math (net of commissions).

The expectancy number is the product of the stock bot's paper phase: it is
what converts the paper book into an income projection. It must be net of
the IBKR Pro commission model (env-driven) — slippage is already applied to
fill prices by the paper executor and must NOT be double-counted here.

Run: python -m pytest test_paper_report.py -v
"""
from __future__ import annotations

import os
import tempfile

from stock_bot.analysis.paper_report import (
    _expectancy_stats,
    _pair_trades,
    _round_trip_commission,
    generate_report,
    read_position_book,
)


def test_commission_us_minimum_applies():
    # 2 shares × $0.005 = $0.01 → minimum $1.00 per side → $2.00 round trip
    assert _round_trip_commission("DLTR", 2) == 2.00


def test_commission_tsx_per_share_above_minimum():
    # 500 shares × $0.01 = $5.00 per side → $10.00 round trip
    assert _round_trip_commission("AC.TO", 500) == 10.00


def _pair(symbol, shares, entry, exit_, exit_date="2026-07-01"):
    return {
        "symbol": symbol, "shares": shares,
        "entry_price": entry, "exit_price": exit_,
        "pnl": round((exit_ - entry) * shares, 2),
        "pnl_pct": round((exit_ - entry) / entry * 100, 2),
        "entry_date": "2026-06-24", "exit_date": exit_date,
        "exit_reason": "TP", "hold_days": 3,
    }


def test_expectancy_nets_out_commissions():
    # One US trade: +$10 gross on 2 shares → $2 round-trip commission → +$8 net
    pairs = [_pair("DLTR", 2, 100.0, 105.0)]
    exp = _expectancy_stats(pairs)
    assert exp is not None
    assert abs(exp["expectancy_usd"] - 8.0) < 1e-9
    # net % is on position value (200): 8/200 = 4%
    assert abs(exp["expectancy_pct"] - 4.0) < 1e-9
    assert exp["net_win_rate"] == 100.0


def test_expectancy_commission_can_flip_small_win_to_loss():
    # +$1.50 gross on a tiny position − $2.00 commission = −$0.50 net
    pairs = [_pair("DLTR", 1, 100.0, 101.5)]
    exp = _expectancy_stats(pairs)
    assert exp["expectancy_usd"] < 0, "commission must be able to flip small wins"
    assert exp["net_win_rate"] == 0.0


def test_expectancy_none_without_pairs():
    assert _expectancy_stats([]) is None


def test_generate_report_renders_expectancy_section():
    tmp = tempfile.mkdtemp()
    trades_csv = os.path.join(tmp, "paper_trades.csv")
    fast_csv   = os.path.join(tmp, "fast_trades.csv")   # absent — must not crash
    with open(trades_csv, "w", encoding="utf-8") as f:
        f.write(
            "timestamp,symbol,side,shares,price,total_value,cash_remaining,reason,confidence\n"
            "2026-06-24 10:00:00,DLTR,BUY,2.0000,100.0000,200.00,800.00,BUY 70% LONGTERM,70\n"
            "2026-07-01 10:00:00,DLTR,SELL,2.0000,110.0000,220.00,1020.00,TAKE_PROFIT_HIT,70\n"
        )
    report = generate_report(
        csv_path=trades_csv,
        state_path=os.path.join(tmp, "missing_state.json"),
        fast_csv_path=fast_csv,
        ibkr_csv_path=os.path.join(tmp, "missing_ibkr.csv"),
        ibkr_state_path=os.path.join(tmp, "missing_ibkr_state.json"),
    )
    assert "EXPECTANCY — NET OF COMMISSIONS" in report
    # $20 gross − $2 commission = $18 net on one trade
    assert "+18.00" in report
    assert "SWING BOOK" in report


_CSV_HEADER = "timestamp,symbol,side,shares,price,total_value,cash_remaining,reason,confidence\n"


def test_position_book_merges_paper_and_ibkr_csvs():
    # The Phase A gate counts strategy trades across the 2026-07-17 executor
    # switch: pairs completed in the sim book AND pairs filled via IBKR must
    # both appear, in timestamp order.
    tmp = tempfile.mkdtemp()
    paper_csv = os.path.join(tmp, "paper_trades.csv")
    ibkr_csv  = os.path.join(tmp, "ibkr_trades.csv")
    with open(paper_csv, "w", encoding="utf-8") as f:
        f.write(
            _CSV_HEADER
            + "2026-06-24 10:00:00,DLTR,BUY,2.0000,100.0000,200.00,800.00,BUY 70%,70\n"
            + "2026-07-17 10:00:00,DLTR,SELL,2.0000,110.0000,220.00,1020.00,EXECUTOR_SWITCH_TO_IBKR,0\n"
        )
    with open(ibkr_csv, "w", encoding="utf-8") as f:
        f.write(
            _CSV_HEADER
            + "2026-07-20 10:00:00,KO,BUY,2.0000,80.0000,160.00,835.30,RULE BUY,60\n"
            + "2026-07-25 10:00:00,KO,SELL,2.0000,85.0000,170.00,1005.30,TAKE_PROFIT_HIT,0\n"
        )
    trades = read_position_book(paper_csv, ibkr_csv)
    assert [t["symbol"] for t in trades] == ["DLTR", "DLTR", "KO", "KO"]
    pairs, open_pos = _pair_trades(trades)
    assert len(pairs) == 2
    assert open_pos == {}
    # Missing IBKR file must not change the sim-only view
    solo = read_position_book(paper_csv, os.path.join(tmp, "nope.csv"))
    assert len(solo) == 2


def test_generate_report_shows_ibkr_account_when_state_exists():
    import json
    tmp = tempfile.mkdtemp()
    paper_csv  = os.path.join(tmp, "paper_trades.csv")
    ibkr_csv   = os.path.join(tmp, "ibkr_trades.csv")
    ibkr_state = os.path.join(tmp, "ibkr_state.json")
    with open(paper_csv, "w", encoding="utf-8") as f:
        f.write(_CSV_HEADER)
    with open(ibkr_csv, "w", encoding="utf-8") as f:
        f.write(
            _CSV_HEADER
            + "2026-07-20 10:00:00,KO,BUY,2.0000,80.0000,160.00,835.30,RULE BUY,60\n"
        )
    with open(ibkr_state, "w", encoding="utf-8") as f:
        json.dump({"account": "DUQ273338", "realized_pnl": 0.0,
                   "starting_cash": 995.30}, f)
    report = generate_report(
        csv_path=paper_csv,
        state_path=os.path.join(tmp, "missing_state.json"),
        fast_csv_path=os.path.join(tmp, "missing_fast.csv"),
        ibkr_csv_path=ibkr_csv,
        ibkr_state_path=ibkr_state,
    )
    assert "IBKR paper DUQ273338" in report
    # Current cash comes from the last IBKR fill's cash_remaining
    assert "835.30" in report
    # Starting cash comes from ibkr_state.json
    assert "995.30" in report


def test_load_active_book_state_ibkr_branch():
    # ibkr mode synthesizes the paper_state.json shape from ibkr files:
    # cash from the last fill's cash_remaining, positions from unpaired BUYs.
    import json
    import stock_bot.analysis.paper_report as pr
    tmp = tempfile.mkdtemp()
    ibkr_csv   = os.path.join(tmp, "ibkr_trades.csv")
    ibkr_state = os.path.join(tmp, "ibkr_state.json")
    with open(ibkr_csv, "w", encoding="utf-8") as f:
        f.write(
            _CSV_HEADER
            + "2026-07-20 10:00:00,KO,BUY,2.0000,80.0000,160.00,835.30,RULE BUY,60\n"
        )
    with open(ibkr_state, "w", encoding="utf-8") as f:
        json.dump({"account": "DUQ273338", "realized_pnl": 1.5,
                   "starting_cash": 995.30, "last_updated": "2026-07-20T10:00:01"}, f)

    saved = (pr._IBKR_CSV, pr._IBKR_STATE_JSON, os.environ.get("STOCK_EXECUTOR"))
    pr._IBKR_CSV, pr._IBKR_STATE_JSON = ibkr_csv, ibkr_state
    os.environ["STOCK_EXECUTOR"] = "ibkr"
    try:
        state = pr.load_active_book_state()
    finally:
        pr._IBKR_CSV, pr._IBKR_STATE_JSON = saved[0], saved[1]
        if saved[2] is None:
            os.environ.pop("STOCK_EXECUTOR", None)
        else:
            os.environ["STOCK_EXECUTOR"] = saved[2]

    assert state["executor"] == "ibkr"
    assert state["account"] == "DUQ273338"
    assert state["cash"] == 835.30
    assert state["starting_cash"] == 995.30
    assert state["realized_pnl"] == 1.5
    assert state["positions"] == {"KO": {"shares": 2.0, "avg_cost": 80.0}}


def test_ibkr_cash_prefers_live_state_snapshot_over_csv():
    # The fill CSV's cash_remaining can be a stale/transient snapshot (e.g. the
    # 2026-07-20 reset-window fill recorded $6,000). When ibkr_state.json holds
    # a live "cash" value the report and active-book state must prefer it.
    import json
    import stock_bot.analysis.paper_report as pr
    tmp = tempfile.mkdtemp()
    paper_csv  = os.path.join(tmp, "paper_trades.csv")
    ibkr_csv   = os.path.join(tmp, "ibkr_trades.csv")
    ibkr_state = os.path.join(tmp, "ibkr_state.json")
    with open(paper_csv, "w", encoding="utf-8") as f:
        f.write(_CSV_HEADER)
    with open(ibkr_csv, "w", encoding="utf-8") as f:
        f.write(
            _CSV_HEADER
            + "2026-07-20 10:00:00,CM,BUY,10.0000,117.7200,1177.20,6000.00,RULE BUY,70\n"
        )
    with open(ibkr_state, "w", encoding="utf-8") as f:
        json.dump({"account": "DUQ273338", "realized_pnl": 0.0,
                   "starting_cash": 5000.0, "cash": 3337.56,
                   "last_updated": "2026-07-21T19:15:05"}, f)

    report = generate_report(
        csv_path=paper_csv,
        state_path=os.path.join(tmp, "missing_state.json"),
        fast_csv_path=os.path.join(tmp, "missing_fast.csv"),
        ibkr_csv_path=ibkr_csv,
        ibkr_state_path=ibkr_state,
    )
    assert "3,337.56" in report          # live snapshot wins
    assert "6,000.00" not in report      # stale CSV value not shown as cash

    saved = (pr._IBKR_CSV, pr._IBKR_STATE_JSON, os.environ.get("STOCK_EXECUTOR"))
    pr._IBKR_CSV, pr._IBKR_STATE_JSON = ibkr_csv, ibkr_state
    os.environ["STOCK_EXECUTOR"] = "ibkr"
    try:
        state = pr.load_active_book_state()
    finally:
        pr._IBKR_CSV, pr._IBKR_STATE_JSON = saved[0], saved[1]
        if saved[2] is None:
            os.environ.pop("STOCK_EXECUTOR", None)
        else:
            os.environ["STOCK_EXECUTOR"] = saved[2]
    assert state["cash"] == 3337.56


if __name__ == "__main__":
    import sys
    failures = 0
    for t in [
        test_commission_us_minimum_applies,
        test_commission_tsx_per_share_above_minimum,
        test_expectancy_nets_out_commissions,
        test_expectancy_commission_can_flip_small_win_to_loss,
        test_expectancy_none_without_pairs,
        test_generate_report_renders_expectancy_section,
        test_position_book_merges_paper_and_ibkr_csvs,
        test_generate_report_shows_ibkr_account_when_state_exists,
        test_load_active_book_state_ibkr_branch,
    ]:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failures += 1
    sys.exit(failures)
