"""
Acceptance tests for bot/accounting/ledger_quantity_reconciliation.py — the
isolated Decimal ledger observer prototype. Uses temporary sqlite databases
(tmp_path) and the real captured BTC/SOL evidence from this session's own
ledger audit, plus targeted synthetic fixtures for each of the three
refinements this module implements (balance-transition tie resolution,
hard opening-balance-verification gate, honestly-scoped wallet-balance-
agreement check) and the additionally requested cases (partial-fetch
failure, restart recovery, duplicate/conflicting events).

No production file is touched by these tests. This module is not imported
by bot/main.py, reconciliation.py, or four_way.py — see the source guard
at the bottom of this file.
"""
import sqlite3
from decimal import Decimal

import pytest

from bot.accounting.ledger_quantity_reconciliation import (
    LedgerEntry, OpeningCheckpoint, init_db, load_ledger_entries, reconcile,
    upsert_ledger_entry, walk_chain,
)

ACCOUNT = "kraken:trade_bot_local"


def _e(ledger_id, ref, type_, asset, amount, fee, balance, ts, account=ACCOUNT, observed="2026-09-21T00:00:00Z"):
    return LedgerEntry(
        ledger_id=ledger_id, reference_id=ref, account_id=account, type=type_, asset=asset,
        amount_raw=amount, fee_raw=fee, balance_raw=balance, exchange_timestamp=ts,
        observed_at=observed,
    )


# ── Real BTC/SOL evidence, verbatim from this session's ledger_reconciliation_audit.py run ──

REAL_BTC_ENTRIES = [
    _e("LB5CZM-RKXTZ-Q5U2KA", "TZA7XB-ANR7K-BJPM6G", "trade", "BTC", "0.0001130000", "0", "0.0001130000", "2026-06-12T00:00:05.767489Z"),
    _e("LIWJL4-OX4PC-GRQUZZ", "TDCRFZ-MWTNB-2NVHO6", "trade", "BTC", "-0.0001100000", "0.0000004400", "0.0000025600", "2026-06-14T03:56:20.125937Z"),
    _e("L2WAF3-CCKSU-AS2S67", "TANQSU-QH4KF-P62EAP", "trade", "BTC", "0.0001080000", "0", "0.0001105600", "2026-06-15T11:00:16.720134Z"),
    _e("L7IQ44-YSCCA-KZMI3P", "TTOXVM-N4YMU-QT5Y4S", "trade", "BTC", "-0.0001080000", "0", "0.0000025600", "2026-06-16T20:00:03.698917Z"),
    _e("LSE7OT-FLBIW-NE5NBO", "TCLP47-3J4Q6-XNYYDG", "trade", "BTC", "0.0005530000", "0", "0.0005555600", "2026-06-20T01:00:17.042088Z"),
    _e("L36BQY-J7KVG-HIUYEH", "TGPVSQ-VRIAF-RCCYVS", "trade", "BTC", "-0.0005555600", "0", "0E-10", "2026-06-22T16:36:14.897519Z"),
    _e("LGBTCK-TWYWU-NDYU7J", "FTYl6Qu-zyZCpaCJTTzXa3kWQ8gIM9", "deposit", "BTC", "0.0003776600", "0", "0.0003776600", "2026-06-26T13:02:42.975782Z"),
    _e("LA2DJ6-Y5TDI-WTX7SR", "TEYLVF-3GXRC-N6RME4", "trade", "BTC", "-0.0003776600", "0", "0E-10", "2026-06-27T20:00:02.066190Z"),
    _e("LPAHBM-BR6MV-O7NKRU", "TKSLTA-5KS5A-NSJNOS", "trade", "BTC", "0.0000850000", "0", "0.0000850000", "2026-07-07T00:00:11.921688Z"),
    _e("LXO5H7-A45AG-ORV6E3", "TSO4MU-FFRTI-ZLHNWB", "trade", "BTC", "0.0000840000", "0", "0.0001690000", "2026-07-15T16:00:10.543783Z"),
    _e("LIHSU2-53PUR-AP5COZ", "TCRZIZ-OVU6S-REHX5K", "trade", "BTC", "-0.0001690000", "0", "0E-10", "2026-07-17T12:15:00.268905Z"),
]

REAL_SOL_ENTRIES = [
    _e("LAI6NC-BFIYK-HZX4XA", "T4ZXHY-FILDO-FHOLHI", "trade", "SOL", "0.0808080000", "0", "0.0808080000", "2026-08-26T20:00:33.208266Z"),
    _e("LEKHJ3-7F3LR-434Y6Y", "TGUCXA-JBM4G-6KEUXC", "trade", "SOL", "-0.0808080000", "0", "0E-10", "2026-08-27T16:23:12.357242Z"),
    _e("LT5C5N-M5ZIM-JCMXNO", "ELDY5NC-MWJ4N-ENASSE", "reward", "SOL", "0.0000051019", "0.0000015305", "0.0000035714", "2026-08-28T04:32:39.256196Z"),
    _e("LLVKPV-V27VR-TYZQEJ", "ELRMKQS-EEZEM-FOWGRT", "reward", "SOL", "0.0000000021", "0.0000000006", "0.0000035729", "2026-09-04T04:32:38.100453Z"),
    _e("LFF6WF-ZG4MS-FUHHKF", "ELJDZNW-WLFT6-OZP7PJ", "reward", "SOL", "0.0000000022", "0.0000000006", "0.0000035745", "2026-09-11T04:32:44.637402Z"),
    _e("L5BOS6-7WREV-4OG4A3", "ELSYCZH-O5LB6-GGQLMZ", "reward", "SOL", "0.0000000018", "0.0000000005", "0.0000035758", "2026-09-18T04:32:49.217036Z"),
]


def test_real_btc_chain_is_consistent_but_unverified_opening_blocks_overall_pass():
    result = reconcile(REAL_BTC_ENTRIES)
    assert result.chain.consistent is True
    assert result.chain.ambiguous_tie_groups == []
    assert result.chain.failing_ledger_ids == []
    assert result.chain.final_balance == Decimal("0")
    assert result.chain.opening_balance_verified is False   # nothing attested
    assert result.overall_pass is False                     # per point 2: hard gate, not a footnote
    assert "opening balance not verified" in result.reason


def test_real_btc_chain_passes_once_zero_opening_is_explicitly_confirmed():
    result = reconcile(
        REAL_BTC_ENTRIES, zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0"), wallet_balance_read_at="2026-09-21T14:00:00Z",
    )
    assert result.chain.opening_balance_verified is True
    assert result.wallet_balance_agrees_at_read is True
    assert result.overall_pass is True


def test_real_sol_chain_passes_with_zero_opening_confirmed_and_matching_wallet_balance():
    result = reconcile(
        REAL_SOL_ENTRIES, zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0.0000035758"), wallet_balance_read_at="2026-09-21T14:00:00Z",
    )
    assert result.chain.final_balance == Decimal("0.0000035758")
    assert result.overall_pass is True


# ── Refinement 1: equal-timestamp ordering by balance-transition arithmetic ────

def test_equal_timestamp_unique_order_is_resolved_by_balance_arithmetic_not_ledger_id():
    """Two tied entries with DIFFERENT deltas — only one order reconciles.
    Ledger IDs are chosen to sort in the OPPOSITE order from the true one,
    proving the resolution comes from the balance arithmetic, not from
    sorting on ledger_id."""
    ts = "2026-06-01T00:00:00Z"
    # True order: +5 first (balance 5), then +3 (balance 8).
    # "Z-first" ledger_id would sort AFTER "A-first" — deliberately opposite
    # of the true order, so an id-sort would get this wrong.
    first = _e("Z-first", "R1", "trade", "BTC", "5", "0", "5", ts)
    second = _e("A-second", "R2", "trade", "BTC", "3", "0", "8", ts)
    result = walk_chain([first, second], zero_opening_confirmed=True)
    assert result.consistent is True
    assert result.ambiguous_tie_groups == []
    assert result.ordered_ledger_ids == ["Z-first", "A-second"]   # true order, not id-sort order
    assert result.final_balance == Decimal("8")


def test_equal_timestamp_genuinely_ambiguous_is_reported_not_asserted():
    """Two tied, net-zero-effect entries (amount == fee) whose recorded
    balance is identical to the running balance before the group — order
    truly cannot be determined from the arithmetic alone, since swapping
    them changes nothing either entry's own recorded balance would catch.
    (A non-zero-net pair can never actually produce this: for two entries
    A, B, both orders (A,B) and (B,A) only agree on each entry's own fixed
    recorded balance when both entries' net deltas are exactly zero — a
    genuine algebraic fact, not a choice of example.)"""
    ts = "2026-06-01T00:00:00Z"
    prior = _e("L0", "R0", "trade", "BTC", "10", "0", "10", "2026-05-01T00:00:00Z")
    e1 = _e("L1", "R1", "trade", "BTC", "5", "5", "10", ts)   # net 0
    e2 = _e("L2", "R2", "trade", "BTC", "3", "3", "10", ts)   # net 0
    result = walk_chain([prior, e1, e2], zero_opening_confirmed=True)
    assert result.ambiguous_tie_groups == [["L1", "L2"]]
    assert result.consistent is True   # ambiguous, not corrupt — a different signal entirely
    assert result.failing_ledger_ids == []
    assert result.final_balance == Decimal("10")


def test_equal_timestamp_no_valid_order_is_a_real_failure_not_an_ordering_artifact():
    """Two tied entries where NO permutation reconciles — a genuine data
    problem, reported as such, distinct from an ambiguous-order finding."""
    ts = "2026-06-01T00:00:00Z"
    e1 = _e("L1", "R1", "trade", "BTC", "1", "0", "99", ts)   # wrong balance either way
    e2 = _e("L2", "R2", "trade", "BTC", "1", "0", "100", ts)
    result = walk_chain([e1, e2], zero_opening_confirmed=True)
    assert result.consistent is False
    assert set(result.failing_ledger_ids) == {"L1", "L2"}
    assert result.ambiguous_tie_groups == []


def test_reconcile_with_ambiguous_tie_never_reports_overall_pass():
    ts = "2026-06-01T00:00:00Z"
    prior = _e("L0", "R0", "trade", "BTC", "10", "0", "10", "2026-05-01T00:00:00Z")
    e1 = _e("L1", "R1", "trade", "BTC", "5", "5", "10", ts)   # net 0
    e2 = _e("L2", "R2", "trade", "BTC", "3", "3", "10", ts)   # net 0
    result = reconcile(
        [prior, e1, e2], zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("10"), wallet_balance_read_at="2026-09-21T00:00:00Z",
    )
    assert result.chain.consistent is True
    assert result.wallet_balance_agrees_at_read is True
    assert result.overall_pass is False   # ambiguity alone blocks overall_pass
    assert "more than one valid order" in result.reason


# ── Refinement 2: opening balance — verified nonzero supported, hard gate ──────

def test_verified_nonzero_opening_checkpoint_is_supported():
    checkpoint = OpeningCheckpoint(
        balance=Decimal("5.0"),
        evidence="manually confirmed against Kraken's own account statement, 2026-09-21",
    )
    entries = [_e("L1", "R1", "trade", "BTC", "2", "0", "7.0", "2026-06-01T00:00:00Z")]
    result = walk_chain(entries, opening_checkpoint=checkpoint)
    assert result.opening_balance_verified is True
    assert result.opening_balance == Decimal("5.0")
    assert result.final_balance == Decimal("7.0")
    assert result.consistent is True


def test_opening_balance_not_verified_blocks_pass_even_with_a_clean_chain_and_matching_wallet():
    entries = [_e("L1", "R1", "trade", "BTC", "2", "0", "2", "2026-06-01T00:00:00Z")]
    result = reconcile(
        entries, wallet_balance_at_read=Decimal("2"), wallet_balance_read_at="2026-09-21T00:00:00Z",
    )
    assert result.chain.consistent is True
    assert result.wallet_balance_agrees_at_read is True
    assert result.chain.opening_balance_verified is False
    assert result.overall_pass is False   # the hard gate — not a footnote


def test_incorrect_opening_checkpoint_produces_a_genuine_chain_failure():
    """A WRONG opening checkpoint isn't a verification problem, it's an
    arithmetic one — walk_chain must still catch it as a failing transition."""
    checkpoint = OpeningCheckpoint(balance=Decimal("999"), evidence="deliberately wrong for this test")
    entries = [_e("L1", "R1", "trade", "BTC", "2", "0", "2", "2026-06-01T00:00:00Z")]
    result = walk_chain(entries, opening_checkpoint=checkpoint)
    assert result.consistent is False
    assert result.failing_ledger_ids == ["L1"]


# ── Refinement 3: wallet_balance_agrees_at_read — honestly scoped ──────────────

def test_coverage_note_is_always_present_and_never_overclaims():
    result = reconcile(REAL_BTC_ENTRIES, zero_opening_confirmed=True)
    assert "does NOT prove every event" in result.coverage_note
    assert "offsetting unseen movements" in result.coverage_note.lower()


def test_wallet_balance_mismatch_resolved_by_bounded_retry():
    entries = [_e("L1", "R1", "trade", "BTC", "1", "0", "1", "2026-06-01T00:00:00Z")]
    late_entry = _e("L2", "R2", "trade", "BTC", "1", "0", "2", "2026-06-02T00:00:00Z")
    calls = {"n": 0}

    def fetch_more(_since):
        calls["n"] += 1
        return [late_entry] if calls["n"] == 1 else []

    result = reconcile(
        entries, zero_opening_confirmed=True, wallet_balance_at_read=Decimal("2"),
        wallet_balance_read_at="2026-09-21T00:00:00Z", fetch_more_since=fetch_more, max_retries=3,
    )
    assert result.wallet_balance_agrees_at_read is True
    assert result.chain.final_balance == Decimal("2")
    assert calls["n"] == 1


def test_wallet_balance_mismatch_exhausting_retries_is_inconclusive_not_false():
    entries = [_e("L1", "R1", "trade", "BTC", "1", "0", "1", "2026-06-01T00:00:00Z")]

    def fetch_more(_since):
        return []   # nothing new ever appears — real mismatch never explained

    result = reconcile(
        entries, zero_opening_confirmed=True, wallet_balance_at_read=Decimal("5"),
        wallet_balance_read_at="2026-09-21T00:00:00Z", fetch_more_since=fetch_more, max_retries=3,
    )
    assert result.wallet_balance_agrees_at_read is None   # inconclusive, not a hard False
    assert result.overall_pass is False
    assert "inconclusive" in result.reason


def test_wallet_balance_mismatch_with_no_retry_fetcher_is_a_definite_disagreement():
    entries = [_e("L1", "R1", "trade", "BTC", "1", "0", "1", "2026-06-01T00:00:00Z")]
    result = reconcile(
        entries, zero_opening_confirmed=True, wallet_balance_at_read=Decimal("5"),
        wallet_balance_read_at="2026-09-21T00:00:00Z",
    )
    assert result.wallet_balance_agrees_at_read is False
    assert result.overall_pass is False


def test_partial_fetch_failure_during_retry_propagates_not_silently_inconclusive():
    """A genuine fetch failure (e.g. a real pagination-coverage error) must
    surface as an exception, never be quietly downgraded to the same
    'inconclusive' status a legitimate empty re-fetch produces."""
    entries = [_e("L1", "R1", "trade", "BTC", "1", "0", "1", "2026-06-01T00:00:00Z")]

    def flaky_fetch(_since):
        raise RuntimeError("ledger pagination coverage could not be established")

    with pytest.raises(RuntimeError, match="coverage could not be established"):
        reconcile(
            entries, zero_opening_confirmed=True, wallet_balance_at_read=Decimal("5"),
            wallet_balance_read_at="2026-09-21T00:00:00Z", fetch_more_since=flaky_fetch,
        )


# ── Restart recovery and duplicate/conflicting events (temp sqlite db) ─────────

def test_restart_recovery_reproduces_the_identical_result(tmp_path):
    db_path = str(tmp_path / "ledger.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    for e in REAL_BTC_ENTRIES:
        upsert_ledger_entry(conn, e)
    conn.close()

    # "Restart": brand-new connection, reload from disk, reconcile again.
    conn2 = sqlite3.connect(db_path)
    reloaded = load_ledger_entries(conn2, ACCOUNT, "BTC")
    result1 = reconcile(reloaded, zero_opening_confirmed=True)
    conn2.close()

    conn3 = sqlite3.connect(db_path)
    reloaded_again = load_ledger_entries(conn3, ACCOUNT, "BTC")
    result2 = reconcile(reloaded_again, zero_opening_confirmed=True)
    conn3.close()

    assert result1.chain.final_balance == result2.chain.final_balance == Decimal("0")
    assert result1.chain.consistent == result2.chain.consistent is True
    assert len(reloaded) == len(REAL_BTC_ENTRIES)


def test_duplicate_identical_upsert_is_a_silent_no_op(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    init_db(conn)
    e = REAL_BTC_ENTRIES[0]
    assert upsert_ledger_entry(conn, e) is True
    assert upsert_ledger_entry(conn, e) is False   # identical re-observation, no-op
    rows = load_ledger_entries(conn, ACCOUNT, "BTC")
    assert len(rows) == 1


def test_conflicting_upsert_for_the_same_key_raises(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    init_db(conn)
    e = REAL_BTC_ENTRIES[0]
    upsert_ledger_entry(conn, e)
    conflicting = _e(e.ledger_id, e.reference_id, e.type, e.asset, "9.9999999", e.fee_raw,
                     e.balance_raw, e.exchange_timestamp, account=e.account_id)
    with pytest.raises(ValueError, match="conflicting ledger entry"):
        upsert_ledger_entry(conn, conflicting)


# ── Exact review reproductions (P1s found against the first version of this module) ──

def test_p1_blank_opening_checkpoint_evidence_is_rejected():
    entries = [_e("L1", "R1", "trade", "BTC", "1", "0", "1", "2026-06-01T00:00:00Z")]
    with pytest.raises(ValueError, match="non-blank"):
        walk_chain(entries, opening_checkpoint=OpeningCheckpoint(Decimal("1"), ""))


def test_p1_infinite_entry_amount_is_rejected():
    entries = [_e("L1", "R1", "trade", "BTC", "Infinity", "0", "Infinity", "2026-06-01T00:00:00Z")]
    with pytest.raises(ValueError, match="finite"):
        walk_chain(entries, zero_opening_confirmed=True)


def test_p1_infinite_ledger_balance_is_rejected():
    entries = [_e("L1", "R1", "trade", "BTC", "1", "0", "Infinity", "2026-06-01T00:00:00Z")]
    with pytest.raises(ValueError, match="finite"):
        walk_chain(entries, zero_opening_confirmed=True)


def test_p1_infinite_wallet_balance_at_read_is_rejected():
    entries = [_e("L1", "R1", "trade", "BTC", "1", "0", "1", "2026-06-01T00:00:00Z")]
    with pytest.raises(ValueError, match="finite"):
        reconcile(
            entries, zero_opening_confirmed=True,
            wallet_balance_at_read=Decimal("Infinity"), wallet_balance_read_at="2026-09-21T00:00:00Z",
        )


def test_p1_mixed_account_and_asset_scope_is_rejected():
    btc_account_a = _e("L1", "R1", "trade", "BTC", "1", "0", "1", "2026-06-01T00:00:00Z", account="A")
    sol_account_b = _e("L2", "R2", "trade", "SOL", "1", "0", "1", "2026-06-01T00:00:01Z", account="B")
    with pytest.raises(ValueError, match="more than one"):
        walk_chain([btc_account_a, sol_account_b], zero_opening_confirmed=True)


def test_p1_negative_fee_is_rejected():
    entries = [_e("L1", "R1", "trade", "BTC", "1", "-0.5", "1.5", "2026-06-01T00:00:00Z")]
    with pytest.raises(ValueError, match="fee must be >= 0"):
        walk_chain(entries, zero_opening_confirmed=True)


def test_p1_opening_checkpoint_validated_even_with_zero_entries():
    """The empty-history branch must not bypass validation entirely."""
    with pytest.raises(ValueError, match="non-blank"):
        walk_chain([], opening_checkpoint=OpeningCheckpoint(Decimal("1"), ""))
    with pytest.raises(ValueError, match="finite"):
        walk_chain([], opening_checkpoint=OpeningCheckpoint(Decimal("Infinity"), "some evidence"))


def test_p1_fractional_vs_whole_second_timestamps_sort_chronologically_not_as_text():
    """Exact review reproduction: '...00:00:00.1Z' (100ms past the second)
    text-sorts BEFORE '...00:00:00Z' (on the second) because '.' < 'Z' as
    characters, even though both are UTC with no offset ambiguity at all —
    a distinct bug from the timezone-offset case already fixed elsewhere.
    A valid two-entry chain in real chronological order must reconcile
    cleanly, not report both transitions broken."""
    first = _e("L1", "R1", "trade", "BTC", "5", "0", "5", "2026-06-01T00:00:00Z")
    second = _e("L2", "R2", "trade", "BTC", "3", "0", "8", "2026-06-01T00:00:00.1Z")
    result = walk_chain([first, second], zero_opening_confirmed=True)
    assert result.consistent is True
    assert result.failing_ledger_ids == []
    assert result.ordered_ledger_ids == ["L1", "L2"]
    assert result.final_balance == Decimal("8")


def test_p1_malformed_timestamp_is_rejected():
    entries = [_e("L1", "R1", "trade", "BTC", "1", "0", "1", "not-a-timestamp")]
    with pytest.raises(ValueError, match="ISO-8601"):
        walk_chain(entries, zero_opening_confirmed=True)


def test_p1_overlapping_retry_returning_an_already_seen_entry_does_not_double_count():
    """Exact review reproduction: starting with [A] already present, a
    retry fetch returns [A, B] (A re-included, e.g. from an inclusive
    'since' cursor). The chain must treat A as the SAME entry, not apply
    its delta twice — the SQLite upsert guard does not protect this
    in-memory retry-merge path at all, this must be enforced independently."""
    a = _e("L1", "R1", "trade", "BTC", "5", "0", "5", "2026-06-01T00:00:00Z")
    b = _e("L2", "R2", "trade", "BTC", "3", "0", "8", "2026-06-02T00:00:00Z")
    calls = {"n": 0}

    def fetch_more(_since):
        calls["n"] += 1
        return [a, b] if calls["n"] == 1 else []   # re-returns A alongside the genuinely new B

    result = reconcile(
        [a], zero_opening_confirmed=True, wallet_balance_at_read=Decimal("8"),
        wallet_balance_read_at="2026-09-21T00:00:00Z", fetch_more_since=fetch_more, max_retries=3,
    )
    assert result.chain.consistent is True
    assert result.chain.final_balance == Decimal("8")
    assert result.wallet_balance_agrees_at_read is True
    assert len(result.chain.ordered_ledger_ids) == 2   # A counted once, not twice


def test_p1_overlapping_retry_with_a_conflicting_duplicate_raises():
    a = _e("L1", "R1", "trade", "BTC", "5", "0", "5", "2026-06-01T00:00:00Z")
    a_conflicting = _e("L1", "R1", "trade", "BTC", "999", "0", "999", "2026-06-01T00:00:00Z")

    def fetch_more(_since):
        return [a_conflicting]

    with pytest.raises(ValueError, match="conflicting ledger entry"):
        reconcile(
            [a], zero_opening_confirmed=True, wallet_balance_at_read=Decimal("999"),
            wallet_balance_read_at="2026-09-21T00:00:00Z", fetch_more_since=fetch_more,
        )


# ── Exact review reproductions, second pass ─────────────────────────────────

def test_p1_legitimate_re_observation_at_a_later_time_does_not_raise():
    """Exact review reproduction: the SAME real exchange entry, re-fetched
    later (a later `observed_at`, our own bookkeeping field, never part of
    what the exchange itself reported), must merge as the identical
    observation it is — not raise as if the exchange payload had changed."""
    a_first_seen = _e("L1", "R1", "trade", "BTC", "5", "0", "5", "2026-06-01T00:00:00Z",
                      observed="2026-09-21T00:00:00Z")
    a_seen_again_later = _e("L1", "R1", "trade", "BTC", "5", "0", "5", "2026-06-01T00:00:00Z",
                            observed="2026-09-21T01:00:00Z")   # only observed_at differs

    def fetch_more(_since):
        return [a_seen_again_later]

    result = reconcile(
        [a_first_seen], zero_opening_confirmed=True, wallet_balance_at_read=Decimal("5"),
        wallet_balance_read_at="2026-09-21T02:00:00Z", fetch_more_since=fetch_more,
    )
    assert result.chain.consistent is True
    assert result.wallet_balance_agrees_at_read is True
    assert len(result.chain.ordered_ledger_ids) == 1   # collapsed, not duplicated


def test_p1_upsert_also_treats_a_later_observed_at_as_the_same_observation(tmp_path):
    """Same fix, DB layer: upsert_ledger_entry must not need this test to
    pass since it already excluded observed_at from its own comparison —
    this proves it now shares the SAME _exchange_payload logic the
    in-memory dedup uses, rather than two independently-maintained tuples
    that merely happened to agree."""
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    init_db(conn)
    a_first = _e("L1", "R1", "trade", "BTC", "5", "0", "5", "2026-06-01T00:00:00Z",
                observed="2026-09-21T00:00:00Z")
    a_later = _e("L1", "R1", "trade", "BTC", "5", "0", "5", "2026-06-01T00:00:00Z",
                observed="2026-09-21T05:00:00Z")
    assert upsert_ledger_entry(conn, a_first) is True
    assert upsert_ledger_entry(conn, a_later) is False   # same observation, later look — no-op, no raise


def test_p1_initial_duplicate_entries_in_one_call_do_not_falsely_fail_the_chain():
    """Exact review reproduction: an initial [A, A] (the same entry appearing
    twice in one call's input, before any retry is even involved) must
    dedup before the walk, not be treated as two real events sharing the
    exact same timestamp with an identical delta — which would double-apply
    A's own amount and falsely report the chain as broken."""
    a = _e("L1", "R1", "trade", "BTC", "5", "0", "5", "2026-06-01T00:00:00Z")
    result = walk_chain([a, a], zero_opening_confirmed=True)
    assert result.consistent is True
    assert result.final_balance == Decimal("5")
    assert result.ordered_ledger_ids == ["L1"]


# ── Oversized tie-group: unresolved, never guessed, never blamed on a
#    downstream entry ───────────────────────────────────────────────────────

def test_p2_oversized_tie_group_stops_the_walk_instead_of_guessing_a_balance():
    """Exact review reproduction: seven simultaneous deposits with recorded
    balances 1..7, supplied in reverse order, followed by a later deposit
    with balance 8. An earlier version of this function picked the LAST
    entry in the (arbitrarily ordered) oversized group as if its balance
    were established truth, then validated the balance-8 entry against
    that guess — falsely reporting entry 8 as broken. The fix: stop at the
    oversized group, report it as unresolved due to the search limit, and
    never validate anything after it against a guessed balance."""
    ts = "2026-06-01T00:00:00Z"
    seven_tied = [
        _e(f"L{i}", f"R{i}", "trade", "BTC", "1", "0", str(i), ts)
        for i in range(7, 0, -1)   # reverse order: recorded balances 7,6,5,4,3,2,1
    ]
    later = _e("L8", "R8", "trade", "BTC", "1", "0", "8", "2026-06-02T00:00:00Z")
    result = walk_chain(seven_tied + [later], zero_opening_confirmed=True)

    assert len(result.unsearched_tie_groups) == 1
    assert set(result.unsearched_tie_groups[0]) == {f"L{i}" for i in range(1, 8)}
    assert "search limit exceeded" in result.reason
    assert "L8" not in result.failing_ledger_ids   # never blamed — never even evaluated
    assert result.final_balance is None            # never guessed
    assert result.ambiguous_tie_groups == []        # a genuinely different category — no search was attempted


def test_p2_oversized_tie_group_blocks_overall_pass():
    ts = "2026-06-01T00:00:00Z"
    seven_tied = [
        _e(f"L{i}", f"R{i}", "trade", "BTC", "1", "0", str(i), ts)
        for i in range(7, 0, -1)
    ]
    result = reconcile(
        seven_tied, zero_opening_confirmed=True, wallet_balance_at_read=Decimal("7"),
        wallet_balance_read_at="2026-09-21T00:00:00Z",
    )
    assert result.overall_pass is False


def test_p2_wallet_agreement_is_unknown_not_disproven_when_ordering_is_unresolved():
    """Exact review reproduction: seven VALID simultaneous deposits (they
    would reconcile fine if searchable) whose true total, 7, exactly
    matches the supplied wallet balance — but the tie group is too large to
    search, so final_balance is None. An earlier version compared
    `None == Decimal("7")`, which silently evaluates to False in Python,
    and reported a definite disagreement — agreement was actually unknown,
    never disproven. No retry callback is supplied here, matching the
    exact reproduction (a retry could not fix this anyway: more fetched
    data doesn't shrink an already-oversized tie group)."""
    ts = "2026-06-01T00:00:00Z"
    seven_tied = [
        _e(f"L{i}", f"R{i}", "trade", "BTC", "1", "0", str(i), ts)
        for i in range(7, 0, -1)
    ]
    result = reconcile(
        seven_tied, zero_opening_confirmed=True, wallet_balance_at_read=Decimal("7"),
        wallet_balance_read_at="2026-09-21T00:00:00Z",
    )
    assert result.chain.final_balance is None
    assert result.wallet_balance_agrees_at_read is None    # unknown, NOT False
    assert "not a disagreement" in result.reason
    assert result.overall_pass is False


def test_the_module_is_not_imported_by_any_production_path():
    import inspect

    import bot.main as main_mod
    from bot.accounting import four_way as four_way_mod
    from bot.accounting import reconciliation as reconciliation_mod

    for mod in (main_mod, four_way_mod, reconciliation_mod):
        src = inspect.getsource(mod)
        assert "ledger_quantity_reconciliation" not in src, f"{mod.__name__} must not import this prototype"
