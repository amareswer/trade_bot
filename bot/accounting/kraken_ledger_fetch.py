"""
Read-only Kraken ledger + balance adapter for the shadow observation-cycle
orchestrator (bot/accounting/ledger_shadow_run.py). Implements the
previously-deferred --live fetch path (scripts/ledger_shadow_run.py),
which stays behind the SAME LEDGER_SHADOW_ENABLED gate checked by the
CLI — this module and run_shadow_cycle() itself never consult that flag.

Every function here is READ-ONLY: privatePostLedgers and fetch_balance
are both query endpoints, never order placement. No import relationship
with bot.main, reconciliation.py, four_way.py, or the CLI's enabled gate
lives here — this module has no ability to trade and no opinion about
whether shadow mode is on.

Pagination mirrors scripts/ledger_reconciliation_audit.py's own
already-tested discipline (page via `ofs` until the accumulated count
matches Kraken's own reported `count`), reimplemented here parsing each
raw entry through parse_raw_ledger_entry directly (id taken from the
envelope's own dictionary key, exactly matching that function's real
contract) rather than through ledger_reconciliation_audit.py's separate
LedgerRow dataclass — this adapter's output is a list[LedgerEntry] ready
for persist_batch/reconcile with no second translation step. Coverage is
verified by UNIQUE (account_id, ledger_id) identity (via the shared
_dedup_entries), not raw row count — the same fix already applied to
tests/crypto/test_ledger_quantity_reconciliation_integration.py after an
earlier review found a raw-count check let duplicate rows silently pass.

Asset scoping: Kraken's Ledgers endpoint returns entries for EVERY asset
in the account in one paginated stream — there is no server-side asset
filter used here, matching scripts/ledger_reconciliation_audit.py's own
verified real-account behavior. This adapter fetches the FULL paginated
stream (coverage-proved across all assets present) and only THEN filters
to the requested asset, reusing scripts/ledger_reconciliation_audit.py's
own _ASSET_CODE_ALIASES table so a caller asking for "BTC" correctly
matches Kraken's internal "XXBT"/"XBT" codes rather than silently
matching nothing. Every matched entry is then NORMALIZED to a single
canonical code (the alias table's own first entry) before being
returned — a real Kraken ledger can mix legacy and modern codes for the
same logical asset, and every downstream call in
ledger_quantity_reconciliation.py scopes by an EXACT string match on
`asset`, so without this normalization a legacy-coded entry could be
persisted under a different string than a modern-coded one and silently
vanish from every trust check with no error raised anywhere.

Balance-read timing: read_wallet_balance() is meant to be passed as
run_shadow_cycle's read_wallet_balance_fn hook, which calls it STRICTLY
AFTER the ledger has been fetched AND persisted — never before, never
concurrently. This ordering is a deliberate choice, not incidental: a
real trade landing between the two reads is unavoidable either way
(there is no atomic snapshot across two separate exchange calls), but
reading the ledger first means the persisted chain can never claim to
already account for activity the balance read hadn't itself observed
yet — the opposite ordering (balance read first, ledger fetched second)
would let the ledger fetch race AHEAD of what the balance read captured,
a strictly worse failure mode for a system whose entire purpose is
proving the persisted chain is trustworthy. A genuine mismatch from this
unavoidable timing gap still correctly resolves as trusted=False for
THIS observation — no retry-across-persist merging is attempted here
(see run_shadow_cycle's own docstring); a fresh subsequent cycle, with
the ledger fetched again from scratch under a new observation_id, is the
retry mechanism, not an in-place patch.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from scripts.ledger_reconciliation_audit import _ASSET_CODE_ALIASES, _build_exchange

from bot.accounting.ledger_quantity_reconciliation import LedgerEntry, _dedup_entries, parse_raw_ledger_entry


class LedgerFetchCoverageError(RuntimeError):
    """Raised when Kraken's own reported ledger count could not be
    matched by unique fetched identities, or when a requested asset has
    no corresponding balance entry — never silently trusted or guessed."""


def resolve_asset_alias_group(asset: str) -> "tuple[str, ...]":
    """The ONE resolver every function in this module uses — returns the
    FULL alias group for `asset`, regardless of which member of that
    group was passed in. _ASSET_CODE_ALIASES is keyed by a canonical
    human name ("BTC") whose own tuple also lists that same canonical
    code as one of its members ("XXBT", "XBT", "BTC") — a real bug this
    fixes: looking the input up ONLY as a dict key (`.get(asset.upper(),
    (asset,))`) resolves "BTC" to the full group correctly, but "XBT" or
    "XXBT" — legitimate members of that exact same group — aren't dict
    KEYS themselves, so they silently fell back to a singleton group
    containing only themselves. A ledger entry coded "XBT" and a balance
    keyed "BTC" would then never be recognized as the same asset the
    caller asked about, purely depending on which spelling they used to
    ask. This resolver instead checks the group's own membership, not
    just its key, so "BTC", "XBT", and "XXBT" all resolve to the exact
    same tuple. Falls back to a singleton group containing the literal
    input, upper-cased, only when it matches no known alias anywhere."""
    asset_upper = asset.upper()
    if asset_upper in _ASSET_CODE_ALIASES:
        return _ASSET_CODE_ALIASES[asset_upper]
    for codes in _ASSET_CODE_ALIASES.values():
        if asset_upper in codes:
            return codes
    return (asset_upper,)


def build_exchange():
    """Thin, explicitly-named wrapper around scripts/
    ledger_reconciliation_audit.py's own _build_exchange() — read-only
    API credentials (Query Ledgers, Query Funds), no order-capable scope
    is exercised anywhere in this module."""
    return _build_exchange()


def fetch_ledger_entries_for_asset(
    ex, *, account_id: str, asset: str, batch_id: str, observed_at: str,
) -> "list[LedgerEntry]":
    """Pages Kraken's raw Ledgers endpoint (`privatePostLedgers`) via
    `ofs` until the accumulated count matches the first response's
    reported `count` (mirroring scripts/ledger_reconciliation_audit.py's
    own loop-termination logic exactly), parses every raw entry through
    parse_raw_ledger_entry, then verifies coverage by UNIQUE identity
    (raises LedgerFetchCoverageError on any drift or undercount — a
    stricter check than the raw accumulated-length termination above,
    which is only ever a loop-stopping heuristic, never the actual safety
    net) before filtering down to `asset` via _ASSET_CODE_ALIASES."""
    all_entries: "list[LedgerEntry]" = []
    reported_count = None
    ofs = 0
    while True:
        resp = ex.privatePostLedgers({"ofs": ofs})
        result = resp.get("result", {})
        this_count = int(result.get("count")) if result.get("count") is not None else None
        if reported_count is None:
            reported_count = this_count
        elif this_count != reported_count:
            raise LedgerFetchCoverageError(
                f"Kraken's reported ledger count drifted mid-pagination "
                f"({reported_count} -> {this_count}) — refusing to trust it"
            )
        page = result.get("ledger", {}) or {}
        if not page:
            break
        all_entries.extend(
            parse_raw_ledger_entry(ledger_id, raw, account_id=account_id, batch_id=batch_id, observed_at=observed_at)
            for ledger_id, raw in page.items()
        )
        if len(all_entries) >= (reported_count or 0):
            break
        ofs += len(page)

    if reported_count is None:
        raise LedgerFetchCoverageError("Kraken never reported a ledger count")

    deduped = _dedup_entries(all_entries)
    if len(deduped) != reported_count:
        raise LedgerFetchCoverageError(
            f"fetched {len(deduped)} UNIQUE ledger entries but Kraken reports {reported_count} total "
            f"(raw fetch returned {len(all_entries)} rows across all pages) — NOT declaring this complete"
        )

    codes = resolve_asset_alias_group(asset)
    canonical_code = codes[0]
    # Normalized to ONE canonical code before returning — real Kraken
    # ledgers can mix legacy and modern codes for the same logical asset
    # (this is exactly why _ASSET_CODE_ALIASES exists at all). Every
    # downstream call (persist_batch / load_ledger_entries / reconcile /
    # verify_batch_completeness / batch_is_complete) scopes by an EXACT
    # string match on `asset` — without this normalization, a
    # legacy-coded entry would be stored under a DIFFERENT string than a
    # modern-coded one for the same asset, and would silently never be
    # returned by a query scoped to the canonical code, i.e. a real
    # historical entry could vanish from every trust check with no error
    # raised anywhere.
    return [replace(e, asset=canonical_code) for e in deduped if e.asset in codes]


def read_wallet_balance(ex, *, asset: str) -> "tuple[Decimal, str]":
    """A single, separate, read-only fetch_balance() call. Returns
    (balance, read_at_iso) — read_at_iso is generated HERE, at the moment
    this call actually completes, never reused from an earlier step's own
    timestamp, so a caller (run_shadow_cycle) can tell exactly when this
    specific reading happened relative to the ledger fetch it followed."""
    codes = resolve_asset_alias_group(asset)
    balances = ex.fetch_balance()
    total = balances.get("total", {}) or {}
    for code in codes:
        if code in total:
            read_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            return Decimal(str(total[code])), read_at
    raise LedgerFetchCoverageError(
        f"fetch_balance() returned no entry for asset {asset!r} under any known code {codes}"
    )


def build_live_fetch_fn(ex, *, asset: str):
    """Returns a fetch_fn(*, account_id, batch_id, observed_at) ->
    list[LedgerEntry] — the exact contract run_shadow_cycle's `fetch_fn`
    expects, completely opaque to run_shadow_cycle itself, which never
    knows this came from a real exchange rather than a fixture."""
    def fetch_fn(*, account_id, batch_id, observed_at):
        return fetch_ledger_entries_for_asset(
            ex, account_id=account_id, asset=asset, batch_id=batch_id, observed_at=observed_at,
        )
    return fetch_fn


def build_read_wallet_balance_fn(ex, *, asset: str):
    """Returns a read_wallet_balance_fn() -> (Decimal, str) — the exact
    contract run_shadow_cycle's `read_wallet_balance_fn` hook expects."""
    def read_fn():
        return read_wallet_balance(ex, asset=asset)
    return read_fn
