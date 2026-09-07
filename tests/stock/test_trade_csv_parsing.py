"""
Regression tests for _row_to_trade — the shared trade-CSV row parser used by
both paper_report and accuracy_tracker (LiveTradingGate Gate 3).

2026-09-07: a hand-backfilled row in ibkr_trades.csv had an UNQUOTED comma
inside the free-text `reason` field ("... flicker bug, ibkr.py fix ..."),
which csv.reader over-split into 10 columns. The old per-reader try/except
then zeroed `price` and `shares` when the shifted `confidence` field failed
to parse — turning RY's real +$6.32 round-trip into a phantom -$842.20 /
-100% loss that fed straight into the go-live gate math.

Run: python -m pytest tests/stock/test_trade_csv_parsing.py -v
"""
from __future__ import annotations

import csv

from stock_bot.analysis.accuracy_tracker import ConfidenceBandTracker
from stock_bot.analysis.paper_report import _pair_trades, _read_trades, _row_to_trade

_HEADER = "timestamp,symbol,side,shares,price,total_value,cash_remaining,reason,confidence\n"


def _row(line: str) -> list[str]:
    return next(csv.reader([line]))


def test_clean_row_parses():
    t = _row_to_trade(_row(
        "2026-08-25 13:52:25,T,BUY,28.0000,25.7100,719.88,5004.90,RULE BUY rsi=71,65"
    ))
    assert t is not None
    assert t["symbol"] == "T"
    assert t["shares"] == 28.0
    assert t["price"] == 25.71
    assert t["confidence"] == 65


def test_header_and_junk_rows_rejected():
    assert _row_to_trade(_row(_HEADER.strip())) is None
    assert _row_to_trade([]) is None
    assert _row_to_trade(["not-a-timestamp", "x"]) is None


def test_unquoted_comma_in_reason_recovered():
    # 10 fields — the reason has a bare comma. Numeric columns 0-6 must survive.
    line = ("2026-08-19 10:12:12,RY,SELL,4.0000,212.1300,848.52,3867.12,"
            "SELL (reconciled — flicker bug, ibkr.py fix 2026-08-19),0")
    t = _row_to_trade(_row(line))
    assert t is not None
    assert t["shares"] == 4.0
    assert t["price"] == 212.13
    assert t["total_value"] == 848.52
    assert t["confidence"] == 0
    assert t["reason"] == "SELL (reconciled — flicker bug, ibkr.py fix 2026-08-19)"


def test_bad_confidence_does_not_zero_price():
    t = _row_to_trade(_row(
        "2026-08-19 10:12:12,RY,SELL,4.0000,212.1300,848.52,3867.12,note,notanumber"
    ))
    assert t is not None
    assert t["price"] == 212.13
    assert t["shares"] == 4.0
    assert t["confidence"] == 0


def test_missing_confidence_column_defaults_zero():
    t = _row_to_trade(_row(
        "2026-07-01 09:30:00,AAPL,BUY,5.0000,190.0000,950.00,1000.00,RULE BUY"
    ))
    assert t is not None
    assert t["confidence"] == 0
    assert t["price"] == 190.0


def test_ry_round_trip_values_correctly_end_to_end(tmp_path):
    csv_path = tmp_path / "ibkr_trades.csv"
    csv_path.write_text(
        _HEADER
        + "2026-07-31 09:48:02,RY,BUY,4.0000,210.5500,842.20,4660.28,RULE BUY | ai=HOLD50,50\n"
        + "2026-08-19 10:12:12,RY,SELL,4.0000,212.1300,848.52,3867.12,"
          "SELL (reconciled — flicker bug, ibkr.py fix),0\n"
    )
    trades = _read_trades(str(csv_path))
    assert len(trades) == 2
    pairs, _ = _pair_trades(trades)
    assert len(pairs) == 1
    assert pairs[0]["pnl"] == round((212.13 - 210.55) * 4, 2)   # +6.32, not -842.20
    assert pairs[0]["pnl"] > 0


def test_accuracy_tracker_uses_the_shared_parser(tmp_path):
    csv_path = tmp_path / "book.csv"
    csv_path.write_text(
        _HEADER
        + "2026-07-31 09:48:02,RY,BUY,4.0000,210.5500,842.20,4660.28,buy,50\n"
        + "2026-08-19 10:12:12,RY,SELL,4.0000,212.1300,848.52,3867.12,sell a, b,0\n"
    )
    tracker = ConfidenceBandTracker()
    trades = tracker.load_trades(str(csv_path))
    assert len(trades) == 2
    assert all(t["price"] > 0 for t in trades)
    pairs = tracker.pair_trades(trades)
    assert pairs and pairs[0]["pnl_pct"] > 0
