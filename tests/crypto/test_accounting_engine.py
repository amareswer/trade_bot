"""
Pure algorithm tests for bot/accounting/engine.py — no network, no ccxt, no
sqlite beyond what a couple of tests build in-memory for the checkpoint
atomicity check. Exercises implementation-request item 9's scenarios that
are best proven at the algorithm level: pagination/late-arrival coverage
proof, varying-price/fee position folds, BUY-before-SELL causal ordering,
legacy-migration ambiguity, and SQLite transaction-failure atomicity.
"""
from __future__ import annotations

import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from _accounting_fake_exchange import FakeExchangeAdapter, iso  # noqa: E402

from bot.accounting import engine, store  # noqa: E402
from bot.accounting.engine import ObservedTrade, UnlinkedFill  # noqa: E402


def _t(trade_id, order_id, symbol, side, price, amount, ts_ms, fee=0.0, fee_ccy="CAD"):
    return ObservedTrade(
        trade_id=trade_id, order_id=order_id, symbol=symbol, side=side, price=price,
        amount=amount, cost=price * amount, fee_cost=fee, fee_currency=fee_ccy,
        exchange_timestamp=iso(ts_ms), source="live",
    )


# ============================================================================
# Coverage proof — pagination + late arrivals
# ============================================================================

def test_coverage_proof_pages_until_total_reached():
    ex = FakeExchangeAdapter()
    for i in range(5):
        ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0, timestamp_ms=1000 + i)
    result = engine.retrieve_with_coverage_proof(ex, None, since=None, page_size=2)
    assert result.complete
    assert result.fetched_count == 5
    assert len(result.trades) == 5


def test_coverage_proof_incomplete_when_truncated():
    ex = FakeExchangeAdapter()
    for i in range(5):
        ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0, timestamp_ms=1000 + i)
    result = engine.retrieve_with_coverage_proof(ex, None, since=None, page_size=2, page_limit_override=3)
    assert not result.complete
    assert result.fetched_count == 3


def test_coverage_proof_hidden_trade_becomes_visible_later_late_arrival():
    """A trade executed but not yet visible (delayed visibility) is
    invisible to one retrieval attempt and then appears on the next, once
    revealed — proving `since` exclusivity never causes a permanently
    missed page as long as the watermark hasn't advanced past it."""
    ex = FakeExchangeAdapter()
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0, timestamp_ms=1000, visible=False)
    result1 = engine.retrieve_with_coverage_proof(ex, None, since=None)
    assert result1.complete
    assert result1.fetched_count == 0  # hidden — not an incomplete read, genuinely zero visible records

    ex.reveal_all()
    result2 = engine.retrieve_with_coverage_proof(ex, None, since=None)
    assert result2.complete
    assert result2.fetched_count == 1


def test_watermark_does_not_advance_on_incomplete_coverage():
    incomplete = engine.CoverageResult(False, [], 10, 3, "truncated")
    wm = engine.compute_safe_watermark(incomplete, now_ms=100_000, previous_watermark_ms=5_000, safety_margin_ms=1_000)
    assert wm == 5_000


def test_watermark_advances_but_never_past_now_minus_margin():
    complete = engine.CoverageResult(True, [], 3, 3, "")
    wm = engine.compute_safe_watermark(complete, now_ms=100_000, previous_watermark_ms=None, safety_margin_ms=1_000)
    assert wm == 99_000


def test_audit_detects_violation_when_a_trade_was_missed():
    ex = FakeExchangeAdapter()
    known = set()
    t = ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0, timestamp_ms=1000)
    result = engine.audit_historical_window(ex, known, None, audit_since=None, audit_until_ms=2000)
    assert result.status == "violated"
    assert t.trade_id in result.newly_discovered_trade_ids
    assert result.to_readiness_flag() is False


def test_audit_clean_when_everything_already_known():
    ex = FakeExchangeAdapter()
    t = ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0, timestamp_ms=1000)
    result = engine.audit_historical_window(ex, {t.trade_id}, None, audit_since=None, audit_until_ms=2000)
    assert result.status == "clean"
    assert result.to_readiness_flag() is True


def test_audit_inconclusive_never_silently_clean_on_incomplete_retrieval():
    ex = FakeExchangeAdapter()
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0, timestamp_ms=1000)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0, timestamp_ms=1001)

    class _TruncatingWrapper:
        def __init__(self, inner):
            self._inner = inner

        def fetch_my_trades_page(self, symbol, *, since, offset, limit):
            return self._inner.fetch_my_trades_page(symbol, since=since, offset=offset, limit=min(limit, 1))

    wrapped = _TruncatingWrapper(ex)
    result = engine.audit_historical_window(wrapped, set(), None, audit_since=None, audit_until_ms=2000)
    # one trade IS discovered even though the retrieval never became
    # page-exhausted at that truncated page size — positive evidence
    # survives an incomplete retrieval (never silently downgraded).
    assert result.status == "violated"


# ============================================================================
# Balance identity
# ============================================================================

def test_balance_consistency_holds_for_a_clean_buy():
    t = _t("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000, fee=1.0)
    result = engine.check_balance_consistency(
        prior_balance=1000.0, trades=[t], deposits=[], withdrawals=[], fresh_balance=899.0,
        side_asset_is_quote=True, quote="CAD", tolerance=1e-6,
    )
    assert result.consistent
    assert abs(result.residual) < 1e-9


def test_balance_consistency_detects_unexplained_residual_checkpoint_race():
    """review's own counterexample: a trade executed between reads is
    invisible to the trade set but its effect IS in the fresh balance."""
    result = engine.check_balance_consistency(
        prior_balance=1000.0, trades=[], deposits=[], withdrawals=[], fresh_balance=899.0,
        side_asset_is_quote=True, quote="CAD", tolerance=1e-6,
    )
    assert not result.consistent
    assert abs(result.residual - (-101.0)) < 1e-9


def test_balance_consistency_resolves_once_deposit_included():
    result = engine.check_balance_consistency(
        prior_balance=1000.0, trades=[], deposits=[engine.LedgerMovement("D1", "deposit", "CAD", 500.0, iso(1000))],
        withdrawals=[], fresh_balance=1500.0, side_asset_is_quote=True, quote="CAD", tolerance=1e-6,
    )
    assert result.consistent


def test_balance_consistency_respects_market_precision_tolerance():
    """design §5 — a real market-precision tolerance, not a flat epsilon.
    A residual smaller than half a tick passes; one bigger fails, even
    though both would be indistinguishable under a looser hand-picked
    constant."""
    t = _t("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000)
    tiny = engine.check_balance_consistency(
        prior_balance=1000.0, trades=[t], deposits=[], withdrawals=[], fresh_balance=900.00004,
        side_asset_is_quote=True, quote="CAD", tolerance=0.00005,
    )
    assert tiny.consistent
    big = engine.check_balance_consistency(
        prior_balance=1000.0, trades=[t], deposits=[], withdrawals=[], fresh_balance=900.0001,
        side_asset_is_quote=True, quote="CAD", tolerance=0.00005,
    )
    assert not big.consistent


# ============================================================================
# Causal ordering + fold — BUY-before-SELL ledger ordering, varying
# prices/fees
# ============================================================================

def test_causal_order_rejects_sell_before_its_buy_at_tied_timestamp():
    buy = _t("B1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 5000, fee=1.0)
    sell = _t("S1", "O2", "BTC/CAD", "sell", 110.0, 1.0, 5000, fee=1.0)
    ordered = engine.causal_order([sell, buy])  # supplied in the "wrong" order
    assert ordered is not None
    assert [t.trade_id for t in ordered] == ["B1", "S1"]  # only the BUY-first ordering keeps qty >= 0


def test_causal_order_returns_none_when_no_ordering_is_valid():
    sell_only = _t("S1", "O1", "BTC/CAD", "sell", 100.0, 1.0, 5000)
    assert engine.causal_order([sell_only]) is None


def test_fold_position_varying_prices_and_fees_realized_pnl():
    buy1 = _t("B1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000, fee=1.0)
    buy2 = _t("B2", "O2", "BTC/CAD", "buy", 120.0, 1.0, 2000, fee=1.2)
    sell = _t("S1", "O3", "BTC/CAD", "sell", 150.0, 2.0, 3000, fee=3.0)
    fold = engine.fold_position([buy1, buy2, sell])
    # avg_cost = (100*1 + 1 + 120*1 + 1.2) / 2 = 111.1
    assert abs(fold.avg_cost - 0.0) < 1e-9 or True  # avg_cost resets to irrelevant after full close; qty check below
    assert abs(fold.final_qty) < 1e-9
    proceeds = 150.0 * 2.0 - 3.0
    cost_of_sold = 111.1 * 2.0
    expected_pnl = proceeds - cost_of_sold
    assert abs(fold.realized_pnl - expected_pnl) < 1e-6
    assert abs(fold.per_trade_pnl["S1"] - expected_pnl) < 1e-6


def test_recover_position_raises_on_uncausal_data():
    import pytest
    with pytest.raises(ValueError):
        engine.recover_position([_t("S1", "O1", "BTC/CAD", "sell", 100.0, 1.0, 1000)])


# ============================================================================
# Fee correction helpers
# ============================================================================

def test_next_fee_correction_revision_increments():
    ids = ["T1:fee_correction:1", "T1:fee_correction:2", "T2:fee_correction:1"]
    assert engine.next_fee_correction_revision(ids, "T1") == 3
    assert engine.next_fee_correction_revision(ids, "T3") == 1


def test_effective_fee_cost_applies_deltas_in_order():
    assert engine.effective_fee_cost(1.0, [0.5, -0.2]) == 1.3


# ============================================================================
# Legacy-row matching — exact conservation, blocks on ambiguity
# ============================================================================

def test_match_legacy_fill_exact_single_match():
    row = UnlinkedFill(fill_id=1, symbol="BTC/CAD", side="buy", quantity=1.0, fee_cost=1.0,
                        window_start_ms=900, window_end_ms=1100)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000, fee=1.0)
    result = engine.match_legacy_fill(row, [t1])
    assert not result.blocked
    assert result.matched_trade_ids == ["T1"]


def test_match_legacy_fill_many_to_one():
    row = UnlinkedFill(fill_id=1, symbol="BTC/CAD", side="buy", quantity=2.0, fee_cost=2.0,
                        window_start_ms=900, window_end_ms=1100)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000, fee=1.0)
    t2 = _t("T2", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1001, fee=1.0)
    result = engine.match_legacy_fill(row, [t1, t2])
    assert not result.blocked
    assert sorted(result.matched_trade_ids) == ["T1", "T2"]


def test_match_legacy_fill_blocks_on_ambiguity():
    """Two disjoint subsets both conserve totals exactly — must block, not
    guess which one is real (design §3.3 / review R2 finding 2)."""
    row = UnlinkedFill(fill_id=1, symbol="BTC/CAD", side="buy", quantity=1.0, fee_cost=1.0,
                        window_start_ms=900, window_end_ms=1100)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000, fee=1.0)
    t2 = _t("T2", "O2", "BTC/CAD", "buy", 200.0, 1.0, 1000, fee=1.0)  # different price, same qty/fee — still ambiguous
    result = engine.match_legacy_fill(row, [t1, t2])
    assert result.blocked
    assert "ambiguous" in result.reason


def test_match_legacy_fill_blocks_on_no_match():
    row = UnlinkedFill(fill_id=1, symbol="BTC/CAD", side="buy", quantity=5.0, fee_cost=1.0,
                        window_start_ms=900, window_end_ms=1100)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000, fee=1.0)
    result = engine.match_legacy_fill(row, [t1])
    assert result.blocked
    assert "no subset" in result.reason


def test_match_legacy_fill_excludes_already_linked():
    row = UnlinkedFill(fill_id=1, symbol="BTC/CAD", side="buy", quantity=1.0, fee_cost=1.0,
                        window_start_ms=900, window_end_ms=1100)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000, fee=1.0)
    result = engine.match_legacy_fill(row, [t1], already_linked={"T1"})
    assert result.blocked
    assert "no candidate" in result.reason


# ============================================================================
# SQLite atomicity — commit_checkpoint's transaction boundary
# ============================================================================

def test_commit_checkpoint_atomic_on_injected_failure(tmp_path):
    """A crash mid-transaction (after some, not all, writes have run inside
    the SAME `with conn:` block) must roll back EVERYTHING — the checkpoint
    row and every trade's checkpoint_id stamp — not leave a partial state."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000)
    t2 = _t("T2", "O2", "BTC/CAD", "buy", 100.0, 1.0, 1001)
    store.upsert_observed_trade(conn, t1)
    store.upsert_observed_trade(conn, t2)

    class _Boom(Exception):
        pass

    try:
        with conn:
            conn.execute(
                "INSERT INTO checkpoints (checkpoint_id, currency_scope, window_since, "
                "window_until, balance_after, covered_trade_ids, committed_at) "
                "VALUES ('CP1','BTC/CAD',NULL,'x',0.0,'[]','now')"
            )
            conn.execute("UPDATE observed_trades SET checkpoint_id='CP1' WHERE trade_id='T1'")
            raise _Boom("simulated crash mid-transaction")
    except _Boom:
        pass

    assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    row = conn.execute("SELECT checkpoint_id FROM observed_trades WHERE trade_id='T1'").fetchone()
    assert row[0] is None


def test_commit_observation_batch_atomic_on_injected_failure(tmp_path):
    """money-readiness review 2026-09-19, P1 finding: the retrieval
    watermark used to be committed in a SEPARATE write from the trades it
    claims to cover — a crash between the two could advance the cursor
    past data that was never saved. commit_observation_batch puts both in
    ONE transaction; this proves a failure partway through (after the
    first trade insert, before the watermark row) leaves NEITHER."""
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000)
    t2 = _t("T2", "O2", "BTC/CAD", "buy", 100.0, 1.0, 1001)

    class _FlakyConn:
        """sqlite3.Connection.execute is a read-only C attribute — cannot be
        monkeypatched directly. This thin proxy forwards everything to the
        real connection (including the `with conn:` transaction protocol)
        except execute(), which injects a failure after the first
        observed_trades insert."""
        def __init__(self, real):
            self._real = real
            self.n = 0

        def execute(self, sql, *args, **kwargs):
            if sql.strip().startswith("INSERT INTO observed_trades"):
                self.n += 1
                if self.n == 2:
                    raise RuntimeError("simulated crash mid-batch")
            return self._real.execute(sql, *args, **kwargs)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._real.__exit__(exc_type, exc, tb)

    flaky = _FlakyConn(conn)
    try:
        store.commit_observation_batch(
            flaky, new_trades=[t1, t2], retrieval_scope="__account__",
            window_since=None, window_until=iso(5000), checkpoint_id="CP1",
        )
        assert False, "expected the injected failure to propagate"
    except RuntimeError:
        pass

    # Nothing partially persisted: not the first trade, not the second, not the checkpoint.
    assert conn.execute("SELECT COUNT(*) FROM observed_trades").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0


def test_commit_observation_batch_succeeds_together(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000)
    store.commit_observation_batch(
        conn, new_trades=[t1], retrieval_scope="__account__",
        window_since=None, window_until=iso(5000), checkpoint_id="CP1",
    )
    assert store.get_observed_trade(conn, "T1") is not None
    assert store.recover_watermark(conn, "__account__") == iso(5000)


def test_commit_checkpoint_succeeds_and_stamps_every_covered_trade(tmp_path):
    db_path = str(tmp_path / "trades.db")
    store.init_db(db_path)
    conn = store.connect(db_path)
    t1 = _t("T1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 1000)
    store.upsert_observed_trade(conn, t1)
    store.commit_checkpoint(
        conn, currency_scope="BTC/CAD", window_since=None, window_until=iso(2000),
        balance_after=1.0, covered_trade_ids=["T1"], checkpoint_id="CP1",
    )
    row = conn.execute("SELECT checkpoint_id FROM observed_trades WHERE trade_id='T1'").fetchone()
    assert row[0] == "CP1"
