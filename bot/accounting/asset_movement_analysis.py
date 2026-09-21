"""
Offline asset-movement reconciliation analysis — NOT wired into the live
reconciliation cycle (bot/main.py, reconciliation.py, four_way.py are all
untouched by this module, and nothing in them imports it; see the source
guard in tests/crypto/test_asset_movement_analysis.py).

── Why this exists ─────────────────────────────────────────────────────────
The production position-fold (engine.causal_order / engine.fold_position*)
only ever sees TRADES. On 2026-09-21 that surfaced a real BTC/CAD "observed
trades could not be causally ordered" block: a SELL on 2026-06-27 required
more BTC than the trade history alone had accumulated. Enabling the
Kraken key's read-only "Deposit" permission (Withdraw stays OFF, per the
standing hard rule) and querying fetch_deposits('BTC') found the missing
piece: a real deposit of 0.00037766 BTC on 2026-06-26T12:43:35Z — the exact
missing quantity, one day before the SELL that needed it.

That is real, useful evidence. It is NOT, by itself, license to fold
deposits into the live reconciliation path, because a deposit carries none
of a trade's guarantees:
  - A trade has a known cost (price * amount + fee) the exchange itself
    reports. A deposit has NO cost basis at all — the exchange has no idea
    what (if anything) was paid for the deposited asset elsewhere.
  - A trade is unambiguously bot-attributable evidence once linked to a
    fill. A deposit is external by definition — crediting it to "the bot's
    own trading" would misrepresent where the asset came from.
  - "The shortfall is explained" is not one fact, it's at least THREE
    separate ones that must not be collapsed into a single flag (a review
    finding against an earlier draft, which had one `complete: bool` that
    a caller could set to True with `coverage_window=("invalid","invalid")`
    and empty evidence): whether the fold's own quantity agrees with an
    independently-read exchange balance, whether the trade/deposit/
    withdrawal history for the window is actually complete, and whether a
    numeric P&L is even available at all (a single quote currency, no
    unknown-cost lots involved). This module reports all three
    independently — see `BalanceAgreement` / `HistoryCoverage` /
    `PnlAvailability` below — and never implies one from another.

This module is the offline tool for reasoning about that evidence safely:
it explains an inventory shortfall when a deposit (or withdrawal) accounts
for it, while refusing to manufacture a cost basis, a bot-attributed
profit, mix assets or quote currencies, silently resolve conflicting
duplicate records, accept a non-finite number, or grant a "fully
reconciled" verdict it hasn't earned. It is deliberately a separate, pure,
no-I/O analysis — not a drop-in replacement for engine.causal_order, and
not something reconciliation.py calls. Promoting any of this into the live
path is a separate, explicit decision for later, not made here.

── Design ───────────────────────────────────────────────────────────────────
Every trade, deposit, and withdrawal for the single requested `asset`
becomes a `_Lot` when it adds to inventory (a BUY or a DEPOSIT) or a
consumption event when it removes from inventory (a SELL or a WITHDRAWAL).
Lots are held in strict chronological order and consumed front-to-back
(FIFO) on a sale or withdrawal.

**FIFO here is an allocation POLICY, not a factual reconstruction.** A
fungible asset like BTC has no physical identity per unit — the exchange
cannot say, and this module cannot prove, that the SELL on 2026-06-27
literally spent the satoshis that arrived in the 2026-06-26 deposit rather
than some other unit already in the wallet. FIFO is simply the most
conservative, auditable convention for attributing which lot a sale is
*deemed* to have drawn from — a stated assumption a reader can disagree
with, not a proof.

A BUY's lot carries a real `cost_per_unit` (price plus its share of the
entry fee, mirroring engine.fold_position's own fee-inclusive convention).
A DEPOSIT's lot carries `cost_per_unit=None` — unknown, always, permanently
for that lot. Nothing anywhere in this module ever converts a `None` cost
into a number. A SELL consumes lots front-to-back under the FIFO policy
above; whatever portion of the sold quantity comes from a `None`-cost lot
has its realized P&L reported as `None` for that portion, never coerced
into a numeric profit or loss the system has no evidence for. A SELL can
also outrun every lot available (no preceding inventory at all, or not
enough of it) — that portion is `unmatched_qty`, a THIRD category distinct
from both known and unknown cost basis: it isn't "unknown cost basis
inventory that arrived from somewhere," it's inventory this analysis has
no record of at all. `SellAttribution.cost_basis_status` records
"known" / "unknown" / "unmatched" / "mixed" so a caller can see the exact
split at a glance, and `PnlAvailability.available` is `False` whenever
EITHER `unknown_qty` OR `unmatched_qty` is nonzero for any sale — an
earlier draft only checked `unknown_qty`, so a sale against literally no
inventory (`known_qty=0`, `unknown_qty=0`) fell through to
`cost_basis_status="known"` and `pnl_availability.available=True`, exactly
backwards for a sale this analysis cannot explain at all. A WITHDRAWAL
consumes lots the same way but produces no P&L attribution at all — it is
a transfer out, not a sale.

Every trade/deposit/withdrawal timestamp is parsed into a real,
timezone-aware UTC instant before anything is chronologically ordered —
unconditionally, whether or not a `coverage_window` is supplied. Comparing
raw ISO strings directly is unsafe the moment two events carry different
UTC offsets: `"2026-06-01T00:30:00-05:00"` (05:30 UTC) text-sorts BEFORE
`"2026-06-01T02:00:00Z"` (02:00 UTC) even though it happens three and a
half hours LATER in real time — a naive sort would let a SELL at 02:00Z
succeed against inventory a BUY doesn't actually deliver until 05:30Z. Two
events parsing to the exact same instant are ordered by a stated,
deterministic convention: inflows (deposits, BUYs) before outflows (SELLs,
withdrawals), then original input order. **This is an assumption, not
necessarily a conservative one** — if the true sequence at that instant
was actually outflow-before-inflow, applying inflow-first can HIDE a real
shortfall that the true order would have revealed, rather than protect
against a false one. Any result touching a tied timestamp is therefore
conditional on this stated convention, not proven (see
`_chronological_order`'s own docstring).

── Input contracts (all enforced — a caller violating one gets a
   ValueError, never a silently-wrong number) ────────────────────────────
- Every numeric field on every trade/deposit/withdrawal, plus
  `closing_balance` and `balance_tolerance` if supplied, must be finite.
  NaN in particular compares False against everything in Python, so a NaN
  `closing_balance` used to sail through the balance-mismatch check as a
  false "no mismatch found" — fixed by rejecting non-finite values up
  front, before any arithmetic touches them.
- Every trade's symbol base asset, every deposit's/withdrawal's `.asset`,
  must equal the single `asset` this call is for. A deposit or withdrawal
  in a DIFFERENT asset can never be used to explain a shortfall in this
  one (an earlier draft had no such check at all — a 1 SOL deposit could
  silently "resolve" a 1 BTC shortfall).
- Every trade's `fee_currency` must equal its own symbol's quote currency,
  AND every trade's symbol must share the SAME quote currency as every
  other trade in the call. Matching the base asset (BTC) is not enough —
  an earlier draft let a BTC/CAD buy and a BTC/USD sell net together as if
  100 CAD and 100 USD were the same number, silently producing a
  fabricated "known P&L = 0". Mixed quote currencies without independent
  FX conversion evidence are rejected outright rather than guessed at.
- Every item in `deposits` must have `type == "deposit"`; every item in
  `withdrawals` must have `type == "withdrawal"` — catches a movement
  record placed in the wrong list.
- Two records sharing the same source id (a trade_id or an entry_id) must
  have IDENTICAL payloads to be treated as the same event observed twice
  (safe to collapse). If they differ — e.g. the same deposit id reported
  with two different amounts — that is a genuine conflict, not a
  duplicate, and is rejected rather than resolved by whichever one
  happened to appear first in the input list.
- `coverage_window`, if supplied, must be two parseable ISO-8601
  timestamps with `since` strictly before `until`, and every supplied
  trade/deposit/withdrawal timestamp must fall inside that window — a
  window that doesn't even cover the evidence given to it is a
  contradiction, not a detail to ignore. An earlier draft only checked
  that *something* was passed for `coverage_window`, so
  `("invalid", "invalid")` — or a window that excluded half the real
  trades — still counted as "coverage present."

── Three separate verdicts, never one flag ─────────────────────────────
- `BalanceAgreement` — does the fold's own `final_qty` match an
  independently-supplied `closing_balance` (e.g. a fresh exchange balance
  read)? `checked=False` (not `agrees=False`) when no closing_balance was
  given at all — "unknown" and "disagrees" are different facts.
- `HistoryCoverage` — was a coverage window declared, and is it internally
  consistent with the supplied evidence? `declared` reflects only that.
  `independently_verified` is **always False** in this module — an
  offline, no-network tool cannot itself prove no trade/deposit/withdrawal
  was missed the way engine.retrieve_with_coverage_proof does (by matching
  Kraken's own reported record count); the caller declaring a window is
  not the same claim as that proof, and this module refuses to conflate
  the two even though nothing here can perform that proof itself.
- `PnlAvailability` — is a numeric, trustworthy P&L even possible for this
  call? False whenever any sell drew from an unknown-cost-basis lot, when
  there were no sells at all, or (structurally impossible after the
  input-contract check above, but still recorded) when quote currencies
  were mixed.
None of these three is inferred from either of the others, and no single
boolean rolls them up — `.explain()` renders all three so a caller cannot
accidentally read one as standing in for the rest.

── Rewards, and the real BTC base-currency-fee correction (added 2026-09-21) ─
A rigorous, pagination-proven, Decimal-exact ledger audit
(scripts/ledger_reconciliation_audit.py) found two things this module did
not originally model:

1. **A trade's fee can be settled in the BASE asset even when
   `ObservedTrade.fee_currency` reports the QUOTE currency.** Trade
   `TDCRFZ-MWTNB-2NVHO6` (a real BTC/CAD sell) has `fee_currency="CAD"`,
   `fee_cost=0.03972` in `observed_trades` — but Kraken's own ledger shows
   a SEPARATE 0.00000044 BTC deduction on the BTC leg, with the CAD leg
   showing `fee=0`. Cross-checking the two legs against each other (their
   implied trade price, and the BTC fee's CAD-equivalent value) matched
   the recorded `fee_cost` within 1% — this STRONGLY SUGGESTS one real
   fee, dual-represented, not two separate charges, but that conclusion
   rests on arithmetic agreement alone; no on-chain or exchange-support
   confirmation was sought, and it is not asserted as certain.
   `base_currency_fee_qty` (optional, `{trade_id: qty}`) lets a caller who
   has done that cross-check supply the REAL base-asset quantity that left
   (or was withheld from) the wallet for a given trade, IN ADDITION to
   `trade.amount`. This module never infers or guesses this value itself —
   it is always an explicit, externally-established fact.

   **A first, corrected draft of this feature (same day) assumed the
   reported `fee_cost` was still a real cash deduction and left the extra
   base-currency quantity's cost basis completely unattributed — a genuine
   conservation bug, caught by requiring "total P&L must equal the actual
   quote-cash change when ending flat with no external flows" as an
   explicit test.** A synthetic round trip exposes it plainly: BUY 1.0 unit
   for $100 cash; SELL reports 0.9 units for $90 cash with `fee_cost=0`
   (because the CAD leg of a real trade like this shows the FULL gross
   value credited — no CAD was ever actually deducted); an extra 0.1 units
   leave as a base-currency fee. Real cash change is exactly -$10
   (-100 + 90). The first draft reported P&L=$0, silently discarding the
   $10 worth of BUY-lot cost basis consumed by the fee with no cash ever
   received for it. Fixed as follows:
   - **Fee settlement currency is now distinguished from reporting
     valuation, explicitly.** When `base_currency_fee_qty` applies to a
     trade, `fee_cost`/`fee_currency` are treated as a VALUATION of the fee
     (what it was worth at execution time, useful for display/tax
     purposes) — NOT a real cash-affecting deduction. Real cash proceeds
     for a SELL become `trade.cost` in full (not `cost - fee_cost`); real
     cash paid for a BUY becomes `trade.cost` in full (not `cost +
     fee_cost`). Trades WITHOUT an override are completely unaffected —
     `fee_cost` is still treated as a genuine cash deduction for them, the
     ordinary and much more common case.
   - **The basis of fee-consumed units is preserved, not discarded.** The
     extra base-currency quantity is consumed from the SAME FIFO lot
     queue as the sale itself (not a separate no-attribution path), so its
     own known/unknown/unmatched cost-basis split is tracked — a THIRD
     bounded extension of the existing unmatched-sale rule (see
     `unmatched_qty` above), added same-day after a review found the fee's
     own unmatched portion was added to `shortfall` (correctly failing
     `ok`) but excluded from the P&L-availability check, so a sale with a
     fully-known main quantity but an entirely uncovered fee still
     reported `pnl_availability.available=True`. A KNOWN-cost portion's
     realized loss (`-cost_per_unit * qty`, since $0 was received for it)
     is added into the SAME trade's `realized_pnl_known` — recognizing the
     fee's true economic cost, from its real acquisition cost, exactly
     once. An UNKNOWN-cost portion (e.g. drawn from a deposit or reward
     lot) is tracked as `fee_consumed_unknown_qty`; a portion with NO
     explaining lot AT ALL (inventory ran out) is tracked as
     `fee_consumed_unmatched_qty`. EITHER one sets
     `fee_unit_basis_unresolved=True` — **never guessed at, folded into a
     "known" number, or defaulted to zero-cost; it stays explicitly
     unresolved**, and `PnlAvailability.available` is `False` whenever it
     is set, exactly like an unknown/unmatched sale already makes P&L
     unavailable.
   - On a BUY with an override: the lot actually added is `trade.amount -
     base_currency_fee_qty[trade_id]` (the true net quantity retained),
     with `cost_per_unit = trade.cost / net_qty` (real cash paid, divided
     by real units retained — `fee_cost` excluded, per the settlement-vs-
     valuation distinction above).
2. **A reward credit (e.g. Kraken auto-staking) is a real, positive,
   unknown-cost-basis inflow — but it is NOT a deposit.** A deposit implies
   an external transfer; a reward is the exchange itself crediting the
   account for holding an asset. Conflating the two would misrepresent
   where the asset came from, exactly the ownership concern that already
   applies to deposits. `rewards` (optional, parallel to `deposits`) takes
   `LedgerMovement`s with `type == "reward"`, folded exactly like a
   deposit (`cost_per_unit=None`, an inflow for chronological tie-breaking)
   but tagged `source_type="reward"` throughout and validated as its own
   distinct type — a `type="deposit"` entry in `rewards`, or vice versa,
   is rejected, never silently accepted as "close enough."
Neither of these is inferred, defaulted, or auto-detected — both require
the caller to supply real, already-established evidence, keeping this
module's core promise (no fabricated ownership, no fabricated cost basis)
intact for these two new event kinds exactly as it already was for
deposits and withdrawals.

Today, withdrawal-equivalent visibility for this Kraken account is NOT
permanently blocked the way an earlier draft of this module implied.
Kraken's private `WithdrawStatus` endpoint (ccxt `fetch_withdrawals`)
does require the "Withdraw" key permission, which correctly stays
disabled per the standing hard rule — but the account's key ALSO has an
independent "Query ledger entries" permission (confirmed present, though
disabled, on the 2026-09-20 permission check), which gates Kraken's
`Ledgers` endpoint (ccxt `fetch_ledger`) — a broader read covering
deposits, withdrawals, trades, and transfers together, entirely separate
from "Withdraw." Enabling ONLY "Query ledger entries" — never tested in
this repo yet — may be a safe way to obtain real withdrawal/transfer
evidence without ever touching the Withdraw toggle. That path is not
implemented here; this module simply accepts `withdrawals` as an input
so it is ready to consume that evidence the moment it exists.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from bot.accounting.engine import LedgerMovement
from bot.accounting.store import ObservedTrade

_QTY_EPS = 1e-9


@dataclass
class SellAttribution:
    trade_id: str
    qty: float
    known_qty: float
    unknown_qty: float
    unmatched_qty: float                 # sold with NO explaining lot at all — neither known nor unknown basis
    realized_pnl_known: "float | None"   # None only when known_qty == 0 (nothing to report)
    cost_basis_status: str               # "known" | "unknown" | "unmatched" | "mixed"
    fee_consumed_qty: float = 0.0         # extra base-currency-fee quantity consumed (base_currency_fee_qty)
    fee_consumed_known_qty: float = 0.0
    fee_consumed_unknown_qty: float = 0.0
    fee_consumed_unmatched_qty: float = 0.0   # fee itself exceeded available inventory — no lot at all
    fee_unit_basis_unresolved: bool = False   # True if any fee-consumed qty had unknown OR unmatched basis


@dataclass
class BalanceAgreement:
    checked: bool                 # was a closing_balance supplied at all?
    agrees: "bool | None"         # None if not checked; True/False if it was
    diff: "float | None"
    reason: str


@dataclass
class HistoryCoverage:
    declared: bool                       # a window was supplied AND passed internal-consistency checks
    since: "str | None"
    until: "str | None"
    independently_verified: bool         # always False here — see module docstring
    reason: str


@dataclass
class PnlAvailability:
    available: bool
    quote_currency: "str | None"
    reason: str


@dataclass
class AssetMovementResult:
    ok: bool                              # no unresolved negative-inventory shortfall remains
    unresolved_shortfall_qty: float        # > 0 only when ok is False
    final_qty: float
    unknown_basis_qty_remaining: float     # currently-held qty whose cost basis is still unknown
    sell_attributions: "list[SellAttribution]"
    duplicate_ids_collapsed: "list[str]"   # source ids that appeared more than once (identical payload)
    balance_agreement: BalanceAgreement
    history_coverage: HistoryCoverage
    pnl_availability: PnlAvailability

    def explain(self) -> str:
        if not self.ok:
            return (f"UNRESOLVED — shortfall of {self.unresolved_shortfall_qty:.10f} remains "
                    f"even after applying the supplied deposits/withdrawals")
        parts = [f"inventory explained (final_qty={self.final_qty:.10f})"]
        if self.unknown_basis_qty_remaining > _QTY_EPS:
            parts.append(f"{self.unknown_basis_qty_remaining:.10f} held with unknown cost basis")
        parts.append(f"balance agreement: {self.balance_agreement.reason}")
        parts.append(f"history coverage: {self.history_coverage.reason}")
        parts.append(f"P&L availability: {self.pnl_availability.reason}")
        return "; ".join(parts)


@dataclass
class _Lot:
    qty: float
    cost_per_unit: "float | None"
    source_id: str
    source_type: str   # "trade" | "deposit" | "reward"


def _require_finite(label: str, value) -> None:
    if value is None:
        return
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number, got {value!r}")


def _parse_iso(label: str, raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{label} must be a parseable ISO-8601 timestamp, got {raw!r}: {exc}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _dedup_by_id(items, id_fn) -> "tuple[list, list[str]]":
    """Collapses an exact repeat of the same id+payload to one entry.
    A repeat of the same id with a DIFFERENT payload is a genuine conflict
    — raised immediately, regardless of which one appeared first, so the
    result can never silently depend on input order."""
    seen: "dict[str, object]" = {}
    duplicates: "list[str]" = []
    for item in items:
        key = id_fn(item)
        if key in seen:
            if seen[key] != item:
                raise ValueError(
                    f"conflicting records for id {key!r}: {seen[key]!r} vs {item!r} — "
                    f"not a duplicate, a genuine data conflict that must be resolved by the "
                    f"caller, not silently picked between"
                )
            duplicates.append(key)
            continue
        seen[key] = item
    return list(seen.values()), duplicates


def _validate_numeric(
    trades: "list[ObservedTrade]", deposits: "list[LedgerMovement]",
    withdrawals: "list[LedgerMovement]", rewards: "list[LedgerMovement]",
    base_currency_fee_qty: "dict[str, float]", closing_balance, balance_tolerance: float,
) -> None:
    for t in trades:
        _require_finite(f"trade {t.trade_id} price", t.price)
        _require_finite(f"trade {t.trade_id} amount", t.amount)
        _require_finite(f"trade {t.trade_id} cost", t.cost)
        _require_finite(f"trade {t.trade_id} fee_cost", t.fee_cost)
    for d in deposits:
        _require_finite(f"deposit {d.entry_id} amount", d.amount)
    for w in withdrawals:
        _require_finite(f"withdrawal {w.entry_id} amount", w.amount)
    for r in rewards:
        _require_finite(f"reward {r.entry_id} amount", r.amount)
    for trade_id, fee_qty in base_currency_fee_qty.items():
        _require_finite(f"base_currency_fee_qty[{trade_id!r}]", fee_qty)
        if fee_qty < 0:
            raise ValueError(
                f"base_currency_fee_qty[{trade_id!r}] must be >= 0, got {fee_qty!r}"
            )
    _require_finite("closing_balance", closing_balance)
    _require_finite("balance_tolerance", balance_tolerance)


def _validate_assets_and_currencies(
    asset: str, trades: "list[ObservedTrade]", deposits: "list[LedgerMovement]",
    withdrawals: "list[LedgerMovement]", rewards: "list[LedgerMovement]",
    base_currency_fee_qty: "dict[str, float]",
) -> "str | None":
    """Returns the single shared quote currency across all trades (or None
    if there are no trades), raising if trades disagree on it."""
    quote: "str | None" = None
    for t in trades:
        try:
            base, this_quote = t.symbol.split("/")
        except ValueError:
            raise ValueError(f"trade {t.trade_id} has an unparseable symbol {t.symbol!r}")
        if base != asset:
            raise ValueError(
                f"trade {t.trade_id} is on {t.symbol} (base {base}), not the requested "
                f"asset {asset!r} — refusing to mix assets in one analysis"
            )
        if t.fee_currency and t.fee_currency != this_quote:
            raise ValueError(
                f"trade {t.trade_id}'s fee_currency {t.fee_currency!r} does not match "
                f"{t.symbol}'s quote currency {this_quote!r}"
            )
        if quote is None:
            quote = this_quote
        elif this_quote != quote:
            raise ValueError(
                f"trade {t.trade_id} is quoted in {this_quote!r} but an earlier trade in this "
                f"call was quoted in {quote!r} — mixing quote currencies without independent "
                f"FX conversion evidence would silently subtract unrelated currencies as if "
                f"they were equal; require one quote currency per call instead"
            )
    for d in deposits:
        if d.asset != asset:
            raise ValueError(
                f"deposit {d.entry_id} is denominated in {d.asset!r}, not the requested "
                f"asset {asset!r} — an asset it did not receive cannot resolve this shortfall"
            )
        if d.type != "deposit":
            raise ValueError(f"entry {d.entry_id} in `deposits` has type={d.type!r}, expected 'deposit'")
    for w in withdrawals:
        if w.asset != asset:
            raise ValueError(
                f"withdrawal {w.entry_id} is denominated in {w.asset!r}, not the requested "
                f"asset {asset!r}"
            )
        if w.type != "withdrawal":
            raise ValueError(f"entry {w.entry_id} in `withdrawals` has type={w.type!r}, expected 'withdrawal'")
    for r in rewards:
        if r.asset != asset:
            raise ValueError(
                f"reward {r.entry_id} is denominated in {r.asset!r}, not the requested "
                f"asset {asset!r}"
            )
        if r.type != "reward":
            raise ValueError(f"entry {r.entry_id} in `rewards` has type={r.type!r}, expected 'reward'")

    trade_ids = {t.trade_id for t in trades}
    for trade_id in base_currency_fee_qty:
        if trade_id not in trade_ids:
            raise ValueError(
                f"base_currency_fee_qty references trade_id {trade_id!r}, which is not in "
                f"`trades` — this must correspond to a real trade actually supplied to this call"
            )
    return quote


def _is_inflow(kind: str, obj) -> bool:
    if kind in ("deposit", "reward"):
        return True
    if kind == "withdrawal":
        return False
    return obj.side == "buy"   # kind == "trade"


def _chronological_order(raw_events: "list[tuple]") -> "list[tuple]":
    """Sorts (kind, timestamp_str, obj) triples by their real, timezone-aware
    UTC instant — never by the raw ISO string. A naive string sort silently
    mis-orders whenever timestamps carry different UTC offsets: e.g.
    "2026-06-01T00:30:00-05:00" (05:30Z) text-sorts BEFORE
    "2026-06-01T02:00:00Z" (02:00Z) even though it happens THREE AND A HALF
    HOURS LATER in real time — a naive sort would let a SELL at 02:00Z
    succeed against inventory a BUY doesn't actually deliver until 05:30Z.
    This parses every event's timestamp unconditionally, regardless of
    whether a coverage window is supplied (an earlier draft only parsed
    timestamps when a coverage window happened to be passed in, leaving
    the sort itself always naive-string-based).

    Same-instant ties are broken by a stated, deterministic convention:
    inflows (deposits, BUYs) are ordered before outflows (SELLs,
    withdrawals) at the identical instant, and ties within the same
    direction keep their original input order. This is an ASSUMPTION, not
    necessarily a conservative one: if the true sequence at that instant
    was actually outflow-before-inflow, applying inflow-first can HIDE a
    real shortfall the true order would have revealed, rather than protect
    against a false one. Any result touching a tied timestamp is
    conditional on this convention, not proven — exactly like the FIFO
    consumption policy documented at module level."""
    decorated = []
    for idx, (kind, ts_raw, obj) in enumerate(raw_events):
        dt = _parse_iso(f"{kind} timestamp", ts_raw)
        rank = 0 if _is_inflow(kind, obj) else 1
        decorated.append((dt, rank, idx, kind, ts_raw, obj))
    decorated.sort(key=lambda e: (e[0], e[1], e[2]))
    return [(kind, ts_raw, obj) for (_dt, _rank, _idx, kind, ts_raw, obj) in decorated]


def _build_history_coverage(
    coverage_window: "tuple[str, str] | None", event_timestamps: "list[str]",
) -> HistoryCoverage:
    if coverage_window is None:
        return HistoryCoverage(
            declared=False, since=None, until=None, independently_verified=False,
            reason="no coverage window supplied",
        )
    since_raw, until_raw = coverage_window
    since_dt = _parse_iso("coverage_window[0] (since)", since_raw)
    until_dt = _parse_iso("coverage_window[1] (until)", until_raw)
    if not since_dt < until_dt:
        raise ValueError(
            f"coverage_window since ({since_raw}) must be strictly before until ({until_raw})"
        )
    for ts in event_timestamps:
        ts_dt = _parse_iso("event timestamp", ts)
        if not (since_dt <= ts_dt <= until_dt):
            raise ValueError(
                f"event timestamp {ts} falls outside the declared coverage_window "
                f"[{since_raw}, {until_raw}] — the window contradicts the evidence supplied"
            )
    return HistoryCoverage(
        declared=True, since=since_raw, until=until_raw, independently_verified=False,
        reason=(
            "window is internally consistent and covers every supplied event, but this is the "
            "CALLER's declared window, not independently verified against the exchange's own "
            "reported record count the way engine.retrieve_with_coverage_proof does — true "
            "completeness of the underlying history is not established by this alone"
        ),
    )


def analyze_with_asset_movements(
    asset: str,
    trades: "list[ObservedTrade]",
    deposits: "list[LedgerMovement]",
    *,
    withdrawals: "list[LedgerMovement] | None" = None,
    rewards: "list[LedgerMovement] | None" = None,
    base_currency_fee_qty: "dict[str, float] | None" = None,
    coverage_window: "tuple[str, str] | None" = None,
    closing_balance: "float | None" = None,
    balance_tolerance: float = 1e-8,
) -> AssetMovementResult:
    """Pure function — no DB access, no exchange calls, no writes anywhere.
    Interleaves trades, deposits, withdrawals, and rewards for a single
    `asset` chronologically and folds them through a FIFO lot queue (an
    allocation policy, not a factual reconstruction — see module docstring).
    Never inserts a synthetic trade for a deposit or reward — each is its
    own event kind throughout, distinguishable in every lot and every dedup
    key, so nothing here can be mistaken for a bot-executed BUY or SELL.
    `base_currency_fee_qty` (optional, `{trade_id: qty}`) is an explicit,
    externally-established correction for a trade whose real fee was
    settled in this asset's own quantity despite `fee_currency` reporting
    something else — never inferred or guessed by this function itself;
    see module docstring for the real 2026-09-21 finding this exists for.
    Reports three independent verdicts (BalanceAgreement / HistoryCoverage
    / PnlAvailability) rather than one rolled-up flag — see module
    docstring."""
    withdrawals_in = withdrawals if withdrawals is not None else []
    rewards_in = rewards if rewards is not None else []
    fee_qty_map = dict(base_currency_fee_qty or {})
    _validate_numeric(trades, deposits, withdrawals_in, rewards_in, fee_qty_map,
                      closing_balance, balance_tolerance)
    quote = _validate_assets_and_currencies(
        asset, trades, deposits, withdrawals_in, rewards_in, fee_qty_map,
    )

    trades, dup_trade_ids = _dedup_by_id(trades, lambda t: f"trade:{t.trade_id}")
    deposits, dup_deposit_ids = _dedup_by_id(deposits, lambda d: f"deposit:{d.entry_id}")
    withdrawals_in, dup_withdrawal_ids = _dedup_by_id(
        withdrawals_in, lambda w: f"withdrawal:{w.entry_id}"
    )
    rewards_in, dup_reward_ids = _dedup_by_id(rewards_in, lambda r: f"reward:{r.entry_id}")
    duplicate_ids = dup_trade_ids + dup_deposit_ids + dup_withdrawal_ids + dup_reward_ids

    raw_events = (
        [("trade", t.exchange_timestamp, t) for t in trades]
        + [("deposit", d.timestamp, d) for d in deposits]
        + [("withdrawal", w.timestamp, w) for w in withdrawals_in]
        + [("reward", r.timestamp, r) for r in rewards_in]
    )
    events = _chronological_order(raw_events)

    history_coverage = _build_history_coverage(coverage_window, [e[1] for e in events])

    lots: "list[_Lot]" = []
    sell_attributions: "list[SellAttribution]" = []
    shortfall = 0.0

    def _consume(qty_needed: float) -> float:
        """Removes qty_needed from the front of the lot queue (FIFO),
        returns any UNMET remainder (0.0 if fully satisfied)."""
        remaining = qty_needed
        while remaining > _QTY_EPS and lots:
            lot = lots[0]
            take = min(lot.qty, remaining)
            lot.qty -= take
            remaining -= take
            if lot.qty <= _QTY_EPS:
                lots.pop(0)
        return remaining

    for kind, _ts, obj in events:
        if kind == "deposit":
            lots.append(_Lot(qty=obj.amount, cost_per_unit=None,
                              source_id=obj.entry_id, source_type="deposit"))
            continue

        if kind == "reward":
            lots.append(_Lot(qty=obj.amount, cost_per_unit=None,
                              source_id=obj.entry_id, source_type="reward"))
            continue

        if kind == "withdrawal":
            unmet = _consume(abs(obj.amount))
            shortfall += unmet
            continue

        trade: ObservedTrade = obj
        base_fee_qty = fee_qty_map.get(trade.trade_id, 0.0)
        has_override = base_fee_qty > _QTY_EPS

        if trade.side == "buy":
            net_qty = trade.amount - base_fee_qty
            if has_override:
                # Settlement-vs-valuation: fee_cost is a reporting figure
                # only when we KNOW the fee was actually taken in this
                # asset's own quantity — real cash paid is trade.cost alone.
                cost_per_unit = trade.cost / net_qty if net_qty > 0 else 0.0
            else:
                cost_per_unit = (trade.cost + trade.fee_cost) / net_qty if net_qty > 0 else 0.0
            lots.append(_Lot(qty=net_qty, cost_per_unit=cost_per_unit,
                              source_id=trade.trade_id, source_type="trade"))
            continue

        # SELL — consume front-to-back (FIFO policy), splitting known vs unknown basis.
        remaining = trade.amount
        known_qty = 0.0
        unknown_qty = 0.0
        pnl_known = 0.0
        if has_override:
            proceeds_per_unit = trade.cost / trade.amount if trade.amount > 0 else 0.0
        else:
            proceeds_per_unit = (trade.cost - trade.fee_cost) / trade.amount if trade.amount > 0 else 0.0
        while remaining > _QTY_EPS and lots:
            lot = lots[0]
            take = min(lot.qty, remaining)
            if lot.cost_per_unit is None:
                unknown_qty += take
            else:
                known_qty += take
                pnl_known += (proceeds_per_unit - lot.cost_per_unit) * take
            lot.qty -= take
            remaining -= take
            if lot.qty <= _QTY_EPS:
                lots.pop(0)
        unmatched_qty = 0.0
        if remaining > _QTY_EPS:
            unmatched_qty = remaining
            shortfall += remaining  # genuinely unresolved even with the supplied evidence

        # The fee-consumed quantity's own cost basis is preserved, not
        # discarded: it draws from the SAME lot queue, tracked separately
        # so a known-cost portion's real loss (zero cash received for it)
        # is recognized, and an unknown-cost portion is flagged explicitly
        # rather than silently folded into "known" or ignored.
        fee_consumed_known_qty = 0.0
        fee_consumed_unknown_qty = 0.0
        fee_remaining = base_fee_qty
        while fee_remaining > _QTY_EPS and lots:
            lot = lots[0]
            take = min(lot.qty, fee_remaining)
            if lot.cost_per_unit is None:
                fee_consumed_unknown_qty += take
            else:
                fee_consumed_known_qty += take
                pnl_known += -lot.cost_per_unit * take   # $0 received for this qty
            lot.qty -= take
            fee_remaining -= take
            if lot.qty <= _QTY_EPS:
                lots.pop(0)
        fee_consumed_unmatched_qty = 0.0
        if fee_remaining > _QTY_EPS:
            fee_consumed_unmatched_qty = fee_remaining
            shortfall += fee_remaining   # the fee itself couldn't even be fully covered
        fee_unit_basis_unresolved = (
            fee_consumed_unknown_qty > _QTY_EPS or fee_consumed_unmatched_qty > _QTY_EPS
        )

        parts_present = [
            name for name, qty in (
                ("known", known_qty), ("unknown", unknown_qty), ("unmatched", unmatched_qty),
            ) if qty > _QTY_EPS
        ]
        if len(parts_present) == 1:
            status = parts_present[0]
        elif len(parts_present) > 1:
            status = "mixed"
        else:
            status = "known"   # degenerate case: trade.amount ~ 0, nothing to attribute
        sell_attributions.append(SellAttribution(
            trade_id=trade.trade_id, qty=trade.amount, known_qty=known_qty,
            unknown_qty=unknown_qty, unmatched_qty=unmatched_qty,
            realized_pnl_known=(pnl_known if (known_qty > _QTY_EPS or fee_consumed_known_qty > _QTY_EPS)
                                else None),
            cost_basis_status=status,
            fee_consumed_qty=base_fee_qty, fee_consumed_known_qty=fee_consumed_known_qty,
            fee_consumed_unknown_qty=fee_consumed_unknown_qty,
            fee_consumed_unmatched_qty=fee_consumed_unmatched_qty,
            fee_unit_basis_unresolved=fee_unit_basis_unresolved,
        ))

    final_qty = sum(l.qty for l in lots)
    unknown_remaining = sum(l.qty for l in lots if l.cost_per_unit is None)
    ok = shortfall <= _QTY_EPS

    if closing_balance is None:
        balance_agreement = BalanceAgreement(
            checked=False, agrees=None, diff=None, reason="no closing_balance supplied",
        )
    else:
        diff = final_qty - closing_balance
        agrees = abs(diff) <= balance_tolerance
        balance_agreement = BalanceAgreement(
            checked=True, agrees=agrees, diff=diff,
            reason=("fold matches the supplied closing balance" if agrees else
                    f"fold predicts {final_qty:.10f}, exchange reports {closing_balance:.10f} "
                    f"(diff {diff:.10f})"),
        )

    any_unaccounted_sale = any(
        (a.unknown_qty > _QTY_EPS or a.unmatched_qty > _QTY_EPS or a.fee_unit_basis_unresolved)
        for a in sell_attributions
    )
    if not sell_attributions:
        pnl_availability = PnlAvailability(
            available=False, quote_currency=quote, reason="no sell events to evaluate P&L for",
        )
    elif any_unaccounted_sale:
        pnl_availability = PnlAvailability(
            available=False, quote_currency=quote,
            reason="at least one sale drew from unknown-cost-basis inventory (e.g. a deposit), had "
                   "no explaining lot at all (unmatched), or consumed a base-currency fee whose "
                   "own unit cost basis is unresolved — P&L is not fully known for that sale",
        )
    else:
        pnl_availability = PnlAvailability(
            available=True, quote_currency=quote,
            reason=f"every sale's cost basis is known, all in {quote}",
        )

    return AssetMovementResult(
        ok=ok, unresolved_shortfall_qty=shortfall, final_qty=final_qty,
        unknown_basis_qty_remaining=unknown_remaining, sell_attributions=sell_attributions,
        duplicate_ids_collapsed=duplicate_ids, balance_agreement=balance_agreement,
        history_coverage=history_coverage, pnl_availability=pnl_availability,
    )
