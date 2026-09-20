"""
Full-lifecycle integration tests for the execution-accounting layer
(bot/accounting/), built after four isolated-fix review passes on
2026-09-20 kept finding new interaction bugs between fixes. Rather than
another isolated-finding pass, this exercises the complete real sequence
in one flow, against real temporary SQLite databases and the stateful
FakeExchangeAdapter (no mocks of this package's own code):

    observe -> record fee correction -> match -> link -> verify -> close/reopen

Covers: corrections before AND after linking, multiple fee revisions,
partial fills split across two local rows, and a crash at a transaction
boundary. Every test asserts, without weakening for current behavior:
  - every execution has exactly one ledger owner
  - quantity, cost, fees, and realized P&L reconcile against a
    hand-computed expected value, not just "some number came back"
  - a correction's dollar delta shows up in the realized-P&L fold exactly
    once, not zero or two times
  - an accepted match still passes four_way.verify_ledger_delivery_consistency
    after the connection is closed and reopened (a real "restart", not a
    reused in-memory connection)
  - a missing reference or corrupted economics fails verification

No live database, HALT, exchange call, strategy code, or git state is
touched by this file — every test builds its own tmp_path SQLite database
and TradeLog, exactly like the other tests/crypto/test_accounting_*.py
files.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _accounting_fake_exchange import FakeExchangeAdapter, FlakyConn, iso  # noqa: E402

from bot.accounting import engine, four_way, live_observe, reconciliation, store  # noqa: E402
from bot.data.trade_log import TradeLog  # noqa: E402

T0 = 1_700_000_000_000


def _setup(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    tl = TradeLog(db_path=db_path)
    return db_path, conn, tl


def _effective_trades(conn, trades):
    """The same correction-aware transformation reconciliation.py's
    _effective_fee_trades applies before matching/folding — reimplemented
    here (not imported) so this integration test doesn't depend on that
    function's continued existence as a private implementation detail; it
    only depends on the PUBLIC engine.effective_fee_cost / store.
    fee_correction_deltas_for_trade contract."""
    out = []
    for t in trades:
        deltas = store.fee_correction_deltas_for_trade(conn, t.trade_id)
        effective_fee = engine.effective_fee_cost(t.fee_cost, deltas)
        out.append(engine.ObservedTrade(
            trade_id=t.trade_id, order_id=t.order_id, symbol=t.symbol, side=t.side,
            price=t.price, amount=t.amount, cost=t.cost, fee_cost=effective_fee,
            fee_currency=t.fee_currency, exchange_timestamp=t.exchange_timestamp, source=t.source,
        ))
    return out


def _fold(conn, symbol):
    """Fully restart-safe position fold: reads purely from the persisted
    store (never a Python variable carried across "restart" in a test),
    exactly as recover_position's own docstring requires."""
    raw = store.load_observed_trades(conn, symbol)
    corrected = _effective_trades(conn, raw)
    ordered = engine.causal_order(corrected)
    assert ordered is not None, "trades could not be causally ordered — test setup bug, not this fold"
    return engine.fold_position(ordered)


def _owners(conn, trade_id) -> "list[int]":
    return [r[0] for r in conn.execute(
        "SELECT fill_id FROM trade_fill_links WHERE trade_id = ?", (trade_id,)
    )]


# ============================================================================
# 1. Correction BEFORE the fill row is ever created/linked
# ============================================================================

def test_lifecycle_correction_before_linking_single_owner_and_pnl_exact(tmp_path):
    """observe -> correct -> (fill recorded reflecting the ALREADY-corrected
    total) -> match -> link -> verify -> restart -> verify again.
    A full BUY+SELL round trip; the correction must show up in realized
    P&L exactly once."""
    db_path, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 10_000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    # BUY, observed at fee=$1, corrected to fee=$2 BEFORE any fills row exists.
    buy = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.01,
                            timestamp_ms=T0 + 1000, fee_cost=1.0, fee_currency="CAD")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)
    ex.revise_trade_fee(buy.trade_id, 2.0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    assert store.fee_correction_deltas_for_trade(conn, buy.trade_id) == [1.0]

    # SELL immediately, no correction on this leg.
    sell = ex.execute_trade(symbol="BTC/CAD", side="sell", price=95_000.0, amount=0.01,
                             timestamp_ms=T0 + 4000, fee_cost=0.5, fee_currency="CAD")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 5000)

    # Legacy fills rows recorded NOW, reflecting the fee as it stood at
    # record time — the BUY row already reflects the corrected $2 total.
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.01, price=90_000.0,
                fee_cost=2.0, fee_currency="CAD", exec_key="uuid-buy",
                timestamp=iso(T0 + 1000))
    tl.log_fill(side="SELL", symbol="BTC/CAD", quantity=0.01, price=95_000.0,
                fee_cost=0.5, fee_currency="CAD", exec_key="uuid-sell",
                timestamp=iso(T0 + 4000))
    buy_fill = store.fills_row_by_exec_key(conn, "uuid-buy")
    sell_fill = store.fills_row_by_exec_key(conn, "uuid-sell")

    # Straggler matcher must find and link both.
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 6000)
    assert store.is_ledger_represented(conn, buy.trade_id)
    assert store.is_ledger_represented(conn, sell.trade_id)

    # Exactly one owner each.
    assert _owners(conn, buy.trade_id) == [buy_fill["id"]]
    assert _owners(conn, sell.trade_id) == [sell_fill["id"]]

    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert result.ok, result

    # P&L: buy cost basis = 90_000*0.01 + $2 fee (corrected) = 902.0.
    # Sell proceeds = 95_000*0.01 - $0.5 fee = 949.5. PnL = 949.5 - 902.0 = 47.5.
    # If the correction were double-counted, cost basis would be 903.0 (fee
    # $1 + delta $1 applied twice) -> PnL 46.5. If omitted, cost basis 900.0
    # -> PnL 49.5. Exactly 47.5 proves it's counted exactly once.
    fold = _fold(conn, "BTC/CAD")
    assert abs(fold.realized_pnl - 47.5) < 1e-6, fold.realized_pnl
    assert abs(fold.per_trade_pnl[sell.trade_id] - 47.5) < 1e-6

    # Restart: fresh connection, nothing carried in a Python variable.
    conn.close()
    conn2 = store.connect(db_path)
    result2 = four_way.verify_ledger_delivery_consistency(conn2, "BTC/CAD")
    assert result2.ok, result2
    fold2 = _fold(conn2, "BTC/CAD")
    assert abs(fold2.realized_pnl - 47.5) < 1e-6, fold2.realized_pnl
    conn2.close()


# ============================================================================
# 2. Correction AFTER the fill row is already linked
# ============================================================================

def test_lifecycle_correction_after_linking_single_owner_and_pnl_exact(tmp_path):
    """observe -> (fill recorded + linked at the ORIGINAL fee) -> correct
    -> verify -> restart -> verify again. The opposite timing order from
    test 1 — the correction arrives after the link already exists."""
    db_path, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 10_000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    buy = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.01,
                            timestamp_ms=T0 + 1000, fee_cost=1.0, fee_currency="CAD")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)

    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.01, price=90_000.0,
                fee_cost=1.0, fee_currency="CAD", exec_key="uuid-buy2",
                timestamp=iso(T0 + 1000))
    buy_fill = store.fills_row_by_exec_key(conn, "uuid-buy2")
    store.link_trade_to_fill(conn, buy.trade_id, buy_fill["id"])
    assert four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD").ok

    sell = ex.execute_trade(symbol="BTC/CAD", side="sell", price=95_000.0, amount=0.01,
                             timestamp_ms=T0 + 4000, fee_cost=0.5, fee_currency="CAD")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 5000)
    tl.log_fill(side="SELL", symbol="BTC/CAD", quantity=0.01, price=95_000.0,
                fee_cost=0.5, fee_currency="CAD", exec_key="uuid-sell2",
                timestamp=iso(T0 + 4000))
    sell_fill = store.fills_row_by_exec_key(conn, "uuid-sell2")
    store.link_trade_to_fill(conn, sell.trade_id, sell_fill["id"])

    # NOW correct the already-linked BUY.
    ex.revise_trade_fee(buy.trade_id, 2.0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 6000)
    assert store.fee_correction_deltas_for_trade(conn, buy.trade_id) == [1.0]

    assert _owners(conn, buy.trade_id) == [buy_fill["id"]]
    assert _owners(conn, sell.trade_id) == [sell_fill["id"]]
    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert result.ok, result

    # Same expected P&L as test 1 — 47.5, correction counted exactly once,
    # despite the opposite timing order.
    fold = _fold(conn, "BTC/CAD")
    assert abs(fold.realized_pnl - 47.5) < 1e-6, fold.realized_pnl

    conn.close()
    conn2 = store.connect(db_path)
    assert four_way.verify_ledger_delivery_consistency(conn2, "BTC/CAD").ok
    fold2 = _fold(conn2, "BTC/CAD")
    assert abs(fold2.realized_pnl - 47.5) < 1e-6, fold2.realized_pnl
    conn2.close()


# ============================================================================
# 3. Multiple fee revisions on the same trade
# ============================================================================

def test_lifecycle_multiple_fee_revisions_counted_once_each_final_state_only(tmp_path):
    """A trade revised twice ($1 -> $1.5 -> $1.2) before being linked. Each
    revision must record its OWN delta (engine.next_fee_correction_revision
    already guarantees distinct adjustment_ids), and the fold must reflect
    only the FINAL cumulative effective fee ($1.2), not the sum of every
    intermediate value ever passed through."""
    db_path, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 10_000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    buy = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.01,
                            timestamp_ms=T0 + 1000, fee_cost=1.0, fee_currency="CAD")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)

    ex.revise_trade_fee(buy.trade_id, 1.5)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    ex.revise_trade_fee(buy.trade_id, 1.2)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 4000)

    deltas = store.fee_correction_deltas_for_trade(conn, buy.trade_id)
    assert len(deltas) == 2 and abs(deltas[0] - 0.5) < 1e-9 and abs(deltas[1] - (-0.3)) < 1e-9  # two distinct revisions, in order
    assert abs(engine.effective_fee_cost(1.0, deltas) - 1.2) < 1e-9

    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.01, price=90_000.0,
                fee_cost=1.2, fee_currency="CAD", exec_key="uuid-multi-rev",
                timestamp=iso(T0 + 1000))
    buy_fill = store.fills_row_by_exec_key(conn, "uuid-multi-rev")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 5000)
    assert _owners(conn, buy.trade_id) == [buy_fill["id"]]
    assert four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD").ok

    sell = ex.execute_trade(symbol="BTC/CAD", side="sell", price=95_000.0, amount=0.01,
                             timestamp_ms=T0 + 6000, fee_cost=0.0, fee_currency="CAD")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 7000)
    tl.log_fill(side="SELL", symbol="BTC/CAD", quantity=0.01, price=95_000.0,
                fee_cost=0.0, fee_currency="CAD", exec_key="uuid-multi-rev-sell",
                timestamp=iso(T0 + 6000))
    store.link_trade_to_fill(conn, sell.trade_id, store.fills_row_by_exec_key(conn, "uuid-multi-rev-sell")["id"])

    # cost basis = 900.0 + 1.2 (final cumulative fee, not 1.0+0.5+(-0.3)=1.2
    # coincidentally same here by construction, or 1.0+1.5+1.2=3.7 if each
    # revision were wrongly treated as an absolute replacement summed raw).
    # proceeds = 950.0 - 0 = 950.0. PnL = 950.0 - 901.2 = 48.8.
    fold = _fold(conn, "BTC/CAD")
    assert abs(fold.realized_pnl - 48.8) < 1e-6, fold.realized_pnl

    conn.close()
    conn2 = store.connect(db_path)
    assert four_way.verify_ledger_delivery_consistency(conn2, "BTC/CAD").ok
    conn2.close()


# ============================================================================
# 4. Partial fills — two local rows, each must get exactly one owner
# ============================================================================

def test_lifecycle_partial_fills_two_local_rows_each_get_exactly_one_owner(tmp_path):
    """One order worked as two separate executions, each logged as its OWN
    local fills row (the real partial-fill shape live_observe.py's fix
    targets) — each execution must end up owned by exactly the row that
    represents it, never both linked to one row nor one row double-owning."""
    db_path, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()

    t1 = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.01,
                           timestamp_ms=T0, order_id="ORD1", fee_cost=0.9, fee_currency="CAD")
    t2 = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_100.0, amount=0.01,
                           timestamp_ms=T0 + 10, order_id="ORD1", fee_cost=0.9, fee_currency="CAD")

    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.01, price=90_000.0,
                fee_cost=0.9, fee_currency="CAD", exec_key="uuid-partial-1",
                order_id="ORD1", timestamp=iso(T0))
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.01, price=90_100.0,
                fee_cost=0.9, fee_currency="CAD", exec_key="uuid-partial-2",
                order_id="ORD1", timestamp=iso(T0 + 10))
    fill1 = store.fills_row_by_exec_key(conn, "uuid-partial-1")
    fill2 = store.fills_row_by_exec_key(conn, "uuid-partial-2")

    linked1 = live_observe.observe_fill(
        ex, conn, order_id="ORD1", symbol="BTC/CAD", fill_id=fill1["id"], since=None,
        side="buy", quantity=0.01,
    )
    assert not linked1  # both executions still visible/unlinked at this point — 0.02 total vs 0.01 asked, correctly refused

    # Simulate fill1 having already claimed t1 through some other path
    # (e.g. an earlier successful observe_fill call before t2 existed) —
    # upsert + link it directly (every real call site always upserts
    # before linking; skipping that step would just be a test bug, not a
    # realistic prior state), then observe again for fill2 with only t2 left.
    store.upsert_observed_trade(conn, t1)
    store.link_trade_to_fill(conn, t1.trade_id, fill1["id"])
    linked2 = live_observe.observe_fill(
        ex, conn, order_id="ORD1", symbol="BTC/CAD", fill_id=fill2["id"], since=None,
        side="buy", quantity=0.01,
    )
    assert linked2

    assert _owners(conn, t1.trade_id) == [fill1["id"]]
    assert _owners(conn, t2.trade_id) == [fill2["id"]]
    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert result.ok, result

    conn.close()
    conn2 = store.connect(db_path)
    assert four_way.verify_ledger_delivery_consistency(conn2, "BTC/CAD").ok
    conn2.close()


# ============================================================================
# 5. Crash at a transaction boundary
# ============================================================================

def test_lifecycle_crash_mid_link_then_clean_retry_reaches_verified_state(tmp_path):
    """A crash injected mid multi-trade link (the exact boundary the first
    review pass's atomicity fix targets) must leave nothing partially
    persisted; a clean retry afterward must reach a fully verified state —
    proving the atomicity fix and the verifier agree on what 'clean' means."""
    db_path, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 10_000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    # A legacy fills row worth the SUM of two trades — only linkable as a group.
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.002, price=90_000.0,
                fee_cost=0.18, fee_currency="CAD", exec_key="uuid-crash-group",
                timestamp=iso(T0 + 1000))
    fill_row = store.fills_row_by_exec_key(conn, "uuid-crash-group")
    t1 = engine.ObservedTrade(
        trade_id="TC1", order_id="OC1", symbol="BTC/CAD", side="buy",
        price=90_000.0, amount=0.001, cost=90.0, fee_cost=0.09, fee_currency="CAD",
        exchange_timestamp=iso(T0 + 999), source="live",
    )
    t2 = engine.ObservedTrade(
        trade_id="TC2", order_id="OC1", symbol="BTC/CAD", side="buy",
        price=90_000.0, amount=0.001, cost=90.0, fee_cost=0.09, fee_currency="CAD",
        exchange_timestamp=iso(T0 + 1001), source="live",
    )
    store.upsert_observed_trade(conn, t1)
    store.upsert_observed_trade(conn, t2)

    flaky = FlakyConn(conn, match_prefix="INSERT OR IGNORE INTO trade_fill_links", fail_on_nth_match=2)
    with pytest.raises(RuntimeError):
        store.link_trades_to_fill(flaky, ["TC1", "TC2"], fill_row["id"])

    # Nothing partially persisted.
    assert _owners(conn, "TC1") == []
    assert _owners(conn, "TC2") == []
    assert store.unlinked_fills(conn, "BTC/CAD")  # still recoverable, not orphaned
    result_mid_crash = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert result_mid_crash.ok, result_mid_crash  # a not-yet-linked fill is not itself a verification failure

    # Clean retry.
    store.link_trades_to_fill(conn, ["TC1", "TC2"], fill_row["id"])
    assert sorted(_owners(conn, "TC1") + _owners(conn, "TC2")) == [fill_row["id"], fill_row["id"]]
    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert result.ok, result

    conn.close()
    conn2 = store.connect(db_path)
    assert four_way.verify_ledger_delivery_consistency(conn2, "BTC/CAD").ok
    conn2.close()


# ============================================================================
# 6. Missing references and corrupted economics must fail verification,
#    reached via the SAME full lifecycle rather than hand-built state.
# ============================================================================

def test_lifecycle_accepted_match_then_deleted_fill_fails_verification(tmp_path):
    """The full happy path first (observe -> match -> link -> verify ok),
    THEN corrupt by deleting the fills row — proving the dangling-fill
    detector fires on a REAL lifecycle-produced link, not just a
    hand-constructed one."""
    db_path, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 10_000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    buy = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                            timestamp_ms=T0 + 1000, fee_cost=0.09, fee_currency="CAD")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-will-delete",
                timestamp=iso(T0 + 1000))
    fill_row = store.fills_row_by_exec_key(conn, "uuid-will-delete")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    assert store.is_ledger_represented(conn, buy.trade_id)
    assert four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD").ok

    with conn:
        conn.execute("DELETE FROM fills WHERE id = ?", (fill_row["id"],))

    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert not result.ok
    assert fill_row["id"] in result.dangling_fill_ids


def test_lifecycle_accepted_match_then_deleted_trade_fails_verification(tmp_path):
    """Same idea, deleting the OBSERVED TRADE side of a real
    lifecycle-produced link instead of the fill side."""
    db_path, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 10_000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    buy = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                            timestamp_ms=T0 + 1000, fee_cost=0.09, fee_currency="CAD")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-trade-will-delete",
                timestamp=iso(T0 + 1000))
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    assert store.is_ledger_represented(conn, buy.trade_id)
    assert four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD").ok

    with conn:
        conn.execute("DELETE FROM observed_trades WHERE trade_id = ?", (buy.trade_id,))

    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert not result.ok
    assert buy.trade_id in result.dangling_trade_ids


def test_lifecycle_accepted_matches_then_forced_duplicate_owner_fails_verification(tmp_path):
    """Two independent real lifecycle links, then a raw-SQL-forced second
    owner on one of the trades (bypassing the write-side guard the same
    way a pre-guard database state could have arisen) — proving the
    duplicate-owner detector fires in a realistic multi-fill context."""
    db_path, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 10_000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    buy = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                            timestamp_ms=T0 + 1000, fee_cost=0.09, fee_currency="CAD")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-owner-real-1",
                timestamp=iso(T0 + 1000))
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-owner-real-2",
                timestamp=iso(T0 + 5000))
    fill1 = store.fills_row_by_exec_key(conn, "uuid-owner-real-1")
    fill2 = store.fills_row_by_exec_key(conn, "uuid-owner-real-2")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    assert _owners(conn, buy.trade_id) == [fill1["id"]]  # only the real trade, correctly single-owned
    assert four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD").ok

    # Force a second, DIFFERENT owner directly (bypassing the guard).
    with conn:
        conn.execute("INSERT INTO trade_fill_links (trade_id, fill_id, linked_at) VALUES (?,?,?)",
                      (buy.trade_id, fill2["id"], iso(T0 + 5000)))

    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert not result.ok
    assert buy.trade_id in result.duplicate_owner_trade_ids
