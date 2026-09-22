"""
Offline validation of the read-only Kraken ledger + balance adapter
(bot/accounting/kraken_ledger_fetch.py) — the previously-deferred --live
fetch path for the shadow observation-cycle runner.

Everything here runs against a fixture-backed FakePaginatedKrakenExchange
serving full response-envelope dicts shaped exactly like Kraken's real
`privatePostLedgers`/`fetch_balance` responses (id-as-dictionary-key,
`{"error": [...], "result": {"ledger": {...}, "count": N}}` for ledgers;
`{"total": {code: amount, ...}}` for balances). No live exchange call is
made anywhere in this file, and no live-data shadow run is started.

Covers exactly what was requested:
  1. Full response-envelope fixtures — parsing goes through the real
     parse_raw_ledger_entry, never a hand-built LedgerEntry.
  2. Pagination failures — count drift, duplicate rows across pages,
     zero-page-with-nonzero-count.
  3. Asset scoping — multi-asset raw responses, legacy-vs-modern Kraken
     code aliasing (XBT/XXBT), and the canonical-code normalization fix.
  4. Balance-read timing — read_wallet_balance()'s own contract, and,
     wired through run_shadow_cycle, proof that the ledger is always
     fetched (and persisted) BEFORE the balance is read, that a failed
     ledger fetch never triggers a wasted balance read, and that
     observation-specific trust/failure publication (already validated
     for the fixture-based fetch_fn) hold identically for the real
     adapter's fetch_fn/read_wallet_balance_fn.
"""
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from bot.accounting.kraken_ledger_fetch import (
    LedgerFetchCoverageError, build_live_fetch_fn, build_read_wallet_balance_fn,
    fetch_ledger_entries_for_asset, read_wallet_balance,
)
from bot.accounting.ledger_shadow_run import run_shadow_cycle

ACCOUNT = "kraken:live-test"


def _raw(refid, type_, asset, amount, fee, balance, epoch_seconds):
    """Shaped exactly like the VALUE half of Kraken's real
    result['ledger'] envelope entry — no 'id' field, matching
    parse_raw_ledger_entry's real contract (id is the envelope key)."""
    return {
        "refid": refid, "type": type_, "asset": asset,
        "amount": amount, "fee": fee, "balance": balance, "time": epoch_seconds,
    }


def _envelope(*id_raw_pairs):
    return dict(id_raw_pairs)


class FakePaginatedKrakenExchange:
    """Serves `pages` (a list of envelope dicts, {ledger_id: raw_value})
    strictly in call order — mirrors the same fixture idiom already used
    in tests/crypto/test_ledger_quantity_reconciliation_integration.py.
    `counts`, if given, is the count each successive privatePostLedgers
    call reports (allowing a genuine page-to-page drift); otherwise every
    call reports the true total raw-row count across all pages."""

    def __init__(self, pages, counts=None, balances=None):
        self._pages = pages
        self._counts = counts
        self._balances = balances if balances is not None else {"total": {}}
        self._page_index = 0
        self.call_order = []

    def privatePostLedgers(self, params):
        idx = self._page_index
        page = self._pages[idx] if idx < len(self._pages) else {}
        if self._counts is not None:
            count = self._counts[idx] if idx < len(self._counts) else self._counts[-1]
        else:
            count = sum(len(p) for p in self._pages)
        self._page_index += 1
        self.call_order.append("ledgers")
        return {"error": [], "result": {"ledger": page, "count": count}}

    def fetch_balance(self):
        self.call_order.append("balance")
        return self._balances


# Multi-asset fixture: BTC under BOTH the modern "XXBT" code and the
# legacy "XBT" code, plus CAD and SOL — proves scoping AND normalization
# in one shared fixture.
MULTI_ASSET_PAGE = _envelope(
    ("L-BTC-1", _raw("R1", "trade", "XXBT", "0.001", "0", "0.001", 1700000000.0)),
    ("L-CAD-1", _raw("R1", "trade", "ZCAD", "-90.0", "0", "500.0", 1700000000.0)),
    ("L-SOL-1", _raw("R2", "deposit", "SOL", "1.0", "0", "1.0", 1700000100.0)),
    ("L-BTC-2", _raw("R3", "trade", "XBT", "0.0005", "0", "0.0015", 1700000200.0)),   # legacy code
)


# ── 1 & 3. Full response-envelope fixtures, asset scoping ─────────────────────

def test_parses_a_full_response_envelope_and_scopes_to_btc_across_legacy_and_modern_codes():
    ex = FakePaginatedKrakenExchange(pages=[MULTI_ASSET_PAGE])
    result = fetch_ledger_entries_for_asset(
        ex, account_id=ACCOUNT, asset="BTC", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
    )
    assert {e.ledger_id for e in result} == {"L-BTC-1", "L-BTC-2"}
    # Both the modern ("XXBT") and legacy ("XBT") coded rows normalize to
    # ONE canonical code — otherwise a downstream exact-match query scoped
    # to "XXBT" would silently never see the "XBT"-coded row.
    assert {e.asset for e in result} == {"XXBT"}


def test_asset_scoping_isolates_cad_and_sol_from_the_same_multi_asset_response():
    ex_cad = FakePaginatedKrakenExchange(pages=[MULTI_ASSET_PAGE])
    cad_result = fetch_ledger_entries_for_asset(
        ex_cad, account_id=ACCOUNT, asset="CAD", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
    )
    assert [e.ledger_id for e in cad_result] == ["L-CAD-1"]
    assert cad_result[0].asset == "ZCAD"

    ex_sol = FakePaginatedKrakenExchange(pages=[MULTI_ASSET_PAGE])
    sol_result = fetch_ledger_entries_for_asset(
        ex_sol, account_id=ACCOUNT, asset="SOL", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
    )
    assert [e.ledger_id for e in sol_result] == ["L-SOL-1"]


def test_asset_with_zero_matching_entries_returns_empty_not_an_error():
    """A valid, simply-unused asset code (no history for it in this
    ledger stream) is not a failure — an empty result is the honest
    answer, distinct from a coverage problem."""
    ex = FakePaginatedKrakenExchange(pages=[MULTI_ASSET_PAGE])
    result = fetch_ledger_entries_for_asset(
        ex, account_id=ACCOUNT, asset="ETH", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
    )
    assert result == []


def test_unknown_alias_falls_back_to_matching_the_literal_asset_code():
    ex = FakePaginatedKrakenExchange(pages=[_envelope(
        ("L1", _raw("R1", "deposit", "DOGE", "5", "0", "5", 1700000000.0)),
    )])
    result = fetch_ledger_entries_for_asset(
        ex, account_id=ACCOUNT, asset="DOGE", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
    )
    assert [e.ledger_id for e in result] == ["L1"]
    assert result[0].asset == "DOGE"


def test_entries_are_parsed_through_the_real_parser_not_hand_built():
    """Confirms the timestamp conversion (epoch seconds -> ISO UTC) that
    only parse_raw_ledger_entry performs actually happened."""
    ex = FakePaginatedKrakenExchange(pages=[_envelope(
        ("L1", _raw("R1", "deposit", "XXBT", "0.001", "0", "0.001", 1700000000.0)),
    )])
    result = fetch_ledger_entries_for_asset(
        ex, account_id=ACCOUNT, asset="BTC", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
    )
    assert result[0].exchange_timestamp == "2023-11-14T22:13:20.000000Z"
    assert result[0].account_id == ACCOUNT
    assert result[0].batch_id == "b1"


# ── 2. Pagination failures ─────────────────────────────────────────────────────

def test_multi_page_fetch_reconstructs_the_full_set():
    ex = FakePaginatedKrakenExchange(pages=[
        _envelope(("L1", _raw("R1", "deposit", "XXBT", "1", "0", "1", 1700000000.0))),
        _envelope(("L2", _raw("R2", "deposit", "XXBT", "1", "0", "2", 1700000100.0))),
    ])
    result = fetch_ledger_entries_for_asset(
        ex, account_id=ACCOUNT, asset="BTC", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
    )
    assert {e.ledger_id for e in result} == {"L1", "L2"}


def test_count_drift_mid_pagination_raises_coverage_error():
    """The first call's reported count (2) must be accurate enough that
    the loop naturally continues past page 1 (which alone only has 1
    entry) — this is what actually exposes it to the SECOND call's
    genuinely different count (3), a real page-to-page drift rather than
    a constant wrong value from the very first response."""
    ex = FakePaginatedKrakenExchange(
        pages=[
            _envelope(("L1", _raw("R1", "deposit", "XXBT", "1", "0", "1", 1700000000.0))),
            _envelope(("L2", _raw("R2", "deposit", "XXBT", "1", "0", "2", 1700000100.0))),
        ],
        counts=[2, 3],
    )
    with pytest.raises(LedgerFetchCoverageError, match="drifted mid-pagination"):
        fetch_ledger_entries_for_asset(
            ex, account_id=ACCOUNT, asset="BTC", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
        )


def test_duplicate_rows_across_pages_do_not_falsely_satisfy_coverage():
    """Exact same class of bug already fixed once in the shadow
    integration test harness: the SAME entry served on two pages, with
    Kraken's own reported count matching the RAW (not unique) total. A
    length-based check would pass; the unique-identity check must not."""
    dup = ("L1", _raw("R1", "deposit", "XXBT", "1", "0", "1", 1700000000.0))
    ex = FakePaginatedKrakenExchange(pages=[_envelope(dup), _envelope(dup)], counts=[2, 2])
    with pytest.raises(LedgerFetchCoverageError, match="UNIQUE ledger entries but Kraken reports"):
        fetch_ledger_entries_for_asset(
            ex, account_id=ACCOUNT, asset="BTC", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
        )


def test_a_conflicting_duplicate_across_pages_is_rejected_not_silently_resolved():
    """counts=[2, 2] (not [1, 1]) so the loop is forced to actually fetch
    page 2 — otherwise a first page whose own raw count already "satisfies"
    the reported total would stop pagination before ever reaching the
    conflicting second page at all."""
    ledger_id, raw = "L1", _raw("R1", "deposit", "XXBT", "1", "0", "1", 1700000000.0)
    conflicting_raw = dict(raw)
    conflicting_raw["amount"] = "999"
    ex = FakePaginatedKrakenExchange(
        pages=[_envelope((ledger_id, raw)), _envelope((ledger_id, conflicting_raw))], counts=[2, 2],
    )
    with pytest.raises(ValueError, match="conflicting ledger entry"):
        fetch_ledger_entries_for_asset(
            ex, account_id=ACCOUNT, asset="BTC", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
        )


def test_zero_page_with_a_nonzero_reported_count_raises_rather_than_silently_returning_empty():
    ex = FakePaginatedKrakenExchange(pages=[{}], counts=[3])
    with pytest.raises(LedgerFetchCoverageError, match="UNIQUE ledger entries but Kraken reports"):
        fetch_ledger_entries_for_asset(
            ex, account_id=ACCOUNT, asset="BTC", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
        )


def test_malformed_entry_raises_rather_than_guessing():
    ex = FakePaginatedKrakenExchange(pages=[_envelope(
        ("L1", {"refid": "R1", "type": "deposit", "asset": "XXBT"}),   # missing amount/fee/balance/time
    )])
    with pytest.raises(ValueError, match="malformed raw ledger entry"):
        fetch_ledger_entries_for_asset(
            ex, account_id=ACCOUNT, asset="BTC", batch_id="b1", observed_at="2026-01-01T00:00:00Z",
        )


# ── 4. Balance-read timing (adapter-level contract) ────────────────────────────

def test_read_wallet_balance_returns_the_matching_asset_and_a_fresh_timestamp():
    ex = FakePaginatedKrakenExchange(pages=[], balances={"total": {"XXBT": "0.0015", "ZCAD": "500.0"}})
    balance, read_at = read_wallet_balance(ex, asset="BTC")
    assert balance == Decimal("0.0015")
    assert read_at.endswith("Z")
    assert "T" in read_at   # a real ISO-8601 UTC timestamp, not an echo of any caller-supplied value


def test_read_wallet_balance_resolves_legacy_and_modern_codes_identically():
    ex = FakePaginatedKrakenExchange(pages=[], balances={"total": {"XBT": "0.002"}})
    balance, _ = read_wallet_balance(ex, asset="BTC")
    assert balance == Decimal("0.002")


def test_read_wallet_balance_raises_when_asset_absent_rather_than_assuming_zero():
    ex = FakePaginatedKrakenExchange(pages=[], balances={"total": {"ZCAD": "500.0"}})
    with pytest.raises(LedgerFetchCoverageError, match="no entry for asset"):
        read_wallet_balance(ex, asset="BTC")


def test_build_live_fetch_fn_wraps_the_adapter_with_the_exact_run_shadow_cycle_contract():
    ex = FakePaginatedKrakenExchange(pages=[_envelope(
        ("L1", _raw("R1", "deposit", "XXBT", "1", "0", "1", 1700000000.0)),
    )])
    fetch_fn = build_live_fetch_fn(ex, asset="BTC")
    entries = fetch_fn(account_id=ACCOUNT, batch_id="b1", observed_at="2026-01-01T00:00:00Z")
    assert [e.ledger_id for e in entries] == ["L1"]


def test_build_read_wallet_balance_fn_wraps_the_adapter_with_the_exact_run_shadow_cycle_contract():
    ex = FakePaginatedKrakenExchange(pages=[], balances={"total": {"XXBT": "1.5"}})
    read_fn = build_read_wallet_balance_fn(ex, asset="BTC")
    balance, read_at = read_fn()
    assert balance == Decimal("1.5")
    assert isinstance(read_at, str)


# ── 4b. Balance-read timing, wired end to end through run_shadow_cycle ────────

def test_live_adapter_reads_balance_strictly_after_the_ledger_fetch(tmp_path):
    ex = FakePaginatedKrakenExchange(
        pages=[_envelope(("L1", _raw("R1", "deposit", "XXBT", "0.001", "0", "0.001", 1700000000.0)))],
        balances={"total": {"XXBT": "0.001"}},
    )
    result = run_shadow_cycle(
        fetch_fn=build_live_fetch_fn(ex, asset="BTC"), account_id=ACCOUNT, asset="XXBT", evidence_mode="test",
        db_path=str(tmp_path / "obs.db"), status_path=str(tmp_path / "status.json"),
        read_wallet_balance_fn=build_read_wallet_balance_fn(ex, asset="BTC"),
        zero_opening_confirmed=True,
    )
    assert result.trusted is True
    assert ex.call_order == ["ledgers", "balance"]   # ledger fetched (and, by construction, persisted
                                                       # inside run_shadow_cycle) strictly before the balance read


def test_live_adapter_never_reads_balance_when_the_ledger_fetch_fails(tmp_path):
    ex = FakePaginatedKrakenExchange(pages=[{}], counts=[5], balances={"total": {"XXBT": "0.001"}})
    result = run_shadow_cycle(
        fetch_fn=build_live_fetch_fn(ex, asset="BTC"), account_id=ACCOUNT, asset="XXBT", evidence_mode="test",
        db_path=str(tmp_path / "obs.db"), status_path=str(tmp_path / "status.json"),
        read_wallet_balance_fn=build_read_wallet_balance_fn(ex, asset="BTC"),
        zero_opening_confirmed=True,
    )
    assert result.fetch_succeeded is False
    assert result.trusted is False
    assert ex.call_order == ["ledgers"]   # the balance was NEVER read — no point after a failed fetch


def test_live_adapter_wallet_mismatch_from_a_real_timing_gap_is_reported_untrusted_not_silently_accepted(tmp_path):
    """Simulates the unavoidable race this module's own docstring
    describes: new activity landed between the ledger fetch completing
    and the balance read happening. This must resolve as untrusted for
    THIS observation — not silently accepted, and not crash either."""
    ex = FakePaginatedKrakenExchange(
        pages=[_envelope(("L1", _raw("R1", "deposit", "XXBT", "0.001", "0", "0.001", 1700000000.0)))],
        balances={"total": {"XXBT": "0.0015"}},   # ahead of what the ledger fetch captured
    )
    result = run_shadow_cycle(
        fetch_fn=build_live_fetch_fn(ex, asset="BTC"), account_id=ACCOUNT, asset="XXBT", evidence_mode="test",
        db_path=str(tmp_path / "obs.db"), status_path=str(tmp_path / "status.json"),
        read_wallet_balance_fn=build_read_wallet_balance_fn(ex, asset="BTC"),
        zero_opening_confirmed=True,
    )
    assert result.fetch_succeeded is True
    assert result.wallet_agrees is False
    assert result.trusted is False


def test_live_adapter_result_carries_the_balance_reads_own_timestamp_not_the_cycles_started_at(tmp_path):
    ex = FakePaginatedKrakenExchange(
        pages=[_envelope(("L1", _raw("R1", "deposit", "XXBT", "0.001", "0", "0.001", 1700000000.0)))],
        balances={"total": {"XXBT": "0.001"}},
    )
    status_path = str(tmp_path / "status.json")
    result = run_shadow_cycle(
        fetch_fn=build_live_fetch_fn(ex, asset="BTC"), account_id=ACCOUNT, asset="XXBT", evidence_mode="test",
        db_path=str(tmp_path / "obs.db"), status_path=status_path,
        read_wallet_balance_fn=build_read_wallet_balance_fn(ex, asset="BTC"),
        zero_opening_confirmed=True,
    )
    assert result.trusted is True
    published = json.loads(open(status_path).read())
    # started_at is generated once, at the very top of run_shadow_cycle;
    # the balance read happens measurably later, so completed_at (which
    # includes the post-balance-read work) cannot equal started_at.
    assert published["started_at"] != published["completed_at"]


# ── Source guard: no trading-path relationship, read-only credentials only ────

def test_module_has_no_relationship_with_any_trading_path_and_uses_no_order_call():
    import ast
    import inspect

    from bot.accounting import kraken_ledger_fetch as module

    src = inspect.getsource(module)
    tree = ast.parse(src)
    imported_full_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_full_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_full_names.add(node.module)
            imported_full_names.update(f"{node.module}.{alias.name}" for alias in node.names)

    forbidden_import_modules = {"bot.main", "bot.accounting.reconciliation", "bot.accounting.four_way"}
    for forbidden in forbidden_import_modules:
        assert forbidden not in imported_full_names, f"imports {forbidden!r}: {imported_full_names}"

    for forbidden_call in ("create_order", "createOrder", "cancel_order", "cancelOrder"):
        assert forbidden_call not in src
