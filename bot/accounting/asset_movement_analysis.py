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
  - A "shortfall explained" verdict is not the same as "reconciliation is
    complete." Completeness additionally requires real withdrawal/transfer
    evidence, a proven coverage window, and a closing balance to check the
    fold against — this module requires all three as actual data, never a
    caller-asserted flag, before it will report `complete=True` (a review
    finding against an earlier draft of this module: passing an empty
    trade/deposit list alongside a bare `withdrawals_available=True` flag
    used to report `complete=True` on zero evidence — fixed below).

This module is the offline tool for reasoning about that evidence safely:
it explains an inventory shortfall when a deposit (or withdrawal) accounts
for it, while refusing to manufacture a cost basis, a bot-attributed
profit, mix assets, silently resolve conflicting duplicate records, or
grant a "fully reconciled" verdict it hasn't earned. It is deliberately a
separate, pure, no-I/O analysis — not a drop-in replacement for
engine.causal_order, and not something reconciliation.py calls. Promoting
any of this into the live path is a separate, explicit decision for later,
not made here.

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
*deemed* to have drawn from, chosen because it matches how the real cost
flow would work if the events are taken at face value in time order — it
is a stated assumption a reader can disagree with, not a proof.

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
- Every trade's symbol base asset, every deposit's/withdrawal's `.asset`,
  must equal the single `asset` this call is for. A deposit or withdrawal
  in a DIFFERENT asset can never be used to explain a shortfall in this
  one (an earlier draft had no such check at all — a 1 SOL deposit could
  silently "resolve" a 1 BTC shortfall).
- Every trade's `fee_currency` must equal its own symbol's quote currency
  (e.g. BTC/CAD's fee must be in CAD) — catches a trade record whose fee
  is denominated in the base asset itself, which this module's qty-only
  fold does not account for and must not silently mis-fold.
- Every item in `deposits` must have `type == "deposit"`; every item in
  `withdrawals` must have `type == "withdrawal"` — catches a movement
  record placed in the wrong list.
- Two records sharing the same source id (a trade_id or an entry_id) must
  have IDENTICAL payloads to be treated as the same event observed twice
  (safe to collapse). If they differ — e.g. the same deposit id reported
  with two different amounts — that is a genuine conflict, not a
  duplicate, and is rejected rather than resolved by whichever one
  happened to appear first in the input list (an earlier draft picked
  "whichever appeared first," making the verdict silently order-dependent
  — fixed below: same-id-different-payload always raises, regardless of
  input order).

── Completeness ─────────────────────────────────────────────────────────
`AssetMovementResult.complete` requires ALL of:
  1. no unresolved negative-inventory shortfall (`ok`),
  2. an actual `withdrawals` list was supplied (not `None` — `None` means
     "not queried this time," an empty list means "queried, found none"),
  3. an actual `coverage_window` (since, until) was supplied, proving the
     deposits/withdrawals given are scoped to a real, stated window rather
     than "whatever the caller happened to pass,"
  4. an actual `closing_balance` (the exchange's own fresh balance reading
     for the window's end) was supplied AND the fold's `final_qty` matches
     it within `balance_tolerance`.
Missing any of 2-4, or a balance mismatch, always yields `complete=False`
with a reason naming exactly what's missing or mismatched — there is no
way to assert completeness by passing a bare flag.

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
so it is ready to consume that evidence the moment it exists, rather than
hard-coding an assumption that it never will.
"""
from __future__ import annotations

from dataclasses import dataclass

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
class AssetMovementResult:
    ok: bool                              # no unresolved negative-inventory shortfall remains
    unresolved_shortfall_qty: float        # > 0 only when ok is False
    final_qty: float
    unknown_basis_qty_remaining: float     # currently-held qty whose cost basis is still unknown
    sell_attributions: "list[SellAttribution]"
    duplicate_ids_collapsed: "list[str]"   # source ids that appeared more than once (identical payload)
    complete: bool
    reason: str

    def explain(self) -> str:
        if not self.ok:
            return (f"UNRESOLVED — shortfall of {self.unresolved_shortfall_qty:.10f} remains "
                    f"even after applying the supplied deposits/withdrawals; {self.reason}")
        parts = [f"inventory explained (final_qty={self.final_qty:.10f})"]
        if self.unknown_basis_qty_remaining > _QTY_EPS:
            parts.append(f"{self.unknown_basis_qty_remaining:.10f} held with unknown cost basis")
        if not self.complete:
            parts.append("NOT a complete verdict — " + self.reason)
        return "; ".join(parts)


@dataclass
class _Lot:
    qty: float
    cost_per_unit: "float | None"
    source_id: str
    source_type: str   # "trade" | "deposit"


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


def _validate_inputs(
    asset: str, trades: "list[ObservedTrade]", deposits: "list[LedgerMovement]",
    withdrawals: "list[LedgerMovement] | None",
) -> None:
    for t in trades:
        try:
            base, quote = t.symbol.split("/")
        except ValueError:
            raise ValueError(f"trade {t.trade_id} has an unparseable symbol {t.symbol!r}")
        if base != asset:
            raise ValueError(
                f"trade {t.trade_id} is on {t.symbol} (base {base}), not the requested "
                f"asset {asset!r} — refusing to mix assets in one analysis"
            )
        if t.fee_currency and t.fee_currency != quote:
            raise ValueError(
                f"trade {t.trade_id}'s fee_currency {t.fee_currency!r} does not match "
                f"{t.symbol}'s quote currency {quote!r} — this module's quantity-only fold "
                f"cannot correctly account for a fee denominated differently than expected"
            )
    for d in deposits:
        if d.asset != asset:
            raise ValueError(
                f"deposit {d.entry_id} is denominated in {d.asset!r}, not the requested "
                f"asset {asset!r} — an asset it did not receive cannot resolve this shortfall"
            )
        if d.type != "deposit":
            raise ValueError(f"entry {d.entry_id} in `deposits` has type={d.type!r}, expected 'deposit'")
    for w in (withdrawals or []):
        if w.asset != asset:
            raise ValueError(
                f"withdrawal {w.entry_id} is denominated in {w.asset!r}, not the requested "
                f"asset {asset!r}"
            )
        if w.type != "withdrawal":
            raise ValueError(f"entry {w.entry_id} in `withdrawals` has type={w.type!r}, expected 'withdrawal'")


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
    bot-executed BUY or SELL."""
    _validate_inputs(asset, trades, deposits, withdrawals)
    withdrawals_were_supplied = withdrawals is not None

    trades, dup_trade_ids = _dedup_by_id(trades, lambda t: f"trade:{t.trade_id}")
    deposits, dup_deposit_ids = _dedup_by_id(deposits, lambda d: f"deposit:{d.entry_id}")
    withdrawals, dup_withdrawal_ids = _dedup_by_id(
        withdrawals or [], lambda w: f"withdrawal:{w.entry_id}"
    )
    duplicate_ids = dup_trade_ids + dup_deposit_ids + dup_withdrawal_ids

    events = (
        [("trade", t.exchange_timestamp, t) for t in trades]
        + [("deposit", d.timestamp, d) for d in deposits]
        + [("withdrawal", w.timestamp, w) for w in withdrawals]
    )
    events.sort(key=lambda e: e[1])

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

    if not ok:
        reason = (f"{shortfall:.10f} of sold/withdrawn quantity has no explaining trade, deposit, "
                   f"OR withdrawal — still a genuine data-integrity gap, not resolved by the "
                   f"supplied evidence")
        complete = False
    else:
        missing = []
        if not withdrawals_were_supplied:
            missing.append("withdrawal records (None means not queried this time)")
        if coverage_window is None:
            missing.append("a coverage window")
        if closing_balance is None:
            missing.append("a closing balance to check the fold against")
        if missing:
            reason = "completeness requires " + ", ".join(missing) + " — none supplied"
            complete = False
        elif abs(final_qty - closing_balance) > balance_tolerance:
            reason = (f"closing balance mismatch: fold predicts {final_qty:.10f}, exchange "
                      f"reports {closing_balance:.10f} (diff {final_qty - closing_balance:.10f})")
            complete = False
        else:
            reason = ""
            complete = True

    return AssetMovementResult(
        ok=ok, unresolved_shortfall_qty=shortfall, final_qty=final_qty,
        unknown_basis_qty_remaining=unknown_remaining, sell_attributions=sell_attributions,
        duplicate_ids_collapsed=duplicate_ids, complete=complete, reason=reason,
    )
