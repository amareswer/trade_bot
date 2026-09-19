"""
Tests for the disposable offline reference model
(execution_accounting_reference_model.py), proving the nine scenarios the
R2 design review required plus the three-separate-results requirement.

Every test uses a SyntheticExchange and a temporary on-disk SQLite database
(tmp_path fixture) — no live network, no import of bot/execution/
live_executor.py or bot/main.py, no coupling to the live trading path.
"""
from __future__ import annotations

import sqlite3

import pytest

from execution_accounting_reference_model import (
    BudgetPool,
    ExchangeCashReconciler,
    LegacyFillRow,
    SyntheticExchange,
    already_linked_trade_ids,
    apply_migration_link,
    assess_readiness,
    audit_historical_window,
    causal_order,
    check_balance_consistency,
    commit_checkpoint,
    compute_safe_watermark,
    fold_position,
    init_db,
    is_watermark_confirmed,
    load_observed_trades,
    migrate_legacy_row,
    recover_position,
    recover_watermark,
    record_fee_revision,
    retrieve_with_coverage_proof,
    verify_ledger_delivery_consistency,
    write_ledger_rows,
)


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "reference.db"))
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# 1. Offsetting unseen events
# ---------------------------------------------------------------------------

def test_offsetting_unseen_events_resolve_correctly_once_visible():
    """Two hidden events (a deposit and a later trade whose proceeds are
    exactly cancelled by an ALSO-hidden withdrawal) must never be silently
    fabricated — the model only ever claims what it can currently see, and
    correctly reconciles once everything becomes visible."""
    ex = SyntheticExchange()
    ex.deposit("CAD", 1000.0, timestamp_ms=0, visible=True)  # the actual opening baseline
    ex.deposit("CAD", 100.0, timestamp_ms=500, visible=False)
    ex.withdraw("CAD", 100.0, timestamp_ms=600, visible=False)

    # At this point nothing NEW is visible: coverage of the (empty) trades
    # window since the baseline is vacuously complete, and the balance
    # identity holds trivially (net real effect of the hidden pair is
    # exactly zero).
    coverage = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=0)
    assert coverage.complete and coverage.fetched_count == 0
    balance = check_balance_consistency(
        prior_balance=1000.0, trades=[], deposits=[], withdrawals=[],
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
    )
    assert balance.consistent

    ex.reveal_all()
    deposits = ex.fetch_deposits("CAD", since_ms=0)
    withdrawals = ex.fetch_withdrawals("CAD", since_ms=0)
    balance2 = check_balance_consistency(
        prior_balance=1000.0, trades=[], deposits=deposits, withdrawals=withdrawals,
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
    )
    assert balance2.consistent
    assert len(deposits) == 1 and len(withdrawals) == 1


def test_coverage_independently_catches_truncated_retrieval_even_when_balance_matches():
    """R2 finding 1's decisive counterexample, concretely realized: a
    SELL's proceeds are exactly offset by an unrelated hidden withdrawal,
    so a balance-consistency check computed from a TRUNCATED trade fetch
    (missing the SELL) passes despite genuinely incomplete history.
    Coverage must catch this independently — never inferred from balance."""
    ex = SyntheticExchange()
    ex.deposit("CAD", 1000.0, timestamp_ms=0, visible=True)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=50_000.0, amount=0.01,
                      timestamp_ms=1_000, trade_id="A")
    ex.execute_trade(symbol="BTC/CAD", side="sell", price=51_000.0, amount=0.01,
                      timestamp_ms=2_000, trade_id="B")
    # Hidden withdrawal that happens to net out trade B's +510 proceeds —
    # NOT fetched this cycle (models "we forgot/failed to check withdrawals").
    ex.withdraw("CAD", 510.0, timestamp_ms=2_500, visible=True)

    coverage = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=0, page_limit_override=1)
    assert coverage.complete is False
    assert coverage.fetched_count == 1 and coverage.reported_total == 2

    # The naive balance check on the truncated (1-trade) set WOULD wrongly
    # say "consistent" — demonstrating exactly why it must never be trusted
    # without an independently-passing coverage result.
    naive_balance = check_balance_consistency(
        prior_balance=1000.0, trades=coverage.trades, deposits=[], withdrawals=[],
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
    )
    assert naive_balance.consistent  # the misleading pass R2 warned about

    report = assess_readiness(coverage, naive_balance, ledger_delivery_ok=True,
                               watermark_confirmed=True)
    assert report.ready is False
    assert "coverage" in report.explain()


def test_hidden_buy_and_sell_do_not_produce_false_readiness_and_are_not_permanently_lost():
    """Reference-model review finding 1, reproduced exactly: a hidden BUY
    and SELL at CAD 100 each cancel out on the real balance AND on visible
    trade history (0 visible, reported_total 0) — a naive reading of
    coverage.complete=True + a matching balance identity would call this
    "ready" despite two real executions never having been observed. This
    proves the fix: (a) assess_readiness refuses to call it ready without
    an independent watermark_confirmed=True, and (b) the watermark the
    system would actually persist (compute_safe_watermark) never advances
    past the hidden trades' own timestamps until the safety margin has
    genuinely elapsed — so once they are revealed within the margin, a
    later checkpoint still finds and correctly includes them. Nothing is
    permanently skipped."""
    ex = SyntheticExchange()
    ex.deposit("CAD", 1000.0, timestamp_ms=0, visible=True)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0,
                      timestamp_ms=1_000, trade_id="H_buy", visible=False)
    ex.execute_trade(symbol="BTC/CAD", side="sell", price=100.0, amount=1.0,
                      timestamp_ms=1_001, trade_id="H_sell", visible=False)

    margin = 5_000
    watermark = None

    # --- cycle 1: nothing visible yet ---
    now1 = 2_000
    coverage1 = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=watermark)
    assert coverage1.complete and coverage1.fetched_count == 0  # vacuously page-exhausted
    balance1 = check_balance_consistency(
        prior_balance=1000.0, trades=coverage1.trades, deposits=[], withdrawals=[],
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
    )
    assert balance1.consistent  # the misleading trivial match R2 warned about

    confirmed1 = is_watermark_confirmed(now1, now_ms=now1, safety_margin_ms=margin)  # window_until == now1 here
    report1 = assess_readiness(coverage1, balance1, ledger_delivery_ok=True,
                                watermark_confirmed=confirmed1)
    # The two hidden trades are real, unaccounted-for executions — this
    # MUST NOT report ready, and it doesn't, because a window ending
    # exactly "now" can never itself be watermark-confirmed.
    assert report1.ready is False

    new_watermark1 = compute_safe_watermark(coverage1, now_ms=now1,
                                             previous_watermark_ms=watermark, safety_margin_ms=margin)
    assert new_watermark1 < 1_000  # correctly refuses to advance past the hidden trades' own time
    watermark = new_watermark1

    # --- reveal, well within the margin ---
    ex.reveal_all()

    # --- cycle 2: enough real time has passed for the window to be safely
    #     confirmed AND the trades are now visible ---
    now2 = 8_000
    coverage2 = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=watermark)
    assert coverage2.complete
    assert {t.trade_id for t in coverage2.trades} == {"H_buy", "H_sell"}

    new_watermark2 = compute_safe_watermark(coverage2, now_ms=now2,
                                             previous_watermark_ms=watermark, safety_margin_ms=margin)
    assert new_watermark2 >= 1_001  # now safely past both hidden trades

    balance2 = check_balance_consistency(
        prior_balance=1000.0, trades=coverage2.trades, deposits=[], withdrawals=[],
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
    )
    assert balance2.consistent  # now a GENUINE match, not a coincidental one

    confirmed2 = is_watermark_confirmed(new_watermark2, now_ms=now2, safety_margin_ms=margin)
    report2 = assess_readiness(coverage2, balance2, ledger_delivery_ok=True,
                                watermark_confirmed=confirmed2)
    assert report2.ready is True
    # Neither hidden trade was ever lost — both were eventually observed
    # and correctly folded into a genuinely-confirmed checkpoint. This
    # relied on revealing them WITHIN the safety margin — see the next
    # test for the explicit, still-open limitation when that assumption
    # doesn't hold.


def test_watermark_confirmed_is_conditional_not_proof_and_audit_detects_a_real_violation(db):
    """Reference-model review R2 finding 1, reproduced exactly: this test
    proves the ACKNOWLEDGED LIMITATION exists — it is not a claim the
    limitation is solved. A BUY and SELL hidden longer than the safety
    margin get permanently skipped by the normal watermark path, and
    is_watermark_confirmed (a pure elapsed-time check against an ASSUMED
    bound) reports True regardless, because by construction a checkpoint
    built via compute_safe_watermark satisfies it the instant it's
    written. audit_historical_window is the separate mechanism that CAN
    detect the violation after the fact by re-querying the exchange
    directly — proven here to catch exactly what the normal path misses,
    and to force readiness back to False when wired into assess_readiness
    via historical_audit_clean."""
    ex = SyntheticExchange()
    ex.deposit("CAD", 1000.0, timestamp_ms=0, visible=True)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0,
                      timestamp_ms=1_000, trade_id="H_buy", visible=False)
    ex.execute_trade(symbol="BTC/CAD", side="sell", price=100.0, amount=1.0,
                      timestamp_ms=1_001, trade_id="H_sell", visible=False)

    margin = 5_000
    now = 10_000  # far beyond the margin relative to the hidden trades' own timestamps

    coverage = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=None)
    assert coverage.complete and coverage.fetched_count == 0  # still hidden

    watermark = compute_safe_watermark(coverage, now_ms=now, previous_watermark_ms=None,
                                        safety_margin_ms=margin)
    assert watermark == 5_000  # past BOTH hidden trades' own timestamps already

    balance = check_balance_consistency(
        prior_balance=1000.0, trades=coverage.trades, deposits=[], withdrawals=[],
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
    )
    assert balance.consistent  # trivially, as before

    confirmed = is_watermark_confirmed(watermark, now_ms=now, safety_margin_ms=margin)
    assert confirmed is True  # a pure elapsed-time check — proves nothing about coverage

    commit_checkpoint(db, currency_scope="CAD", window_since_ms=None, window_until_ms=watermark,
                       balance_after=ex.fetch_balance_total("CAD"), trades=coverage.trades)

    # Without an audit, the pipeline reports ready — this IS the
    # acknowledged limitation, not a fixed claim about it.
    unaudited = assess_readiness(coverage, balance, ledger_delivery_ok=True,
                                  watermark_confirmed=confirmed)
    assert unaudited.ready is True
    assert "conditional" in unaudited.explain()

    ex.reveal_all()
    # An exclusive since=5000 query permanently misses both — they
    # executed at 1000/1001, strictly before the watermark.
    post_reveal = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=watermark)
    assert post_reveal.fetched_count == 0  # the real, still-open gap

    # audit_historical_window re-queries the ALREADY-watermarked window
    # directly and finds what the normal path will never see again.
    audit = audit_historical_window(ex, db, "BTC/CAD", audit_since_ms=None,
                                     audit_until_ms=watermark)
    assert audit.violated is True
    assert sorted(audit.newly_discovered_trade_ids) == ["H_buy", "H_sell"]

    audited_report = assess_readiness(coverage, balance, ledger_delivery_ok=True,
                                       watermark_confirmed=confirmed,
                                       historical_audit_clean=not audit.violated)
    assert audited_report.ready is False
    assert "audit" in audited_report.explain().lower()


def test_audit_of_a_genuinely_clean_window_confirms_nothing_was_missed(db):
    """The complementary, non-violation case: a window with no hidden
    trades produces a clean audit, and readiness reports the STRONGEST
    evidence tier this model can produce (explicitly distinguished in
    explain() from the merely-elapsed-time, unaudited case above)."""
    ex = SyntheticExchange()
    ex.deposit("CAD", 1000.0, timestamp_ms=0, visible=True)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0,
                      timestamp_ms=1_000, trade_id="A")  # visible from the start

    coverage = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=None)
    watermark = compute_safe_watermark(coverage, now_ms=10_000, previous_watermark_ms=None,
                                        safety_margin_ms=5_000)
    commit_checkpoint(db, currency_scope="CAD", window_since_ms=None, window_until_ms=watermark,
                       balance_after=ex.fetch_balance_total("CAD"), trades=coverage.trades)
    balance = check_balance_consistency(
        prior_balance=1000.0, trades=coverage.trades, deposits=[], withdrawals=[],
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
    )
    confirmed = is_watermark_confirmed(watermark, now_ms=10_000, safety_margin_ms=5_000)

    audit = audit_historical_window(ex, db, "BTC/CAD", audit_since_ms=None, audit_until_ms=watermark)
    assert audit.violated is False
    assert audit.newly_discovered_trade_ids == []

    report = assess_readiness(coverage, balance, ledger_delivery_ok=True,
                               watermark_confirmed=confirmed, historical_audit_clean=True)
    assert report.ready is True
    assert "audit" in report.explain() and "strongest" in report.explain()


# ---------------------------------------------------------------------------
# 2. A fill between API reads (the original checkpoint race)
# ---------------------------------------------------------------------------

def test_fill_between_reads_produces_explicit_unexplained_residual_then_resolves():
    ex = SyntheticExchange()
    prior_balance = 1000.0
    ex.deposit("CAD", prior_balance, timestamp_ms=0, visible=True)

    # Trade executes but is not yet visible to history reads — balance,
    # however, is authoritative and immediate (real exchange semantics).
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=50_000.0, amount=0.01,
                      timestamp_ms=1_000, trade_id="A", visible=False)

    coverage = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=0)
    assert coverage.complete and coverage.fetched_count == 0  # nothing visible yet
    balance = check_balance_consistency(
        prior_balance=prior_balance, trades=coverage.trades, deposits=[], withdrawals=[],
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
    )
    assert balance.consistent is False
    assert abs(balance.residual - (-500.0)) < 1e-9  # the unseen BUY's cash effect

    ex.reveal_all()
    coverage2 = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=0)
    assert coverage2.complete and coverage2.fetched_count == 1
    balance2 = check_balance_consistency(
        prior_balance=prior_balance, trades=coverage2.trades, deposits=[], withdrawals=[],
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
    )
    assert balance2.consistent


# ---------------------------------------------------------------------------
# 3. Multi-page / late history
# ---------------------------------------------------------------------------

def test_multi_page_retrieval_assembles_full_window():
    ex = SyntheticExchange()
    for i in range(5):
        ex.execute_trade(symbol="BTC/CAD", side="buy", price=50_000.0, amount=0.001,
                          timestamp_ms=1_000 + i, trade_id=f"T{i}")
    coverage = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=0, page_size=2)
    assert coverage.complete
    assert coverage.fetched_count == 5 == coverage.reported_total


def test_reported_total_drift_mid_pagination_is_refused_not_silently_accepted():
    class _DriftingSource:
        """Hand-built two-call source: page 1 reports total=1, page 2
        (continuing the same retrieval attempt) reports total=2 — models a
        new trade becoming visible to the exchange's own count mid-attempt."""
        def __init__(self):
            self._call = 0

        def fetch_my_trades_page(self, symbol, since_ms=None, offset=0, limit=50):
            from execution_accounting_reference_model import SynTrade, TradePage
            self._call += 1
            if self._call == 1:
                t = SynTrade("A", "OA", "BTC/CAD", "buy", 50_000.0, 0.001, 50.0, 0.0, "CAD", 1_000)
                return TradePage([t], reported_total=1, next_offset=1)
            t = SynTrade("B", "OB", "BTC/CAD", "buy", 50_000.0, 0.001, 50.0, 0.0, "CAD", 1_100)
            return TradePage([t], reported_total=2, next_offset=None)

    coverage = retrieve_with_coverage_proof(_DriftingSource(), "BTC/CAD", since_ms=0, page_size=1)
    assert coverage.complete is False
    assert "drifted" in coverage.reason


# ---------------------------------------------------------------------------
# 4. Same-timestamp BUY/SELL — causal ordering vs. opaque-id ordering
# ---------------------------------------------------------------------------

def test_same_timestamp_buy_sell_causal_order_gives_correct_pnl_not_lexical_order():
    from execution_accounting_reference_model import SynTrade

    buy = SynTrade("Z_buy", "O1", "BTC/CAD", "buy", 100.0, 1.0, 100.0, 0.80, "CAD", 5_000)
    sell = SynTrade("A_sell", "O2", "BTC/CAD", "sell", 101.0, 1.0, 101.0, 0.40, "CAD", 5_000)
    # Lexical trade-id order would put "A_sell" before "Z_buy" — wrong.

    ordered = causal_order([sell, buy])  # fed in an arbitrary/adversarial order too
    assert ordered is not None
    assert [t.trade_id for t in ordered] == ["Z_buy", "A_sell"]

    fold = fold_position(ordered)
    # Matches the exact figures from the original PASS-10 review finding:
    # correct round-trip net P&L is -$0.20.
    assert abs(fold.realized_pnl - (-0.20)) < 1e-9
    assert abs(fold.per_trade_pnl["A_sell"] - (-0.20)) < 1e-9

    # Demonstrate the WRONG number a naive lexical-id sort would produce,
    # to make the contrast concrete rather than assumed.
    wrong_order = sorted([sell, buy], key=lambda t: t.trade_id)
    assert [t.trade_id for t in wrong_order] == ["A_sell", "Z_buy"]
    wrong_fold = fold_position(wrong_order)
    # SELL-before-BUY: the SELL is folded against a zero cost basis (no
    # BUY has been applied yet), so its full proceeds are misread as
    # realized profit — 100.60, not the correct -0.20 round-trip loss.
    # fold_position doesn't itself validate non-negativity (causal_order
    # does, which is exactly why skipping causal_order is unsafe).
    assert abs(wrong_fold.per_trade_pnl["A_sell"] - 100.60) < 1e-9
    assert wrong_fold.per_trade_pnl["A_sell"] != fold.per_trade_pnl["A_sell"]


def test_same_timestamp_impossible_ordering_returns_none_not_a_guess():
    from execution_accounting_reference_model import SynTrade

    # A SELL with no preceding BUY at all, tied in time with nothing that
    # could ever cover it.
    sell_only = SynTrade("S1", "O1", "BTC/CAD", "sell", 100.0, 1.0, 100.0, 0.0, "CAD", 5_000)
    result = causal_order([sell_only])
    assert result is None


# ---------------------------------------------------------------------------
# 5. Aggregate legacy-row migration
# ---------------------------------------------------------------------------

def test_legacy_row_migration_matches_conserved_subset_not_proximity():
    from execution_accounting_reference_model import SynTrade

    legacy = LegacyFillRow(fill_id=1, symbol="BTC/CAD", side="buy", quantity=0.02,
                            cost=1000.0, fee_cost=1.0, window_start_ms=0, window_end_ms=10_000)
    t1 = SynTrade("T1", "O1", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.5, "CAD", 1_000)
    t2 = SynTrade("T2", "O1", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.5, "CAD", 1_050)
    # A decoy trade that is CLOSER in time to nothing in particular but
    # must not be pulled in just because a naive proximity match might.
    decoy = SynTrade("T3", "O2", "BTC/CAD", "buy", 40_000.0, 0.005, 200.0, 0.2, "CAD", 1_010)

    result = migrate_legacy_row(legacy, [t1, t2, decoy])
    assert result.blocked is False
    assert sorted(result.matched_trade_ids) == ["T1", "T2"]


def test_legacy_row_migration_blocks_on_genuine_ambiguity():
    from execution_accounting_reference_model import SynTrade

    legacy = LegacyFillRow(fill_id=2, symbol="BTC/CAD", side="buy", quantity=0.02,
                            cost=1000.0, fee_cost=0.0, window_start_ms=0, window_end_ms=10_000)
    # Two DIFFERENT trades, each independently summing to the legacy row's
    # totals when paired with a different partner — genuinely ambiguous.
    t1 = SynTrade("T1", "O1", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.0, "CAD", 1_000)
    t2 = SynTrade("T2", "O1", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.0, "CAD", 1_050)
    t3 = SynTrade("T3", "O2", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.0, "CAD", 1_100)
    t4 = SynTrade("T4", "O2", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.0, "CAD", 1_150)
    # {T1,T2} and {T3,T4} both conserve totals exactly — ambiguous.
    result = migrate_legacy_row(legacy, [t1, t2, t3, t4])
    assert result.blocked is True
    assert "ambiguous" in result.reason


def test_legacy_row_migration_never_renames_existing_exec_key(db):
    with db:
        db.execute(
            "INSERT INTO fills (id, exec_key, timestamp_ms, side, symbol, quantity, "
            "price, fee_cost, source) VALUES (1, 'legacy-uuid-1', 900, 'buy', 'BTC/CAD', "
            "0.02, 50000.0, 1.0, 'legacy_migration')"
        )
    from execution_accounting_reference_model import SynTrade
    legacy = LegacyFillRow(fill_id=1, symbol="BTC/CAD", side="buy", quantity=0.02,
                            cost=1000.0, fee_cost=1.0, window_start_ms=0, window_end_ms=10_000)
    t1 = SynTrade("T1", "O1", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.5, "CAD", 1_000)
    t2 = SynTrade("T2", "O1", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.5, "CAD", 1_050)
    result = migrate_legacy_row(legacy, [t1, t2])
    apply_migration_link(db, result, [t1, t2])

    row = db.execute("SELECT exec_key FROM fills WHERE id = 1").fetchone()
    assert row[0] == "legacy-uuid-1"  # untouched — never rewritten to a trade_id
    links = db.execute("SELECT trade_id FROM legacy_links WHERE legacy_fill_id = 1").fetchall()
    assert sorted(r[0] for r in links) == ["T1", "T2"]
    # Migrated trades ARE observed, but carry no ledger_written_at claim
    # against a NEW fills row of their own — the legacy row is authoritative.
    observed = db.execute(
        "SELECT ledger_written_at FROM observed_trades WHERE trade_id IN ('T1','T2')"
    ).fetchall()
    assert all(r[0] is not None for r in observed)  # linked, via legacy_links, not a new fills row
    assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1  # no duplicate row


def test_migration_then_ordinary_replay_does_not_duplicate_economics(db):
    """Reference-model review finding 2, reproduced exactly: migrating a
    trade into a legacy row, then running ordinary write_ledger_rows for
    that SAME trade (as a normal checkpoint cycle would, unaware it was
    just migrated), used to insert a SECOND fills row — one execution
    represented twice. is_ledger_represented is now the single predicate
    BOTH paths consult, so replay skips anything already migration-linked."""
    with db:
        db.execute(
            "INSERT INTO fills (id, exec_key, timestamp_ms, side, symbol, quantity, "
            "price, fee_cost, source) VALUES (1, 'legacy-uuid-1', 900, 'buy', 'BTC/CAD', "
            "0.01, 100.0, 0.5, 'legacy_migration')"
        )
    from execution_accounting_reference_model import SynTrade
    legacy = LegacyFillRow(fill_id=1, symbol="BTC/CAD", side="buy", quantity=0.01,
                            cost=100.0, fee_cost=0.5, window_start_ms=0, window_end_ms=2_000)
    t = SynTrade("T", "O1", "BTC/CAD", "buy", 100.0, 0.01, 100.0, 0.5, "CAD", 1_000)

    result = migrate_legacy_row(legacy, [t])
    assert result.blocked is False
    apply_migration_link(db, result, [t])

    # Ordinary replay for the SAME trade, as an unrelated normal checkpoint
    # cycle would run it, unaware a migration already claimed T.
    write_ledger_rows(db, [t], fold_position([t]))

    fills = db.execute("SELECT exec_key, quantity FROM fills").fetchall()
    assert fills == [("legacy-uuid-1", 0.01)]  # exactly one row — T never got its own


def test_migration_cannot_claim_a_trade_already_linked_to_a_different_legacy_row(db):
    """Reference-model review finding 2's cross-migration uniqueness demand:
    individual subset matching alone cannot establish that two SEPARATE
    legacy rows don't both consume the same execution — already_linked_
    trade_ids + migrate_legacy_row's already_linked param must exclude a
    once-claimed trade from a later migration's candidate pool."""
    from execution_accounting_reference_model import SynTrade
    t = SynTrade("T", "O1", "BTC/CAD", "buy", 100.0, 0.01, 100.0, 0.5, "CAD", 1_000)

    legacy_a = LegacyFillRow(fill_id=1, symbol="BTC/CAD", side="buy", quantity=0.01,
                              cost=100.0, fee_cost=0.5, window_start_ms=0, window_end_ms=2_000)
    result_a = migrate_legacy_row(legacy_a, [t])
    assert result_a.blocked is False
    apply_migration_link(db, result_a, [t])

    legacy_b = LegacyFillRow(fill_id=2, symbol="BTC/CAD", side="buy", quantity=0.01,
                              cost=100.0, fee_cost=0.5, window_start_ms=0, window_end_ms=2_000)
    linked = already_linked_trade_ids(db)
    assert linked == {"T"}
    result_b = migrate_legacy_row(legacy_b, [t], already_linked=linked)
    assert result_b.blocked is True
    assert "no candidate trades" in result_b.reason or "no subset" in result_b.reason

    # Defense in depth: even bypassing already_linked, apply_migration_link
    # itself refuses to relink an already-claimed trade to a different row.
    forced = migrate_legacy_row(legacy_b, [t])  # no already_linked this time
    assert forced.blocked is False
    with pytest.raises(ValueError, match="already linked"):
        apply_migration_link(db, forced, [t])


# ---------------------------------------------------------------------------
# 6. Checkpoint commit failure
# ---------------------------------------------------------------------------

def test_checkpoint_commit_failure_leaves_no_partial_state(db):
    from execution_accounting_reference_model import SynTrade
    t = SynTrade("A", "O1", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.0, "CAD", 1_000)

    result = commit_checkpoint(db, currency_scope="CAD", window_since_ms=0, window_until_ms=2_000,
                                balance_after=500.0, trades=[t], fail_before_commit=True)
    assert result is None
    assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM observed_trades").fetchone()[0] == 0

    result2 = commit_checkpoint(db, currency_scope="CAD", window_since_ms=0, window_until_ms=2_000,
                                 balance_after=500.0, trades=[t], fail_before_commit=False)
    assert result2 is not None
    assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
    row = db.execute("SELECT checkpoint_id FROM observed_trades WHERE trade_id = 'A'").fetchone()
    assert row[0] == result2


def test_mid_transaction_failure_rolls_back_real_prior_writes(db):
    """Reference-model review finding 3: fail_before_commit returns before
    any SQL executes, which only proves an early return makes no writes —
    it does not prove rollback of a transaction that had ALREADY performed
    real inserts. fail_after_n_trade_inserts raises INSIDE the same `with
    conn:` block after the checkpoint row and one observed_trades row have
    genuinely been written, forcing sqlite3's real rollback machinery to
    undo them — verified by a fresh query finding NOTHING afterward, not
    inferred from the function never having tried."""
    from execution_accounting_reference_model import SynTrade
    t1 = SynTrade("A", "O1", "BTC/CAD", "buy", 100.0, 1.0, 100.0, 0.0, "CAD", 1_000)
    t2 = SynTrade("B", "O2", "BTC/CAD", "buy", 100.0, 1.0, 100.0, 0.0, "CAD", 1_500)

    result = commit_checkpoint(
        db, currency_scope="CAD", window_since_ms=0, window_until_ms=2_000,
        balance_after=800.0, trades=[t1, t2], fail_after_n_trade_inserts=1,
    )
    assert result is None
    # The checkpoint row itself, and trade A's row (inserted before the
    # injected failure on trade B), must BOTH be gone — true atomicity,
    # not a partial commit.
    assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM observed_trades").fetchone()[0] == 0

    # A clean retry (no injected failure) succeeds normally and commits both.
    result2 = commit_checkpoint(
        db, currency_scope="CAD", window_since_ms=0, window_until_ms=2_000,
        balance_after=800.0, trades=[t1, t2],
    )
    assert result2 is not None
    assert db.execute("SELECT COUNT(*) FROM observed_trades").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# 7. Repeated fee revision — idempotent, AND economically convergent
# ---------------------------------------------------------------------------

def test_repeated_fee_revision_is_idempotent(db):
    from execution_accounting_reference_model import SynTrade
    t = SynTrade("A", "O1", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.5, "CAD", 1_000)
    commit_checkpoint(db, currency_scope="CAD", window_since_ms=0, window_until_ms=2_000,
                       balance_after=500.5, trades=[t])

    assert record_fee_revision(db, "A", 0.6) is True     # a real change: revision 1
    assert record_fee_revision(db, "A", 0.6) is False     # same value re-observed — no-op
    assert record_fee_revision(db, "A", 0.6) is False     # again — still no-op
    assert record_fee_revision(db, "A", 0.7) is True      # a further real change: revision 2

    count = db.execute("SELECT COUNT(*) FROM fee_corrections WHERE trade_id='A'").fetchone()[0]
    assert count == 2


def test_fee_revision_converges_with_real_balance_only_once_correction_is_included(db):
    """Reference-model review finding: 'the fake exchange never adjusts
    cash for that revised fee' — the original fee-revision test proved
    only DB-row deduplication, not economic convergence. This proves both
    halves: revise_trade_fee now moves REAL exchange cash, and a balance-
    consistency check using the trade's STALE originally-observed fee
    shows a genuine residual, restored to consistent only once the
    recorded correction's delta is included."""
    ex = SyntheticExchange()
    ex.deposit("CAD", 1000.0, timestamp_ms=0, visible=True)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0,
                      fee_cost=0.5, timestamp_ms=1_000, trade_id="A")

    from execution_accounting_reference_model import SynTrade
    stale_trade = SynTrade("A", "O1", "BTC/CAD", "buy", 100.0, 1.0, 100.0, 0.5, "CAD", 1_000)
    balance0 = check_balance_consistency(
        prior_balance=1000.0, trades=[stale_trade], deposits=[], withdrawals=[],
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
    )
    assert balance0.consistent  # 1000 - 100 - 0.5 = 899.5, matches

    delta = ex.revise_trade_fee("A", 0.8, timestamp_ms=1_500)  # settles higher
    assert abs(delta - 0.3) < 1e-9
    assert abs(ex.fetch_balance_total("CAD") - 899.2) < 1e-9  # real cash actually moved

    # Using ONLY the stale originally-observed fee (0.5), the identity now
    # shows exactly the missed correction as a residual.
    balance1 = check_balance_consistency(
        prior_balance=1000.0, trades=[stale_trade], deposits=[], withdrawals=[],
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
    )
    assert balance1.consistent is False
    assert abs(balance1.residual - (-0.3)) < 1e-9

    recorded = record_fee_revision(db, "A", 0.8)
    assert recorded is True
    balance2 = check_balance_consistency(
        prior_balance=1000.0, trades=[stale_trade], deposits=[], withdrawals=[],
        fresh_balance=ex.fetch_balance_total("CAD"), side_asset_is_quote=True, quote="CAD",
        fee_correction_deltas=[delta],
    )
    assert balance2.consistent  # genuine economic convergence, not just a dedup row


def test_recovered_position_applies_fee_corrections_after_a_real_restart(tmp_path):
    """Reference-model review R2 finding 2, reproduced exactly: BUY 1@100
    and SELL 1@101, both originally zero fee, ledger-written normally.
    Recording a fee revision setting the SELL's fee to $2 and then doing a
    REAL close/reopen used to still report +$1 realized P&L (the correct
    answer is -$1) — recover_position's underlying load_observed_trades
    read only each trade's frozen ORIGINAL fee_cost, never joining against
    fee_corrections. Fixed at the read layer, so every reconstruction
    consumer (recover_position, verify_ledger_delivery_consistency, this
    file's own fold pipeline) is correction-aware without duplicating the
    join."""
    from execution_accounting_reference_model import SynTrade
    db_path = str(tmp_path / "fee_correction_restart.db")
    conn1 = init_db(db_path)
    buy = SynTrade("BUY1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 100.0, 0.0, "CAD", 1_000)
    sell = SynTrade("SELL1", "O2", "BTC/CAD", "sell", 101.0, 1.0, 101.0, 0.0, "CAD", 2_000)
    commit_checkpoint(conn1, currency_scope="CAD", window_since_ms=None, window_until_ms=2_000,
                       balance_after=1001.0, trades=[buy, sell])
    write_ledger_rows(conn1, [buy, sell], fold_position(causal_order([buy, sell])))

    pre = recover_position(conn1, "BTC/CAD")
    assert abs(pre.realized_pnl - 1.0) < 1e-9   # 101 - 100, both fees still zero

    assert record_fee_revision(conn1, "SELL1", 2.0) is True
    conn1.close()

    conn2 = init_db(db_path)
    post = recover_position(conn2, "BTC/CAD")
    assert abs(post.realized_pnl - (-1.0)) < 1e-9   # 101 - 2 - 100 = -1, corrected
    conn2.close()


def test_fee_correction_on_entry_updates_remaining_basis_and_already_closed_pnl(tmp_path):
    """Review R2 finding 2's specific further demand: 'Entry-fee revisions
    must update remaining basis and realized P&L for already-closed
    portions.' fold_position is always a full recompute over ALL observed
    trades (never incremental), so a corrected BUY fee automatically flows
    into both the REMAINING position's avg_cost and an ALREADY-recorded
    partial SELL's own realized P&L the next time reconstruction runs —
    proven here with a partial holding, not just a fully-closed round
    trip, across a real close/reopen."""
    from execution_accounting_reference_model import SynTrade
    db_path = str(tmp_path / "entry_fee_correction.db")
    conn1 = init_db(db_path)
    buy = SynTrade("BUY1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 100.0, 0.0, "CAD", 1_000)
    partial_sell = SynTrade("SELL1", "O2", "BTC/CAD", "sell", 110.0, 0.4, 44.0, 0.0, "CAD", 2_000)
    commit_checkpoint(conn1, currency_scope="CAD", window_since_ms=None, window_until_ms=2_000,
                       balance_after=1044.0, trades=[buy, partial_sell])
    write_ledger_rows(conn1, [buy, partial_sell], fold_position(causal_order([buy, partial_sell])))

    pre = recover_position(conn1, "BTC/CAD")
    assert abs(pre.avg_cost - 100.0) < 1e-9
    assert abs(pre.realized_pnl - 4.0) < 1e-9    # 0.4*110 - 100*0.4 = 44-40
    assert abs(pre.final_qty - 0.6) < 1e-9

    assert record_fee_revision(conn1, "BUY1", 1.0) is True  # entry fee settles to $1
    conn1.close()

    conn2 = init_db(db_path)
    post = recover_position(conn2, "BTC/CAD")
    assert abs(post.avg_cost - 101.0) < 1e-9          # (100+1)/1 — corrected entry basis
    assert abs(post.realized_pnl - 3.6) < 1e-9         # 44 - 101*0.4 = 44-40.4 — the ALREADY-
                                                         # closed portion's pnl updates too
    conn2.close()


def test_ledger_verification_detects_economic_corruption_not_just_representation(db):
    """Reference-model review R2 finding 3, reproduced exactly: rewriting
    an already-correct SELL fills row's stored quantity to 99 and pnl to
    999, while leaving its exec_key untouched, used to still pass
    verify_ledger_delivery_consistency — the checker only looked at
    identity presence, never at stored values. Now compares stored
    quantity/price/fee/pnl against the trade's true (correction-aware)
    observed payload and recomputed fold."""
    from execution_accounting_reference_model import SynTrade
    buy = SynTrade("BUY1", "O1", "BTC/CAD", "buy", 100.0, 1.0, 100.0, 0.0, "CAD", 1_000)
    sell = SynTrade("SELL1", "O2", "BTC/CAD", "sell", 101.0, 1.0, 101.0, 0.0, "CAD", 2_000)
    commit_checkpoint(db, currency_scope="CAD", window_since_ms=None, window_until_ms=2_000,
                       balance_after=1001.0, trades=[buy, sell])
    write_ledger_rows(db, [buy, sell], fold_position(causal_order([buy, sell])))
    assert verify_ledger_delivery_consistency(db, "BTC/CAD") is True

    with db:
        db.execute("UPDATE fills SET quantity = 99, pnl = 999 WHERE exec_key = 'SELL1'")

    assert verify_ledger_delivery_consistency(db, "BTC/CAD") is False


def test_ledger_verification_detects_broken_legacy_conservation(db):
    """Review R2 finding 3's further demand: validate conserved legacy
    totals here too, not just native-row values."""
    with db:
        db.execute(
            "INSERT INTO fills (id, exec_key, timestamp_ms, side, symbol, quantity, "
            "price, fee_cost, source) VALUES (1, 'legacy-uuid-1', 900, 'buy', 'BTC/CAD', "
            "0.02, 50000.0, 1.0, 'legacy_migration')"
        )
    from execution_accounting_reference_model import SynTrade
    legacy = LegacyFillRow(fill_id=1, symbol="BTC/CAD", side="buy", quantity=0.02,
                            cost=1000.0, fee_cost=1.0, window_start_ms=0, window_end_ms=10_000)
    t1 = SynTrade("T1", "O1", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.5, "CAD", 1_000)
    t2 = SynTrade("T2", "O1", "BTC/CAD", "buy", 50_000.0, 0.01, 500.0, 0.5, "CAD", 1_050)
    result = migrate_legacy_row(legacy, [t1, t2])
    apply_migration_link(db, result, [t1, t2])
    assert verify_ledger_delivery_consistency(db, "BTC/CAD") is True

    with db:
        db.execute("UPDATE fills SET fee_cost = 999 WHERE id = 1")
    assert verify_ledger_delivery_consistency(db, "BTC/CAD") is False


# ---------------------------------------------------------------------------
# 8. Cash budget vs. actual exchange cash
# ---------------------------------------------------------------------------

def test_budget_pool_and_exchange_cash_stay_independent_no_double_count():
    from execution_accounting_reference_model import SynTrade

    pool = BudgetPool(total_capital=1000.0)
    pool.allocate("BTC/CAD", 500.0)  # decision at signal time

    reconciler = ExchangeCashReconciler(opening_balance=1000.0, quote="CAD")
    # Actual fill differs from the budgeted figure (slippage + fee).
    actual_fill = SynTrade("A", "O1", "BTC/CAD", "buy", 49_800.0, 0.01, 498.0, 0.80, "CAD", 1_000)
    reconciler.apply_trades([actual_fill])

    assert pool.invested_budget == 500.0            # unchanged by the fill — a decision figure
    assert pool.free_pool_cash == 500.0
    assert abs(reconciler.expected_balance - 501.2) < 1e-9   # 1000 - 498 - 0.80

    ex = SyntheticExchange()
    ex.deposit("CAD", 1000.0, timestamp_ms=0)
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=49_800.0, amount=0.01,
                      fee_cost=0.80, timestamp_ms=1_000, trade_id="A")
    assert abs(ex.fetch_balance_total("CAD") - reconciler.expected_balance) < 1e-9
    # And explicitly NOT equal to the budget-based figure — proving the two
    # must never be summed or substituted for one another.
    assert abs(ex.fetch_balance_total("CAD") - pool.free_pool_cash) > 1e-6


# ---------------------------------------------------------------------------
# 9. Three successive restarts
# ---------------------------------------------------------------------------

def test_three_successive_restarts_converge_with_no_duplication(db):
    from execution_accounting_reference_model import SynTrade
    ex = SyntheticExchange()
    ex.deposit("CAD", 1000.0, timestamp_ms=0)

    # --- "restart" 1: one clean trade, committed normally ---
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=50_000.0, amount=0.001,
                      timestamp_ms=1_000, trade_id="T1")
    coverage1 = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=0)
    assert coverage1.complete
    cp1 = commit_checkpoint(db, currency_scope="CAD", window_since_ms=0, window_until_ms=1_000,
                             balance_after=ex.fetch_balance_total("CAD"), trades=coverage1.trades)
    write_ledger_rows(db, coverage1.trades, fold_position(coverage1.trades))
    assert cp1 is not None

    # --- "restart" 2: a second trade, but the checkpoint commit CRASHES ---
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=51_000.0, amount=0.001,
                      timestamp_ms=2_000, trade_id="T2")
    coverage2 = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=1_000)
    assert coverage2.complete
    cp2_failed = commit_checkpoint(db, currency_scope="CAD", window_since_ms=1_000, window_until_ms=2_000,
                                    balance_after=ex.fetch_balance_total("CAD"), trades=coverage2.trades,
                                    fail_before_commit=True)
    assert cp2_failed is None
    # T2 must NOT be durably recorded yet.
    assert db.execute("SELECT COUNT(*) FROM observed_trades WHERE trade_id='T2'").fetchone()[0] == 0

    # --- "restart" 3: retry T2's window (idempotent re-fetch, same since_ms
    #     as restart 2 because the cursor never advanced past the failed
    #     commit), plus a third trade in the same cycle ---
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=52_000.0, amount=0.001,
                      timestamp_ms=3_000, trade_id="T3")
    coverage3 = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=1_000)  # unchanged cursor
    assert coverage3.complete
    assert {t.trade_id for t in coverage3.trades} == {"T2", "T3"}
    cp3 = commit_checkpoint(db, currency_scope="CAD", window_since_ms=1_000, window_until_ms=3_000,
                             balance_after=ex.fetch_balance_total("CAD"), trades=coverage3.trades)
    write_ledger_rows(db, coverage3.trades, fold_position(coverage3.trades))
    assert cp3 is not None

    all_trades = db.execute("SELECT trade_id FROM observed_trades").fetchall()
    assert sorted(r[0] for r in all_trades) == ["T1", "T2", "T3"]
    fills = db.execute("SELECT exec_key FROM fills").fetchall()
    assert sorted(r[0] for r in fills) == ["T1", "T2", "T3"]  # no duplicates, none lost

    # Final balance identity across the whole run, from the true opening
    # baseline, must match — no double-application, no loss.
    total_cost = 0.001 * (50_000.0 + 51_000.0 + 52_000.0)
    expected_balance = 1000.0 - total_cost
    assert abs(ex.fetch_balance_total("CAD") - expected_balance) < 1e-6


def test_genuine_restart_recovers_state_purely_from_disk_across_three_process_lifetimes(tmp_path):
    """Reference-model review finding 3: the prior restart test kept the
    same connection, the same Python variables, and never actually closed/
    reopened SQLite — it tested SyntheticExchange's own bookkeeping, not
    real recovery, and its all-BUYs trade set couldn't expose a lost cost
    basis. This test closes and reopens the database between every phase,
    recovers the watermark and the entire position PURELY via
    recover_watermark()/recover_position() (no carried Python state), and
    uses a BUY followed by a partial SELL then a full SELL at THREE
    different prices/fees — so a wrong persisted cost basis would show up
    directly in the recovered realized P&L, cross-checked against the real
    exchange's own actual cash movement."""
    db_path = str(tmp_path / "restart.db")
    ex = SyntheticExchange()  # the "exchange" outlives every bot-process restart below
    ex.deposit("CAD", 1000.0, timestamp_ms=0, visible=True)

    # ---- process lifetime 1: BUY ----
    conn1 = init_db(db_path)
    assert recover_watermark(conn1, "CAD") is None  # nothing persisted yet
    ex.execute_trade(symbol="BTC/CAD", side="buy", price=100.0, amount=1.0,
                      fee_cost=0.5, timestamp_ms=1_000, trade_id="BUY1")
    coverage1 = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=None)
    assert coverage1.complete
    commit_checkpoint(conn1, currency_scope="CAD", window_since_ms=None, window_until_ms=1_000,
                       balance_after=ex.fetch_balance_total("CAD"), trades=coverage1.trades)
    full_fold1 = fold_position(causal_order(load_observed_trades(conn1, "BTC/CAD")))
    write_ledger_rows(conn1, coverage1.trades, full_fold1)
    conn1.close()  # <-- genuine close, no state survives except what's on disk

    # ---- process lifetime 2: reopen, recover, partial SELL ----
    conn2 = init_db(db_path)
    assert recover_watermark(conn2, "CAD") == 1_000
    recovered2 = recover_position(conn2, "BTC/CAD")
    assert abs(recovered2.final_qty - 1.0) < 1e-9
    assert abs(recovered2.avg_cost - 100.5) < 1e-9
    assert abs(recovered2.realized_pnl - 0.0) < 1e-9

    ex.execute_trade(symbol="BTC/CAD", side="sell", price=120.0, amount=0.4,
                      fee_cost=0.2, timestamp_ms=2_000, trade_id="SELL1")
    coverage2 = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=1_000)
    assert coverage2.complete and {t.trade_id for t in coverage2.trades} == {"SELL1"}
    commit_checkpoint(conn2, currency_scope="CAD", window_since_ms=1_000, window_until_ms=2_000,
                       balance_after=ex.fetch_balance_total("CAD"), trades=coverage2.trades)
    full_fold2 = fold_position(causal_order(load_observed_trades(conn2, "BTC/CAD")))
    write_ledger_rows(conn2, coverage2.trades, full_fold2)
    conn2.close()

    # ---- process lifetime 3: reopen, recover, full closing SELL ----
    conn3 = init_db(db_path)
    assert recover_watermark(conn3, "CAD") == 2_000
    recovered3 = recover_position(conn3, "BTC/CAD")
    assert abs(recovered3.final_qty - 0.6) < 1e-9
    assert abs(recovered3.avg_cost - 100.5) < 1e-9          # unchanged — no new BUY
    assert abs(recovered3.realized_pnl - 7.6) < 1e-9         # 0.4*120-0.2 - 100.5*0.4

    ex.execute_trade(symbol="BTC/CAD", side="sell", price=90.0, amount=0.6,
                      fee_cost=0.3, timestamp_ms=3_000, trade_id="SELL2")
    coverage3 = retrieve_with_coverage_proof(ex, "BTC/CAD", since_ms=2_000)
    assert coverage3.complete and {t.trade_id for t in coverage3.trades} == {"SELL2"}
    commit_checkpoint(conn3, currency_scope="CAD", window_since_ms=2_000, window_until_ms=3_000,
                       balance_after=ex.fetch_balance_total("CAD"), trades=coverage3.trades)
    full_fold3 = fold_position(causal_order(load_observed_trades(conn3, "BTC/CAD")))
    write_ledger_rows(conn3, coverage3.trades, full_fold3)
    conn3.close()

    # ---- final independent verification: reopen ONE more time ----
    conn4 = init_db(db_path)
    final = recover_position(conn4, "BTC/CAD")
    assert abs(final.final_qty - 0.0) < 1e-9
    assert abs(final.realized_pnl - 1.0) < 1e-9   # 7.6 + (0.6*90-0.3 - 100.5*0.6) = 7.6 - 6.6
    # Cross-check against the REAL exchange's own actual cash movement —
    # since the position ends flat with no other cash events, the disk-
    # recovered realized P&L must equal the exchange's total cash delta
    # from the true opening balance exactly.
    actual_cash_delta = ex.fetch_balance_total("CAD") - 1000.0
    assert abs(final.realized_pnl - actual_cash_delta) < 1e-9
    assert verify_ledger_delivery_consistency(conn4, "BTC/CAD") is True
    fills = conn4.execute("SELECT exec_key FROM fills ORDER BY id").fetchall()
    assert [r[0] for r in fills] == ["BUY1", "SELL1", "SELL2"]
    conn4.close()


# ---------------------------------------------------------------------------
# Overall: readiness is three separate results, not one merged boolean
# ---------------------------------------------------------------------------

def test_readiness_report_names_the_specific_failing_component():
    from execution_accounting_reference_model import CoverageResult

    good_coverage = CoverageResult(True, [], 0, 0)
    bad_balance = check_balance_consistency(
        prior_balance=1000.0, trades=[], deposits=[], withdrawals=[],
        fresh_balance=999.0, side_asset_is_quote=True, quote="CAD",
    )
    report = assess_readiness(good_coverage, bad_balance, ledger_delivery_ok=True,
                               watermark_confirmed=True)
    assert report.ready is False
    assert report.coverage_complete is True
    assert report.balance_consistent is False
    assert report.ledger_delivery_ok is True
    assert "balance" in report.explain() and "coverage" not in report.explain()

    all_good = assess_readiness(
        good_coverage,
        check_balance_consistency(prior_balance=1000.0, trades=[], deposits=[], withdrawals=[],
                                   fresh_balance=1000.0, side_asset_is_quote=True, quote="CAD"),
        ledger_delivery_ok=True, watermark_confirmed=True,
    )
    assert all_good.ready is True

    # The explicit evidence/unknown state: page-exhausted but not yet aged
    # past the safety margin must block readiness on ITS OWN, distinct from
    # coverage or balance — a caller cannot mistake "nothing visible yet"
    # for "proven complete."
    provisional = assess_readiness(
        good_coverage,
        check_balance_consistency(prior_balance=1000.0, trades=[], deposits=[], withdrawals=[],
                                   fresh_balance=1000.0, side_asset_is_quote=True, quote="CAD"),
        ledger_delivery_ok=True, watermark_confirmed=False,
    )
    assert provisional.ready is False
    assert "watermark" in provisional.explain() and "provisional" in provisional.explain()
