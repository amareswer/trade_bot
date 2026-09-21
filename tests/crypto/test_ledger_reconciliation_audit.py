"""
Tests for scripts/ledger_reconciliation_audit.py. No real network call —
a FakeExchange stub returns raw dicts shaped exactly like Kraken's private
Ledgers response (as inspected directly from ccxt/kraken.py and a real
account this session). A temp SQLite file stands in for trades.db.
"""
import sqlite3
from decimal import Decimal

import pytest

from scripts.ledger_reconciliation_audit import (
    CoverageError, LedgerRow, _rows_for_asset, fetch_all_ledger_entries, reconcile_asset,
)


class FakePaginatedExchange:
    """Splits `entries` (a dict of {id: raw_entry}) across pages of
    `page_size`, exactly like Kraken's real `ofs`-paginated Ledgers
    endpoint, so the pagination-proof loop is exercised for real."""

    def __init__(self, entries: dict, page_size: int = 2, count_override=None):
        self._entries = entries
        self._page_size = page_size
        self._ids = list(entries.keys())
        self._count_override = count_override

    def privatePostLedgers(self, params):
        ofs = params.get("ofs", 0)
        page_ids = self._ids[ofs:ofs + self._page_size]
        page = {i: self._entries[i] for i in page_ids}
        count = self._count_override if self._count_override is not None else len(self._ids)
        return {"error": [], "result": {"ledger": page, "count": str(count)}}


def _raw(refid, type_, asset, amount, fee, balance, time_):
    return {
        "refid": refid, "type": type_, "aclass": "currency", "asset": asset,
        "amount": amount, "fee": fee, "balance": balance, "time": time_,
    }


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
    conn.executemany("INSERT INTO observed_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def test_pagination_is_verified_across_multiple_pages():
    entries = {
        "L1": _raw("R1", "trade", "XXBT", "0.0001000000", "0E-10", "0.0001000000", 1000.0),
        "L2": _raw("R2", "trade", "XXBT", "-0.0000500000", "0E-10", "0.0000500000", 2000.0),
        "L3": _raw("R3", "deposit", "XXBT", "0.0002000000", "0E-10", "0.0002500000", 3000.0),
    }
    ex = FakePaginatedExchange(entries, page_size=2)
    rows = fetch_all_ledger_entries(ex)
    assert len(rows) == 3
    assert [r.id for r in rows] == ["L1", "L2", "L3"]  # sorted by time


def test_count_drift_mid_pagination_raises_not_silently_trusted():
    entries = {
        "L1": _raw("R1", "trade", "XXBT", "0.0001", "0", "0.0001", 1000.0),
        "L2": _raw("R2", "trade", "XXBT", "0.0001", "0", "0.0002", 2000.0),
    }
    ex = FakePaginatedExchange(entries, page_size=1)
    # First page reports count=2 (from __init__ default), force a drift by
    # overriding to a different count on later calls via a stateful wrapper.
    call_count = {"n": 0}
    real_call = ex.privatePostLedgers

    def flaky(params):
        call_count["n"] += 1
        resp = real_call(params)
        if call_count["n"] == 2:
            resp["result"]["count"] = "99"
        return resp

    ex.privatePostLedgers = flaky
    with pytest.raises(CoverageError, match="drifted"):
        fetch_all_ledger_entries(ex)


def test_final_count_mismatch_raises():
    entries = {"L1": _raw("R1", "trade", "XXBT", "0.0001", "0", "0.0001", 1000.0)}
    ex = FakePaginatedExchange(entries, page_size=1, count_override=5)
    with pytest.raises(CoverageError, match="fetched 1"):
        fetch_all_ledger_entries(ex)


def test_non_numeric_field_is_rejected_not_guessed():
    entries = {"L1": _raw("R1", "trade", "XXBT", "not-a-number", "0", "0", 1000.0)}
    ex = FakePaginatedExchange(entries, page_size=1)
    with pytest.raises(CoverageError, match="non-numeric"):
        fetch_all_ledger_entries(ex)


def test_rows_for_asset_maps_kraken_native_codes():
    rows = [
        LedgerRow("L1", "R1", "trade", "XXBT", Decimal("1"), Decimal("0"), Decimal("1"), 1.0),
        LedgerRow("L2", "R2", "trade", "SOL", Decimal("1"), Decimal("0"), Decimal("1"), 2.0),
    ]
    assert [r.id for r in _rows_for_asset(rows, "BTC")] == ["L1"]
    assert [r.id for r in _rows_for_asset(rows, "SOL")] == ["L2"]


def test_exact_decimal_reconciliation_reproduces_the_real_btc_fee_finding(tmp_path):
    """Reproduces the real 2026-09-21 finding using the ACTUAL numbers from
    the account: a nonzero BTC-denominated ledger fee on one trade is
    cross-checked against that trade's OTHER (CAD) leg, which shows fee=0
    — never asserted as a second, separate charge without that check."""
    conn = _make_db(tmp_path, [
        ("R1", "O1", "BTC/CAD", "buy", 90000.0, 0.000113, 10.0, 0.08, "CAD", "2026-06-12T00:00:00Z", "live"),
        ("R2", "O2", "BTC/CAD", "sell", 90280.1, 0.00011, 9.93081, 0.03972, "CAD",
         "2026-06-14T00:00:00Z", "live"),
    ])
    btc_leg = LedgerRow("L1", "R2", "trade", "XXBT", Decimal("-0.00011"), Decimal("0.00000044"),
                        Decimal("0.00000256"), 2.0)
    cad_leg = LedgerRow("L2", "R2", "trade", "ZCAD", Decimal("9.9308"), Decimal("0"),
                        Decimal("99.8082"), 2.0)
    buy_leg = LedgerRow("L3", "R1", "trade", "XXBT", Decimal("0.000113"), Decimal("0"),
                        Decimal("0.000113"), 1.0)
    rows_by_refid = {"R1": [buy_leg], "R2": [btc_leg, cad_leg]}
    report = reconcile_asset([buy_leg, btc_leg], conn, "BTC", rows_by_refid)
    assert "reconciles exactly" in report
    assert "strongly suggests ONE real fee" in report
    assert "NOT a second, separate charge" in report
    assert "✗ MISMATCH" not in report


def test_cross_currency_check_does_not_assert_a_single_fee_when_the_numbers_disagree(tmp_path):
    """If the other leg ALSO has a nonzero fee, or the converted value
    doesn't match, the report must say inconclusive — never assert a
    conclusion the arithmetic doesn't support."""
    conn = _make_db(tmp_path, [
        ("R1", "O1", "BTC/CAD", "sell", 90000.0, 0.0001, 9.0, 0.01, "CAD", "2026-06-14T00:00:00Z", "live"),
    ])
    btc_leg = LedgerRow("L1", "R1", "trade", "XXBT", Decimal("-0.0001"), Decimal("0.00000044"),
                        Decimal("0"), 2.0)
    cad_leg = LedgerRow("L2", "R1", "trade", "ZCAD", Decimal("9.0"), Decimal("0.5"),  # nonzero fee too
                        Decimal("9.0"), 2.0)
    rows_by_refid = {"R1": [btc_leg, cad_leg]}
    report = reconcile_asset([btc_leg], conn, "BTC", rows_by_refid)
    assert "inconclusive" in report
    assert "strongly suggests ONE real fee" not in report


def test_a_real_mismatch_is_reported_not_hidden(tmp_path):
    conn = _make_db(tmp_path, [])
    rows = [
        # Kraken claims a balance that does NOT follow from amount - fee.
        LedgerRow("L1", "R1", "trade", "XXBT", Decimal("0.0001"), Decimal("0"), Decimal("0.0005"), 1.0),
    ]
    report = reconcile_asset(rows, conn, "BTC")
    assert "✗ MISMATCH" in report
    assert "UNRECONCILED" in report
    assert "not rounding" in report
    assert "no tolerance has been applied" in report


def test_a_trade_row_with_no_matching_observed_trades_row_is_flagged(tmp_path):
    conn = _make_db(tmp_path, [])
    rows = [
        LedgerRow("L1", "UNKNOWN-REFID", "trade", "XXBT", Decimal("0.0001"), Decimal("0"),
                  Decimal("0.0001"), 1.0),
    ]
    report = reconcile_asset(rows, conn, "BTC")
    assert "no matching observed_trades row" in report


def test_the_script_never_calls_create_order_or_writes():
    import inspect

    import scripts.ledger_reconciliation_audit as mod
    src = inspect.getsource(mod)
    assert ".create_order(" not in src
    assert "INSERT INTO" not in src
    assert "UPDATE " not in src
    assert "DELETE FROM" not in src
