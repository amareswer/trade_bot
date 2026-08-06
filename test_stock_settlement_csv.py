"""
Tests for the settlement/FX tax-record-keeping CSV (added 2026-08-05,
punch-list item #9 — Canadian ACB/FX record-keeping, minimal scope: capture
the fields, no gain computation or report).

Critical invariant under test: paper_trades.csv / ibkr_trades.csv (the
FROZEN 9-column schema — see CLAUDE.md hard rules, ConfidenceBandTracker
depends on it exactly) are NEVER modified by this feature. Settlement date
+ FX rate at trade time go into a separate file entirely.
"""
import csv
from datetime import date

import pytest

import stock_bot.execution.ibkr as ibkr_mod
import stock_bot.execution.paper as paper_mod
from stock_bot.data.price_feed import _sector_cache
from stock_bot.execution.ibkr import IBKRExecutor
from stock_bot.execution.paper import StockPaperExecutor, _next_business_day


# ── _next_business_day (pure, both modules implement it identically) ────────

def test_friday_settles_monday_paper():
    assert _next_business_day(date(2026, 8, 7)) == date(2026, 8, 10)   # Fri -> Mon


def test_friday_settles_monday_ibkr():
    assert ibkr_mod._next_business_day(date(2026, 8, 7)) == date(2026, 8, 10)


def test_weekday_settles_next_day():
    assert _next_business_day(date(2026, 8, 4)) == date(2026, 8, 5)   # Tue -> Wed


def test_saturday_settles_monday():
    assert _next_business_day(date(2026, 8, 8)) == date(2026, 8, 10)   # Sat -> Mon (Sun skipped)


# ── StockPaperExecutor: settlement CSV is separate from the frozen one ──────

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_mod, "_STATE_JSON", str(tmp_path / "state.json"))
    monkeypatch.setattr(paper_mod, "_TRADES_CSV", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(paper_mod, "_RESET_FLAG", str(tmp_path / ".reset"))
    monkeypatch.setattr(paper_mod, "_SETTLEMENT_CSV", str(tmp_path / "settlement.csv"))
    _sector_cache["TEST"] = "other"
    _sector_cache["TEST.TO"] = "other"
    return tmp_path


def test_frozen_trades_csv_header_unchanged(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.buy("TEST", 10, 50.0, reason="test")
    with open(sandbox / "trades.csv") as f:
        header = next(csv.reader(f))
    assert header == [
        "timestamp", "symbol", "side", "shares",
        "price", "total_value", "cash_remaining", "reason", "confidence",
    ]


def test_settlement_csv_written_on_buy(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    order = ex.buy("TEST", 10, 50.0, reason="test")
    assert order.status.value == "FILLED"
    with open(sandbox / "settlement.csv") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["timestamp", "symbol", "side", "settlement_date", "fx_rate_at_trade"]
    assert len(rows) == 2
    assert rows[1][1] == "TEST" and rows[1][2] == "BUY"
    assert rows[1][3]   # settlement_date populated
    assert float(rows[1][4]) > 0   # bare "TEST" symbol is treated as non-CAD (USD) — real fx_rate,
                                    # not 1.0; the CAD case is covered separately below


def test_settlement_csv_written_on_sell(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.buy("TEST", 10, 50.0, reason="test")
    order = ex.sell("TEST", 10, 55.0, reason="test")
    assert order.status.value == "FILLED"
    with open(sandbox / "settlement.csv") as f:
        rows = list(csv.reader(f))
    assert len(rows) == 3   # header + BUY + SELL
    assert rows[2][2] == "SELL"


def test_cad_symbol_records_fx_rate_one(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.buy("TEST.TO", 10, 50.0, reason="test")
    with open(sandbox / "settlement.csv") as f:
        rows = list(csv.reader(f))
    assert float(rows[1][4]) == pytest.approx(1.0)


def test_settlement_join_key_matches_frozen_csv_row(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0)
    ex.buy("TEST", 10, 50.0, reason="test")
    with open(sandbox / "trades.csv") as f:
        trade_row = list(csv.reader(f))[1]
    with open(sandbox / "settlement.csv") as f:
        settlement_row = list(csv.reader(f))[1]
    assert trade_row[0] == settlement_row[0]   # timestamp
    assert trade_row[1] == settlement_row[1]   # symbol
    assert trade_row[2] == settlement_row[2]   # side


# ── IBKRExecutor: same guarantees ────────────────────────────────────────────

from test_ibkr_executor import FakeIB, make_executor   # reuse the hermetic broker fake


@pytest.fixture
def ibkr_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(ibkr_mod, "_STATE_JSON", str(tmp_path / "ibkr_state.json"))
    monkeypatch.setattr(ibkr_mod, "_TRADES_CSV", str(tmp_path / "ibkr_trades.csv"))
    monkeypatch.setattr(ibkr_mod, "_SETTLEMENT_CSV", str(tmp_path / "ibkr_settlement.csv"))
    monkeypatch.setattr(ibkr_mod, "get_sector", lambda s: "other")
    return tmp_path


def test_ibkr_frozen_trades_csv_header_unchanged(ibkr_sandbox):
    fake = FakeIB(fill_price=101.25)
    ex = make_executor(fake)
    try:
        ex.buy("KO", 3, 100.0, reason="test")
        with open(ibkr_sandbox / "ibkr_trades.csv") as f:
            header = next(csv.reader(f))
        assert header == [
            "timestamp", "symbol", "side", "shares",
            "price", "total_value", "cash_remaining", "reason", "confidence",
        ]
    finally:
        ex.disconnect()


def test_ibkr_settlement_csv_written_on_buy(ibkr_sandbox):
    fake = FakeIB(fill_price=101.25)
    ex = make_executor(fake)
    try:
        order = ex.buy("KO", 3, 100.0, reason="test")
        assert order.status.value == "FILLED"
        with open(ibkr_sandbox / "ibkr_settlement.csv") as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["timestamp", "symbol", "side", "settlement_date", "fx_rate_at_trade"]
        assert len(rows) == 2
        assert rows[1][1] == "KO" and rows[1][2] == "BUY"
    finally:
        ex.disconnect()
