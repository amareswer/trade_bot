"""
Cross-path production P&L tracing and integration tests — requested after
the accounting-package lifecycle tests (test_accounting_lifecycle_integration.py)
proved the accounting package's OWN internal fold is correct, but that fold
is not what a human actually sees. This file traces, and tests against, the
REAL functions on each real reporting path, using a real temp SQLite
trades.db and TradeLog — no reimplementation of any path's own math.

===========================================================================
TRACE: where does "P&L" come from, and is it gross or net?
===========================================================================

1. bot/portfolio/position_manager.py PositionManager.on_sell()
   -> pnl = (price - avg_entry) * quantity
   GROSS. avg_entry (from on_buy) is a price-only weighted average — no
   fee is ever added to cost basis, and no fee is ever subtracted from
   this pnl. This is the value bot/main.py holds in a local `pnl` variable
   at fill time.

2. bot/execution/live_executor.py LiveExecutor._portfolio.realized_pnl
   -> pnl = (fill_price - self._portfolio._cost_basis) * quantity
   ALSO GROSS, and the SAME formula shape as (1) — but tracked in a
   COMPLETELY SEPARATE internal Portfolio object, not delegating to or
   reading from PositionManager at all. Two independent trackers exist.

3. fills.pnl (bot/data/trade_log.py, written via trade_log.log_fill(pnl=...))
   -> bot/main.py passes PositionManager's pnl from (1) straight through.
   GROSS, stored verbatim. fills.fee_cost is a SEPARATE column on the same
   row — the schema itself never combines them.

4. Telegram fill alert (bot/alerts/telegram.py TelegramAlerter.fill())
   -> formats whatever `pnl` it's given (again, (1)'s gross value) as
   "Gross P&L: $X.XX" (accounting review, fifth pass, 2026-09-20: was
   unlabeled "P&L:" before this pass — fixed so it's never mistaken for
   live_comparison.py's fee-inclusive net figure).

5. Dashboard (bot/dashboard/renderer.py _render_symbol_block())
   -> "Realized P&L" card = kw['realized_pnl'], i.e. (1) again — GROSS,
      unlabeled as gross in the UI.
   -> Sub-label "fees $F · net $N" where net = realized - fees_paid.
      fees_paid (bot/main.py: getattr(executor, "fees_paid", 0.0)) is
      LiveExecutor._fees_paid, a running total incremented by every
      fill's own fee_cost (BUY and SELL alike) AND by every fee
      correction's delta_fee, each exactly once, at the point it occurs
      (bot/execution/live_executor.py, _apply_ordinary_order_fee_only_
      adjustment and the analogous native-stop path). This "net" is a
      real, correctly-computed AGGREGATE net (cumulative gross minus
      cumulative fees paid so far) — genuinely net, not double-counted —
      but it is NOT the same computation as (6) below: it can transiently
      differ from a rigorous per-trade net while a position is open
      (its entry fee is already in fees_paid before any matching exit
      has realized a gain to net it against), converging only once all
      positions are flat. For a single, fully-closed round trip (this
      file's test scenario) the two agree exactly, which the test below
      verifies numerically rather than assuming.

6. live_comparison.py _compute_live_metrics()
   -> The rigorous, per-CLOSED-trade net: a SELL's net pnl = its own
      gross pnl, minus its own exit fee, minus its proportional (by
      quantity) share of the entry fee(s) of the BUY(s) it's closing —
      allocated FIFO by symbol, fee_adjustments folded in per order_id
      BEFORE allocation. `net_pnl`/`pf`/`win_rate` are the trustworthy
      figures; `total_pnl`/`gross_pf`/`gross_win_rate` are the gross
      figures, kept alongside for comparison, never for gating. This is
      the most correct net figure in the codebase and already has its
      own dedicated test suite (test_live_comparison.py) — this file
      does not re-prove its internal correctness, only that it's reached
      by real fills.db data end-to-end and agrees with the other paths
      on gross.

7. bot/accounting/engine.fold_position() (bot/accounting/, via
   four_way.diff_position_against_fold)
   -> A THIRD, independent net computation: fee IS included in both
      avg_cost (buy side) and proceeds (sell side). Used ONLY as an
      internal cross-check against LiveExecutor's own state to detect a
      restart-recovery wiring bug — never surfaced to a human as "the"
      P&L. See test_accounting_lifecycle_integration.py for its own
      correctness proof.

8. bot/accounting/four_way.py — what does it ACTUALLY receive?
   Traced at the real call site, bot/main.py ~line 3514:
       _live_positions = {sym: {
           "qty": exc.position, "avg_cost": exc.avg_entry,
           "realized_pnl": exc.portfolio.realized_pnl,   # <- (2) above, GROSS
       } for sym, exc in executors.items()}
   NOT PositionManager (1) — confirmed by reading the call site, not
   assumed. And critically: diff_position_against_fold's `ok` determination
   is `qty_ok and cost_ok` — realized_pnl (live GROSS vs fold NET) is
   captured in the result for display only and is NEVER compared for
   pass/fail. This is why comparing a gross live figure against a net
   fold figure does not currently create a false reconciliation failure:
   the code was already scoped to sidestep that exact comparison, not by
   a documented design decision anywhere in the module, but as an
   emergent fact of what it happens to check. Verified empirically below,
   not just read from the source.

   avg_cost IS compared (cost_ok, cash_tolerance=0.01). A PRIOR version of
   this file flagged (but did not fix) a real latent gap here: live_avg_cost
   (gross) was being diffed against fold_position's avg_cost (fee-inclusive
   NET) — for an OPEN position with a nonzero entry fee this would exceed
   cash_tolerance by roughly fee/quantity and falsely block. Fixed
   (accounting review, fifth pass, 2026-09-20): four_way now reconstructs
   a GROSS avg_cost via the new engine.fold_position_gross (mirroring
   LiveExecutor's own gross methodology exactly) and compares gross
   against gross — same tolerance, not widened, not removed. See
   test_open_buy_with_entry_fee_reconciles_after_gross_fix and its sibling
   tests below for the scenarios that would have failed before this fix.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _accounting_fake_exchange import FakeExchangeAdapter, iso  # noqa: E402

import live_comparison as lc  # noqa: E402
from bot.accounting import engine, four_way, reconciliation, store  # noqa: E402
from bot.data.trade_log import TradeLog  # noqa: E402
from bot.portfolio.position_manager import PositionManager  # noqa: E402

T0 = 1_700_000_000_000
BUY_PRICE  = 90_000.0
SELL_PRICE = 91_000.0
QTY        = 0.001           # (91_000 - 90_000) * 0.001 = $1.00 gross, by construction
BUY_FEE    = 0.80
SELL_FEE   = 0.40


def test_production_paths_agree_on_gross_1_00_and_net_neg_0_20(tmp_path):
    """The exact scenario from the review: BUY fee $0.80 + gross SELL
    profit $1.00 + SELL fee $0.40 must produce gross profit $1.00 and net
    loss $0.20 — checked through the REAL PositionManager, a REAL
    TradeLog-backed sqlite db, the REAL live_comparison metrics function,
    and the REAL dashboard renderer, not a reimplementation of any of
    them."""
    db_path = str(tmp_path / "trades.db")
    tl = TradeLog(db_path=db_path)
    pm = PositionManager()

    # --- BUY: real PositionManager + real TradeLog row -----------------
    pm.on_buy(BUY_PRICE, QTY)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=QTY, price=BUY_PRICE,
                exchange="kraken", fee_cost=BUY_FEE, fee_currency="CAD",
                order_id="O-BUY", exec_key="uuid-buy", timestamp=iso(T0))

    # --- SELL: real PositionManager computes GROSS pnl, exactly as
    #     bot/main.py's own fill-processing code does -----------------
    gross_pnl = pm.on_sell(SELL_PRICE, QTY)
    assert gross_pnl == pytest.approx(1.00), gross_pnl   # PositionManager's own number — GROSS

    tl.log_fill(side="SELL", symbol="BTC/CAD", quantity=QTY, price=SELL_PRICE,
                pnl=gross_pnl, exchange="kraken", fee_cost=SELL_FEE, fee_currency="CAD",
                order_id="O-SELL", exec_key="uuid-sell", timestamp=iso(T0 + 1000))

    assert not pm.has_position
    assert pm.realized_pnl == pytest.approx(1.00)   # PositionManager's cumulative — still GROSS

    # --- Path 6: live_comparison.py's real, rigorous net --------------
    fills = lc._load_fills(db_path)
    metrics = lc._compute_live_metrics(fills)
    assert metrics["total_pnl"] == pytest.approx(1.00)        # gross, correctly labeled
    assert metrics["net_pnl"] == pytest.approx(-0.20)         # net, the real round-trip result
    assert metrics["win_rate"] == 0.0          # a net loss, not a win
    assert metrics["gross_win_rate"] == 1.0    # gross still (correctly) shows a win

    # --- Path 5: the dashboard's real aggregate net --------------------
    # fees_paid mirrors LiveExecutor._fees_paid: every fill's own fee,
    # BUY and SELL alike, summed once each.
    fees_paid = BUY_FEE + SELL_FEE
    aggregate_net = pm.realized_pnl - fees_paid   # the renderer's own formula
    assert aggregate_net == pytest.approx(-0.20)
    # For this single, fully-closed round trip, the dashboard's simple
    # aggregate net and live_comparison's rigorous per-trade FIFO net
    # agree exactly — verified numerically, not assumed to always hold
    # (they would diverge with an open position or multiple overlapping
    # trades, per the trace above).
    assert aggregate_net == pytest.approx(metrics["net_pnl"])

    from bot.dashboard import renderer
    out_path = str(tmp_path / "dashboard.html")
    renderer.write(
        path=out_path, exchange="kraken", symbol="BTC/CAD", strategy="test",
        tick=1, price=SELL_PRICE, signal="HOLD", rsi=None, trend=None,
        state="IDLE", cooldown=0, last_trade="", cash=1000.0,
        position=pm.quantity, avg_entry=pm.avg_entry, unrealized_pnl=0.0,
        realized_pnl=pm.realized_pnl, total_value=1000.0,
        fills=[
            {"time": "t1", "side": "BUY", "qty": QTY, "price": BUY_PRICE, "total": BUY_PRICE * QTY, "pnl": None},
            {"time": "t2", "side": "SELL", "qty": QTY, "price": SELL_PRICE, "total": SELL_PRICE * QTY, "pnl": gross_pnl},
        ],
        tick_log=[], fees_paid=fees_paid,
    )
    with open(out_path) as f:
        html = f.read()
    assert "$1.00" in html          # gross "Realized P&L" card
    assert "-$0.20" in html or "$-0.20" in html   # the "net $..." sub-label

    # --- Path 8: four_way — no false reconciliation block -------------
    # Real observed_trades ledger for the SAME two fills, linked, so the
    # ledger-delivery and position-fold checks have real data.
    store.init_db(db_path)
    conn = store.connect(db_path)
    buy_trade = engine.ObservedTrade(
        trade_id="T-BUY", order_id="O-BUY", symbol="BTC/CAD", side="buy",
        price=BUY_PRICE, amount=QTY, cost=BUY_PRICE * QTY, fee_cost=BUY_FEE,
        fee_currency="CAD", exchange_timestamp=iso(T0), source="live",
    )
    sell_trade = engine.ObservedTrade(
        trade_id="T-SELL", order_id="O-SELL", symbol="BTC/CAD", side="sell",
        price=SELL_PRICE, amount=QTY, cost=SELL_PRICE * QTY, fee_cost=SELL_FEE,
        fee_currency="CAD", exchange_timestamp=iso(T0 + 1000), source="live",
    )
    store.upsert_observed_trade(conn, buy_trade)
    store.upsert_observed_trade(conn, sell_trade)
    buy_fill_row = store.fills_row_by_exec_key(conn, "uuid-buy")
    sell_fill_row = store.fills_row_by_exec_key(conn, "uuid-sell")
    store.link_trade_to_fill(conn, "T-BUY", buy_fill_row["id"])
    store.link_trade_to_fill(conn, "T-SELL", sell_fill_row["id"])

    ledger_result = four_way.verify_ledger_delivery_consistency(conn, "BTC/CAD")
    assert ledger_result.ok, ledger_result

    # The REAL shape bot/main.py builds — LiveExecutor.portfolio's GROSS
    # realized_pnl, methodologically identical to PositionManager's (both
    # are (price - cost_basis) * qty with no fee term), confirmed by
    # trace above. Not PositionManager itself — that's not what
    # production actually passes here.
    diff = four_way.diff_position_against_fold(
        conn, "BTC/CAD", live_qty=0.0, live_avg_cost=0.0, live_realized_pnl=1.00,
    )
    assert diff.ok, diff
    # fold_realized_pnl (NET, fee-inclusive) and live_realized_pnl (GROSS)
    # genuinely differ here — proving the module does NOT quietly make
    # them agree; it simply never compares them for pass/fail.
    assert abs(diff.fold_realized_pnl - diff.live_realized_pnl) > 0.01
    assert diff.fold_realized_pnl == pytest.approx(-0.20)   # the accounting package's own net, for reference only

    conn.close()


def test_fee_correction_then_restart_changes_net_exactly_once(tmp_path):
    """Apply a fee correction to the SELL leg, restart (close/reopen the
    connection and reload from persisted state only), and verify: net
    reporting changes exactly once (not zero, not twice), gross reporting
    is untouched and still correctly labeled, four_way's ledger-delivery
    check still passes using the SAME correction-aware fee both matching
    and verification already agree on (see bot/accounting/four_way.py's
    verify_ledger_delivery_consistency fee_conserves check), and re-running
    the whole computation again does not apply the correction a second time."""
    db_path = str(tmp_path / "trades.db")
    tl = TradeLog(db_path=db_path)
    pm = PositionManager()

    pm.on_buy(BUY_PRICE, QTY)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=QTY, price=BUY_PRICE,
                exchange="kraken", fee_cost=BUY_FEE, fee_currency="CAD",
                order_id="O-BUY2", exec_key="uuid-buy2", timestamp=iso(T0))
    gross_pnl = pm.on_sell(SELL_PRICE, QTY)
    tl.log_fill(side="SELL", symbol="BTC/CAD", quantity=QTY, price=SELL_PRICE,
                pnl=gross_pnl, exchange="kraken", fee_cost=SELL_FEE, fee_currency="CAD",
                order_id="O-SELL2", exec_key="uuid-sell2", timestamp=iso(T0 + 1000))

    metrics_before = lc._compute_live_metrics(lc._load_fills(db_path))
    assert metrics_before["net_pnl"] == pytest.approx(-0.20)
    assert metrics_before["total_pnl"] == pytest.approx(1.00)   # gross, unaffected by anything yet

    # A late correction settles the SELL's real fee $0.10 higher than
    # first recorded — the REAL production path (TradeLog.log_fee_adjustment,
    # the same table both LiveExecutor's journal replay and live_comparison
    # read/write).
    tl.log_fee_adjustment(order_id="O-SELL2", symbol="BTC/CAD", delta_fee=0.10,
                           fee_currency="CAD", adjustment_id="O-SELL2:fee_correction:1")

    # "Restart": fresh TradeLog/connections, nothing carried in a Python variable.
    fills_after   = lc._load_fills(db_path)
    fee_adj_after = lc._load_fee_adjustments(db_path)
    metrics_after = lc._compute_live_metrics(fills_after, fee_adj_after)

    assert metrics_after["total_pnl"] == pytest.approx(1.00)          # gross: untouched, still correctly labeled
    assert metrics_after["net_pnl"] == pytest.approx(-0.30)           # net: changed by exactly the $0.10 delta, once
    assert abs((metrics_after["net_pnl"] - metrics_before["net_pnl"]) - (-0.10)) < 1e-9

    # Re-running the exact same read+compute again (a second "restart")
    # must NOT apply the correction again — log_fee_adjustment's own
    # idempotency only prevents a duplicate STORED row; this instead
    # proves the READ+FOLD side is idempotent too (re-summing the same
    # persisted rows every time, never accumulating further).
    fills_again   = lc._load_fills(db_path)
    fee_adj_again = lc._load_fee_adjustments(db_path)
    metrics_again = lc._compute_live_metrics(fills_again, fee_adj_again)
    assert metrics_again["net_pnl"] == pytest.approx(-0.30)
    assert metrics_again["total_pnl"] == pytest.approx(1.00)

    # --- four_way: matching and verification must agree on the SAME
    #     corrected fee (the exact interaction the third review pass's
    #     fee-policy-consistency fix targets) --------------------------
    store.init_db(db_path)
    conn = store.connect(db_path)
    buy_trade = engine.ObservedTrade(
        trade_id="T-BUY2", order_id="O-BUY2", symbol="BTC/CAD", side="buy",
        price=BUY_PRICE, amount=QTY, cost=BUY_PRICE * QTY, fee_cost=BUY_FEE,
        fee_currency="CAD", exchange_timestamp=iso(T0), source="live",
    )
    sell_trade = engine.ObservedTrade(
        trade_id="T-SELL2", order_id="O-SELL2", symbol="BTC/CAD", side="sell",
        price=SELL_PRICE, amount=QTY, cost=SELL_PRICE * QTY, fee_cost=SELL_FEE,
        fee_currency="CAD", exchange_timestamp=iso(T0 + 1000), source="live",
    )
    store.upsert_observed_trade(conn, buy_trade)
    store.upsert_observed_trade(conn, sell_trade)
    buy_fill_row = store.fills_row_by_exec_key(conn, "uuid-buy2")
    sell_fill_row = store.fills_row_by_exec_key(conn, "uuid-sell2")
    store.link_trade_to_fill(conn, "T-BUY2", buy_fill_row["id"])
    store.link_trade_to_fill(conn, "T-SELL2", sell_fill_row["id"])

    # This fills row's OWN fee_cost ($0.40) reflects the PRE-correction
    # amount (recorded before the correction arrived) — the ledger-delivery
    # check must accept it via the ORIGINAL-fee branch of its either/or
    # fee_conserves test, exactly the "correction after linking" timing.
    store.commit_checkpoint(  # no-op checkpoint just to exercise a persisted read path
        conn, currency_scope="CAD", window_since=None, window_until=iso(T0 + 2000),
        balance_after=0.0, covered_trade_ids=[], checkpoint_id="cp-1",
    )
    conn.close()

    conn2 = store.connect(db_path)
    result = four_way.verify_ledger_delivery_consistency(conn2, "BTC/CAD")
    assert result.ok, result   # no false reconciliation block from the correction
    conn2.close()


# ============================================================================
# Gross-vs-gross fix (accounting review, fifth pass, 2026-09-20) — the
# scenarios that would have false-failed (or, for the corrupted-value
# case, should still correctly fail) under the pre-fix comparison.
# ============================================================================

def test_open_buy_with_entry_fee_reconciles_after_gross_fix(tmp_path):
    """An OPEN position (no SELL yet) with a nonzero entry fee — the exact
    shape that would have false-failed against the pre-fix code (live
    gross avg_cost vs fold_position's fee-inclusive avg_cost, off by
    fee/quantity = $800/unit here, vastly more than cash_tolerance)."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    pm = PositionManager()
    pm.on_buy(BUY_PRICE, QTY)

    buy_trade = engine.ObservedTrade(
        trade_id="T-OPEN-BUY", order_id="O1", symbol="BTC/CAD", side="buy",
        price=BUY_PRICE, amount=QTY, cost=BUY_PRICE * QTY, fee_cost=BUY_FEE,
        fee_currency="CAD", exchange_timestamp=iso(T0), source="live",
    )
    store.upsert_observed_trade(conn, buy_trade)

    diff = four_way.diff_position_against_fold(
        conn, "BTC/CAD", live_qty=pm.quantity, live_avg_cost=pm.avg_entry, live_realized_pnl=0.0,
    )
    assert diff.ok, diff
    assert diff.fold_avg_cost == pytest.approx(BUY_PRICE)              # gross — matches live exactly
    assert diff.fold_avg_cost_net == pytest.approx(BUY_PRICE + BUY_FEE / QTY)  # fee-inclusive — genuinely different
    assert abs(diff.fold_avg_cost_net - diff.fold_avg_cost) > 1.0      # proves the two are NOT coincidentally equal
    conn.close()


def test_partial_sell_leaving_inventory_reconciles(tmp_path):
    """A partial exit — some inventory remains open. Gross avg_cost must
    be unchanged by the partial SELL (PositionManager doesn't recompute
    avg_entry on a partial exit, and neither does the gross fold)."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    pm = PositionManager()
    full_qty = 0.002
    sell_qty = 0.001
    pm.on_buy(BUY_PRICE, full_qty)
    pm.on_sell(SELL_PRICE, sell_qty)   # closes half

    buy_trade = engine.ObservedTrade(
        trade_id="T-PARTIAL-BUY", order_id="O1", symbol="BTC/CAD", side="buy",
        price=BUY_PRICE, amount=full_qty, cost=BUY_PRICE * full_qty, fee_cost=1.0,
        fee_currency="CAD", exchange_timestamp=iso(T0), source="live",
    )
    sell_trade = engine.ObservedTrade(
        trade_id="T-PARTIAL-SELL", order_id="O2", symbol="BTC/CAD", side="sell",
        price=SELL_PRICE, amount=sell_qty, cost=SELL_PRICE * sell_qty, fee_cost=0.45,
        fee_currency="CAD", exchange_timestamp=iso(T0 + 1000), source="live",
    )
    store.upsert_observed_trade(conn, buy_trade)
    store.upsert_observed_trade(conn, sell_trade)

    assert pm.has_position
    diff = four_way.diff_position_against_fold(
        conn, "BTC/CAD", live_qty=pm.quantity, live_avg_cost=pm.avg_entry, live_realized_pnl=pm.realized_pnl,
    )
    assert diff.ok, diff
    assert diff.fold_qty == pytest.approx(full_qty - sell_qty)
    assert diff.fold_avg_cost == pytest.approx(BUY_PRICE)   # unchanged by the partial exit, gross or net
    conn.close()


def test_entry_fee_correction_then_restart_still_reconciles_gross(tmp_path):
    """An entry-fee correction on an OPEN position must not affect the
    GROSS reconciliation (fees never enter it), while the NET
    (fee-inclusive) reconstruction moves by exactly the correction, once —
    checked across a real restart (fresh connection)."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    tl = TradeLog(db_path=db_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 10_000.0, timestamp_ms=T0)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    buy = ex.execute_trade(symbol="BTC/CAD", side="buy", price=BUY_PRICE, amount=QTY,
                            timestamp_ms=T0 + 1000, fee_cost=BUY_FEE, fee_currency="CAD")
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)

    pm = PositionManager()
    pm.on_buy(BUY_PRICE, QTY)   # gross avg_entry, independent of any fee or correction

    diff_before = four_way.diff_position_against_fold(
        conn, "BTC/CAD", live_qty=pm.quantity, live_avg_cost=pm.avg_entry, live_realized_pnl=0.0,
    )
    assert diff_before.ok, diff_before
    net_before = diff_before.fold_avg_cost_net

    ex.revise_trade_fee(buy.trade_id, BUY_FEE + 0.20)
    reconciliation.run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    deltas = store.fee_correction_deltas_for_trade(conn, buy.trade_id)
    assert len(deltas) == 1 and abs(deltas[0] - 0.20) < 1e-9

    # Restart: fresh connection, nothing carried in a Python variable.
    conn.close()
    conn2 = store.connect(db_path)
    diff_after = four_way.diff_position_against_fold(
        conn2, "BTC/CAD", live_qty=pm.quantity, live_avg_cost=pm.avg_entry, live_realized_pnl=0.0,
    )
    assert diff_after.ok, diff_after   # gross reconciliation unaffected by the correction
    assert diff_after.fold_avg_cost == pytest.approx(diff_before.fold_avg_cost)   # gross: unchanged
    assert diff_after.fold_avg_cost_net == pytest.approx(net_before + 0.20 / QTY)  # net: moved by exactly the correction
    conn2.close()

    # A further "restart" (re-read) must not apply the correction again.
    conn3 = store.connect(db_path)
    diff_again = four_way.diff_position_against_fold(
        conn3, "BTC/CAD", live_qty=pm.quantity, live_avg_cost=pm.avg_entry, live_realized_pnl=0.0,
    )
    assert diff_again.fold_avg_cost_net == pytest.approx(diff_after.fold_avg_cost_net)
    conn3.close()


def test_corrupted_live_average_entry_still_fails_reconciliation(tmp_path):
    """The fix must not have widened the tolerance or removed the check to
    make false failures go away — a genuinely WRONG live_avg_cost (a
    corrupted executor value, not a fee-methodology mismatch) must still
    be caught."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    buy_trade = engine.ObservedTrade(
        trade_id="T-CORRUPT", order_id="O1", symbol="BTC/CAD", side="buy",
        price=BUY_PRICE, amount=QTY, cost=BUY_PRICE * QTY, fee_cost=BUY_FEE,
        fee_currency="CAD", exchange_timestamp=iso(T0), source="live",
    )
    store.upsert_observed_trade(conn, buy_trade)

    diff = four_way.diff_position_against_fold(
        conn, "BTC/CAD", live_qty=QTY, live_avg_cost=95_000.0,  # WRONG — real gross avg_cost is 90_000.0
        live_realized_pnl=0.0,
    )
    assert not diff.ok
    assert "avg_cost diff" in diff.reason
    conn.close()
