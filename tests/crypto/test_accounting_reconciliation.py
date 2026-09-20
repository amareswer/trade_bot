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
from _accounting_fake_exchange import FakeExchangeAdapter, FlakyConn  # noqa: E402

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


def test_default_block_state_blocks_every_symbol_before_any_cycle():
    """money-readiness review 2026-09-19, P0 finding: the bare default
    BlockState() used to read as fully unblocked, so a BUY evaluated
    before the very first reconciliation cycle ever ran would sail
    through. This is the state bot/main.py must use at startup, before
    the first cycle has had a chance to run."""
    from bot.accounting.reconciliation import BlockState
    fresh = BlockState()
    assert fresh.blocked_for_buy("BTC/CAD")
    assert fresh.blocked_for_buy("SOL/CAD")
    assert fresh.blocked_for_buy("ANY/SYMBOL")
    assert "no reconciliation cycle" in fresh.explain()


def test_observation_phase_exception_returns_a_blocked_not_a_permissive_state(tmp_path):
    """money-readiness review 2026-09-19, P0/P1: an exception during the
    observation phase (retrieval, fee-correction sweep, or the atomic
    commit) must come back as a BLOCKED state with reconciled=False —
    never partially successful, never something a caller could mistake
    for 'the previous good cycle is still valid'."""
    _, conn, tl = _setup(tmp_path)

    class _BrokenExchange:
        def fetch_my_trades_page(self, *a, **k):
            raise RuntimeError("simulated exchange outage")

    state = run_cycle(_BrokenExchange(), conn, tl, quote="CAD", symbols=["BTC/CAD"], now_ms=T0)
    assert not state.reconciled
    assert state.blocked_for_buy("BTC/CAD")
    assert "observation phase raised" in state.coverage_reason


def test_a_successful_cycle_followed_by_a_failing_one_ends_up_blocked(tmp_path):
    """The exact P0 scenario named in the review: cycle 1 succeeds
    (permissive), cycle 2's exchange then fails outright — the SECOND
    call's own return value must be blocked. (bot/main.py is responsible
    for actually REPLACING its stored state with this return value rather
    than defensively keeping the old one — this test proves run_cycle's
    own contract; the main.py wiring is covered by not silently
    swallowing a raised exception into "keep the old state".)"""
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    _, conn, tl = _setup(tmp_path)
    good_state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    assert not good_state.blocked_for_buy("BTC/CAD")

    class _NowBroken:
        def fetch_my_trades_page(self, *a, **k):
            raise RuntimeError("exchange now unreachable")

    bad_state = run_cycle(_NowBroken(), conn, tl, quote="CAD", symbols=["BTC/CAD"], now_ms=T0 + 200)
    assert bad_state.blocked_for_buy("BTC/CAD")
    assert bad_state is not good_state  # a fresh object — nothing here can be "the old permissive state"


# ============================================================================
# Freshness expiry (money-readiness review 2026-09-20, P1: "a previously
# successful reconciliation may approve BUYs for up to the configured
# interval without a freshness deadline").
# ============================================================================

def test_clean_state_blocks_once_older_than_max_age(tmp_path):
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    _, conn, tl = _setup(tmp_path)
    state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    assert state.reconciled
    assert not state.blocked_for_buy("BTC/CAD")  # no staleness args — old behavior unaffected

    max_age_ms = 3_600_000  # 1h
    # Just within the deadline: still clean.
    assert not state.blocked_for_buy(
        "BTC/CAD", now_ms=state.computed_at_ms + max_age_ms - 1, max_age_ms=max_age_ms,
    )
    # Past the deadline: blocked, even though nothing about the state
    # itself changed — only real elapsed time did.
    assert state.blocked_for_buy(
        "BTC/CAD", now_ms=state.computed_at_ms + max_age_ms + 1, max_age_ms=max_age_ms,
    )
    assert "stale" in state.explain(now_ms=state.computed_at_ms + max_age_ms + 1, max_age_ms=max_age_ms)


def test_stale_check_is_opt_in_omitting_either_arg_keeps_old_behavior(tmp_path):
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    _, conn, tl = _setup(tmp_path)
    state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    far_future = state.computed_at_ms + 999_999_999
    assert not state.blocked_for_buy("BTC/CAD", now_ms=far_future)              # max_age_ms omitted
    assert not state.blocked_for_buy("BTC/CAD", max_age_ms=1000)                # now_ms omitted
    assert not state.blocked_for_buy("BTC/CAD")                                 # both omitted


def test_failed_scheduled_refresh_replaces_a_stale_clean_state_with_a_hard_block(tmp_path):
    """The scenario the review named: a clean state ages toward its
    deadline, and the scheduled refresh that was supposed to renew it
    fails outright — the replacement state (what bot/main.py actually
    stores) must be a hard block, not merely 'still the old clean state,
    now also technically stale'. Combines the P0 replace-don't-preserve
    fix with the P1 freshness fix end to end."""
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    _, conn, tl = _setup(tmp_path)
    good_state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=1, now_ms=T0 + 100)
    assert good_state.reconciled

    class _NowBroken:
        def fetch_my_trades_page(self, *a, **k):
            raise RuntimeError("exchange unreachable during the scheduled refresh")

    much_later = T0 + 100 + 7_200_000  # 2h later — well past any reasonable interval+grace
    refreshed = run_cycle(_NowBroken(), conn, tl, quote="CAD", symbols=["BTC/CAD"], now_ms=much_later)
    assert not refreshed.reconciled
    assert refreshed.blocked_for_buy("BTC/CAD")
    # And even if a caller forgot to pass staleness args, the P0 fix alone
    # already blocks this on `reconciled` — belt and suspenders.
    assert refreshed.blocked_for_buy("BTC/CAD", now_ms=much_later, max_age_ms=3_600_000)


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


def test_fee_correction_crash_leaves_no_orphan_and_retry_converges(tmp_path):
    """money-readiness review 2026-09-20, P1: a fee correction detected in
    the SAME cycle as a batch-commit failure must not survive that
    failure on its own — and once the underlying failure is gone, a plain
    retry (same inputs) must still converge to the correct final state."""
    db_path, conn, tl = _setup(tmp_path)
    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 100)
    t = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                          timestamp_ms=T0 + 1000, fee_cost=0.09, fee_currency="CAD")
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)
    ex.revise_trade_fee(t.trade_id, 0.15)

    flaky = FlakyConn(conn, match_prefix="INSERT INTO checkpoints")
    state = run_cycle(ex, flaky, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    # run_cycle's own top-level try/except must have caught the injected
    # failure and returned a blocked state, never propagated it.
    assert not state.reconciled
    assert "observation phase raised" in state.coverage_reason
    # And nothing partially persisted from the attempt.
    assert store.fee_correction_deltas_for_trade(conn, t.trade_id) == []
    stored = store.get_observed_trade(conn, t.trade_id)
    assert stored.fee_cost == 0.09  # unchanged — the correction never landed

    # Retry with the SAME inputs (the flaky wrapper is gone) — converges cleanly.
    state2 = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 4000)
    assert state2.reconciled
    deltas = store.fee_correction_deltas_for_trade(conn, t.trade_id)
    assert len(deltas) == 1
    assert abs(deltas[0] - 0.06) < 1e-9


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


def test_resolve_exit_quantity_shrinks_to_a_smaller_fresh_balance():
    """A stale local quantity larger than what the exchange actually shows
    (e.g. a missed correction) must shrink to the exchange-confirmed
    reality, avoiding an oversell rejection."""
    ex = FakeExchangeAdapter()
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.005, timestamp_ms=T0)
    qty = resolve_exit_quantity(ex, "BTC/CAD", tracked_qty=999.0)
    assert abs(qty - 0.005) < 1e-9


def test_resolve_exit_quantity_never_exceeds_tracked_qty_external_holdings():
    """Money-readiness review 2026-09-19, P1: 'explicit handling for...
    external holdings.' A fresh exchange balance LARGER than what the bot
    itself tracks (someone else's coins in the same account, or an
    under-tracked fill) must never inflate the sell size beyond what the
    bot has a basis to claim as its own."""
    ex = FakeExchangeAdapter()
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.5, timestamp_ms=T0)
    qty = resolve_exit_quantity(ex, "BTC/CAD", tracked_qty=0.005)
    assert abs(qty - 0.005) < 1e-9


def test_resolve_exit_quantity_falls_back_on_a_failed_fresh_read():
    class _Broken:
        def fetch_balance_total(self, asset):
            raise RuntimeError("network blip")

    qty = resolve_exit_quantity(_Broken(), "BTC/CAD", tracked_qty=0.005)
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


# ============================================================================
# Delayed-visibility straggler linking (money-readiness review 2026-09-20,
# P1: "the periodic reconciliation cycle does not implement the promised
# late-fill matcher").
# ============================================================================

def test_straggler_links_a_delayed_visibility_fill_to_existing_row(tmp_path):
    """The exact scenario the review named: a fill is logged first (the
    live path already wrote its `fills` row via a synthetic exec_key, but
    live_observe's synchronous linker never ran — e.g. the exchange
    visibility was delayed past it), and the NEXT reconciliation cycle
    links exactly one observed trade to that existing row."""
    _, conn, tl = _setup(tmp_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-abc-123",
                order_id="ORD1", timestamp=engine.iso_from_ms(T0 + 1000))
    fill_row = store.fills_row_by_exec_key(conn, "uuid-abc-123")
    assert fill_row is not None
    assert not store.is_ledger_represented(conn, "trade-does-not-exist-yet")

    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 100)

    # The real trade executes but is NOT yet visible — same delayed-
    # visibility mechanism as the checkpoint-race tests above.
    t = ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                          timestamp_ms=T0 + 1000, order_id="ORD1", fee_cost=0.09,
                          fee_currency="CAD", visible=False)
    state1 = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)
    assert state1.blocked_for_buy("BTC/CAD")  # unexplained residual — not yet observed
    assert not store.is_ledger_represented(conn, t.trade_id)

    # Now it becomes visible.
    ex.reveal_all()
    state2 = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    assert state2.explain() == "ok"
    assert store.is_ledger_represented(conn, t.trade_id)
    assert store.trade_ids_for_fill(conn, fill_row["id"]) == [t.trade_id]


def test_straggler_linking_is_idempotent_across_cycles(tmp_path):
    _, conn, tl = _setup(tmp_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-1",
                order_id="ORD1", timestamp=engine.iso_from_ms(T0 + 1000))
    fill_row = store.fills_row_by_exec_key(conn, "uuid-1")

    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 100)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                      timestamp_ms=T0 + 1000, order_id="ORD1", fee_cost=0.09, fee_currency="CAD")
    state1 = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)
    assert state1.explain() == "ok"
    linked_once = store.trade_ids_for_fill(conn, fill_row["id"])
    assert len(linked_once) == 1

    # A further cycle with nothing new must not re-link, duplicate, or block.
    state2 = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    assert state2.explain() == "ok"
    assert store.trade_ids_for_fill(conn, fill_row["id"]) == linked_once


def test_straggler_linking_blocks_on_ambiguous_match(tmp_path):
    """Two real trades that BOTH exactly conserve an unlinked fills row's
    totals — never guessed, always blocks that symbol for manual review
    (same discipline as migration.py's ambiguity handling)."""
    _, conn, tl = _setup(tmp_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-amb",
                order_id="ORD1", timestamp=engine.iso_from_ms(T0 + 1000))

    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 100)
    # Two DIFFERENT orders' trades, same qty/fee, both inside the fills
    # row's matching window — genuinely ambiguous which one it represents.
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                      timestamp_ms=T0 + 990, order_id="ORD_X", fee_cost=0.09, fee_currency="CAD")
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=88_000.0, amount=0.001,
                      timestamp_ms=T0 + 1010, order_id="ORD_Y", fee_cost=0.09, fee_currency="CAD")
    state = run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)

    assert state.blocked_for_buy("BTC/CAD")
    assert "straggler-link" in state.symbol_reason["BTC/CAD"]
    assert "ambiguous" in state.symbol_reason["BTC/CAD"]


def test_straggler_linking_survives_restart(tmp_path):
    """A link made in one process must be visible and treated as already-
    resolved by a genuinely fresh connection opened later ('restart'),
    not re-processed or re-blocked."""
    db_path, conn, tl = _setup(tmp_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-restart",
                order_id="ORD1", timestamp=engine.iso_from_ms(T0 + 1000))
    fill_row = store.fills_row_by_exec_key(conn, "uuid-restart")

    ex = FakeExchangeAdapter()
    ex.deposit("CAD", 1000.0, timestamp_ms=T0)
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 100)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=90_000.0, amount=0.001,
                      timestamp_ms=T0 + 1000, order_id="ORD1", fee_cost=0.09, fee_currency="CAD")
    run_cycle(ex, conn, tl, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 2000)
    linked = store.trade_ids_for_fill(conn, fill_row["id"])
    assert len(linked) == 1
    conn.close()

    # "Restart": fresh connection, fresh TradeLog, same db file.
    conn2 = store.connect(db_path)
    tl2 = TradeLog(db_path=db_path)
    state = run_cycle(ex, conn2, tl2, quote="CAD", symbols=["BTC/CAD"], safety_margin_s=100_000, now_ms=T0 + 3000)
    assert state.explain() == "ok"
    assert store.trade_ids_for_fill(conn2, fill_row["id"]) == linked


def test_straggler_linking_crash_leaves_no_partial_link(tmp_path):
    """link_trade_to_fill's own transaction (store.py) must not leave a
    trade_fill_links row without the paired ledger_written_at stamp, or
    vice versa, if interrupted mid-write."""
    _, conn, tl = _setup(tmp_path)
    tl.log_fill(side="BUY", symbol="BTC/CAD", quantity=0.001, price=90_000.0,
                fee_cost=0.09, fee_currency="CAD", exec_key="uuid-crash",
                order_id="ORD1", timestamp=engine.iso_from_ms(T0 + 1000))
    fill_row = store.fills_row_by_exec_key(conn, "uuid-crash")
    t = engine.ObservedTrade(
        trade_id="T-crash", order_id="ORD1", symbol="BTC/CAD", side="buy",
        price=90_000.0, amount=0.001, cost=90.0, fee_cost=0.09, fee_currency="CAD",
        exchange_timestamp=engine.iso_from_ms(T0 + 1000), source="live",
    )
    store.upsert_observed_trade(conn, t)

    flaky = FlakyConn(conn, match_prefix="UPDATE observed_trades SET ledger_written_at")
    try:
        store.link_trade_to_fill(flaky, "T-crash", fill_row["id"])
        assert False, "expected the injected failure to propagate"
    except RuntimeError:
        pass

    # Nothing partially persisted — no link row, no ledger_written_at stamp.
    assert not store.is_ledger_represented(conn, "T-crash")
    stored = store.get_observed_trade(conn, "T-crash")
    assert stored is not None  # the trade itself was already committed before this call

    # Retry succeeds cleanly.
    store.link_trade_to_fill(conn, "T-crash", fill_row["id"])
    assert store.is_ledger_represented(conn, "T-crash")
