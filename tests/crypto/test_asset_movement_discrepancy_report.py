"""
Tests for scripts/asset_movement_discrepancy_report.py. No real network
call anywhere — a FakeExchange stub stands in for ccxt, and a temp SQLite
file (real file, real schema) stands in for trades.db. Focuses on the
pure logic: reading observed trades read-only, and building the report
text from whatever the (fake) exchange returns, including its failure
paths (withdrawals/ledger blocked).
"""
import sqlite3

import pytest

from scripts.asset_movement_discrepancy_report import (
    _load_observed_trades, _readonly_connect, build_report,
)


class FakeExchange:
    def __init__(self, *, deposits=None, withdrawals_error=None, ledger_error=None,
                 balance_total=0.0):
        self._deposits = deposits or []
        self._withdrawals_error = withdrawals_error
        self._ledger_error = ledger_error
        self._balance_total = balance_total

    def fetch_deposits(self, code=None):
        return self._deposits

    def fetch_withdrawals(self, code=None):
        if self._withdrawals_error:
            raise self._withdrawals_error
        return []

    def fetch_ledger(self):
        if self._ledger_error:
            raise self._ledger_error
        return []

    def fetch_balance(self):
        return {"BTC": {"total": self._balance_total}}


def _make_db(tmp_path, rows):
    db_path = str(tmp_path / "trades.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE observed_trades (
            trade_id TEXT, order_id TEXT, symbol TEXT, side TEXT, price REAL,
            amount REAL, cost REAL, fee_cost REAL, fee_currency TEXT,
            exchange_timestamp TEXT, source TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO observed_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows,
    )
    conn.commit()
    conn.close()
    return db_path


def test_load_observed_trades_filters_by_asset_prefix(tmp_path):
    db_path = _make_db(tmp_path, [
        ("T1", "O1", "BTC/CAD", "buy", 90000.0, 0.001, 90.0, 0.1, "CAD", "2026-06-01T00:00:00Z", "live"),
        ("T2", "O2", "SOL/CAD", "buy", 100.0, 1.0, 100.0, 0.1, "CAD", "2026-06-01T00:00:00Z", "live"),
    ])
    conn = _readonly_connect(db_path)
    btc_trades = _load_observed_trades(conn, "BTC")
    sol_trades = _load_observed_trades(conn, "SOL")
    assert [t.trade_id for t in btc_trades] == ["T1"]
    assert [t.trade_id for t in sol_trades] == ["T2"]


def test_readonly_connect_never_creates_a_missing_database(tmp_path):
    import os
    missing = str(tmp_path / "does_not_exist.db")
    with pytest.raises(sqlite3.OperationalError):
        _readonly_connect(missing)
    assert not os.path.exists(missing)


def test_report_shows_resolved_shortfall_when_a_deposit_explains_it(tmp_path):
    db_path = _make_db(tmp_path, [
        ("T1", "O1", "BTC/CAD", "sell", 90000.0, 0.001, 90.0, 0.1, "CAD", "2026-06-02T00:00:00Z", "live"),
    ])
    conn = _readonly_connect(db_path)
    fake_ex = FakeExchange(
        deposits=[{"id": "d1", "amount": 0.001, "timestamp": 1748736000000}],
        withdrawals_error=Exception('kraken {"error":["EGeneral:Permission denied"]}'),
        ledger_error=Exception('kraken {"error":["EGeneral:Permission denied"]}'),
        balance_total=0.0,
    )
    report = build_report("BTC", conn, fake_ex, db_path)
    assert "Shortfall resolved (`ok`): **True**" in report
    assert "fetch_deposits" in report and "OK — 1 record" in report
    assert "fetch_withdrawals" in report and "FAILED" in report
    assert "fetch_ledger" in report and "FAILED" in report


def test_report_shows_unresolved_shortfall_with_no_deposit_evidence(tmp_path):
    db_path = _make_db(tmp_path, [
        ("T1", "O1", "BTC/CAD", "sell", 90000.0, 0.001, 90.0, 0.1, "CAD", "2026-06-02T00:00:00Z", "live"),
    ])
    conn = _readonly_connect(db_path)
    fake_ex = FakeExchange(deposits=[], balance_total=0.0)
    report = build_report("BTC", conn, fake_ex, db_path)
    assert "Shortfall resolved (`ok`): **False**" in report


def test_report_surfaces_a_rejected_analysis_input_instead_of_crashing(tmp_path):
    """If the real evidence itself is contradictory (e.g. mismatched quote
    currencies), the report must say so plainly, not crash the whole run."""
    db_path = _make_db(tmp_path, [
        ("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 100.0, 0.0, "CAD", "2026-06-01T00:00:00Z", "live"),
        ("T2", "O2", "BTC/USD", "sell", 100.0, 1.0, 100.0, 0.0, "USD", "2026-06-02T00:00:00Z", "live"),
    ])
    conn = _readonly_connect(db_path)
    fake_ex = FakeExchange(deposits=[], balance_total=0.0)
    report = build_report("BTC", conn, fake_ex, db_path)
    assert "ANALYSIS REJECTED THE INPUT" in report


def test_report_does_not_write_to_the_database():
    """Source guard: the report-building path must never write — only
    read-only connections and calls appear in it."""
    import inspect

    import scripts.asset_movement_discrepancy_report as mod
    src = inspect.getsource(mod)
    assert ".create_order(" not in src
    assert "INSERT INTO" not in src
    assert "UPDATE " not in src
    assert "DELETE FROM" not in src
