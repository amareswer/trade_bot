"""
End-to-end integration test for the isolated Decimal ledger observer
prototype. Exercises the full pipeline a real caller would drive:

    captured RAW exchange responses (Kraken-shaped envelope pages)
        -> parsed by parse_raw_ledger_entry (the actual intended parser,
           not hand-built LedgerEntry objects)
        -> paginated fetch with a coverage proof over UNIQUE identities,
           not raw row counts (the same discipline scripts/
           ledger_reconciliation_audit.py already uses in production-
           adjacent tooling — reimplemented here at TEST-HARNESS level so
           this reconciliation module itself gains no live-fetching
           capability)
        -> atomic batch persistence (rows + a manifest of every ledger_id
           the batch claims, in ONE transaction) into a temporary SQLite
           database
        -> reconciliation (walk_chain / reconcile) PLUS the required
           companion batch-completeness check — a "trusted" result needs
           BOTH, since chain arithmetic alone cannot prove complete
           delivery, and completeness alone (zero batches ever recorded)
           proves nothing either
        -> close the connection, reopen it (simulating a process restart)
        -> repeat the whole cycle

No production file is touched. No live exchange call is made anywhere in
this file — the "exchange" is a fixture-backed fake serving Kraken-shaped
envelope pages. The manual HALT kill-switch, trading parameters, and the
live bot process are all untouched; this module remains unimported by any
production path (see the source guard at the bottom of this file).

Review history, same milestone:
  Round 1 fixed: prefix-truncation crashes escaping detection, pagination
  coverage counting raw rows instead of unique identities, and fixtures
  bypassing the raw parser entirely.
  Round 2 (this version) fixed two further real gaps:
  1. verify_batch_completeness() reported complete=True whenever ZERO
     batches had ever been recorded for an (account_id, asset) pair — a
     freshly initialized database, or an account whose every fetch
     attempt failed before persist_batch ever ran, is a "never observed"
     state, not a vacuously satisfied one. It also only checked rows
     carrying a non-blank batch_id column, so a row inserted with the
     DEFAULT blank batch_id (bypassing persist_batch, net-zero effect on
     the chain) was invisible to either check. Both fixed in the core
     module: completeness now requires >=1 completed batch, and checks
     EVERY row's ledger_id against a batch-membership manifest table
     regardless of that row's own batch_id column.
  2. parse_raw_ledger_entry() previously required raw["id"], but Kraken's
     real Ledgers response never puts the id inside the entry — the id is
     the DICTIONARY KEY of result["ledger"], exactly as
     scripts/ledger_reconciliation_audit.py's own working adapter already
     handles it (`for entry_id, raw in all_entries.items()`). The parser
     now takes ledger_id as an explicit separate argument, and this file's
     fixtures are restructured as (ledger_id, raw_value) pairs assembled
     into real envelope-shaped page dicts, closing the gap where the
     fixture injected its own "id" field to paper over the boundary. An
     earlier version of this file's own docstrings claimed the old shape
     was "confirmed against a real response this session" — it was not;
     that claim is removed.
"""
import sqlite3
from decimal import Decimal

import pytest

from bot.accounting.ledger_quantity_reconciliation import (
    LedgerEntry, _dedup_entries, batch_is_complete, init_db, load_ledger_entries,
    parse_raw_ledger_entry, persist_batch, reconcile, upsert_ledger_entry, verify_batch_completeness,
)

ACCOUNT = "kraken:trade_bot_local"
ASSET = "XXBT"   # Kraken's own internal code for BTC, exactly as the real ledger reports it


def _e(ledger_id, ref, amount, fee, balance, ts, type_="trade", batch_id=""):
    return LedgerEntry(
        ledger_id=ledger_id, reference_id=ref, account_id=ACCOUNT, type=type_, asset=ASSET,
        amount_raw=amount, fee_raw=fee, balance_raw=balance, exchange_timestamp=ts,
        observed_at="2026-09-21T00:00:00Z", batch_id=batch_id,
    )


def _raw(refid, type_, asset, amount, fee, balance, epoch_seconds):
    """The per-entry VALUE shape inside Kraken's real `result['ledger']`
    envelope. Deliberately has NO 'id' field — the id is the envelope's
    own dictionary key, never a field on the entry itself (see
    parse_raw_ledger_entry's docstring and scripts/
    ledger_reconciliation_audit.py's own `all_entries.update(page)`
    handling, which reads ids from `.items()`, never from inside the row)."""
    return {
        "refid": refid, "type": type_, "asset": asset,
        "amount": amount, "fee": fee, "balance": balance, "time": epoch_seconds,
    }


def _envelope(*id_raw_pairs):
    """Builds one page exactly as Kraken's real `result['ledger']` shape:
    {ledger_id: raw_entry_value, ...}."""
    return dict(id_raw_pairs)


# (ledger_id, raw_value) pairs — constructed for this test file so each
# row's own balance transition is internally self-consistent (opening 0 ->
# BUY -> SELL back to ~0 -> DEPOSIT). These are NOT re-quoted from a live
# fetch made in any session, and are not asserted to be; only the shape
# (field names, id-as-envelope-key, epoch-seconds `time`) mirrors Kraken's
# real format.
REAL_RAW_BUY = ("LSE7OT-FLBIW-NE5NBO", _raw("TCLP47-3J4Q6-XNYYDG", "trade", "XXBT",
                "0.0005555600", "0", "0.0005555600", 1781917217.042088))
REAL_RAW_SELL = ("L36BQY-J7KVG-HIUYEH", _raw("TGPVSQ-VRIAF-RCCYVS", "trade", "XXBT",
                 "-0.0005555600", "0", "0E-10", 1782146174.897519))
REAL_RAW_DEPOSIT = ("LGBTCK-TWYWU-NDYU7J", _raw("FTYl6Qu-zyZCpaCJTTzXa3kWQ8gIM9", "deposit", "XXBT",
                    "0.0003776600", "0", "0.0003776600", 1782478962.975782))


class CoverageError(RuntimeError):
    """Raised by the test-harness pagination loop below — mirrors
    scripts/ledger_reconciliation_audit.py's own CoverageError, kept local
    to this test file since it belongs to the harness, not the module
    under test."""


class FakePaginatedLedgerExchange:
    """A fixture-backed stand-in for Kraken's raw, `ofs`-paginated Ledgers
    endpoint. `pages` is a list of envelope DICTS (`{ledger_id: raw_value}`,
    exactly Kraken's own `result['ledger']` shape per page — the harness
    must parse them, exactly like a real caller would). `counts`, if
    given, is a list of the count each page reports (allowing a genuine
    page-to-page CHANGE, not one constant override); otherwise every page
    reports the true total unique-row count. `raise_on_page`, if set,
    raises on that 0-based page index."""

    def __init__(self, pages, counts=None, raise_on_page=None):
        self._pages = pages
        self._counts = counts
        self._raise_on_page = raise_on_page

    def fetch_page(self, page_index):
        if self._raise_on_page is not None and page_index == self._raise_on_page:
            raise RuntimeError(f"simulated exchange failure on page {page_index}")
        page = self._pages[page_index] if page_index < len(self._pages) else {}
        if self._counts is not None:
            count = self._counts[page_index] if page_index < len(self._counts) else self._counts[-1]
        else:
            count = len({k for p in self._pages for k in p})
        next_index = page_index + 1 if page_index + 1 < len(self._pages) else None
        return page, count, next_index


def fetch_all_with_coverage_proof(
    exchange: FakePaginatedLedgerExchange, *, account_id: str, batch_id: str, observed_at: str,
) -> "list[LedgerEntry]":
    """The pagination-with-coverage-proof contract a real ledger_observe.py
    must satisfy: pages envelope dicts, parses each entry through
    parse_raw_ledger_entry with the id taken from the envelope KEY (never
    hand-built, never smuggled inside the value), and — the fix for
    finding 2 — verifies coverage by UNIQUE (account_id, ledger_id)
    identity, not raw row count, rejecting a genuine conflicting duplicate
    along the way via the module's own _dedup_entries. Raises
    CoverageError rather than returning anything if the unique count never
    matches what the exchange itself reported."""
    all_raw_entries: "list[LedgerEntry]" = []
    reported_count = None
    page_index = 0
    while True:
        raw_page, count, next_index = exchange.fetch_page(page_index)
        if reported_count is None:
            reported_count = count
        elif count != reported_count:
            raise CoverageError(f"count drifted mid-pagination ({reported_count} -> {count})")
        all_raw_entries.extend(
            parse_raw_ledger_entry(ledger_id, raw, account_id=account_id, batch_id=batch_id, observed_at=observed_at)
            for ledger_id, raw in raw_page.items()
        )
        if next_index is None:
            break
        page_index = next_index

    if reported_count is None:
        raise CoverageError("exchange never reported a count")

    deduped = _dedup_entries(all_raw_entries)   # raises on a genuine conflicting duplicate
    if len(deduped) != reported_count:
        raise CoverageError(
            f"fetched {len(deduped)} UNIQUE entries but exchange reports {reported_count} "
            f"(raw fetch returned {len(all_raw_entries)} rows total across all pages)"
        )
    return deduped


def _trusted_reconciliation(conn, account_id, asset, *, observation_batch_id, **reconcile_kwargs):
    """What an actual caller must do to call a result trusted: the chain
    walk's own overall_pass, AND aggregate batch-completeness evidence
    (nothing currently persisted is orphaned or partial), AND — the fix
    for the most recent finding — the SPECIFIC observation attempt being
    assessed (`observation_batch_id`, always supplied by the caller, never
    inferred) must itself have a completed manifest. Chain arithmetic
    alone (round 1) cannot prove nothing was lost to an interrupted write;
    aggregate completeness alone (round 2) proves nothing when zero
    batches were ever recorded; and aggregate completeness ALSO cannot
    prove THIS attempt succeeded, since an older, unrelated successful
    batch trivially keeps the aggregate "complete" even when the caller's
    actual current fetch failed outright and left no trace — an unchanged
    wallet balance (no activity, or offsetting unseen movements) can make
    that failure invisible to a balance comparison too. Binding to the
    caller's own observation_batch_id via batch_is_complete() is what
    closes this: an older batch can never substitute for it."""
    loaded = load_ledger_entries(conn, account_id, asset)
    chain_result = reconcile(loaded, **reconcile_kwargs)
    completeness = verify_batch_completeness(conn, account_id, asset)
    this_observation_complete = batch_is_complete(conn, account_id, asset, observation_batch_id)
    trusted = chain_result.overall_pass and completeness.complete and this_observation_complete
    return chain_result, completeness, this_observation_complete, trusted


# ── 1. Full pipeline, happy path, across a close/reopen cycle, repeated twice ──

def test_full_pipeline_end_to_end_across_close_reopen_repeated_twice(tmp_path):
    db_path = str(tmp_path / "ledger.db")

    exchange1 = FakePaginatedLedgerExchange(
        pages=[_envelope(REAL_RAW_BUY), _envelope(REAL_RAW_SELL, REAL_RAW_DEPOSIT)],
    )
    fetched1 = fetch_all_with_coverage_proof(
        exchange1, account_id=ACCOUNT, batch_id="batch-1", observed_at="2026-09-21T00:00:00Z",
    )
    assert len(fetched1) == 3

    conn = sqlite3.connect(db_path)
    init_db(conn)
    persist_batch(conn, account_id=ACCOUNT, asset=ASSET, batch_id="batch-1",
                  entries=fetched1, committed_at="2026-09-21T00:00:01Z")
    conn.close()   # simulate the process ending

    conn = sqlite3.connect(db_path)
    chain1, completeness1, this_obs1, trusted1 = _trusted_reconciliation(
        conn, ACCOUNT, ASSET, observation_batch_id="batch-1", zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0.0003776600"), wallet_balance_read_at="2026-09-21T00:00:02Z",
    )
    conn.close()
    assert trusted1 is True
    assert completeness1.complete is True
    assert this_obs1 is True
    assert chain1.chain.final_balance == Decimal("0.0003776600")

    # --- Cycle 2: a later batch, one genuinely new entry. ---
    later_pair = ("LNEW1-NEW1-NEW1", _raw("TNEW1", "trade", "XXBT", "0.0000100000", "0",
                                          "0.0003876600", 1782500000.0))
    exchange2 = FakePaginatedLedgerExchange(pages=[_envelope(later_pair)])
    fetched2 = fetch_all_with_coverage_proof(
        exchange2, account_id=ACCOUNT, batch_id="batch-2", observed_at="2026-09-21T01:00:00Z",
    )
    conn = sqlite3.connect(db_path)
    persist_batch(conn, account_id=ACCOUNT, asset=ASSET, batch_id="batch-2",
                  entries=fetched2, committed_at="2026-09-21T01:00:01Z")
    conn.close()

    conn = sqlite3.connect(db_path)
    chain2, completeness2, this_obs2, trusted2 = _trusted_reconciliation(
        conn, ACCOUNT, ASSET, observation_batch_id="batch-2", zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0.0003876600"), wallet_balance_read_at="2026-09-21T01:00:02Z",
    )
    conn.close()
    assert trusted2 is True
    assert this_obs2 is True
    assert chain2.chain.final_balance == Decimal("0.0003876600")


# ── 2. Coverage failures must abort before any persistence ────────────────────

def test_coverage_count_drift_between_pages_prevents_any_persistence(tmp_path):
    """A GENUINE page-to-page change (page 0 reports 1, page 1 reports 2) —
    not a constant wrong count from the start."""
    exchange = FakePaginatedLedgerExchange(
        pages=[_envelope(REAL_RAW_BUY), _envelope(REAL_RAW_SELL)], counts=[1, 2],
    )
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    init_db(conn)
    with pytest.raises(CoverageError, match="drifted mid-pagination"):
        fetch_all_with_coverage_proof(
            exchange, account_id=ACCOUNT, batch_id="b", observed_at="2026-09-21T00:00:00Z",
        )
    assert load_ledger_entries(conn, ACCOUNT, ASSET) == []
    conn.close()


def test_coverage_failure_mid_fetch_prevents_any_persistence(tmp_path):
    exchange = FakePaginatedLedgerExchange(
        pages=[_envelope(REAL_RAW_BUY), _envelope(REAL_RAW_SELL)], raise_on_page=1,
    )
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    init_db(conn)
    with pytest.raises(RuntimeError, match="simulated exchange failure"):
        fetch_all_with_coverage_proof(
            exchange, account_id=ACCOUNT, batch_id="b", observed_at="2026-09-21T00:00:00Z",
        )
    assert load_ledger_entries(conn, ACCOUNT, ASSET) == []
    conn.close()


def test_duplicate_rows_across_pages_do_not_falsely_satisfy_coverage(tmp_path):
    """Exact review reproduction: three pages each returning the SAME raw
    row [A], [A], [A], with the exchange reporting count=3. A length-based
    coverage check would pass (3 raw rows == reported 3); the fix checks
    UNIQUE identity count (1 real entry) against the reported count and
    must reject this as a coverage failure, not silently accept it and let
    deduplication quietly leave one entry."""
    exchange = FakePaginatedLedgerExchange(
        pages=[_envelope(REAL_RAW_BUY), _envelope(REAL_RAW_BUY), _envelope(REAL_RAW_BUY)],
        counts=[3, 3, 3],
    )
    with pytest.raises(CoverageError, match="UNIQUE entries but exchange reports"):
        fetch_all_with_coverage_proof(
            exchange, account_id=ACCOUNT, batch_id="b", observed_at="2026-09-21T00:00:00Z",
        )


def test_conflicting_duplicate_across_pages_is_rejected_not_silently_resolved():
    """The SAME ledger_id appearing on two pages with a DIFFERENT payload —
    a genuine conflict, must raise, never be silently picked between."""
    ledger_id, raw = REAL_RAW_BUY
    conflicting_raw = dict(raw)
    conflicting_raw["amount"] = "9.9999999"   # same id, different real payload
    exchange = FakePaginatedLedgerExchange(
        pages=[_envelope(REAL_RAW_BUY), _envelope((ledger_id, conflicting_raw))], counts=[1, 1],
    )
    with pytest.raises(ValueError, match="conflicting ledger entry"):
        fetch_all_with_coverage_proof(
            exchange, account_id=ACCOUNT, batch_id="b", observed_at="2026-09-21T00:00:00Z",
        )


def test_overlap_within_a_single_pagination_call_dedupes_correctly():
    """Overlap WITHIN one fetch_all_with_coverage_proof call (not just
    between separate observation cycles, already covered elsewhere): page 0
    returns [BUY, SELL], page 1 re-returns [SELL, DEPOSIT] — SELL genuinely
    appears in both. The exchange's own reported count (3, the true unique
    total) must be honored, and the result must contain exactly 3 entries."""
    exchange = FakePaginatedLedgerExchange(
        pages=[_envelope(REAL_RAW_BUY, REAL_RAW_SELL), _envelope(REAL_RAW_SELL, REAL_RAW_DEPOSIT)],
        counts=[3, 3],
    )
    result = fetch_all_with_coverage_proof(
        exchange, account_id=ACCOUNT, batch_id="b", observed_at="2026-09-21T00:00:00Z",
    )
    assert len(result) == 3
    assert {e.ledger_id for e in result} == {REAL_RAW_BUY[0], REAL_RAW_SELL[0], REAL_RAW_DEPOSIT[0]}


# ── 3. Overlapping observation cycles must persist and reconcile once ─────────

def test_overlapping_observation_cycles_persist_and_reconcile_the_shared_entry_once(tmp_path):
    db_path = str(tmp_path / "ledger.db")

    cycle1 = FakePaginatedLedgerExchange(pages=[_envelope(REAL_RAW_BUY, REAL_RAW_SELL)])
    fetched1 = fetch_all_with_coverage_proof(
        cycle1, account_id=ACCOUNT, batch_id="cycle-1", observed_at="2026-09-21T00:00:00Z",
    )
    conn = sqlite3.connect(db_path)
    init_db(conn)
    persist_batch(conn, account_id=ACCOUNT, asset=ASSET, batch_id="cycle-1",
                  entries=fetched1, committed_at="2026-09-21T00:00:01Z")
    conn.close()

    # Cycle 2's window overlaps cycle 1's — SELL legitimately reappears.
    cycle2 = FakePaginatedLedgerExchange(pages=[_envelope(REAL_RAW_SELL, REAL_RAW_DEPOSIT)])
    fetched2 = fetch_all_with_coverage_proof(
        cycle2, account_id=ACCOUNT, batch_id="cycle-2", observed_at="2026-09-21T01:00:00Z",
    )
    conn = sqlite3.connect(db_path)
    persist_batch(conn, account_id=ACCOUNT, asset=ASSET, batch_id="cycle-2",
                  entries=fetched2, committed_at="2026-09-21T01:00:01Z")
    conn.close()

    conn = sqlite3.connect(db_path)
    chain, completeness, this_obs, trusted = _trusted_reconciliation(
        conn, ACCOUNT, ASSET, observation_batch_id="cycle-2", zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0.0003776600"), wallet_balance_read_at="2026-09-21T02:00:00Z",
    )
    loaded = load_ledger_entries(conn, ACCOUNT, ASSET)
    conn.close()

    assert len(loaded) == 3   # BUY, SELL, DEPOSIT — SELL counted once despite appearing in both cycles
    assert trusted is True
    assert completeness.checked_batches == 2   # both cycle-1 and cycle-2 hold real completion evidence


# ── 4. Interrupted persistence must never yield a trusted success ─────────────

def test_interrupted_persistence_via_a_raised_exception_leaves_no_partial_batch(tmp_path):
    """Exact review reproduction: fetch +5, +2, -2 (a batch that would
    reconcile cleanly, and whose true final balance a truncated PREFIX can
    coincidentally match too — +5 alone and +5+2-2 both net to +5). A real
    exception is injected DURING persistence, after the first row would
    have been written, proving persist_batch's atomicity: nothing survives
    a crash partway through, not even the first row."""
    plus5 = ("L1", _raw("R1", "trade", "XXBT", "5", "0", "5", 1000.0))
    plus2 = ("L2", _raw("R2", "trade", "XXBT", "2", "0", "7", 1001.0))
    minus2 = ("L3", _raw("R3", "trade", "XXBT", "-2", "0", "5", 1002.0))
    exchange = FakePaginatedLedgerExchange(pages=[_envelope(plus5, plus2, minus2)])
    fetched = fetch_all_with_coverage_proof(
        exchange, account_id=ACCOUNT, batch_id="crash-batch", observed_at="2026-09-21T00:00:00Z",
    )
    assert len(fetched) == 3   # coverage was genuinely fine — the interruption happens during persistence

    db_path = str(tmp_path / "ledger.db")

    class _SimulatedCrash(RuntimeError):
        pass

    class _FlakyConnection(sqlite3.Connection):
        """sqlite3.Connection.execute is a read-only C-level attribute on
        an instance, so it cannot be monkeypatched directly — subclassing
        via the `factory=` argument is the supported way to intercept
        calls made through the connection itself."""
        _insert_calls = 0

        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("INSERT INTO LEDGER_ENTRIES"):
                type(self)._insert_calls += 1
                if type(self)._insert_calls == 2:   # crash while writing the SECOND row (+2)
                    raise _SimulatedCrash("simulated process crash mid-persist_batch")
            return super().execute(sql, *args, **kwargs)

    conn = sqlite3.connect(db_path, factory=_FlakyConnection)
    init_db(conn)
    with pytest.raises(_SimulatedCrash):
        persist_batch(conn, account_id=ACCOUNT, asset=ASSET, batch_id="crash-batch",
                      entries=fetched, committed_at="2026-09-21T00:00:01Z")
    conn.close()   # the "process" dies here — nothing was ever committed

    conn = sqlite3.connect(db_path)
    loaded = load_ledger_entries(conn, ACCOUNT, ASSET)
    completeness = verify_batch_completeness(conn, ACCOUNT, ASSET)
    conn.close()
    assert loaded == []                        # atomicity: not even the FIRST row survived
    assert completeness.checked_batches == 0
    assert completeness.complete is False       # zero confirmed batches is never "complete"


def test_a_prefix_only_batch_reconciles_cleanly_but_is_never_trusted_without_completeness(tmp_path):
    """The heart of finding 1 (round 1): simulate a batch where the
    completion marker's own bookkeeping was bypassed entirely — a row
    persisted directly via upsert_ledger_entry, as a non-atomic writer
    might — leaving a self-consistent PREFIX (+5 alone) that even agrees
    with the real wallet balance (since +5+2-2 nets to the same +5). Chain
    arithmetic alone reports this as a full pass; batch-completeness
    evidence must be what actually catches it."""
    plus5 = _e("L1", "R1", "5", "0", "5", "2026-06-01T00:00:00Z", batch_id="orphan-batch")
    db_path = str(tmp_path / "ledger.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    upsert_ledger_entry(conn, plus5)   # written directly — no persist_batch, no manifest, no marker
    conn.close()

    conn = sqlite3.connect(db_path)
    chain, completeness, this_obs, trusted = _trusted_reconciliation(
        conn, ACCOUNT, ASSET, observation_batch_id="orphan-batch", zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("5"), wallet_balance_read_at="2026-09-21T00:00:00Z",
    )
    conn.close()

    assert chain.overall_pass is True          # chain arithmetic alone is fooled — exactly the finding
    assert completeness.complete is False      # no batch was ever recorded, and the row is unmanifested
    assert completeness.checked_batches == 0
    assert "L1" in completeness.unmanifested_ledger_ids
    assert this_obs is False                   # "orphan-batch" itself has no manifest either
    assert trusted is False                    # the combined check is NOT fooled


def test_zero_batches_ever_recorded_is_not_complete_even_with_zero_entries(tmp_path):
    """Round-2 finding 1, reproduction 1: a freshly initialized database
    that has never had a single successful fetch persisted into it. Chain
    reconciliation over an empty history trivially passes (no entries,
    verified zero opening, wallet reads zero) — but NO evidence exists
    that this account/asset was ever actually observed. The previous
    verify_batch_completeness design returned complete=True here (`not []
    and not []` is vacuously True); it must now report complete=False."""
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    init_db(conn)
    chain, completeness, this_obs, trusted = _trusted_reconciliation(
        conn, ACCOUNT, ASSET, observation_batch_id="never-attempted", zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0"), wallet_balance_read_at="2026-09-21T00:00:00Z",
    )
    conn.close()

    assert chain.overall_pass is True           # the chain itself has nothing to disagree with
    assert completeness.checked_batches == 0
    assert completeness.complete is False       # never observed is not the same as confirmed-empty
    assert "no completed batch has ever been recorded" in completeness.reason
    assert this_obs is False
    assert trusted is False


def test_confirmed_empty_batch_is_complete_unlike_never_having_fetched_at_all(tmp_path):
    """The necessary contrast to the test above: a batch that was
    successfully fetched and persisted, and genuinely contained zero
    entries (a real account with no lifetime ledger activity for this
    asset), IS distinguishable from "never even tried" — because it has
    an actual completion marker recorded via persist_batch, unlike the
    untouched-database case."""
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    init_db(conn)
    persist_batch(conn, account_id=ACCOUNT, asset=ASSET, batch_id="confirmed-empty",
                  entries=[], committed_at="2026-09-21T00:00:00Z")
    chain, completeness, this_obs, trusted = _trusted_reconciliation(
        conn, ACCOUNT, ASSET, observation_batch_id="confirmed-empty", zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0"), wallet_balance_read_at="2026-09-21T00:00:01Z",
    )
    conn.close()

    assert completeness.checked_batches == 1
    assert completeness.complete is True
    assert this_obs is True
    assert trusted is True


def test_unmanifested_row_with_default_blank_batch_id_is_caught_even_when_net_zero(tmp_path):
    """Round-2 finding 1, reproduction 2: a row inserted directly (bypassing
    persist_batch entirely, so batch_id defaults to '') whose amount/fee
    net to exactly zero effect on the running balance. The OLD
    verify_batch_completeness filtered its "unrecorded" check on
    `batch_id != ''`, so a blank-batch-id row was invisible to it
    regardless of what it contained. The fix checks ledger_id membership
    directly, which does not depend on the batch_id column at all."""
    zero_net = _e("L-ZERO", "R-ZERO", "0", "0", "0", "2026-06-01T00:00:00Z")   # default batch_id=""
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    init_db(conn)
    # A real, confirmed batch exists for OTHER activity, so this isn't
    # simply the "zero batches ever" case above — it specifically proves
    # the blank-batch-id row is caught even when a genuine batch record
    # also exists.
    real_entry = _e("L-REAL", "R-REAL", "3", "0", "3", "2026-06-02T00:00:00Z", batch_id="real-batch")
    persist_batch(conn, account_id=ACCOUNT, asset=ASSET, batch_id="real-batch",
                  entries=[real_entry], committed_at="2026-09-21T00:00:00Z")
    upsert_ledger_entry(conn, zero_net)   # bypasses persist_batch — no manifest entry anywhere
    conn.close()

    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    completeness = verify_batch_completeness(conn, ACCOUNT, ASSET)
    conn.close()

    assert completeness.checked_batches == 1     # "real-batch" itself is genuinely fine
    assert completeness.incomplete_batches == []
    assert "L-ZERO" in completeness.unmanifested_ledger_ids
    assert completeness.complete is False


def _setup_batch1_then_failed_batch2(db_path):
    """Shared setup for the three regressions below: batch-1 completes
    normally; a batch-2 fetch attempt then fails entirely (the exchange
    call itself raises before persist_batch is ever called for it — no
    trace of batch-2 exists anywhere in the database). Returns nothing;
    the caller reconnects and reconciles against whatever wallet balance
    its own scenario requires."""
    conn = sqlite3.connect(db_path)
    init_db(conn)
    cycle1 = FakePaginatedLedgerExchange(pages=[_envelope(REAL_RAW_BUY)])
    fetched1 = fetch_all_with_coverage_proof(
        cycle1, account_id=ACCOUNT, batch_id="batch-1", observed_at="2026-09-21T00:00:00Z",
    )
    persist_batch(conn, account_id=ACCOUNT, asset=ASSET, batch_id="batch-1",
                  entries=fetched1, committed_at="2026-09-21T00:00:01Z")
    conn.close()

    cycle2 = FakePaginatedLedgerExchange(pages=[{}], raise_on_page=0)
    with pytest.raises(RuntimeError, match="simulated exchange failure"):
        fetch_all_with_coverage_proof(
            cycle2, account_id=ACCOUNT, batch_id="batch-2", observed_at="2026-09-21T01:00:00Z",
        )


def test_older_completed_batch_does_not_vouch_for_a_failed_newer_fetch_mismatched_balance(tmp_path):
    """batch-1 completes; batch-2's fetch fails outright. Here the real
    wallet balance has genuinely moved on (real BTC arrived that batch-2
    was supposed to, but never did, capture) — a balance comparison alone
    would already catch this case. Binding to batch-2's own manifest via
    observation_batch_id makes the failure detected for the RIGHT reason
    (no manifest for batch-2 at all), not merely because the numbers
    happened to disagree — see the two regressions below for the cases
    where the numbers do NOT disagree."""
    db_path = str(tmp_path / "ledger.db")
    _setup_batch1_then_failed_batch2(db_path)

    conn = sqlite3.connect(db_path)
    chain, completeness, this_obs, trusted = _trusted_reconciliation(
        conn, ACCOUNT, ASSET, observation_batch_id="batch-2", zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0.0009999999"),   # NOT what batch-1 alone predicts
        wallet_balance_read_at="2026-09-21T02:00:00Z",
    )
    conn.close()

    assert completeness.checked_batches == 1     # batch-1's own record is genuinely, correctly complete
    assert completeness.complete is True         # batch-1 truthfully describes what IS persisted
    assert chain.wallet_balance_agrees_at_read is False   # a coincidental extra signal here, not the real proof
    assert this_obs is False                     # the actual reason: batch-2 itself has no manifest at all
    assert trusted is False


def test_failed_newer_observation_with_unchanged_balance_is_not_trusted(tmp_path):
    """The exact gap the previous version of this test suite missed: if
    the real-world balance HAPPENS to be unchanged since batch-1 (no new
    activity occurred), a wallet-balance comparison alone reports
    agreement and — without the observation_batch_id binding — the result
    would be falsely trusted purely because an OLDER, unrelated batch's
    completeness satisfied the aggregate check. The binding must catch
    this regardless: batch-2, the observation actually being assessed,
    has no manifest, full stop."""
    db_path = str(tmp_path / "ledger.db")
    _setup_batch1_then_failed_batch2(db_path)

    conn = sqlite3.connect(db_path)
    chain, completeness, this_obs, trusted = _trusted_reconciliation(
        conn, ACCOUNT, ASSET, observation_batch_id="batch-2", zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0.0005555600"),   # EXACTLY what batch-1 alone already predicts
        wallet_balance_read_at="2026-09-21T02:00:00Z",
    )
    conn.close()

    assert chain.wallet_balance_agrees_at_read is True    # a balance check alone would be fooled here
    assert completeness.complete is True                  # so would the old aggregate-only check
    assert this_obs is False                              # binding to batch-2 specifically is what catches it
    assert trusted is False


def test_failed_newer_observation_with_offsetting_movements_is_not_trusted(tmp_path):
    """A second, distinct real-world scenario that produces the SAME
    observable input as the "unchanged balance" case above, and must be
    denied trust identically: real activity DID occur during batch-2's
    missed window — a deposit and a withdrawal of the same size — but they
    net to zero, so the sampled wallet balance still reads exactly what
    batch-1 alone predicts. The reconciler cannot distinguish "nothing
    happened" from "two things happened that cancelled out" from the
    balance alone (this is precisely what _COVERAGE_NOTE documents as
    wallet_balance_agrees_at_read's own inherent limitation) — which is
    exactly why trust must never rest on that comparison alone, and must
    instead require batch-2's own manifest to exist."""
    db_path = str(tmp_path / "ledger.db")
    _setup_batch1_then_failed_batch2(db_path)
    # In the real world (never fetched, never persisted): +0.0001 BTC
    # deposit and a -0.0001 BTC withdrawal both happened inside batch-2's
    # missed window, netting to zero — the wallet reads the same value as
    # the unchanged-balance case above for a completely different reason.

    conn = sqlite3.connect(db_path)
    chain, completeness, this_obs, trusted = _trusted_reconciliation(
        conn, ACCOUNT, ASSET, observation_batch_id="batch-2", zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0.0005555600"),   # net-zero real activity looks identical
        wallet_balance_read_at="2026-09-21T02:00:00Z",
    )
    conn.close()

    assert chain.wallet_balance_agrees_at_read is True
    assert completeness.complete is True
    assert this_obs is False
    assert trusted is False


def test_successful_retry_of_a_previously_failed_observation_restores_trust(tmp_path):
    """The necessary positive counterpart: after batch-2 fails once, a
    RETRY (same batch_id, a fresh fetch attempt) that actually succeeds
    and persists must restore trust — proving the fix is a genuine binding
    to "does THIS observation have a manifest," not a one-way latch that
    can never recover once a batch_id has failed once."""
    db_path = str(tmp_path / "ledger.db")
    _setup_batch1_then_failed_batch2(db_path)

    # Confirm the failure state first.
    conn = sqlite3.connect(db_path)
    _, _, this_obs_before, trusted_before = _trusted_reconciliation(
        conn, ACCOUNT, ASSET, observation_batch_id="batch-2", zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0.0005555600"), wallet_balance_read_at="2026-09-21T02:00:00Z",
    )
    conn.close()
    assert this_obs_before is False
    assert trusted_before is False

    # Retry batch-2 — this time the exchange call succeeds.
    retry = FakePaginatedLedgerExchange(pages=[_envelope(REAL_RAW_SELL, REAL_RAW_DEPOSIT)])
    fetched_retry = fetch_all_with_coverage_proof(
        retry, account_id=ACCOUNT, batch_id="batch-2", observed_at="2026-09-21T03:00:00Z",
    )
    conn = sqlite3.connect(db_path)
    persist_batch(conn, account_id=ACCOUNT, asset=ASSET, batch_id="batch-2",
                  entries=fetched_retry, committed_at="2026-09-21T03:00:01Z")
    conn.close()

    conn = sqlite3.connect(db_path)
    chain_after, completeness_after, this_obs_after, trusted_after = _trusted_reconciliation(
        conn, ACCOUNT, ASSET, observation_batch_id="batch-2", zero_opening_confirmed=True,
        wallet_balance_at_read=Decimal("0.0003776600"), wallet_balance_read_at="2026-09-21T03:00:02Z",
    )
    conn.close()

    assert this_obs_after is True
    assert completeness_after.checked_batches == 2   # batch-1 and the now-successful batch-2
    assert completeness_after.complete is True
    assert chain_after.wallet_balance_agrees_at_read is True
    assert trusted_after is True


def test_the_integration_test_itself_makes_no_live_call_and_touches_no_production_file():
    """Source guard: everything above this test must be fixture-driven —
    checked by scanning the module's source EXCLUDING this test's own body,
    since a forbidden marker spelled out as a literal string to search for
    would otherwise trivially match itself (the same false-positive class
    already hit once earlier this session with a "create_order" guard)."""
    import inspect
    import sys

    full_src = inspect.getsource(sys.modules[__name__])
    this_test_src = inspect.getsource(test_the_integration_test_itself_makes_no_live_call_and_touches_no_production_file)
    src_under_test = full_src.replace(this_test_src, "")

    forbidden_import = "".join(["import", " ", "ccxt"])
    forbidden_module = "".join(["bot", ".", "main"])
    forbidden_flag = "".join(["logs", "/", "HALT"])
    forbidden_cred_a = "".join(["api", "_", "key"])
    forbidden_cred_b = "".join(["api", "key"])

    assert forbidden_import not in src_under_test
    assert forbidden_module not in src_under_test
    assert forbidden_flag not in src_under_test
    assert forbidden_cred_a not in src_under_test.lower()
    assert forbidden_cred_b not in src_under_test.lower()
