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
into a numeric profit or loss the system has no evidence for.
`SellAttribution.cost_basis_status` records "known" / "unknown" / "mixed"
so a caller can see the split at a glance. A WITHDRAWAL consumes lots the
same way but produces no P&L attribution at all — it is a transfer out,
not a sale.

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
    realized_pnl_known: "float | None"   # None only when known_qty == 0 (nothing to report)
    cost_basis_status: str               # "known" | "unknown" | "mixed"


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
    source_type: str   # "trade" | "deposit"


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
    withdrawals: "list[LedgerMovement]", closing_balance, balance_tolerance: float,
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
    _require_finite("closing_balance", closing_balance)
    _require_finite("balance_tolerance", balance_tolerance)


def _validate_assets_and_currencies(
    asset: str, trades: "list[ObservedTrade]", deposits: "list[LedgerMovement]",
    withdrawals: "list[LedgerMovement]",
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
    return quote


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
    coverage_window: "tuple[str, str] | None" = None,
    closing_balance: "float | None" = None,
    balance_tolerance: float = 1e-8,
) -> AssetMovementResult:
    """Pure function — no DB access, no exchange calls, no writes anywhere.
    Interleaves trades, deposits, and (if supplied) withdrawals for a single
    `asset` chronologically and folds them through a FIFO lot queue (an
    allocation policy, not a factual reconstruction — see module docstring).
    Never inserts a synthetic trade for a deposit — deposits and
    withdrawals are their own event kinds throughout, distinguishable in
    every lot and every dedup key, so nothing here can be mistaken for a
    bot-executed BUY or SELL. Reports three independent verdicts
    (BalanceAgreement / HistoryCoverage / PnlAvailability) rather than one
    rolled-up flag — see module docstring."""
    withdrawals_in = withdrawals if withdrawals is not None else []
    _validate_numeric(trades, deposits, withdrawals_in, closing_balance, balance_tolerance)
    quote = _validate_assets_and_currencies(asset, trades, deposits, withdrawals_in)

    trades, dup_trade_ids = _dedup_by_id(trades, lambda t: f"trade:{t.trade_id}")
    deposits, dup_deposit_ids = _dedup_by_id(deposits, lambda d: f"deposit:{d.entry_id}")
    withdrawals_in, dup_withdrawal_ids = _dedup_by_id(
        withdrawals_in, lambda w: f"withdrawal:{w.entry_id}"
    )
    duplicate_ids = dup_trade_ids + dup_deposit_ids + dup_withdrawal_ids

    events = (
        [("trade", t.exchange_timestamp, t) for t in trades]
        + [("deposit", d.timestamp, d) for d in deposits]
        + [("withdrawal", w.timestamp, w) for w in withdrawals_in]
    )
    events.sort(key=lambda e: e[1])

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

        if kind == "withdrawal":
            unmet = _consume(abs(obj.amount))
            shortfall += unmet
            continue

        trade: ObservedTrade = obj
        if trade.side == "buy":
            cost_per_unit = (trade.cost + trade.fee_cost) / trade.amount if trade.amount > 0 else 0.0
            lots.append(_Lot(qty=trade.amount, cost_per_unit=cost_per_unit,
                              source_id=trade.trade_id, source_type="trade"))
            continue

        # SELL — consume front-to-back (FIFO policy), splitting known vs unknown basis.
        remaining = trade.amount
        known_qty = 0.0
        unknown_qty = 0.0
        pnl_known = 0.0
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
        if remaining > _QTY_EPS:
            shortfall += remaining  # genuinely unresolved even with the supplied evidence

        if known_qty > _QTY_EPS and unknown_qty > _QTY_EPS:
            status = "mixed"
        elif unknown_qty > _QTY_EPS:
            status = "unknown"
        else:
            status = "known"
        sell_attributions.append(SellAttribution(
            trade_id=trade.trade_id, qty=trade.amount, known_qty=known_qty,
            unknown_qty=unknown_qty,
            realized_pnl_known=(pnl_known if known_qty > _QTY_EPS else None),
            cost_basis_status=status,
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

    any_unknown_sale = any(a.unknown_qty > _QTY_EPS for a in sell_attributions)
    if not sell_attributions:
        pnl_availability = PnlAvailability(
            available=False, quote_currency=quote, reason="no sell events to evaluate P&L for",
        )
    elif any_unknown_sale:
        pnl_availability = PnlAvailability(
            available=False, quote_currency=quote,
            reason="at least one sale drew from unknown-cost-basis inventory (e.g. a deposit) — "
                   "P&L is not fully known for that sale",
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
