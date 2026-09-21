"""
Rigorous, read-only Kraken ledger reconciliation audit.

Strengthens scripts/asset_movement_discrepancy_report.py's earlier,
looser pass (which used ccxt's unified fetch_ledger() — a SINGLE,
unproven-complete API call — and float arithmetic, then called a real
0.00000044 BTC discrepancy "rounding" without ever tracing where it
actually came from). This script fixes both gaps:

1. Pagination is verified, not assumed. ccxt's fetch_ledger() calls
   Kraken's Ledgers endpoint exactly once and discards the raw response's
   own `count` field (confirmed by reading ccxt/kraken.py's source
   directly — it never loops on `ofs`). This script instead calls
   `privatePostLedgers` directly, paginates via `ofs`, and only declares
   the result complete once the accumulated entry count equals Kraken's
   own reported `count` — the same discipline
   bot/accounting/engine.retrieve_with_coverage_proof already uses for
   trade history, applied here to the ledger for the first time.

2. Every amount, fee, and balance is parsed as `decimal.Decimal` directly
   from Kraken's raw string fields (never through a float) and every
   ledger row is independently re-summed (`running_balance += amount -
   fee`) and compared, EXACTLY, against Kraken's own reported `balance`
   for that row. A mismatch is reported as a mismatch — this script never
   labels an unexplained difference "rounding," and never widens a
   tolerance to make one disappear.

3. Every `type == 'trade'` ledger row is joined by `refid` to the matching
   `observed_trades.trade_id` row (from trades.db, read-only) so a
   base-currency fee visible ONLY in the ledger (never in
   `fetch_my_trades()`, and therefore never in `observed_trades` or the
   offline analyzer's fold) is surfaced explicitly next to the trade it
   belongs to, rather than surfacing later as an unexplained residual in
   a completely different tool.

Real finding this audit exists to make reproducible (2026-09-21): BTC
ledger entry for trade TDCRFZ-MWTNB-2NVHO6 carries `fee: "0.0000004400"`
denominated in BTC ITSELF — a second, base-currency fee entirely separate
from the 0.03972 CAD fee already recorded in `observed_trades` for the
same trade. `fetch_my_trades()` never reports this fee at all; it is only
visible via the Ledgers endpoint. Once this fee is included, the full
11-entry BTC ledger reconciles to EXACTLY 0 BTC, matching the live
balance exactly — the "0.00000044 BTC residual" reported earlier was
entirely an artifact of the offline analyzer's fold not knowing this
second fee existed, not a real unexplained gap, and NOT "rounding."

Makes NO order call, mutates NO production state, does not touch
logs/HALT, and does not write to trades.db (read-only connection). The
only write is the new audit report file itself.

Usage:
    .venv/bin/python scripts/ledger_reconciliation_audit.py [--assets BTC,SOL] [--db logs/trades.db]
"""
import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

import ccxt  # noqa: E402

from config import cfg  # noqa: E402

_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "logs", "trades.db")

# Kraken's internal asset codes vs. the unified codes this script reports on.
_ASSET_CODE_ALIASES = {
    "BTC": ("XXBT", "XBT", "BTC"),
    "ETH": ("XETH", "ETH"),
    "SOL": ("SOL",),
    "CAD": ("ZCAD", "CAD"),
    "USD": ("ZUSD", "USD"),
}


def _build_exchange():
    cls = getattr(ccxt, cfg.exchange.exchange.lower())
    ex = cls({
        "apiKey": cfg.exchange.api_key, "secret": cfg.exchange.api_secret, "timeout": 15_000,
    })
    ex.load_markets()
    return ex


def _readonly_connect(db_path: str) -> sqlite3.Connection:
    abs_path = os.path.abspath(db_path)
    return sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True)


@dataclass
class LedgerRow:
    id: str
    refid: str
    type: str
    asset: str            # Kraken's own raw asset code (e.g. "XXBT")
    amount: Decimal        # signed, exactly as Kraken reports it
    fee: Decimal            # always >= 0 in Kraken's own convention
    balance: Decimal        # Kraken's own reported running balance after this entry
    time_epoch: float


class CoverageError(RuntimeError):
    """Raised when paginated retrieval cannot be proven complete — never
    silently trusted."""


def fetch_all_ledger_entries(ex, *, page_ofs_step: int = 50) -> "list[LedgerRow]":
    """Pages Kraken's raw Ledgers endpoint via `ofs` until the accumulated
    entry count equals the FIRST response's reported `count` — mirroring
    engine.retrieve_with_coverage_proof's discipline, applied here to the
    ledger for the first time. Raises CoverageError rather than returning
    a result that might be silently incomplete."""
    all_entries: "dict[str, dict]" = {}
    reported_count = None
    ofs = 0
    while True:
        resp = ex.privatePostLedgers({"ofs": ofs})
        result = resp.get("result", {})
        this_count = int(result.get("count")) if result.get("count") is not None else None
        if reported_count is None:
            reported_count = this_count
        elif this_count != reported_count:
            raise CoverageError(
                f"Kraken's reported ledger count drifted mid-pagination "
                f"({reported_count} -> {result.get('count')}) — window unstable, refusing to trust it"
            )
        page = result.get("ledger", {}) or {}
        if not page:
            break
        all_entries.update(page)
        if len(all_entries) >= (reported_count or 0):
            break
        ofs += len(page)
    if reported_count is None or len(all_entries) != reported_count:
        raise CoverageError(
            f"fetched {len(all_entries)} ledger entries but Kraken reports {reported_count} total "
            f"— NOT declaring this complete"
        )

    rows = []
    for entry_id, raw in all_entries.items():
        try:
            rows.append(LedgerRow(
                id=entry_id, refid=str(raw.get("refid") or ""), type=str(raw.get("type") or ""),
                asset=str(raw.get("asset") or ""),
                amount=Decimal(str(raw.get("amount"))), fee=Decimal(str(raw.get("fee"))),
                balance=Decimal(str(raw.get("balance"))), time_epoch=float(raw.get("time")),
            ))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise CoverageError(f"ledger entry {entry_id} has a non-numeric field, refusing to guess: {exc}")
    rows.sort(key=lambda r: (r.time_epoch, r.id))
    return rows


def _rows_for_asset(rows: "list[LedgerRow]", asset: str) -> "list[LedgerRow]":
    codes = _ASSET_CODE_ALIASES.get(asset, (asset,))
    return [r for r in rows if r.asset in codes]


def _load_observed_trade_by_id(conn: sqlite3.Connection, trade_id: str):
    row = conn.execute(
        "SELECT trade_id, symbol, side, amount, fee_cost, fee_currency FROM observed_trades "
        "WHERE trade_id = ?",
        (trade_id,),
    ).fetchone()
    return row


def _cross_currency_fee_check(
    row: LedgerRow, rows_by_refid: "dict[str, list[LedgerRow]]", obs_fee_cost, obs_fee_currency,
) -> str:
    """Before ever calling a base-currency ledger fee a SEPARATE charge from
    the fee_cost already recorded in observed_trades, checks the OTHER
    currency leg of the same real-world trade (same refid). Two concrete,
    checkable outcomes, never asserted without the check itself passing:
    - the other leg's own fee is ~0 AND the base fee's value, converted at
      this trade's own implied price, matches observed_trades.fee_cost
      within 1% -> one real fee, reported in fee_currency terms but
      actually settled in this asset's quantity (single charge, dual
      representation);
    - otherwise -> genuinely inconclusive or a real second charge; never
      claimed as either without the numbers actually supporting it."""
    if row.fee == 0:
        return ""
    other_legs = [r for r in rows_by_refid.get(row.refid, []) if r.id != row.id]
    if len(other_legs) != 1:
        return (f" — this leg has a nonzero fee ({row.fee}) but {len(other_legs)} other leg(s) "
                f"share this refid (expected exactly 1 for a simple spot trade) — inconclusive, "
                f"not asserting either way")
    other = other_legs[0]
    if abs(other.amount) == 0:
        return " — other leg has zero amount, cannot derive an implied price — inconclusive"
    implied_price = abs(other.amount) / abs(row.amount)
    implied_fee_in_other_ccy = row.fee * implied_price
    try:
        obs_fee_cost_dec = Decimal(str(obs_fee_cost))
    except (InvalidOperation, TypeError):
        return " — observed_trades.fee_cost is not numeric, cannot cross-check"
    within_1pct = (
        obs_fee_cost_dec != 0
        and abs(implied_fee_in_other_ccy - obs_fee_cost_dec) / obs_fee_cost_dec <= Decimal("0.01")
    )
    if other.fee == 0 and within_1pct:
        return (
            f" — the other leg ({other.asset}) shows fee=0 and this fee, converted at this "
            f"trade's own implied price ({implied_price:.2f}), is {implied_fee_in_other_ccy:.6f} "
            f"{obs_fee_currency} — matches observed_trades' recorded fee_cost of {obs_fee_cost} "
            f"{obs_fee_currency} within 1%. This strongly suggests ONE real fee, settled in "
            f"{row.asset} quantity and merely reported in {obs_fee_currency} terms in trade "
            f"history — NOT a second, separate charge. (Not proven beyond this arithmetic "
            f"check; no on-chain/exchange-support confirmation was sought.)"
        )
    return (
        f" — other leg ({other.asset}) fee={other.fee}, implied {obs_fee_currency}-equivalent of "
        f"this fee is {implied_fee_in_other_ccy:.6f} vs recorded fee_cost {obs_fee_cost} — does "
        f"NOT cleanly match a single-fee explanation; inconclusive, reported as-is, not asserted "
        f"either way"
    )


def reconcile_asset(
    rows_for_asset: "list[LedgerRow]", conn: sqlite3.Connection, asset: str,
    rows_by_refid: "dict[str, list[LedgerRow]]" = None,
) -> str:
    rows_by_refid = rows_by_refid or {}
    lines = [f"## {asset} — ledger reconciliation ({len(rows_for_asset)} entries)", ""]
    if not rows_for_asset:
        lines.append("No ledger entries for this asset.")
        return "\n".join(lines)

    lines.append("| time (UTC) | id | refid | type | amount | fee | my_running_balance | "
                 "kraken_balance | match | joined observed_trades note |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")

    running = Decimal("0")
    all_match = True
    for row in rows_for_asset:
        running = running + row.amount - row.fee
        match = (running == row.balance)
        all_match = all_match and match
        note = ""
        if row.type == "trade":
            obs = _load_observed_trade_by_id(conn, row.refid)
            if obs is None:
                note = "**no matching observed_trades row for this refid**"
            else:
                _, symbol, side, obs_amount, obs_fee_cost, obs_fee_currency = obs
                qty_match = Decimal(str(obs_amount)) == abs(row.amount)
                note = (f"observed_trades: {side} {obs_amount} {symbol}, "
                        f"fee_cost={obs_fee_cost} {obs_fee_currency}"
                        + ("" if qty_match else " **QTY MISMATCH vs ledger**"))
                note += _cross_currency_fee_check(row, rows_by_refid, obs_fee_cost, obs_fee_currency)
        ts = datetime.fromtimestamp(row.time_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        lines.append(
            f"| {ts} | {row.id} | {row.refid} | {row.type} | {row.amount} | {row.fee} | "
            f"{running} | {row.balance} | {'✓' if match else '✗ MISMATCH'} | {note} |"
        )

    lines.append("")
    final_balance = rows_for_asset[-1].balance
    lines.append(f"- Independently re-summed final balance: **{running}**")
    lines.append(f"- Kraken's own final reported balance: **{final_balance}**")
    if running == final_balance and all_match:
        lines.append("- **Every row reconciles exactly — no unexplained residual anywhere in this chain.**")
    else:
        lines.append(
            "- **UNRECONCILED — at least one row above does not match. This is reported as an "
            "unresolved difference, not rounding, and no tolerance has been applied.**"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", default="BTC,SOL")
    parser.add_argument("--db", default=_DEFAULT_DB)
    args = parser.parse_args()

    ex = _build_exchange()
    conn = _readonly_connect(args.db)

    try:
        all_rows = fetch_all_ledger_entries(ex)
        coverage_note = (f"Pagination verified complete: {len(all_rows)} entries fetched, matching "
                         f"Kraken's own reported total exactly.")
    except CoverageError as exc:
        print(f"COVERAGE NOT ESTABLISHED — refusing to produce a report from unverified data: {exc}")
        return 1

    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = os.path.join(_PROJECT_ROOT, "logs", f"ledger_reconciliation_audit_{date_str}.md")

    sections = [
        "# Ledger reconciliation audit",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} — read-only. "
        f"{coverage_note} All arithmetic below uses `decimal.Decimal` on Kraken's raw string "
        f"fields, never a float. No order placed, no production state written, `logs/HALT` "
        f"untouched.",
        "",
    ]
    rows_by_refid: "dict[str, list]" = {}
    for r in all_rows:
        rows_by_refid.setdefault(r.refid, []).append(r)

    for asset in assets:
        sections.append(reconcile_asset(_rows_for_asset(all_rows, asset), conn, asset, rows_by_refid))
        sections.append("")

    report = "\n".join(sections)
    with open(out_path, "w") as f:
        f.write(report)
    print(report)
    print(f"\nWritten to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
