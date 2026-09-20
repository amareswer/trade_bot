"""
Tests for bot/accounting/four_way.py — the joint verification across
exchange data, executor/PositionManager state, and SQLite (design §9,
implementation item 7). Every check must be reported separately and by
name; `ready`/`explain()` are checked to reflect exactly that.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _accounting_fake_exchange import FakeExchangeAdapter, iso  # noqa: E402

from bot.accounting import four_way, reconciliation, store  # noqa: E402
from bot.data.trade_log import TradeLog  # noqa: E402

T0 = 1_700_000_000_000


def _setup(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    tl = TradeLog(db_path=db_path)
    return conn, tl


def test_ready_true_when_everything_agrees(tmp_path):
    conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001, timestamp_ms=T0 + 1000)
    state = reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 2000)

    report = four_way.run_four_way_verification(
        conn, state, symbols=["BTC/CAD"],
        live_positions={"BTC/CAD": {"qty": 0.001, "avg_cost": 90_000.0, "realized_pnl": 0.0}},
    )
    assert report.ready, report.explain()


def test_not_ready_when_block_state_has_a_blocked_symbol(tmp_path):
    conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                      timestamp_ms=T0 + 1000, visible=False)
    state = reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 2000)
    assert state.blocked_for_buy("BTC/CAD")

    report = four_way.run_four_way_verification(
        conn, state, symbols=["BTC/CAD"],
        live_positions={"BTC/CAD": {"qty": 0.0, "avg_cost": 0.0, "realized_pnl": 0.0}},
    )
    assert not report.ready
    assert "exchange-data" in report.explain()


def test_position_fold_diff_detects_a_wiring_bug_not_exchange_problem(tmp_path):
    """PositionManager reports a DIFFERENT qty than the persisted
    observed_trades fold implies — a wiring bug (recovery didn't actually
    read from the ledger), reported distinctly from any exchange-data
    block."""
    conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001, timestamp_ms=T0 + 1000)
    state = reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 2000)
    assert state.explain() == "ok"

    report = four_way.run_four_way_verification(
        conn, state, symbols=["BTC/CAD"],
        live_positions={"BTC/CAD": {"qty": 999.0, "avg_cost": 1.0, "realized_pnl": 0.0}},
    )
    assert not report.ready
    assert not report.position_diff["BTC/CAD"].ok
    assert "position-fold" in report.explain()


def test_position_diff_flat_and_no_observed_trades_is_genuinely_ok(tmp_path):
    """A symbol that has never traded (flat, zero observed_trades) has
    nothing at risk to verify — this IS treated as healthy."""
    conn, _ = _setup(tmp_path)
    diff = four_way.diff_position_against_fold(
        conn, "BTC/CAD", live_qty=0.0, live_avg_cost=0.0, live_realized_pnl=0.0,
    )
    assert diff.ok
    assert "nothing at risk" in diff.reason


def test_position_diff_not_ok_when_holding_with_zero_observed_trades(tmp_path):
    """Money-readiness review 2026-09-19: 'do not let an empty or
    pre-migration ledger appear healthy merely because there are no link
    errors.' A REAL held position with zero ledger evidence (the exact
    pre-migration gap) must NOT report ok=True."""
    conn, _ = _setup(tmp_path)
    diff = four_way.diff_position_against_fold(
        conn, "BTC/CAD", live_qty=0.05, live_avg_cost=90_000.0, live_realized_pnl=0.0,
    )
    assert not diff.ok
    assert "ZERO observed_trades" in diff.reason


def test_four_way_not_ready_when_holding_a_position_with_no_ledger_history(tmp_path):
    """End-to-end version of the above through run_four_way_verification —
    proves the escalation actually reaches the top-level ready/explain."""
    conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    state = reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    assert state.explain() == "ok"

    report = four_way.run_four_way_verification(
        conn, state, symbols=["BTC/CAD"],
        live_positions={"BTC/CAD": {"qty": 0.05, "avg_cost": 90_000.0, "realized_pnl": 0.0}},
    )
    assert not report.ready
    assert "position-fold" in report.explain()


def test_ledger_delivery_consistency_ok_after_a_clean_link(tmp_path):
    conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001, timestamp_ms=T0 + 1000)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 2000)

    # No links were created yet in this flow (reconciliation.py only
    # OBSERVES trades — live_observe.py / migration.py are what create
    # trade_fill_links) — so ledger-delivery is trivially ok (nothing
    # claims to be ledger_written yet).
    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert result.ok


def test_ledger_delivery_detects_orphaned_marker(tmp_path):
    conn, _ = _setup(tmp_path)
    t = store.ObservedTrade(
        trade_id="T1", order_id="O1", symbol="BTC/CAD", side="buy", price=90_000.0,
        amount=0.001, cost=90.0, fee_cost=0.0, fee_currency="CAD",
        exchange_timestamp=iso(T0), source="live",
    )
    store.upsert_observed_trade(conn, t)
    # Corrupt state directly: ledger_written_at set with NO trade_fill_links row.
    with conn:
        conn.execute("UPDATE observed_trades SET ledger_written_at = ? WHERE trade_id = 'T1'", (iso(T0),))
    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert not result.ok


def test_ledger_delivery_economics_ok_for_a_correctly_linked_fill(tmp_path):
    """A single trade correctly linked to a fill whose own quantity/fee it
    exactly represents must NOT be flagged — proves the new economics
    check doesn't false-positive on the ordinary, correct case."""
    conn, tl = _setup(tmp_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-clean",
                timestamp=iso(T0))
    fill_row = store.fills_row_by_exec_key(conn, "uuid-clean")
    t = store.ObservedTrade(
        trade_id="T9", order_id="O9", symbol="BTC/CAD", side="buy", price=90_000.0,
        amount=0.001, cost=90.0, fee_cost=0.09, fee_currency="CAD",
        exchange_timestamp=iso(T0), source="live",
    )
    store.upsert_observed_trade(conn, t)
    store.link_trade_to_fill(conn, "T9", fill_row["id"])

    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert result.ok
    assert result.double_represented_fill_ids == []


def test_ledger_delivery_detects_double_represented_fill(tmp_path):
    """Accounting review follow-up, 2026-09-20, P1 reproduction: two
    quantity-1 trades both linked to a single quantity-1 fill (the exact
    live_observe.py misallocation this same review found and fixed
    separately). Link/marker EXISTENCE alone — the pre-fix check — returns
    ok=True here, since both trades genuinely have a link row and a marker;
    only comparing the linked trades' summed economics against what the
    fill itself recorded catches the over-allocation."""
    conn, tl = _setup(tmp_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-dup",
                timestamp=iso(T0))
    fill_row = store.fills_row_by_exec_key(conn, "uuid-dup")
    t1 = store.ObservedTrade(
        trade_id="T1", order_id="O1", symbol="BTC/CAD", side="buy", price=90_000.0,
        amount=0.001, cost=90.0, fee_cost=0.045, fee_currency="CAD",
        exchange_timestamp=iso(T0), source="live",
    )
    t2 = store.ObservedTrade(
        trade_id="T2", order_id="O1", symbol="BTC/CAD", side="buy", price=90_000.0,
        amount=0.001, cost=90.0, fee_cost=0.045, fee_currency="CAD",
        exchange_timestamp=iso(T0 + 10), source="live",
    )
    store.upsert_observed_trade(conn, t1)
    store.upsert_observed_trade(conn, t2)
    # Fee sums even conserve (0.045 + 0.045 = 0.09) — only quantity (0.002
    # linked vs 0.001 recorded) exposes the misallocation, proving this
    # isn't just re-checking what a simpler total-fee check would catch.
    store.link_trades_to_fill(conn, ["T1", "T2"], fill_row["id"])

    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert not result.ok
    assert fill_row["id"] in result.double_represented_fill_ids


def test_ledger_delivery_detects_duplicate_owner(tmp_path):
    """Second review pass, 2026-09-20, P1: 'linking one exchange trade to
    two identical fill rows returned ok=True.' store.link_trades_to_fill
    now REFUSES to create this going forward (see its own docstring) —
    this proves the read-side check also catches it as defense in depth,
    for a row that predates the guard or was corrupted some other way."""
    conn, tl = _setup(tmp_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-owner-a",
                timestamp=iso(T0))
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-owner-b",
                timestamp=iso(T0 + 1000))
    fill_a = store.fills_row_by_exec_key(conn, "uuid-owner-a")
    fill_b = store.fills_row_by_exec_key(conn, "uuid-owner-b")
    t = store.ObservedTrade(
        trade_id="T-dup-owner", order_id="O1", symbol="BTC/CAD", side="buy", price=90_000.0,
        amount=0.001, cost=90.0, fee_cost=0.09, fee_currency="CAD",
        exchange_timestamp=iso(T0), source="live",
    )
    store.upsert_observed_trade(conn, t)
    # Bypass the write-side guard directly (raw SQL) to construct the
    # corrupted state it now prevents — proving the READ-side check also
    # catches it independently.
    with conn:
        conn.execute("INSERT INTO trade_fill_links (trade_id, fill_id, linked_at) VALUES (?,?,?)",
                      ("T-dup-owner", fill_a["id"], iso(T0)))
        conn.execute("INSERT INTO trade_fill_links (trade_id, fill_id, linked_at) VALUES (?,?,?)",
                      ("T-dup-owner", fill_b["id"], iso(T0 + 1000)))

    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert not result.ok
    assert result.duplicate_owner_trade_ids == ["T-dup-owner"]


def test_ledger_delivery_detects_dangling_fill_reference(tmp_path):
    """Second review pass, 2026-09-20, P1: 'deleting the referenced fills
    also returned ok=True because missing rows are skipped.' A link
    pointing at a fills row that no longer exists must fail loudly, not be
    silently passed over."""
    conn, tl = _setup(tmp_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-vanish",
                timestamp=iso(T0))
    fill_row = store.fills_row_by_exec_key(conn, "uuid-vanish")
    t = store.ObservedTrade(
        trade_id="T-vanish", order_id="O1", symbol="BTC/CAD", side="buy", price=90_000.0,
        amount=0.001, cost=90.0, fee_cost=0.09, fee_currency="CAD",
        exchange_timestamp=iso(T0), source="live",
    )
    store.upsert_observed_trade(conn, t)
    store.link_trade_to_fill(conn, "T-vanish", fill_row["id"])

    with conn:
        conn.execute("DELETE FROM fills WHERE id = ?", (fill_row["id"],))

    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert not result.ok
    assert fill_row["id"] in result.dangling_fill_ids


def test_ledger_delivery_ok_despite_a_later_valid_fee_correction(tmp_path):
    """Second review pass, 2026-09-20, P1 reproduction: a correctly
    recorded fee correction (the exchange settling a different final fee
    than first observed) must not flip a genuinely correct link to a
    failure. fills.fee_cost is a frozen execution-time value the
    correction never touches — comparing against each trade's ORIGINAL
    fee_cost, not the corrected effective one, is what keeps a valid
    correction from looking like a broken link."""
    conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-fee-corr",
                timestamp=iso(T0 + 1000))
    fill_row = store.fills_row_by_exec_key(conn, "uuid-fee-corr")

    t = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                          timestamp_ms=T0 + 1000, fee_cost=0.09, fee_currency="CAD")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)
    store.link_trade_to_fill(conn, t.trade_id, fill_row["id"])
    assert four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD").ok

    ex.revise_trade_fee(t.trade_id, 0.15)  # exchange settles a higher final fee
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    deltas = store.fee_correction_deltas_for_trade(conn, t.trade_id)
    assert len(deltas) == 1 and abs(deltas[0] - 0.06) < 1e-9  # correction genuinely recorded

    result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert result.ok, result.double_represented_fill_ids  # the fix: correction must not break this
