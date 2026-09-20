"""
Integration tests for bot/accounting/reconciliation.py against a real
sqlite trades.db (tmp_path) and a real bot.data.trade_log.TradeLog — the
FakeExchangeAdapter (tests/crypto/_accounting_fake_exchange.py) is the only
non-production piece, standing in for a real ccxt.kraken connection.

Covers implementation-request item 9 scenarios: delayed visibility,
multiple unresolved orders, fee corrections, shared account cash across two
symbols, and three-or-more-restarts recovery (via engine.recover_position,
independent of anything held in a Python variable across "restarts" — each
call opens against the persisted store only).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _accounting_fake_exchange import FakeExchangeAdapter  # noqa: E402

from bot.accounting import engine, store  # noqa: E402
from bot.accounting.reconciliation import run_cycle, resolve_exit_quantity  # noqa: E402
from bot.data.trade_log import TradeLog  # noqa: E402

T0 = 1_700_000_000_000


def _setup(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    tl = TradeLog(db_path=db_path)
    return db_path, conn, tl


def test_bootstrap_cycle_never_blocks_and_commits_opening_checkpoints(tmp_path):
    _, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 500.0, timestamp_ms=T0)
    state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    assert state.explain() == "ok"
    assert not state.blocked_for_buy("BTC/CAD")
    assert store.latest_checkpoint(conn, "CAD") is not None
    assert store.latest_checkpoint(conn, "BTC/CAD") is not None


def test_clean_buy_then_sell_round_trip_never_blocks(tmp_path):
    _, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)

    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                      timestamp_ms=T0 + 1000, fee_cost=0.09, fee_currency="CAD")
    state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 2000)
    assert state.explain() == "ok"

    ex.execute_trade(symbol="BTC/CAD", side="sell", price=91_000.0, amount=0.001,
                      timestamp_ms=T0 + 3000, fee_cost=0.09, fee_currency="CAD")
    state2 = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 4000)
    assert state2.explain() == "ok"
    assert not state2.blocked_for_buy("BTC/CAD")


def test_delayed_visibility_blocks_then_self_resolves(tmp_path):
    """review's core counterexample: a trade executed but not yet visible
    to fetch_my_trades leaves an unexplained residual — the scope must
    BLOCK, not guess, and resolve automatically once it becomes visible."""
    _, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    # Trade happens but is NOT yet visible (propagation lag).
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                      timestamp_ms=T0 + 1000, fee_cost=0.09, fee_currency="CAD", visible=False)
    state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 2000)
    assert state.account_cash_blocked
    assert state.blocked_for_buy("BTC/CAD")
    assert "residual" in state.account_cash_reason

    # Still invisible — stays blocked, not silently resolved.
    state2 = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 2500)
    assert state2.blocked_for_buy("BTC/CAD")

    # Now it becomes visible — the SAME window is re-verified and resolves
    # cleanly, with no human intervention and no data ever guessed at.
    ex.reveal_all()
    state3 = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 3000)
    assert state3.explain() == "ok"
    assert not state3.blocked_for_buy("BTC/CAD")


def test_multiple_unresolved_orders_both_missing_block_and_both_resolve(tmp_path):
    """Two different orders, both invisible at once — the identity check
    doesn't need to know HOW MANY trades are missing, only that the
    balance is unexplained; once BOTH become visible, one cycle clears it."""
    _, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.0005,
                      timestamp_ms=T0 + 1000, order_id="ORD_A", fee_cost=0.05,
                      fee_currency="CAD", visible=False)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=91_000.0, amount=0.0004,
                      timestamp_ms=T0 + 1500, order_id="ORD_B", fee_cost=0.04,
                      fee_currency="CAD", visible=False)
    state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 2000)
    assert state.blocked_for_buy("BTC/CAD")

    ex.reveal_all()
    state2 = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 3000)
    assert state2.explain() == "ok"
    trades = store.load_observed_trades(conn, "BTC/CAD")
    assert len(trades) == 2
    assert {t.order_id for t in trades} == {"ORD_A", "ORD_B"}


def test_fee_correction_recorded_via_existing_fee_adjustments_table(tmp_path):
    _, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100, now_ms=T0 + 100)

    t = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                          timestamp_ms=T0 + 1000, fee_cost=0.09, fee_currency="CAD")
    # First cycle observes the trade at its original fee. Use a safety
    # margin long enough that the SAME window is still being re-fetched
    # (overlap) on the next cycle, so the correction is actually seen.
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)

    ex.revise_trade_fee(t.trade_id, 0.15)  # exchange settles a higher final fee
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)

    deltas = store.fee_correction_deltas_for_trade(conn, t.trade_id)
    assert len(deltas) == 1
    assert abs(deltas[0] - 0.06) < 1e-9
    assert abs(engine.effective_fee_cost(0.09, deltas) - 0.15) < 1e-9
    # observed_trades' own frozen original payload is untouched.
    stored = store.get_observed_trade(conn, t.trade_id)
    assert stored.fee_cost == 0.09


def test_price_change_on_known_trade_is_an_anomaly_not_a_correction(tmp_path):
    _, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 100)
    t = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001, timestamp_ms=T0 + 1000)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)

    # Simulate a corrupted/adversarial re-read reporting a different price
    # for the SAME trade id — never silently accepted.
    ex._trades[[tid for tid, (tt, _) in enumerate(ex._trades) if tt.trade_id == t.trade_id][0]] = (
        engine.ObservedTrade(t.trade_id, t.order_id, t.symbol, t.side, 12345.0, t.amount,
                              t.cost, t.fee_cost, t.fee_currency, t.exchange_timestamp, t.source),
        True,
    )
    state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    assert state.blocked_for_buy("BTC/CAD")
    assert "ANOMALY" in state.symbol_reason["BTC/CAD"] or "ANOMALY" in state.account_cash_reason
    # The original stored payload must remain untouched.
    stored = store.get_observed_trade(conn, t.trade_id)
    assert stored.price == 90_000.0


def test_shared_account_cash_blocks_every_symbol_not_just_one(tmp_path):
    """design §6/§7: cash is checked once at the account level — an
    unreconciled residual blocks BUYs for EVERY symbol drawing on the pool,
    even one whose own base-asset inventory checks out clean."""
    _, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD", "SOL/CAD"], safety_margin_s=1, now_ms=T0 + 100)

    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                      timestamp_ms=T0 + 1000, fee_cost=0.09, fee_currency="CAD")
    ex.execute_trade(symbol="SOL/CAD", side="buy", price=200.0, amount=0.5,
                      timestamp_ms=T0 + 1500, fee_cost=0.1, fee_currency="CAD", visible=False)
    state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD", "SOL/CAD"], safety_margin_s=1, now_ms=T0 + 2000)

    assert state.account_cash_blocked  # the hidden SOL trade breaks the SHARED cash identity
    assert state.blocked_for_buy("BTC/CAD"), "BTC/CAD's own base-asset check is clean, but cash blocks it too"
    assert state.blocked_for_buy("SOL/CAD")


def test_deposit_mid_window_is_correctly_attributed_not_double_counted(tmp_path):
    """Regression for a real bug caught while building this: using the
    conservative retrieval watermark (rather than the actual balance
    capture moment) as a scope checkpoint's own window boundary
    double-counted a deposit that landed inside the safety-margin gap."""
    _, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 500)

    ex.deposit("CAD", 250.0, timestamp_ms=T0 + 1000)
    state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 1500)
    assert state.explain() == "ok", state.explain()

    # And it must not ALSO be re-counted a third time on the next cycle.
    state2 = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 2000)
    assert state2.explain() == "ok", state2.explain()


def test_three_restarts_recover_identical_position_purely_from_persisted_store(tmp_path):
    """Three separate 'restarts': each one opens a FRESH connection and
    calls engine.recover_position with nothing carried over in a Python
    variable — proving recovery is genuinely derived from disk, not from
    whatever state a test happened to still be holding."""
    db_path, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001, timestamp_ms=T0 + 1000)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 2000)
    conn.close()

    # Restart 1
    conn = store.connect(db_path)
    fold1 = engine.recover_position(store.load_observed_trades(conn, "BTC/CAD"))
    assert abs(fold1.final_qty - 0.001) < 1e-9
    conn.close()

    # A second trade happens between "restarts".
    conn = store.connect(db_path)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=95_000.0, amount=0.002, timestamp_ms=T0 + 3000)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 4000)
    conn.close()

    # Restart 2
    conn = store.connect(db_path)
    fold2 = engine.recover_position(store.load_observed_trades(conn, "BTC/CAD"))
    assert abs(fold2.final_qty - 0.003) < 1e-9
    conn.close()

    # A SELL, then restart a third time.
    conn = store.connect(db_path)
    ex.execute_trade(symbol="BTC/CAD", side="sell", price=100_000.0, amount=0.003, timestamp_ms=T0 + 5000)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 6000)
    conn.close()

    # Restart 3
    conn = store.connect(db_path)
    fold3 = engine.recover_position(store.load_observed_trades(conn, "BTC/CAD"))
    assert abs(fold3.final_qty) < 1e-9
    assert fold3.realized_pnl > 0  # bought at 90k/95k, sold at 100k


def test_resolve_exit_quantity_uses_fresh_balance_never_a_stale_local_value(tmp_path):
    ex = FakeExchangeAdapter()
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.005, timestamp_ms=T0)
    qty = resolve_exit_quantity(ex, "BTC/CAD", fallback_qty=999.0)
    assert abs(qty - 0.005) < 1e-9


def test_resolve_exit_quantity_falls_back_on_a_failed_fresh_read():
    class _Broken:
        def fetch_balance_total(self, asset):
            raise RuntimeError("network blip")

    qty = resolve_exit_quantity(_Broken(), "BTC/CAD", fallback_qty=0.005)
    assert qty == 0.005


def test_coverage_incomplete_this_cycle_blocks_every_symbol(tmp_path):
    _, conn, tl = _setup(tmp_path)

    class _AlwaysTruncated(FakeExchangeAdapter):
        def fetch_my_trades_page(self, symbol, *, since, offset, limit):
            page = super().fetch_my_trades_page(symbol, since=since, offset=offset, limit=limit)
            # Lie about the total so coverage can never be proven complete.
            return type(page)(trades=page.trades, reported_total=page.reported_total + 5, next_offset=None)

    ex = _AlwaysTruncated()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    assert state.coverage_blocked
    assert state.blocked_for_buy("BTC/CAD")
