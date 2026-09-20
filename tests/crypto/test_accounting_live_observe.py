"""
Tests for bot/accounting/live_observe.py — the synchronous, exact
(order_id-matched) per-fill observation path (implementation item 2).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _accounting_fake_exchange import FakeExchangeAdapter  # noqa: E402

from bot.accounting import live_observe, store  # noqa: E402

T0 = 1_700_000_000_000


def test_observe_fill_links_the_exact_order_match(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    ex = FakeExchangeAdapter()
    t = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                          timestamp_ms=T0, order_id="ORD1")

    linked = live_observe.observe_fill(
        ex, conn, order_id="ORD1", symbol="BTC/CAD", fill_id=42, since=None,
        side="buy", quantity=0.001,
    )
    assert linked
    assert store.is_ledger_represented(conn, t.trade_id)
    assert store.trade_ids_for_fill(conn, 42) == [t.trade_id]


def test_observe_fill_no_op_when_trade_not_yet_visible(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    ex = FakeExchangeAdapter()
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                      timestamp_ms=T0, order_id="ORD1", visible=False)

    linked = live_observe.observe_fill(
        ex, conn, order_id="ORD1", symbol="BTC/CAD", fill_id=42, since=None,
        side="buy", quantity=0.001,
    )
    assert not linked
    assert store.trade_ids_for_fill(conn, 42) == []


def test_observe_fill_ignores_a_different_orders_trade(tmp_path):
    """Two orders on the same symbol — only the exact order_id match links,
    proving this path is exact rather than proximity-based."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    ex = FakeExchangeAdapter()
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                      timestamp_ms=T0, order_id="ORD_OTHER")
    t2 = ex.execute_trade(symbol="BTC/CAD", side="buy", price=91_000.0, amount=0.002,
                           timestamp_ms=T0 + 10, order_id="ORD1")

    linked = live_observe.observe_fill(
        ex, conn, order_id="ORD1", symbol="BTC/CAD", fill_id=7, since=None,
        side="buy", quantity=0.002,
    )
    assert linked
    assert store.trade_ids_for_fill(conn, 7) == [t2.trade_id]


def test_observe_fill_never_raises_on_a_broken_exchange(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)

    class _Broken:
        def fetch_my_trades_page(self, *a, **k):
            raise RuntimeError("network blip")

    result = live_observe.observe_fill(
        _Broken(), conn, order_id="ORD1", symbol="BTC/CAD", fill_id=1, since=None,
        side="buy", quantity=0.001,
    )
    assert result is False


def test_observe_fill_no_op_without_an_order_id(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    ex = FakeExchangeAdapter()
    result = live_observe.observe_fill(
        ex, conn, order_id="", symbol="BTC/CAD", fill_id=1, since=None,
        side="buy", quantity=0.001,
    )
    assert result is False
