"""
Shadow-only observation-cycle orchestrator for the isolated Decimal ledger
observer (bot/accounting/ledger_quantity_reconciliation.py).

NOT wired into bot/main.py, reconciliation.py, or four_way.py. Does not
read or write logs/HALT, logs/risk_state.json, or logs/trades.db. Has no
effect on any trading decision — this module cannot even express one; it
has no import relationship with any execution or risk code (see the
source guard in this module's own test file).

What this adds, on top of the already-tested reconciliation primitives
(walk_chain / reconcile / persist_batch / verify_batch_completeness /
batch_is_complete — all reused UNCHANGED, never reimplemented here):

1. A FRESH observation_id for every cycle (new_observation_id()) — never
   reused across cycles, so a retry after a failure is always a genuinely
   new, independently-verifiable attempt, and an old attempt's id can
   never be silently recycled to make a new attempt look like a
   continuation of one that already succeeded.

2. A single entry point, run_shadow_cycle(), that composes one full
   observation: fetch (via a caller-supplied, source-agnostic fetch_fn)
   -> persist (atomically, via persist_batch) -> reconcile -> publish a
   status record — using the EXACT trust binding validated by
   tests/crypto/test_ledger_quantity_reconciliation_integration.py:
   chain.overall_pass AND verify_batch_completeness().complete AND
   batch_is_complete() for THIS cycle's own observation_id specifically.
   A prior cycle's success can never substitute for the current one.

3. publish_status() ALWAYS overwrites the status file with the CURRENT
   cycle's own result — a failed fetch or an untrusted reconciliation is
   published exactly as-is, never skipped in favor of leaving an older
   success in place. Uses bot/atomic_json.atomic_write_json (the same
   tmp+rename primitive production state files already rely on) so a
   crash mid-publish can't leave a half-written status file.

4. Failure-lifecycle publishing (fixed after a review found the gap):
   an in-progress status is published for THIS observation_id BEFORE any
   database or fetch work begins. If that initial publish itself fails,
   the whole cycle aborts immediately (nothing is attempted that nobody
   could see evidence of). From there, a fetch failure OR any failure
   during persistence/reconciliation (e.g. a conflicting ledger entry
   raised by persist_batch) is caught and published as a terminal failed
   result for this SAME observation_id — an exception occurring anywhere
   after the fetch used to propagate uncaught, leaving whatever the
   PREVIOUS cycle's status happened to be sitting on disk looking exactly
   like a still-current, still-trusted result. If publishing that final
   result itself fails, the exception is allowed to propagate (never
   swallowed) — the initial in-progress status remains the last thing
   actually on disk, never a stale success from an unrelated earlier
   cycle.

5. is_shadow_enabled() — a plain, off-by-default env-var gate
   (LEDGER_SHADOW_ENABLED). Deliberately NOT checked inside
   run_shadow_cycle() itself, which stays a pure, directly-testable
   function; the gate belongs to whatever entry point actually invokes
   this (see scripts/ledger_shadow_run.py), mirroring how cfg.accounting
   .enabled gates bot/main.py's wiring without living inside the
   accounting primitives themselves.

6. evidence_mode — a REQUIRED string every caller must supply (e.g.
   "fixture" or "live"), stamped into the database on its first use and
   checked on every later call (EvidenceModeConflict). This is the fix
   for a real review finding: the CLI's fixture and live modes used to
   default to the SAME database/status/account-id, so running the
   documented fixture example and then a live cycle against the same
   path silently mixed a synthetic fixture deposit into what was
   supposed to be real observation history — reconciliation then failed
   for a genuinely correct real account purely because of leftover
   fixture data. Mode-specific CLI defaults reduce how often this
   happens by accident; this stamp is what makes it impossible even
   under an explicit --db override naming the same file for both modes.
   Every ShadowCycleResult (including the in-progress placeholder and
   every failure path) carries its own evidence_mode, so a published
   status is always self-labeling.

7. history_path (optional, additive) — the fix for another real review
   finding: publish_status()'s status_path, by design, only ever shows
   the LATEST outcome, so after several real observation cycles only the
   last one's full detail survived anywhere; an earlier untrusted result
   (e.g. "opening balance not verified") was silently gone once a later
   cycle published its own result. When given, every result this
   function ever produces — in-progress placeholders and terminal
   outcomes alike — is additionally appended (never overwritten) to
   history_path via append_observation_history(), so a complete record
   of every run survives regardless of what any later cycle reports.
   Omitted by default: existing callers are unaffected.

8. The zero_opening_confirmed conditional-labeling note (fixed after a
   review caught a real overclaim in a human-facing report, not in this
   code — the code's own trust check was always correctly gated on this
   flag). zero_opening_confirmed=True is a BARE ASSERTION from the
   caller — it carries no evidence string and is not independently
   checked for truth by anything in this module or in
   ledger_quantity_reconciliation.py. Chain self-consistency starting
   from an assumed zero (every transition's own arithmetic checking out)
   is real evidence of something narrower than "the account started at
   zero": it only shows that the OLDEST entry a given fetch retrieved
   implies a zero balance immediately before it. It does not, on its
   own, confirm that entry was the account's true first-ever activity in
   this asset, or rule out earlier history outside whatever window the
   ledger API actually returned (retention limits, an earlier funding
   path never exposed via this endpoint, etc.) — precisely the
   distinction opening_checkpoint's own required `evidence` field exists
   to force a caller to think through and write down, rather than
   silently wave through a bare flag. When zero_opening_confirmed is used
   without an opening_checkpoint, every terminal ShadowCycleResult now
   carries an explicit NOTE to this effect in its own `reason` field —
   including when the result is otherwise trusted=True — so this
   qualification survives into the published artifact itself, not just a
   verbal caveat easy to drop from a later summary.

Storage is fully caller-specified (db_path / status_path are required
arguments, never a hardcoded production path) — this module has no
opinion about where shadow state lives beyond "wherever the caller says,"
which is what "keep all storage isolated" actually requires: nothing here
can even accidentally resolve to logs/trades.db. run_shadow_cycle also
creates db_path's parent directory before opening it (mirroring what
atomic_write_json already does for status_path), closing a first-run gap
a prior review found: a nested, not-yet-existing shadow directory made
sqlite3.connect() raise before any of the above logic ever ran.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from os import environ

from bot.accounting.ledger_quantity_reconciliation import (
    LedgerEntry, batch_is_complete, init_db, load_ledger_entries, persist_batch, reconcile,
    verify_batch_completeness,
)
from bot.atomic_json import atomic_write_json


def _iso_now(now: "datetime | None" = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_observation_id(*, now: "datetime | None" = None, prefix: str = "shadow") -> str:
    """A fresh id for one observation cycle. Timestamp component is purely
    for human readability/sortability in a status/log listing; the actual
    freshness guarantee comes from the uuid4 component, so two calls in
    the same clock tick (or with a fixed injected `now` in a test) still
    never collide."""
    ts = _iso_now(now)
    return f"{prefix}-{ts}-{uuid.uuid4().hex[:12]}"


@dataclass
class ShadowCycleResult:
    observation_id: str
    evidence_mode: str                    # e.g. "fixture" or "live" — every published status self-labels
    started_at: str
    completed_at: "str | None"           # None while in_progress — no terminal outcome exists yet
    in_progress: bool                     # True ONLY for the placeholder published before any work begins
    fetch_succeeded: "bool | None"        # None while in_progress (not yet attempted)
    trusted: bool                         # always False while in_progress or on any failure
    chain_pass: "bool | None"             # None if the fetch itself failed — no chain was ever walked
    wallet_agrees: "bool | None"
    completeness_complete: "bool | None"
    this_observation_complete: bool       # False whenever there is no completed manifest for THIS
                                            # observation_id — in_progress, a fetch failure, or a
                                            # persistence/reconciliation failure all mean this is False
    reason: str
    error: "str | None"


class EvidenceModeConflict(RuntimeError):
    """Raised when a shadow database's already-recorded evidence_mode
    (stamped on its first use) does not match the mode of the CURRENT
    invocation. Mixing synthetic fixture data with real live observation
    history in the same database is never allowed, even under an
    explicit path override that happens to name the same file for both —
    this is a database-level property, not something CLI defaults alone
    can guarantee."""


def publish_status(status_path: str, result: ShadowCycleResult) -> None:
    """Always overwrites status_path with THIS cycle's own result — a
    failed, in-progress, or untrusted cycle is published exactly as it
    is, never skipped in favor of an older success already on disk. Uses
    bot/atomic_json.atomic_write_json's tmp+rename primitive, which has a
    useful additional property this module leans on directly: if THIS
    call raises (disk full, permission error, whatever), the tmp file
    write or the final os.replace() never completes, so the PREVIOUS
    contents of status_path are left untouched — a failed publish can
    never itself corrupt or half-write the file, it can only fail to
    update it, which is exactly the "leave the in-progress status" fallback
    run_shadow_cycle relies on when a final publish itself fails."""
    atomic_write_json(status_path, asdict(result))


def append_observation_history(history_path: "str | None", result: ShadowCycleResult) -> None:
    """Appends ONE line of this result's full JSON to history_path (JSONL,
    append-only). This exists because publish_status()'s status_path is
    deliberately the OPPOSITE kind of record — it always reflects only
    the CURRENT/latest outcome, by design, so a failure can never be
    masked by an older success sitting on disk. That is the right
    property for "what should I trust right now," but it means every
    earlier result is destroyed the moment a newer one is published — a
    real reviewed gap: after several real observation cycles, only the
    last one's full detail survived anywhere. history_path is the
    separate, additive record: every result ever produced (in-progress
    placeholders and terminal outcomes alike, trusted or not) is kept,
    in order, so a full account of every run remains inspectable
    regardless of what any later cycle reports.

    A no-op when history_path is None (the default) — this is additive
    and opt-in, not a change to run_shadow_cycle's core trust logic.
    Uses a plain append, not atomic_write_json's tmp+replace (which is
    for whole-file REPLACEMENT and would not fit an append-only log): a
    torn last line from a crash mid-append is the one accepted risk
    here, exactly like any ordinary JSONL audit log, and never affects
    any earlier line's integrity."""
    if history_path is None:
        return
    dirpath = os.path.dirname(os.path.abspath(history_path))
    os.makedirs(dirpath, exist_ok=True)
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(result)) + "\n")


def read_observation_history(history_path: str) -> "list[dict]":
    """Reads back every record append_observation_history has ever
    written, in order, as plain dicts (not reconstructed ShadowCycleResult
    objects — this is a read-only inspection helper, not something
    run_shadow_cycle itself consumes). Returns an empty list if the file
    has never been created."""
    if not os.path.exists(history_path):
        return []
    records = []
    with open(history_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def is_shadow_enabled() -> bool:
    """Off by default. Deliberately not consulted inside run_shadow_cycle
    itself — this is the entry point's own responsibility, so the cycle
    logic stays a plain, directly-testable function with no environment
    coupling."""
    return environ.get("LEDGER_SHADOW_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _in_progress_result(observation_id: str, evidence_mode: str, started_at: str) -> ShadowCycleResult:
    return ShadowCycleResult(
        observation_id=observation_id, evidence_mode=evidence_mode, started_at=started_at, completed_at=None,
        in_progress=True, fetch_succeeded=None, trusted=False, chain_pass=None,
        wallet_agrees=None, completeness_complete=None, this_observation_complete=False,
        reason=f"observation {observation_id!r} is in progress", error=None,
    )


def _failed_result(
    observation_id: str, evidence_mode: str, started_at: str, completed_at: str, *,
    fetch_succeeded: bool, reason: str, error: str,
) -> ShadowCycleResult:
    return ShadowCycleResult(
        observation_id=observation_id, evidence_mode=evidence_mode, started_at=started_at, completed_at=completed_at,
        in_progress=False, fetch_succeeded=fetch_succeeded, trusted=False, chain_pass=None,
        wallet_agrees=None, completeness_complete=None, this_observation_complete=False,
        reason=reason, error=error,
    )


def _ensure_evidence_mode(conn: sqlite3.Connection, evidence_mode: str) -> None:
    """Stamps a shadow database with its evidence_mode on first use, or
    verifies an existing stamp matches. A shadow-orchestrator-specific
    concern deliberately kept OUT of ledger_quantity_reconciliation.py's
    own init_db() — that module is the general-purpose reconciliation
    primitive (meant to also serve a future non-shadow, real production
    integration per its own docstring), and has no reason to know
    "fixture" from "live"; this table belongs entirely to this
    orchestrator layer."""
    conn.execute("CREATE TABLE IF NOT EXISTS shadow_evidence_mode (evidence_mode TEXT NOT NULL)")
    row = conn.execute("SELECT evidence_mode FROM shadow_evidence_mode LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO shadow_evidence_mode (evidence_mode) VALUES (?)", (evidence_mode,))
        conn.commit()
        return
    existing_mode = row[0]
    if existing_mode != evidence_mode:
        raise EvidenceModeConflict(
            f"this database was already stamped evidence_mode={existing_mode!r} on an earlier run; "
            f"refusing to run a {evidence_mode!r} cycle against it — use a separate database per mode"
        )


def run_shadow_cycle(
    *,
    fetch_fn,
    account_id: str,
    asset: str,
    db_path: str,
    status_path: str,
    evidence_mode: str,
    wallet_balance_at_read: "Decimal | None" = None,
    wallet_balance_read_at: "str | None" = None,
    read_wallet_balance_fn=None,
    opening_checkpoint=None,
    zero_opening_confirmed: bool = False,
    history_path: "str | None" = None,
    now: "datetime | None" = None,
) -> ShadowCycleResult:
    """Runs exactly one observation cycle end to end and publishes its
    result at every stage. `fetch_fn(*, account_id, batch_id,
    observed_at) -> list[LedgerEntry]` is supplied by the caller and is
    completely source-agnostic — this function never constructs an
    exchange connection, makes a network call, or knows whether its data
    came from a fixture or a live account.

    Publishing sequence (the fix for a review finding: only fetch
    failures used to be caught, so a persistence or reconciliation
    failure propagated uncaught and left the PREVIOUS cycle's success
    sitting on disk looking current):
      1. Publish an in-progress placeholder for this observation_id
         BEFORE any database or fetch work. If this itself raises, the
         whole cycle aborts immediately (propagates) — no work is
         attempted that nobody could see evidence of.
      2. A fetch_fn failure is caught and published as a terminal result
         with fetch_succeeded=False.
      3. ANY failure during persist_batch / reconcile /
         verify_batch_completeness / batch_is_complete (a genuinely
         distinct case from a fetch failure — data WAS fetched, e.g. a
         conflicting ledger entry was the problem) is caught and
         published as a terminal result with fetch_succeeded=True,
         trusted=False.
      4. In every case, if the publish call for a terminal result itself
         raises, that exception propagates rather than being swallowed —
         the in-progress status from step 1 is what remains on disk, not
         a stale unrelated success.

    `read_wallet_balance_fn`, if given, is called INSIDE the same
    persist/reconcile try block, AFTER persist_batch succeeds and BEFORE
    reconcile() runs — never before the fetch, never concurrently with
    it. This ordering is deliberate: it guarantees the persisted chain
    never claims to already account for activity a balance read hadn't
    itself observed yet (see bot/accounting/kraken_ledger_fetch.py's
    module docstring for the full reasoning behind reading the ledger
    before the balance, not the other way around). Its return value,
    `(balance, read_at)`, overrides `wallet_balance_at_read` /
    `wallet_balance_read_at` for this cycle only. A failure inside it is
    handled with the exact same "cycle failed after a successful fetch"
    path as a persist_batch or reconcile() failure — including never
    being invoked at all if the fetch itself already failed, avoiding a
    wasted balance read when there is nothing to reconcile against.
    When `read_wallet_balance_fn` is omitted (every existing caller),
    behavior is unchanged: the fixed `wallet_balance_at_read` /
    `wallet_balance_read_at` values passed in are used as before.

    Trust, when a cycle actually completes, is computed with the exact
    binding validated by tests/crypto/
    test_ledger_quantity_reconciliation_integration.py: chain.overall_pass
    AND verify_batch_completeness().complete AND batch_is_complete() bound
    to this cycle's own, freshly-generated observation_id — an older
    successful batch can never substitute for the current attempt."""
    started_at = _iso_now(now)
    observation_id = new_observation_id(now=now)

    def _publish(result: ShadowCycleResult) -> None:
        # Both calls record the SAME result; publish_status's status_path
        # always reflects only this (the latest) outcome, while
        # history_path (opt-in, additive) accumulates every one ever
        # produced — see append_observation_history's own docstring for
        # why these are deliberately two different kinds of record.
        # Order matters: status_path first, matching this function's own
        # documented "if a publish raises, propagate — see docstring"
        # contract at every call site below; history_path is best-effort
        # additional fidelity, not the primary trust signal.
        publish_status(status_path, result)
        append_observation_history(history_path, result)

    # Published before any database or fetch work. If this raises, abort
    # entirely — see the docstring above.
    _publish(_in_progress_result(observation_id, evidence_mode, started_at))

    conn = None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        conn = sqlite3.connect(db_path)
        init_db(conn)

        try:
            _ensure_evidence_mode(conn, evidence_mode)
        except EvidenceModeConflict as exc:
            result = _failed_result(
                observation_id, evidence_mode, started_at, _iso_now(now), fetch_succeeded=False,
                reason=f"evidence mode conflict for observation {observation_id!r}: {exc}", error=str(exc),
            )
            _publish(result)   # if this raises, propagate — see docstring
            return result

        try:
            entries: "list[LedgerEntry]" = fetch_fn(
                account_id=account_id, batch_id=observation_id, observed_at=started_at,
            )
        except Exception as exc:
            result = _failed_result(
                observation_id, evidence_mode, started_at, _iso_now(now), fetch_succeeded=False,
                reason=f"fetch failed for observation {observation_id!r}: {exc}", error=str(exc),
            )
            _publish(result)   # if this raises, propagate — see docstring
            return result

        try:
            persist_batch(
                conn, account_id=account_id, asset=asset, batch_id=observation_id,
                entries=entries, committed_at=_iso_now(now),
            )
            loaded = load_ledger_entries(conn, account_id, asset)
            effective_balance, effective_read_at = wallet_balance_at_read, wallet_balance_read_at
            if read_wallet_balance_fn is not None:
                effective_balance, effective_read_at = read_wallet_balance_fn()
            chain_result = reconcile(
                loaded, opening_checkpoint=opening_checkpoint, zero_opening_confirmed=zero_opening_confirmed,
                wallet_balance_at_read=effective_balance, wallet_balance_read_at=effective_read_at,
            )
            completeness = verify_batch_completeness(conn, account_id, asset)
            this_observation_complete = batch_is_complete(conn, account_id, asset, observation_id)
        except Exception as exc:
            result = _failed_result(
                observation_id, evidence_mode, started_at, _iso_now(now), fetch_succeeded=True,
                reason=f"cycle failed after a successful fetch for observation {observation_id!r}: {exc}",
                error=str(exc),
            )
            _publish(result)   # if this raises, propagate — see docstring
            return result

        trusted = chain_result.overall_pass and completeness.complete and this_observation_complete
        reason_parts = [chain_result.reason]
        if not completeness.complete:
            reason_parts.append(f"completeness: {completeness.reason}")
        if not this_observation_complete:
            reason_parts.append(f"observation {observation_id!r} has no completed manifest")
        if zero_opening_confirmed and opening_checkpoint is None:
            # Review finding: a bare zero_opening_confirmed=True flag was
            # being treated (in at least one prior report to a human) as
            # if it were independent confirmation of the account's true
            # opening balance. It is not — it is only the caller's own
            # assertion. All it can honestly mean, EVEN WHEN the chain
            # below is fully self-consistent, is that the OLDEST entry
            # this specific fetch retrieved implies a zero balance
            # immediately before it; that does not independently confirm
            # this was the account's actual first-ever activity in this
            # asset, or rule out earlier offsetting history outside the
            # fetched/retained ledger window. Labeled explicitly here,
            # inside the trusted result itself, rather than left as a
            # verbal caveat that's easy to drop when the result is later
            # summarized. A caller with genuine independent evidence
            # should use opening_checkpoint (which requires a written
            # evidence string) instead of this bare flag.
            reason_parts.append(
                "NOTE: opening balance was ASSERTED zero via the bare zero_opening_confirmed flag, "
                "not independently verified — see this note's own explanation in the module docstring"
            )
        result = ShadowCycleResult(
            observation_id=observation_id, evidence_mode=evidence_mode, started_at=started_at,
            completed_at=_iso_now(now), in_progress=False, fetch_succeeded=True, trusted=trusted,
            chain_pass=chain_result.overall_pass, wallet_agrees=chain_result.wallet_balance_agrees_at_read,
            completeness_complete=completeness.complete, this_observation_complete=this_observation_complete,
            reason="; ".join(reason_parts), error=None,
        )
        _publish(result)
        return result
    finally:
        if conn is not None:
            conn.close()
