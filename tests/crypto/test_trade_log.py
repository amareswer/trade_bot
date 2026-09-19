"""
Tests for bot/data/trade_log.py.

2026-09-18 review finding: TradeLog had zero test coverage, which is how a
hardcoded 12-column list silently dropping the fee_cost/fee_currency columns
from recent()'s SELECT * results went unnoticed.
"""
from __future__ import annotations

import pytest

from bot.data.trade_log import TradeLog


@pytest.fixture
def tl(tmp_path):
    return TradeLog(db_path=str(tmp_path / "trades.db"))


def test_log_fill_and_recent_round_trip(tl):
    tl.log_fill(
        "BUY", "BTC/CAD", 0.001, 90_000.0,
        exchange="kraken", fee_cost=0.36, fee_currency="CAD",
    )
    rows = tl.recent()
    assert len(rows) == 1
    assert rows[0]["side"] == "BUY"
    assert rows[0]["symbol"] == "BTC/CAD"


def test_recent_includes_fee_columns():
    """2026-09-18 review finding, reproduced: recent()'s old hardcoded
    12-column zip() silently dropped fee_cost/fee_currency from a real
    14-column SELECT * row. Both must now come back."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tl = TradeLog(db_path=f"{d}/trades.db")
        tl.log_fill(
            "SELL", "BTC/CAD", 0.001, 91_000.0, pnl=1.0,
            exchange="kraken", fee_cost=0.728, fee_currency="CAD",
        )
        row = tl.recent()[0]
        assert "fee_cost" in row
        assert "fee_currency" in row
        assert row["fee_cost"] == pytest.approx(0.728)
        assert row["fee_currency"] == "CAD"


def test_recent_column_count_matches_schema(tl):
    """The dynamic cursor.description approach must produce every column
    the table actually has — not a stale count."""
    tl.log_fill("BUY", "BTC/CAD", 0.001, 90_000.0, exchange="kraken")
    row = tl.recent()[0]
    assert len(row) == 16   # id, timestamp, side, symbol, quantity, price,
                             # value, pnl, exchange, signal_reason,
                             # risk_decision, notes, fee_cost, fee_currency,
                             # exec_key, order_id


def test_recent_respects_limit_and_order(tl):
    for i in range(3):
        tl.log_fill("BUY", "BTC/CAD", 0.001, 90_000.0 + i, exchange="kraken")
    rows = tl.recent(limit=2)
    assert len(rows) == 2
    assert rows[0]["price"] == 90_002.0   # most recent first


def test_summary_empty_db_returns_zeros(tl):
    assert tl.summary() == {"trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "net_pnl": 0.0}


def test_summary_reports_gross_and_net_pnl():
    """2026-09-18 review finding: summary() only ever reported gross pnl —
    net_pnl must be the exact identity (sum of realized pnl minus every
    fee paid, BUY entry + SELL exit) across the whole logged history."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tl = TradeLog(db_path=f"{d}/trades.db")
        tl.log_fill("BUY",  "BTC/CAD", 0.001, 90_000.0, exchange="kraken", fee_cost=0.36)
        tl.log_fill("SELL", "BTC/CAD", 0.001, 91_000.0, pnl=1.0,
                    exchange="kraken", fee_cost=0.728)

        s = tl.summary()
        assert s["trades"] == 1
        assert s["total_pnl"] == pytest.approx(1.0)          # gross, unchanged
        assert s["net_pnl"] == pytest.approx(1.0 - 0.36 - 0.728)   # a real net loss


def test_summary_win_rate_uses_gross_pnl_sign():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tl = TradeLog(db_path=f"{d}/trades.db")
        tl.log_fill("SELL", "BTC/CAD", 0.001, 91_000.0, pnl=5.0,  exchange="kraken")
        tl.log_fill("SELL", "BTC/CAD", 0.001, 89_000.0, pnl=-2.0, exchange="kraken")
        s = tl.summary()
        assert s["trades"] == 2
        assert s["win_rate"] == 0.5


def test_log_fill_rejects_zero_quantity(tl):
    with pytest.raises(ValueError):
        tl.log_fill("BUY", "BTC/CAD", 0.0, 90_000.0)


# ---------------------------------------------------------------------------
# 2026-09-18 FOLLOW-UP review finding (P1): journal replay could duplicate a
# committed fill if a crash landed between the DB insert succeeding and the
# caller's ack being persisted. exec_key makes the insert idempotent.
# ---------------------------------------------------------------------------

def test_exec_key_makes_insert_idempotent(tl):
    """Reproduced by the follow-up review: insert a fill, then simulate the
    crash-before-ack window by calling log_fill again with the SAME
    exec_key — must produce exactly one row, not two."""
    tl.log_fill("SELL", "BTC/CAD", 0.001, 91_000.0, pnl=1.0,
                exchange="kraken", exec_key="native-stop:s1#1")
    tl.log_fill("SELL", "BTC/CAD", 0.001, 91_000.0, pnl=1.0,
                exchange="kraken", exec_key="native-stop:s1#1")   # retried replay

    rows = tl.recent(limit=10)
    assert len(rows) == 1


def test_different_exec_keys_both_insert(tl):
    tl.log_fill("SELL", "BTC/CAD", 0.001, 91_000.0, exec_key="a#1")
    tl.log_fill("SELL", "BTC/CAD", 0.001, 92_000.0, exec_key="a#2")
    assert len(tl.recent(limit=10)) == 2


def test_empty_exec_key_never_deduplicates(tl):
    """Ordinary fills (no exec_key) must never be treated as duplicates of
    each other, even if every other field is identical."""
    tl.log_fill("SELL", "BTC/CAD", 0.001, 91_000.0, pnl=1.0, exchange="kraken")
    tl.log_fill("SELL", "BTC/CAD", 0.001, 91_000.0, pnl=1.0, exchange="kraken")
    assert len(tl.recent(limit=10)) == 2


def test_timestamp_override_preserves_original_execution_time(tl):
    """2026-09-18 follow-up review finding: a replayed row's timestamp used
    to become replay time, not the original fill time."""
    tl.log_fill(
        "SELL", "BTC/CAD", 0.001, 91_000.0, pnl=1.0, exchange="kraken",
        timestamp="2026-09-01T00:00:00Z",
    )
    row = tl.recent()[0]
    assert row["timestamp"] == "2026-09-01T00:00:00Z"


def test_recovered_sell_carries_real_pnl_not_null(tl):
    """2026-09-18 follow-up review finding: a recovered SELL row had
    pnl=NULL and was silently excluded from every live PF/win-rate calc
    (which filters on pnl IS NOT NULL). Journal replay now passes pnl
    through explicitly."""
    tl.log_fill(
        "SELL", "BTC/CAD", 0.001, 78_000.0, pnl=-20.0, exchange="kraken",
        signal_reason="recovered_from_crash", exec_key="native-stop:s1#1",
    )
    row = tl.recent()[0]
    assert row["pnl"] == -20.0

    with tl._connect() as conn:
        sell_rows = conn.execute(
            "SELECT pnl FROM fills WHERE side='SELL' AND pnl IS NOT NULL"
        ).fetchall()
    assert len(sell_rows) == 1   # not excluded by the NOT NULL filter
