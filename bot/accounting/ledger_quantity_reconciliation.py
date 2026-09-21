"""
Isolated Decimal ledger observer — offline prototype, NOT wired into bot/main.py.

Implements the "authoritative quantity reconciliation" component described in
CRYPTO_BOT_LEDGER_MOVEMENT_INTEGRATION_PROPOSAL_2026-09-21.md (v4), refined
once more per its own review: every ledger entry (trade, deposit, withdrawal,
reward — all of them, not a subset) is walked in Decimal, order is resolved
by balance-transition arithmetic rather than any assumed sort, an opening
balance must be explicitly verified (zero or nonzero) before an overall PASS
is possible, and "the wallet balance agrees at a fresh read" is labeled and
scoped for exactly what it proves — no more, no less.

Not wired into any production path. Does not alter production data. Does not
influence any trading decision. `logs/HALT` is untouched by this module's
existence — it has no import relationship with bot/main.py, reconciliation.py,
or four_way.py (see the source guard in this module's own test file).

── Three refinements over the v4 proposal, incorporated directly here ──────

1. Equal-timestamp ordering by balance-transition arithmetic, not ledger_id.
   Two entries sharing an `exchange_timestamp` cannot be safely ordered by
   sorting on their own id — that's reproducible, but reproducible is not the
   same as correct, and a wrong-but-deterministic order could report a real
   chain as broken (or a broken chain as fine) purely as an artifact of sort
   order. Instead: every permutation of a tied group is tried against the
   group's own recorded `balance_raw` values, using the running balance from
   before the group. A permutation "works" only if every entry in it
   satisfies `running + amount - fee == balance_raw` in that exact sequence.
   - Exactly one working permutation: that is the resolved order — not
     guessed, derived from the ledger's own arithmetic evidence.
   - Zero working permutations: `chain_consistent=False` for that segment —
     a genuine data problem, not an ordering artifact.
   - More than one working permutation: genuinely ambiguous from the
     evidence available — reported as `ambiguous_tie_groups`, which blocks
     `overall_pass` the same way an unresolved coverage gap does, but is
     never conflated with `chain_consistent=False`. An ambiguous tie is not
     proof of corruption; it is proof this evidence alone cannot determine
     the true order.

2. Opening balance verification is a hard PASS prerequisite, and a verified
   NONZERO checkpoint is supported, not just zero. `opening_balance_verified`
   must be `True` — either because the caller supplied an externally
   verified `opening_checkpoint` (a balance amount plus a description of how
   it was established, for a chain that does not start at zero), or because
   the caller explicitly attests `zero_opening_confirmed=True` (this really
   is the account's first-ever activity for this asset, confirmed some way
   outside this function — never inferred merely because the earliest
   fetched entry's own arithmetic happens to imply a zero start). Omitting
   both means `opening_balance_verified=False`, and `overall_pass` can never
   be `True` regardless of how clean everything else is — this is not a
   footnote alongside an otherwise-passing result, it gates the result.

3. `wallet_balance_agrees_at_read` (renamed from an earlier draft's
   "current_as_of_now") is scoped honestly: it proves the wallet's balance,
   read once, matches what the chain predicts AT THAT SAMPLED INSTANT. It
   does NOT prove every event up to that instant has been observed — a
   deposit and a withdrawal of the same size, both unseen, would leave the
   sampled balance unchanged while two real events went unrecorded. This
   module never states or implies completeness from this check alone;
   `coverage_note` is always populated, in every result, describing exactly
   this limitation, so a consumer of `LedgerReconciliationResult` cannot
   read `wallet_balance_agrees_at_read=True` as "nothing was missed."
"""
from __future__ import annotations

import itertools
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

_MAX_TIE_GROUP_SIZE_FOR_PERMUTATION_SEARCH = 6   # 6! = 720, still fast; beyond this, refuse rather than hang

_COVERAGE_NOTE = (
    "wallet_balance_agrees_at_read proves the wallet's balance matched the chain's own "
    "prediction at the moment it was sampled — it does NOT prove every event up to that "
    "moment was observed. Two offsetting unseen movements (e.g. a deposit and a withdrawal "
    "of the same size) would leave the sampled balance unchanged while real events went "
    "unrecorded. This flag alone is never sufficient evidence of completeness."
)


@dataclass(frozen=True)
class LedgerEntry:
    ledger_id: str
    reference_id: str
    account_id: str
    type: str            # 'deposit' | 'withdrawal' | 'reward' | 'trade'
    asset: str
    amount_raw: str       # Decimal string, signed
    fee_raw: str           # Decimal string, always >= 0
    balance_raw: str       # Decimal string — Kraken's own running balance after this entry
    exchange_timestamp: str
    observed_at: str

    @property
    def amount(self) -> Decimal:
        return Decimal(self.amount_raw)

    @property
    def fee(self) -> Decimal:
        return Decimal(self.fee_raw)

    @property
    def balance(self) -> Decimal:
        return Decimal(self.balance_raw)


@dataclass
class OpeningCheckpoint:
    balance: Decimal
    evidence: str   # human-readable description of how this was established — required, never blank


@dataclass
class ChainWalkResult:
    consistent: bool                          # True only if every transition (incl. resolved ties) checks out
    ambiguous_tie_groups: "list[list[str]]" = field(default_factory=list)   # groups of ledger_ids, >1 valid order FOUND
    unsearched_tie_groups: "list[list[str]]" = field(default_factory=list)  # groups too large to search AT ALL —
                                                                            # distinct from ambiguous: no search was
                                                                            # ever attempted, order is simply unknown
    failing_ledger_ids: "list[str]" = field(default_factory=list)          # entries with NO valid reconciling order
    opening_balance_verified: bool = False
    opening_balance: "Decimal | None" = None
    final_balance: "Decimal | None" = None    # None if the walk stopped early on an unsearched tie group
    ordered_ledger_ids: "list[str]" = field(default_factory=list)
    reason: str = ""


@dataclass
class LedgerReconciliationResult:
    chain: ChainWalkResult
    wallet_balance_agrees_at_read: "bool | None"     # None if no fresh balance was supplied to check
    wallet_balance_read_at: "str | None"
    coverage_note: str
    overall_pass: bool
    reason: str


def _require_decimal(label: str, raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except (InvalidOperation, TypeError):
        raise ValueError(f"{label} is not a valid decimal string: {raw!r}")


def _parse_timestamp(label: str, raw: str) -> datetime:
    """Timezone-aware instant, never a raw-string comparison. Two ISO-8601
    UTC timestamps that differ only in whether they carry fractional
    seconds — e.g. "2026-01-01T00:00:00.1Z" (100ms past the second) vs
    "2026-01-01T00:00:00Z" (exactly on the second) — sort BACKWARDS as
    plain strings ('.' sorts before 'Z'), even though both are already UTC
    with no offset ambiguity at all. This is a distinct failure mode from
    the timezone-offset bug already fixed in asset_movement_analysis.py's
    own _parse_iso, and is fixed here the same way: parse first, sort by
    the parsed instant, never by the raw text."""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{label} is not a parseable ISO-8601 timestamp, got {raw!r}: {exc}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _require_finite(label: str, value: Decimal) -> None:
    if not value.is_finite():
        raise ValueError(f"{label} must be a finite decimal, got {value}")


def _validate_entry(e: LedgerEntry) -> None:
    _parse_timestamp(f"entry {e.ledger_id!r} exchange_timestamp", e.exchange_timestamp)
    amount = _require_decimal(f"entry {e.ledger_id!r} amount_raw", e.amount_raw)
    fee = _require_decimal(f"entry {e.ledger_id!r} fee_raw", e.fee_raw)
    balance = _require_decimal(f"entry {e.ledger_id!r} balance_raw", e.balance_raw)
    _require_finite(f"entry {e.ledger_id!r} amount", amount)
    _require_finite(f"entry {e.ledger_id!r} fee", fee)
    _require_finite(f"entry {e.ledger_id!r} balance", balance)
    if fee < 0:
        raise ValueError(f"entry {e.ledger_id!r} fee must be >= 0, got {fee}")


def _validate_single_scope(entries: "list[LedgerEntry]") -> None:
    account_ids = {e.account_id for e in entries}
    assets = {e.asset for e in entries}
    if len(account_ids) > 1:
        raise ValueError(
            f"entries span more than one account: {sorted(account_ids)} — a single call must "
            f"cover exactly one account_id"
        )
    if len(assets) > 1:
        raise ValueError(
            f"entries span more than one asset: {sorted(assets)} — a single call must cover "
            f"exactly one asset"
        )


def _validate_opening_checkpoint(checkpoint: "OpeningCheckpoint | None") -> None:
    if checkpoint is None:
        return
    _require_finite("opening checkpoint balance", checkpoint.balance)
    if not checkpoint.evidence or not checkpoint.evidence.strip():
        raise ValueError(
            "OpeningCheckpoint.evidence must be a non-blank description of how this balance "
            "was independently verified — a blank string is not evidence"
        )


def _exchange_payload(e: LedgerEntry) -> tuple:
    """The fields that actually describe what the exchange reported.
    Deliberately excludes `observed_at` — purely local bookkeeping (when
    OUR system happened to fetch this row), never part of the exchange's
    own event data. Two LedgerEntry objects for the same real event
    re-observed at different times must compare equal here even though the
    dataclass's own default equality (which includes observed_at) would
    say they differ — this is the ONE canonical definition of "the same
    observation", shared by every place that needs to tell a legitimate
    re-observation apart from a genuine conflict."""
    return (e.reference_id, e.type, e.asset, e.amount_raw, e.fee_raw, e.balance_raw,
            e.exchange_timestamp)


def _dedup_entries(entries: "list[LedgerEntry]") -> "list[LedgerEntry]":
    """Collapses exact re-observations (same (account_id, ledger_id), same
    _exchange_payload) to one entry. A DIFFERENT exchange payload for an
    already-seen key is a genuine conflict, raised immediately — never
    silently picked between. Used both to dedup a caller's raw input list
    before it is ever walked (an initial [A, A] must not double-apply A's
    delta) and to merge newly-fetched retry evidence with what was already
    on hand."""
    by_key: "dict[tuple[str, str], LedgerEntry]" = {}
    for e in entries:
        key = (e.account_id, e.ledger_id)
        if key in by_key:
            if _exchange_payload(by_key[key]) != _exchange_payload(e):
                raise ValueError(
                    f"conflicting ledger entry for {key!r}: "
                    f"{by_key[key]!r} vs {e!r} — not a duplicate, a genuine data conflict"
                )
            continue
        by_key[key] = e
    return list(by_key.values())


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ledger_entries (
            ledger_id TEXT NOT NULL,
            reference_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            type TEXT NOT NULL,
            asset TEXT NOT NULL,
            amount_raw TEXT NOT NULL,
            fee_raw TEXT NOT NULL,
            balance_raw TEXT NOT NULL,
            exchange_timestamp TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (account_id, ledger_id)
        )
    """)
    conn.commit()


def upsert_ledger_entry(conn: sqlite3.Connection, entry: LedgerEntry) -> bool:
    """Idempotent upsert keyed on (account_id, ledger_id). An identical
    re-observation is a silent no-op (returns False). A DIFFERENT payload
    for the same key raises — never silently overwritten."""
    row = conn.execute(
        "SELECT ledger_id, reference_id, account_id, type, asset, amount_raw, fee_raw, "
        "balance_raw, exchange_timestamp, observed_at FROM ledger_entries "
        "WHERE account_id = ? AND ledger_id = ?",
        (entry.account_id, entry.ledger_id),
    ).fetchone()
    if row is not None:
        existing_entry = LedgerEntry(*row)
        if _exchange_payload(existing_entry) != _exchange_payload(entry):
            raise ValueError(
                f"conflicting ledger entry for (account_id={entry.account_id!r}, "
                f"ledger_id={entry.ledger_id!r}): existing={_exchange_payload(existing_entry)!r} "
                f"vs incoming={_exchange_payload(entry)!r} — not a duplicate, a genuine data "
                f"conflict that must be resolved by the caller"
            )
        return False
    conn.execute(
        "INSERT INTO ledger_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
        (entry.ledger_id, entry.reference_id, entry.account_id, entry.type, entry.asset,
         entry.amount_raw, entry.fee_raw, entry.balance_raw, entry.exchange_timestamp,
         entry.observed_at),
    )
    conn.commit()
    return True


def load_ledger_entries(conn: sqlite3.Connection, account_id: str, asset: str) -> "list[LedgerEntry]":
    rows = conn.execute(
        "SELECT ledger_id, reference_id, account_id, type, asset, amount_raw, fee_raw, "
        "balance_raw, exchange_timestamp, observed_at FROM ledger_entries "
        "WHERE account_id = ? AND asset = ?",
        (account_id, asset),
    ).fetchall()
    return [LedgerEntry(*row) for row in rows]


def _group_by_timestamp(entries: "list[LedgerEntry]") -> "list[list[LedgerEntry]]":
    """Groups by the PARSED UTC instant, never the raw string — see
    _parse_timestamp's own docstring for why a raw-string sort silently
    mis-orders even two already-UTC timestamps that merely differ in
    fractional-second formatting."""
    decorated = [(_parse_timestamp(f"entry {e.ledger_id!r} exchange_timestamp", e.exchange_timestamp), e)
                 for e in entries]
    decorated.sort(key=lambda pair: pair[0])
    groups: "list[list[LedgerEntry]]" = []
    last_instant = None
    for instant, e in decorated:
        if groups and last_instant == instant:
            groups[-1].append(e)
        else:
            groups.append([e])
        last_instant = instant
    return groups


def _max_timestamp_entry(entries: "list[LedgerEntry]") -> LedgerEntry:
    """Same parsed-instant discipline as _group_by_timestamp — the retry
    cursor must never be chosen by comparing raw ISO strings."""
    return max(
        entries,
        key=lambda e: _parse_timestamp(f"entry {e.ledger_id!r} exchange_timestamp", e.exchange_timestamp),
    )


def walk_chain(
    entries: "list[LedgerEntry]",
    *,
    opening_checkpoint: "OpeningCheckpoint | None" = None,
    zero_opening_confirmed: bool = False,
) -> ChainWalkResult:
    """Walks every ledger entry for one (account_id, asset) pair, in
    chronological order, resolving same-timestamp ties by balance-transition
    arithmetic (never by sorting on ledger_id or any other incidental key).
    Every transition is checked individually — a chain with two offsetting
    errors that happen to cancel by the last entry still fails here, it does
    not silently pass because the final total matches."""
    _validate_opening_checkpoint(opening_checkpoint)   # run even when entries is empty
    for e in entries:
        _validate_entry(e)
    entries = _dedup_entries(entries)   # an initial [A, A] must not double-apply A's own delta
    _validate_single_scope(entries)

    if not entries:
        return ChainWalkResult(
            consistent=True, opening_balance_verified=(opening_checkpoint is not None or zero_opening_confirmed),
            opening_balance=(opening_checkpoint.balance if opening_checkpoint else Decimal("0")),
            final_balance=(opening_checkpoint.balance if opening_checkpoint else Decimal("0")),
            reason="no entries to reconcile",
        )

    if opening_checkpoint is not None:
        running = opening_checkpoint.balance
        opening_verified = True
        opening_balance = opening_checkpoint.balance
    else:
        opening_balance = Decimal("0")
        running = Decimal("0")
        opening_verified = zero_opening_confirmed

    ambiguous_tie_groups: "list[list[str]]" = []
    unsearched_tie_groups: "list[list[str]]" = []
    failing_ledger_ids: "list[str]" = []
    ordered_ledger_ids: "list[str]" = []
    consistent = True
    stopped_early = False

    for group in _group_by_timestamp(entries):
        if len(group) == 1:
            e = group[0]
            expected = running + e.amount - e.fee
            if expected != e.balance:
                consistent = False
                failing_ledger_ids.append(e.ledger_id)
                # Trust the exchange's own reported balance going forward rather than
                # letting one bad row cascade a false mismatch through everything after it.
                running = e.balance
            else:
                running = e.balance
            ordered_ledger_ids.append(e.ledger_id)
            continue

        if len(group) > _MAX_TIE_GROUP_SIZE_FOR_PERMUTATION_SEARCH:
            # A full permutation search was never attempted (too large to search
            # safely) — this group's true order, and therefore the running balance
            # coming out of it, is genuinely UNKNOWN, not "ambiguous" in the sense
            # the branch below means (which only applies after an exhaustive search
            # found >1 valid order). An earlier version of this function picked
            # group[-1]'s own claimed balance as a guess and kept walking — every
            # transition checked AFTER that guess was being validated against a
            # number this function had no basis for, which produced exactly the
            # false "downstream entry is broken" report this fix exists to remove.
            # Correct behavior: stop here. Do not guess. Do not validate anything
            # after this point against an unresolved balance.
            unsearched_tie_groups.append([e.ledger_id for e in group])
            stopped_early = True
            break

        working_perms: "list[tuple[LedgerEntry, ...]]" = []
        for perm in itertools.permutations(group):
            r = running
            ok = True
            for e in perm:
                r = r + e.amount - e.fee
                if r != e.balance:
                    ok = False
                    break
            if ok:
                working_perms.append(perm)

        if len(working_perms) == 0:
            consistent = False
            failing_ledger_ids.extend(e.ledger_id for e in group)
            running = group[-1].balance
            ordered_ledger_ids.extend(e.ledger_id for e in group)
        elif len(working_perms) == 1:
            resolved = working_perms[0]
            running = resolved[-1].balance
            ordered_ledger_ids.extend(e.ledger_id for e in resolved)
        else:
            # More than one order is arithmetically valid — genuinely ambiguous,
            # not a corruption finding. Advance running balance using the first
            # working permutation's own final balance (all working permutations
            # necessarily agree on this since they all satisfy the same group's
            # own recorded balances) and flag the ambiguity explicitly.
            ambiguous_tie_groups.append([e.ledger_id for e in group])
            running = working_perms[0][-1].balance
            ordered_ledger_ids.extend(e.ledger_id for e in working_perms[0])

    reason_parts = []
    if stopped_early:
        reason_parts.append(
            f"ordering unresolved: search limit exceeded ({len(unsearched_tie_groups[-1])} entries tied "
            f"at one instant, exceeding the {_MAX_TIE_GROUP_SIZE_FOR_PERMUTATION_SEARCH}-entry search "
            f"limit) — stopped without validating any transition after this point against a guessed balance"
        )
    if failing_ledger_ids:
        reason_parts.append(f"{len(failing_ledger_ids)} entries have no reconciling transition")
    if ambiguous_tie_groups:
        reason_parts.append(f"{len(ambiguous_tie_groups)} tie group(s) have more than one valid order")
    if not opening_verified:
        reason_parts.append("opening balance not verified")
    reason = "; ".join(reason_parts) if reason_parts else "every transition reconciles"

    return ChainWalkResult(
        consistent=consistent, ambiguous_tie_groups=ambiguous_tie_groups,
        unsearched_tie_groups=unsearched_tie_groups,
        failing_ledger_ids=failing_ledger_ids, opening_balance_verified=opening_verified,
        opening_balance=opening_balance, final_balance=(None if stopped_early else running),
        ordered_ledger_ids=ordered_ledger_ids, reason=reason,
    )


def reconcile(
    entries: "list[LedgerEntry]",
    *,
    opening_checkpoint: "OpeningCheckpoint | None" = None,
    zero_opening_confirmed: bool = False,
    wallet_balance_at_read: "Decimal | None" = None,
    wallet_balance_read_at: "str | None" = None,
    fetch_more_since: "Callable[[str], list[LedgerEntry]] | None" = None,
    max_retries: int = 3,
) -> LedgerReconciliationResult:
    """The full check. `fetch_more_since(last_exchange_timestamp) -> list[LedgerEntry]`
    is an injectable fetcher (real callers pass a live-API-backed function;
    tests pass a fixture-backed fake) used only for the bounded retry when
    `wallet_balance_at_read` disagrees with the chain's own final balance —
    never used to silently paper over a chain-consistency failure."""
    if wallet_balance_at_read is not None:
        _require_finite("wallet_balance_at_read", wallet_balance_at_read)

    working_entries = list(entries)
    chain = walk_chain(working_entries, opening_checkpoint=opening_checkpoint,
                       zero_opening_confirmed=zero_opening_confirmed)

    wallet_agrees: "bool | None" = None
    if wallet_balance_at_read is not None:
        attempts = 0
        while True:
            if chain.final_balance == wallet_balance_at_read:
                wallet_agrees = True
                break
            if fetch_more_since is None or attempts >= max_retries:
                wallet_agrees = False if fetch_more_since is None else None  # None => inconclusive
                break
            last_ts = _max_timestamp_entry(working_entries).exchange_timestamp if working_entries else ""
            new_entries = fetch_more_since(last_ts)
            attempts += 1
            if not new_entries:
                wallet_agrees = None   # exhausted retries with no new evidence — inconclusive
                if attempts >= max_retries:
                    break
                continue
            working_entries = _dedup_entries(working_entries + new_entries)
            chain = walk_chain(working_entries, opening_checkpoint=opening_checkpoint,
                               zero_opening_confirmed=zero_opening_confirmed)

    overall_pass = (
        chain.consistent
        and not chain.ambiguous_tie_groups
        and not chain.unsearched_tie_groups
        and chain.opening_balance_verified
        and wallet_agrees is True
    )

    reason_parts = [chain.reason]
    if wallet_agrees is None and wallet_balance_at_read is not None:
        reason_parts.append("wallet balance agreement inconclusive after bounded retries")
    elif wallet_agrees is False:
        reason_parts.append("wallet balance does not agree with the chain's final balance")
    elif wallet_balance_at_read is None:
        reason_parts.append("no wallet balance supplied to check agreement")

    return LedgerReconciliationResult(
        chain=chain, wallet_balance_agrees_at_read=wallet_agrees,
        wallet_balance_read_at=wallet_balance_read_at, coverage_note=_COVERAGE_NOTE,
        overall_pass=overall_pass, reason="; ".join(reason_parts),
    )
