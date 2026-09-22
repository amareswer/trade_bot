"""
Offline validation of the shadow-only ledger reconciliation runner
(bot/accounting/ledger_shadow_run.py + scripts/ledger_shadow_run.py).

Everything here runs against tmp_path (never a real logs/ path) and a
fixture-backed fetch function (never a live exchange call). This is the
"validate offline" step requested before any live-data shadow run is
started — no such run is started by this file, or anywhere in this
session.

Covers exactly the four properties requested:
  1. Preserves the tested trust checks — every assertion below is phrased
     in terms of the SAME chain.overall_pass / verify_batch_completeness /
     batch_is_complete composition already validated in
     test_ledger_quantity_reconciliation_integration.py; nothing here
     reimplements or relaxes that logic.
  2. A fresh observation_id every cycle.
  3. A failed or untrusted cycle is published as-is, never masked by an
     older success already on disk.
  4. Storage is fully caller-specified (isolated), the feature is off by
     default, and there is no import relationship with any trading path.
"""
import json
import os
import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from bot.accounting.ledger_quantity_reconciliation import LedgerEntry, load_ledger_entries
from bot.accounting.ledger_shadow_run import (
    EvidenceModeConflict, append_observation_history, is_shadow_enabled, new_observation_id,
    publish_status, read_observation_history, run_shadow_cycle,
)

ACCOUNT = "kraken:shadow-test"
ASSET = "XXBT"


def _entry(ledger_id, amount, balance, batch_id, ts="2026-01-01T00:00:00.000000Z"):
    return LedgerEntry(
        ledger_id=ledger_id, reference_id=f"R-{ledger_id}", account_id=ACCOUNT, type="deposit",
        asset=ASSET, amount_raw=amount, fee_raw="0", balance_raw=balance,
        exchange_timestamp=ts, observed_at=ts, batch_id=batch_id,
    )


def _ok_fetch(entries):
    def fetch_fn(*, account_id, batch_id, observed_at):
        return [replace(e, account_id=account_id, batch_id=batch_id, observed_at=observed_at) for e in entries]
    return fetch_fn


def _failing_fetch(message="simulated exchange failure"):
    def fetch_fn(*, account_id, batch_id, observed_at):
        raise RuntimeError(message)
    return fetch_fn


# ── 1. Fresh observation id every cycle ────────────────────────────────────────

def test_new_observation_id_is_never_reused_even_with_an_identical_clock():
    from datetime import datetime, timezone
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = {new_observation_id(now=fixed_now) for _ in range(50)}
    assert len(ids) == 50   # uuid4 component guarantees freshness regardless of clock resolution


def test_each_run_shadow_cycle_call_uses_a_distinct_observation_id(tmp_path):
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    r1 = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    r2 = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L2", "1", "2", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("2"), wallet_balance_read_at="2026-01-01T01:00:01Z",
        zero_opening_confirmed=True,
    )
    assert r1.observation_id != r2.observation_id


# ── 2. Trust checks preserved — a successful cycle is genuinely trusted ────────

def test_successful_cycle_is_trusted_using_the_full_composed_check(tmp_path):
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    result = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "0.0001", "0.0001", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("0.0001"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert result.fetch_succeeded is True
    assert result.chain_pass is True
    assert result.wallet_agrees is True
    assert result.completeness_complete is True
    assert result.this_observation_complete is True
    assert result.trusted is True

    loaded = load_ledger_entries(sqlite3.connect(db_path), ACCOUNT, ASSET)
    assert len(loaded) == 1
    assert loaded[0].batch_id == result.observation_id   # persisted under the fresh id, not a hand-picked one


def test_wallet_mismatch_is_not_trusted_even_though_the_fetch_itself_succeeded(tmp_path):
    """Preserves the distinction between "fetch failed" and "fetched fine
    but doesn't reconcile" — both must be untrusted, but for different,
    correctly-labeled reasons."""
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    result = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "0.0001", "0.0001", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("999"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert result.fetch_succeeded is True
    assert result.wallet_agrees is False
    assert result.trusted is False


# ── 3. A failed/untrusted cycle is published as-is, never masked ──────────────

def test_a_fetch_failure_persists_nothing_and_publishes_untrusted(tmp_path):
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    result = run_shadow_cycle(
        fetch_fn=_failing_fetch(), account_id=ACCOUNT, asset=ASSET, evidence_mode="test",
        db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("0"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert result.fetch_succeeded is False
    assert result.trusted is False
    assert result.this_observation_complete is False
    assert "simulated exchange failure" in result.error

    conn = sqlite3.connect(db_path)
    # init_db still runs (schema exists) but nothing was ever persisted for this failed attempt.
    assert load_ledger_entries(conn, ACCOUNT, ASSET) == []
    conn.close()

    published = json.loads(open(status_path).read())
    assert published["observation_id"] == result.observation_id
    assert published["trusted"] is False


def test_a_failed_cycle_overwrites_a_prior_success_in_the_published_status(tmp_path):
    """The exact property requested: publishing must never retain an
    older success once a newer attempt has failed."""
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    success = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert success.trusted is True
    published_after_success = json.loads(open(status_path).read())
    assert published_after_success["trusted"] is True
    assert published_after_success["observation_id"] == success.observation_id

    failure = run_shadow_cycle(
        fetch_fn=_failing_fetch(), account_id=ACCOUNT, asset=ASSET, evidence_mode="test",
        db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T02:00:01Z",
        zero_opening_confirmed=True,
    )
    assert failure.trusted is False
    assert failure.observation_id != success.observation_id

    published_after_failure = json.loads(open(status_path).read())
    # The critical assertion: the status file now shows the FAILED cycle's
    # own id and verdict, not the earlier success left in place.
    assert published_after_failure["observation_id"] == failure.observation_id
    assert published_after_failure["observation_id"] != success.observation_id
    assert published_after_failure["trusted"] is False
    assert published_after_failure["fetch_succeeded"] is False


def test_a_recovered_cycle_after_a_failure_is_published_as_trusted_again(tmp_path):
    """The necessary positive counterpart — publishing isn't a one-way
    latch either; a later genuine success is reported as such."""
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    run_shadow_cycle(
        fetch_fn=_failing_fetch(), account_id=ACCOUNT, asset=ASSET, evidence_mode="test",
        db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T02:00:01Z",
        zero_opening_confirmed=True,
    )
    recovered = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L2", "1", "2", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("2"), wallet_balance_read_at="2026-01-01T03:00:01Z",
        zero_opening_confirmed=True,
    )
    assert recovered.trusted is True

    published = json.loads(open(status_path).read())
    assert published["observation_id"] == recovered.observation_id
    assert published["trusted"] is True


def test_publish_status_write_is_atomic_leaving_no_tmp_file_behind(tmp_path):
    status_path = str(tmp_path / "status.json")
    result = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=str(tmp_path / "obs.db"), status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert os.path.exists(status_path)
    assert not os.path.exists(status_path + ".tmp")
    assert json.loads(open(status_path).read())["observation_id"] == result.observation_id


# ── 3b. Failures AFTER a successful fetch must never leave a stale status ─────
# (review finding: only fetch_fn exceptions were caught; a persist_batch or
# reconcile() exception propagated uncaught, leaving whatever the PREVIOUS
# cycle's status happened to be sitting on disk looking exactly like a
# still-current, still-trusted result.)

def test_persistence_failure_after_a_prior_success_does_not_leave_the_old_status_visible(tmp_path):
    """Exact review reproduction: a successful cycle, then a conflicting
    ledger entry on the next cycle raises inside persist_batch itself
    (not the fetch) — the published status must reflect THIS failed
    cycle, never the earlier success."""
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    success = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert success.trusted is True

    def conflicting_fetch(*, account_id, batch_id, observed_at):
        # Same ledger_id as the prior cycle, DIFFERENT payload — a genuine
        # conflict persist_batch's own guard must reject.
        return [_entry("L1", "999", "999", batch_id)]

    failure = run_shadow_cycle(
        fetch_fn=conflicting_fetch, account_id=ACCOUNT, asset=ASSET, evidence_mode="test",
        db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T02:00:01Z",
        zero_opening_confirmed=True,
    )
    assert failure.fetch_succeeded is True      # the fetch itself worked fine — this is NOT a fetch failure
    assert failure.trusted is False
    assert "conflicting ledger entry" in failure.error
    assert failure.observation_id != success.observation_id

    published = json.loads(open(status_path).read())
    assert published["observation_id"] == failure.observation_id   # NOT the stale success
    assert published["trusted"] is False


def test_reconciliation_failure_after_a_prior_success_does_not_leave_the_old_status_visible(tmp_path):
    """Same shape, different failure stage: an entry that persist_batch
    happily stores (it does not validate finiteness or sign) but
    reconcile()'s own _validate_entry rejects (a negative fee) — proves
    the catch-all covers reconcile()/verify_batch_completeness()/
    batch_is_complete(), not just persist_batch."""
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    success = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert success.trusted is True

    def negative_fee_fetch(*, account_id, batch_id, observed_at):
        return [LedgerEntry(
            ledger_id="L2", reference_id="R-L2", account_id=account_id, type="trade", asset=ASSET,
            amount_raw="1", fee_raw="-1", balance_raw="1",
            exchange_timestamp="2026-01-01T01:00:00.000000Z", observed_at=observed_at, batch_id=batch_id,
        )]

    failure = run_shadow_cycle(
        fetch_fn=negative_fee_fetch, account_id=ACCOUNT, asset=ASSET, evidence_mode="test",
        db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T02:00:01Z",
        zero_opening_confirmed=True,
    )
    assert failure.fetch_succeeded is True
    assert failure.trusted is False
    assert "fee must be >= 0" in failure.error
    assert failure.observation_id != success.observation_id

    published = json.loads(open(status_path).read())
    assert published["observation_id"] == failure.observation_id
    assert published["trusted"] is False


def test_initial_in_progress_publish_failure_aborts_the_cycle_entirely(tmp_path, monkeypatch):
    """If the VERY FIRST publish (the in-progress placeholder, before any
    database or fetch work) itself fails, the whole cycle must abort —
    nothing should be attempted that nobody could see evidence of."""
    import bot.accounting.ledger_shadow_run as shadow_module

    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    def always_fails(status_path_arg, result):
        raise RuntimeError("simulated publish outage")

    monkeypatch.setattr(shadow_module, "publish_status", always_fails)

    with pytest.raises(RuntimeError, match="simulated publish outage"):
        run_shadow_cycle(
            fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
            account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
            wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
            zero_opening_confirmed=True,
        )
    assert not os.path.exists(status_path)   # the failed publish never wrote anything
    assert not os.path.exists(db_path)        # aborted before any db/fetch work began


def test_final_publication_failure_after_a_fetch_failure_propagates_and_leaves_in_progress_status(tmp_path, monkeypatch):
    """The requested "publication failure after a prior success" case: a
    genuine success is on disk, then a later cycle's fetch fails AND the
    publish of that failure result itself fails. The exception must
    propagate (never be swallowed to fake a clean return), and what's
    left on disk must be the in-progress placeholder for the NEW
    observation_id — never the stale earlier success, and never a
    fabricated "everything is fine" result either."""
    import bot.accounting.ledger_shadow_run as shadow_module

    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    success = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert success.trusted is True   # published via the REAL publish_status, before the wrapper below exists

    real_publish = shadow_module.publish_status
    calls = {"n": 0}

    def flaky_publish(status_path_arg, result):
        calls["n"] += 1
        if calls["n"] == 2:   # this cycle's SECOND publish — the terminal failed-result one, not the
                                # first (in-progress), which must still succeed
            raise RuntimeError("simulated publish outage")
        return real_publish(status_path_arg, result)

    monkeypatch.setattr(shadow_module, "publish_status", flaky_publish)

    with pytest.raises(RuntimeError, match="simulated publish outage"):
        run_shadow_cycle(
            fetch_fn=_failing_fetch(), account_id=ACCOUNT, asset=ASSET, evidence_mode="test",
            db_path=db_path, status_path=status_path,
            wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T02:00:01Z",
            zero_opening_confirmed=True,
        )
    assert calls["n"] == 2   # confirms the failure hit the intended (final) publish, not the in-progress one

    published = json.loads(open(status_path).read())
    assert published["in_progress"] is True                      # the in-progress marker survives
    assert published["trusted"] is False
    assert published["observation_id"] != success.observation_id  # NOT the stale earlier success


# ── 3c. First-run storage directory (review finding: sqlite3.connect() ────────
# raised on a not-yet-existing nested path before any of the above logic ran)

def test_run_shadow_cycle_creates_a_nonexistent_nested_db_directory(tmp_path):
    db_path = str(tmp_path / "a" / "b" / "c" / "obs.db")
    status_path = str(tmp_path / "a" / "b" / "c" / "status.json")
    assert not os.path.exists(os.path.dirname(db_path))

    result = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert result.trusted is True
    assert os.path.exists(db_path)


def test_cli_creates_a_nonexistent_nested_shadow_directory_on_first_run(tmp_path, monkeypatch):
    """The exact review reproduction, via the actual CLI entry point: a
    new, never-before-seen nested shadow path must not raise
    OperationalError('unable to open database file')."""
    monkeypatch.setenv("LEDGER_SHADOW_ENABLED", "true")
    import scripts.ledger_shadow_run as cli_module

    db_path = str(tmp_path / "nested" / "dir" / "obs.db")
    status_path = str(tmp_path / "nested" / "dir" / "status.json")
    assert not os.path.exists(os.path.dirname(db_path))

    exit_code = cli_module.main(["--fixture", "--db", db_path, "--status", status_path])

    assert exit_code == 0
    assert os.path.exists(db_path)
    assert os.path.exists(status_path)


# ── 3d. Evidence-mode separation: fixture and live must never share a DB ──────
# (review finding: identical default db/status/account-id for both CLI modes
# meant running the documented fixture command, then a live cycle against
# the same path, persisted a synthetic FIXTURE-1 deposit into what was
# supposed to be real observation history — an empty real account then
# incorrectly failed reconciliation because of leftover fixture data.)

def test_run_shadow_cycle_stamps_a_fresh_database_with_its_evidence_mode(tmp_path):
    db_path = str(tmp_path / "obs.db")
    result = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="fixture",
        db_path=db_path, status_path=str(tmp_path / "status.json"),
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert result.trusted is True
    assert result.evidence_mode == "fixture"

    conn = sqlite3.connect(db_path)
    stamped = conn.execute("SELECT evidence_mode FROM shadow_evidence_mode").fetchone()[0]
    conn.close()
    assert stamped == "fixture"


def test_run_shadow_cycle_rejects_a_live_cycle_against_a_database_stamped_fixture(tmp_path):
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    fixture_result = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="fixture",
        db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert fixture_result.trusted is True

    # A "live" cycle against the SAME database (an explicit path override,
    # not a default collision) must be rejected outright — never silently
    # let a synthetic fixture deposit masquerade as real observation history.
    live_result = run_shadow_cycle(
        fetch_fn=_ok_fetch([]), account_id=ACCOUNT, asset=ASSET, evidence_mode="live",
        db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("0"), wallet_balance_read_at="2026-01-01T02:00:01Z",
        zero_opening_confirmed=True,
    )
    assert live_result.fetch_succeeded is False   # rejected before the fetch was ever attempted
    assert live_result.trusted is False
    assert "evidence mode conflict" in live_result.reason
    assert live_result.evidence_mode == "live"

    published = json.loads(open(status_path).read())
    assert published["observation_id"] == live_result.observation_id
    assert published["trusted"] is False


def test_run_shadow_cycle_rejects_a_fixture_cycle_against_a_database_stamped_live(tmp_path):
    """The symmetric direction — a database that has only ever seen real
    live cycles must also reject a fixture cycle."""
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    run_shadow_cycle(
        fetch_fn=_ok_fetch([]), account_id=ACCOUNT, asset=ASSET, evidence_mode="live",
        db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("0"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )

    fixture_result = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="fixture",
        db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T02:00:01Z",
        zero_opening_confirmed=True,
    )
    assert fixture_result.fetch_succeeded is False
    assert fixture_result.trusted is False
    assert "evidence mode conflict" in fixture_result.reason


def test_cli_reproduces_and_rejects_the_original_fixture_then_live_reuse(tmp_path, monkeypatch):
    """The EXACT review reproduction, via the actual CLI: run the
    documented --fixture example, then run --live against the SAME
    explicit --db/--status/--account-id (an explicit override, not a
    default collision — proving the guarantee holds even then). Before
    the fix, an empty real account incorrectly failed reconciliation
    because FIXTURE-1 remained in its history; now the second run must
    be cleanly rejected as an evidence-mode conflict instead."""
    monkeypatch.setenv("LEDGER_SHADOW_ENABLED", "true")
    import bot.accounting.kraken_ledger_fetch as kraken_module
    import scripts.ledger_shadow_run as cli_module

    shared_db = str(tmp_path / "shared.db")
    shared_status = str(tmp_path / "shared_status.json")
    shared_account = "kraken:trade_bot_local"

    fixture_exit = cli_module.main([
        "--fixture", "--db", shared_db, "--status", shared_status, "--account-id", shared_account,
    ])
    assert fixture_exit == 0

    class _EmptyRealAccount:
        def privatePostLedgers(self, params):
            return {"error": [], "result": {"ledger": {}, "count": 0}}

        def fetch_balance(self):
            return {"total": {"XXBT": "0"}}

    monkeypatch.setattr(kraken_module, "build_exchange", lambda: _EmptyRealAccount())

    live_exit = cli_module.main([
        "--live", "--asset", "BTC", "--zero-opening-confirmed",
        "--db", shared_db, "--status", shared_status, "--account-id", shared_account,
    ])

    published = json.loads(open(shared_status).read())
    assert live_exit == 1
    assert published["trusted"] is False
    assert "evidence mode conflict" in published["reason"]
    # Specifically NOT the old false failure this bug caused (a wallet
    # mismatch from leftover fixture data) — the rejection must name the
    # real cause.
    assert "wallet balance does not agree" not in published["reason"]


def test_evidence_mode_field_labels_every_published_status(tmp_path):
    """"Label status output with its mode" — checked across the
    in-progress placeholder, a fetch failure, and a terminal success."""
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")

    run_shadow_cycle(
        fetch_fn=_failing_fetch(), account_id=ACCOUNT, asset=ASSET, evidence_mode="fixture",
        db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("0"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert json.loads(open(status_path).read())["evidence_mode"] == "fixture"

    result = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="fixture",
        db_path=db_path, status_path=status_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:02Z",
        zero_opening_confirmed=True,
    )
    assert result.evidence_mode == "fixture"
    assert json.loads(open(status_path).read())["evidence_mode"] == "fixture"


# ── 3e. Observation history: an append-only record, separate from status ──────
# (review finding: publish_status's status_path always shows only the LATEST
# outcome by design — so after several real cycles, an earlier untrusted
# result, e.g. "opening balance not verified", was silently lost the moment a
# later cycle published its own result. history_path is the separate,
# additive record that never overwrites anything.)

def test_history_is_not_recorded_when_history_path_is_omitted(tmp_path):
    """Default behavior for every existing caller is unaffected."""
    run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test",
        db_path=str(tmp_path / "obs.db"), status_path=str(tmp_path / "status.json"),
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert not os.path.exists(str(tmp_path / "history.jsonl"))


def test_history_accumulates_every_cycles_terminal_result_without_losing_earlier_ones(tmp_path):
    """The exact property requested: an earlier untrusted result must
    still be readable after a later cycle succeeds — status.json alone
    cannot show this (it only ever holds the latest), history.jsonl must."""
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")
    history_path = str(tmp_path / "history.jsonl")

    untrusted = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "0.0001", "0.0001", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test",
        db_path=db_path, status_path=status_path, history_path=history_path,
        wallet_balance_at_read=Decimal("0.0001"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=False,   # deliberately NOT asserted — mirrors the real live run's first cycle
    )
    assert untrusted.trusted is False
    assert "opening balance not verified" in untrusted.reason

    trusted = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "0.0001", "0.0001", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test",
        db_path=db_path, status_path=status_path, history_path=history_path,
        wallet_balance_at_read=Decimal("0.0001"), wallet_balance_read_at="2026-01-01T00:00:02Z",
        zero_opening_confirmed=True,
    )
    assert trusted.trusted is True

    # status.json now shows ONLY the second (trusted) outcome — the first
    # is gone from it, exactly the documented, deliberate behavior.
    published = json.loads(open(status_path).read())
    assert published["observation_id"] == trusted.observation_id
    assert published["trusted"] is True

    # history.jsonl, in contrast, still has BOTH — nothing was lost.
    history = read_observation_history(history_path)
    terminal_records = [r for r in history if not r["in_progress"]]
    assert [r["observation_id"] for r in terminal_records] == [
        untrusted.observation_id, trusted.observation_id,
    ]
    assert terminal_records[0]["trusted"] is False
    assert "opening balance not verified" in terminal_records[0]["reason"]
    assert terminal_records[1]["trusted"] is True


def test_history_also_records_in_progress_and_failed_observations_not_just_successes(tmp_path):
    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")
    history_path = str(tmp_path / "history.jsonl")

    failure = run_shadow_cycle(
        fetch_fn=_failing_fetch(), account_id=ACCOUNT, asset=ASSET, evidence_mode="test",
        db_path=db_path, status_path=status_path, history_path=history_path,
        wallet_balance_at_read=Decimal("0"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    history = read_observation_history(history_path)
    # One in-progress placeholder, then the terminal failure — both for
    # the SAME observation_id, both preserved.
    assert len(history) == 2
    assert history[0]["in_progress"] is True
    assert history[0]["observation_id"] == failure.observation_id
    assert history[1]["in_progress"] is False
    assert history[1]["observation_id"] == failure.observation_id
    assert history[1]["fetch_succeeded"] is False


def test_read_observation_history_returns_empty_list_for_a_never_created_file(tmp_path):
    assert read_observation_history(str(tmp_path / "nonexistent_history.jsonl")) == []


def test_append_observation_history_is_a_no_op_when_history_path_is_none():
    """Direct unit test of the primitive itself — never raises, never
    creates anything, when the caller hasn't opted in."""
    append_observation_history(None, _in_progress_result_for_test())


def _in_progress_result_for_test():
    from bot.accounting.ledger_shadow_run import _in_progress_result
    return _in_progress_result("obs-1", "test", "2026-01-01T00:00:00Z")


def test_history_creates_a_nonexistent_nested_directory(tmp_path):
    history_path = str(tmp_path / "nested" / "dir" / "history.jsonl")
    run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test",
        db_path=str(tmp_path / "obs.db"), status_path=str(tmp_path / "status.json"),
        history_path=history_path,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert os.path.exists(history_path)


def test_cli_live_and_fixture_modes_pass_through_a_mode_specific_history_path(tmp_path, monkeypatch):
    """The CLI's own wiring for this — mode-specific default history
    files, matching the same fixture/live separation as --db/--status."""
    monkeypatch.setenv("LEDGER_SHADOW_ENABLED", "true")
    import scripts.ledger_shadow_run as cli_module

    assert "history" in cli_module._MODE_DEFAULTS["fixture"]
    assert "history" in cli_module._MODE_DEFAULTS["live"]
    assert cli_module._MODE_DEFAULTS["fixture"]["history"] != cli_module._MODE_DEFAULTS["live"]["history"]

    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")
    history_path = str(tmp_path / "history.jsonl")
    exit_code = cli_module.main([
        "--fixture", "--db", db_path, "--status", status_path, "--history", history_path,
    ])
    assert exit_code == 0
    assert len(read_observation_history(history_path)) == 2   # in-progress + terminal


# ── 4. Isolation, off-by-default, no trading-path relationship ────────────────

def test_two_independent_asset_pairs_never_share_state(tmp_path):
    """Different db/status paths (as any two isolated shadow runs, or a
    real run vs. a test, would use) never interact."""
    db_a, status_a = str(tmp_path / "a.db"), str(tmp_path / "a_status.json")
    db_b, status_b = str(tmp_path / "b.db"), str(tmp_path / "b_status.json")

    run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=db_a, status_path=status_a,
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert not os.path.exists(db_b)
    assert not os.path.exists(status_b)


def test_is_shadow_enabled_defaults_false_and_recognizes_truthy_values(monkeypatch):
    monkeypatch.delenv("LEDGER_SHADOW_ENABLED", raising=False)
    assert is_shadow_enabled() is False

    for value in ("true", "True", "1", "yes", "on"):
        monkeypatch.setenv("LEDGER_SHADOW_ENABLED", value)
        assert is_shadow_enabled() is True

    for value in ("false", "0", "", "no", "off"):
        monkeypatch.setenv("LEDGER_SHADOW_ENABLED", value)
        assert is_shadow_enabled() is False


def test_run_shadow_cycle_itself_does_not_consult_the_env_gate(tmp_path, monkeypatch):
    """The gate belongs to the entry point, not the cycle logic — a direct
    call to run_shadow_cycle must work regardless of LEDGER_SHADOW_ENABLED,
    exactly like cfg.accounting.enabled gates bot/main.py's wiring without
    living inside the accounting primitives themselves."""
    monkeypatch.delenv("LEDGER_SHADOW_ENABLED", raising=False)
    result = run_shadow_cycle(
        fetch_fn=_ok_fetch([_entry("L1", "1", "1", "placeholder")]),
        account_id=ACCOUNT, asset=ASSET, evidence_mode="test", db_path=str(tmp_path / "obs.db"),
        status_path=str(tmp_path / "status.json"),
        wallet_balance_at_read=Decimal("1"), wallet_balance_read_at="2026-01-01T00:00:01Z",
        zero_opening_confirmed=True,
    )
    assert result.trusted is True


def test_the_shadow_run_module_has_no_relationship_with_any_trading_path():
    """Source guard: no ACTUAL IMPORT of bot.main, reconciliation.py,
    four_way.py, or any live exchange/order construct, in either the core
    module or the CLI script — checked as import statements, not bare
    substrings, since words like "reconciliation" legitimately appear
    throughout this module's own prose describing what it does NOT import
    (the same self-matching trap hit earlier this session: a docstring
    honestly naming the thing it avoids trivially "contains" that name)."""
    import ast
    import inspect

    from bot.accounting import ledger_shadow_run as core_module
    import scripts.ledger_shadow_run as cli_module

    # Exact module paths, not bare substrings — "reconciliation" alone
    # would also (wrongly) flag this module's legitimate import of
    # bot.accounting.ledger_quantity_reconciliation.
    forbidden_import_modules = {"bot.main", "bot.accounting.reconciliation", "bot.accounting.four_way"}
    forbidden_calls = {"create_order", "createOrder"}

    for module in (core_module, cli_module):
        src = inspect.getsource(module)
        tree = ast.parse(src)
        imported_full_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_full_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_full_names.add(node.module)
                imported_full_names.update(f"{node.module}.{alias.name}" for alias in node.names)
        for forbidden in forbidden_import_modules:
            assert forbidden not in imported_full_names, (
                f"{module.__name__} imports {forbidden!r}: {imported_full_names}"
            )
        for forbidden_call in forbidden_calls:
            assert forbidden_call not in src

    # Both defaults are checked precisely (exact resolved constant, not a
    # source substring) by test_cli_default_paths_point_under_logs_shadow_
    # never_production_trades_db below — a literal-string scan here would
    # only re-trip on this module's own honest docstring prose about what
    # it does NOT touch, the same self-matching trap noted above.


def test_cli_does_nothing_when_disabled(tmp_path, monkeypatch, capsys):
    """Nested, not-yet-existing paths — proves this doesn't just skip the
    db/status FILES, it never even creates their PARENT directory (the
    directory-creation fix must stay strictly behind the enabled check)."""
    monkeypatch.delenv("LEDGER_SHADOW_ENABLED", raising=False)
    import scripts.ledger_shadow_run as cli_module

    db_path = str(tmp_path / "nested" / "dir" / "obs.db")
    status_path = str(tmp_path / "nested" / "dir" / "status.json")
    exit_code = cli_module.main(["--fixture", "--db", db_path, "--status", status_path])

    assert exit_code == 0
    assert not os.path.exists(os.path.dirname(db_path))   # not even the parent directory
    assert not os.path.exists(db_path)
    assert not os.path.exists(status_path)
    assert "off by default" in capsys.readouterr().out


def test_cli_fixture_mode_runs_one_trusted_cycle_when_enabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEDGER_SHADOW_ENABLED", "true")
    import scripts.ledger_shadow_run as cli_module

    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")
    exit_code = cli_module.main(["--fixture", "--db", db_path, "--status", status_path])

    assert exit_code == 0
    assert os.path.exists(db_path)
    published = json.loads(open(status_path).read())
    assert published["trusted"] is True
    assert published["fetch_succeeded"] is True


def test_cli_live_mode_never_constructs_an_exchange_when_disabled(tmp_path, monkeypatch):
    """When LEDGER_SHADOW_ENABLED is unset, --live must not even try to
    build a real (network-touching) exchange connection — the enabled
    check must gate everything, including exchange construction, not
    just the eventual cycle."""
    monkeypatch.delenv("LEDGER_SHADOW_ENABLED", raising=False)
    import bot.accounting.kraken_ledger_fetch as kraken_module
    import scripts.ledger_shadow_run as cli_module

    def _must_not_be_called():
        raise AssertionError("build_exchange() was called despite LEDGER_SHADOW_ENABLED being unset")

    monkeypatch.setattr(kraken_module, "build_exchange", _must_not_be_called)

    db_path = str(tmp_path / "nested" / "dir" / "obs.db")
    status_path = str(tmp_path / "nested" / "dir" / "status.json")
    exit_code = cli_module.main(["--live", "--db", db_path, "--status", status_path])

    assert exit_code == 0
    assert not os.path.exists(os.path.dirname(db_path))
    assert not os.path.exists(status_path)


class _FakeLiveKrakenExchange:
    """A minimal, self-contained fake used only to prove the CLI's --live
    WIRING (argument parsing -> adapter construction -> run_shadow_cycle)
    is correct with ZERO real network access — build_exchange() itself
    (the one function that would actually call ccxt's real
    load_markets()) is monkeypatched to return this instead. `asset_code`/
    `balance_code` let a test independently vary which Kraken code the
    ledger ENTRY carries vs. which code the BALANCE response carries —
    proving alias resolution is symmetric regardless of which spelling
    shows up on which side."""

    def __init__(self, asset_code="XXBT", balance_code="XXBT"):
        self.call_order = []
        self._asset_code = asset_code
        self._balance_code = balance_code

    def privatePostLedgers(self, params):
        self.call_order.append("ledgers")
        return {
            "error": [], "result": {
                "ledger": {"L1": {
                    "refid": "R1", "type": "deposit", "asset": self._asset_code,
                    "amount": "0.0001", "fee": "0", "balance": "0.0001", "time": 1700000000.0,
                }},
                "count": 1,
            },
        }

    def fetch_balance(self):
        self.call_order.append("balance")
        return {"total": {self._balance_code: "0.0001"}}


def test_cli_live_mode_runs_a_real_cycle_against_a_mocked_exchange_with_zero_network_calls(tmp_path, monkeypatch):
    """Proves the full --live wiring (arg parsing -> build_exchange ->
    build_live_fetch_fn/build_read_wallet_balance_fn -> run_shadow_cycle)
    end to end, entirely offline: build_exchange() is the ONLY function
    that would ever touch the network, and it is replaced here with a
    fixture-backed fake — no real ccxt call, no real Kraken account, no
    live-data shadow run started by this test or by building this CLI."""
    monkeypatch.setenv("LEDGER_SHADOW_ENABLED", "true")
    import bot.accounting.kraken_ledger_fetch as kraken_module
    import scripts.ledger_shadow_run as cli_module

    fake_exchange = _FakeLiveKrakenExchange()
    monkeypatch.setattr(kraken_module, "build_exchange", lambda: fake_exchange)

    db_path = str(tmp_path / "obs.db")
    status_path = str(tmp_path / "status.json")
    exit_code = cli_module.main([
        "--live", "--asset", "BTC", "--zero-opening-confirmed",
        "--db", db_path, "--status", status_path,
    ])

    assert exit_code == 0
    assert fake_exchange.call_order == ["ledgers", "balance"]   # ledger fetched strictly before balance read
    published = json.loads(open(status_path).read())
    assert published["trusted"] is True
    assert published["fetch_succeeded"] is True


def test_cli_live_mode_without_zero_opening_confirmed_correctly_reports_untrusted(tmp_path, monkeypatch):
    """Without an explicitly asserted opening checkpoint, a real
    account's first cycle must NOT be trusted — there is no assumed
    anchor point for pre-existing history. This is the safe, honest
    default, not a bug."""
    monkeypatch.setenv("LEDGER_SHADOW_ENABLED", "true")
    import bot.accounting.kraken_ledger_fetch as kraken_module
    import scripts.ledger_shadow_run as cli_module

    fake_exchange = _FakeLiveKrakenExchange()
    monkeypatch.setattr(kraken_module, "build_exchange", lambda: fake_exchange)

    exit_code = cli_module.main([
        "--live", "--asset", "BTC",
        "--db", str(tmp_path / "obs.db"), "--status", str(tmp_path / "status.json"),
    ])

    assert exit_code == 1
    published = json.loads(open(str(tmp_path / "status.json")).read())
    assert published["trusted"] is False


def test_cli_live_mode_resolves_btc_xbt_and_xxbt_identically(tmp_path, monkeypatch):
    """Review finding: the adapter's alias lookup only matched a
    canonical dict KEY ("BTC"), so requesting the CLI's own default
    "XXBT" (a legitimate MEMBER of that same alias group, not a key)
    silently fell back to matching only literal "XXBT" — dropping an
    "XBT"-coded ledger entry and failing to find a "BTC"-keyed balance
    entirely. All three spellings, requested via --asset, must now
    resolve identically end to end through the CLI."""
    monkeypatch.setenv("LEDGER_SHADOW_ENABLED", "true")
    import bot.accounting.kraken_ledger_fetch as kraken_module
    import scripts.ledger_shadow_run as cli_module

    for requested_alias, entry_code, balance_code in [
        ("BTC", "XBT", "BTC"),
        ("XBT", "XXBT", "BTC"),
        ("XXBT", "XBT", "BTC"),   # the CLI's own default alias — the exact reproduction
    ]:
        fake_exchange = _FakeLiveKrakenExchange(asset_code=entry_code, balance_code=balance_code)
        monkeypatch.setattr(kraken_module, "build_exchange", lambda fx=fake_exchange: fx)

        db_path = str(tmp_path / f"obs_{requested_alias}.db")
        status_path = str(tmp_path / f"status_{requested_alias}.json")
        exit_code = cli_module.main([
            "--live", "--asset", requested_alias, "--zero-opening-confirmed",
            "--db", db_path, "--status", status_path,
        ])

        published = json.loads(open(status_path).read())
        assert exit_code == 0, f"--asset {requested_alias} failed: {published}"
        assert published["trusted"] is True, f"--asset {requested_alias} was not trusted: {published}"


def test_cli_default_paths_point_under_logs_shadow_never_production_trades_db_and_differ_by_mode():
    """Review finding: fixture and live modes used to share identical
    default db/status/account-id, so running the documented fixture
    example and then a live cycle persisted a synthetic deposit into
    what was meant to be real observation history. Defaults must now
    differ per mode."""
    import scripts.ledger_shadow_run as cli_module

    fixture_defaults = cli_module._MODE_DEFAULTS["fixture"]
    live_defaults = cli_module._MODE_DEFAULTS["live"]

    for defaults in (fixture_defaults, live_defaults):
        assert "shadow" in defaults["db"]
        assert defaults["db"].endswith("observations.db")
        assert "trades.db" not in defaults["db"]

    assert fixture_defaults["db"] != live_defaults["db"]
    assert fixture_defaults["status"] != live_defaults["status"]
    assert fixture_defaults["account_id"] != live_defaults["account_id"]
