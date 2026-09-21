"""
Read-only asset-movement discrepancy report.

Runs bot/accounting/asset_movement_analysis.py (the offline, non-production
analyzer — see that module's own docstring) against REAL evidence for each
requested asset: observed trades read from the production trades.db
(read-only), plus fresh, read-only fetch_deposits/fetch_withdrawals/
fetch_ledger/fetch_balance calls against the live Kraken account. Writes a
dated markdown report to logs/ and prints a summary.

Makes NO order call, mutates NO production state, does NOT touch
logs/HALT, and does NOT write to trades.db (opened read-only — see
_readonly_connect, same discipline as scripts/accounting_shadow_report.py).
The only write this script performs is the new report file itself.

Built 2026-09-21 per explicit direction: "obtain read-only ledger evidence
to explain the remaining BTC/SOL balances and establish movement coverage.
Use the analyzer to produce a discrepancy report; don't add more
production accounting logic until that evidence is available." This
script is that tool — it does not feed anything back into bot/main.py or
reconciliation.py.

Usage:
    .venv/bin/python scripts/asset_movement_discrepancy_report.py [--assets BTC,SOL] [--db logs/trades.db]
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

import ccxt  # noqa: E402

from bot.accounting.asset_movement_analysis import analyze_with_asset_movements  # noqa: E402
from bot.accounting.engine import LedgerMovement  # noqa: E402
from bot.accounting.store import ObservedTrade  # noqa: E402
from config import cfg  # noqa: E402

_DEFAULT_DB = os.path.join(_PROJECT_ROOT, "logs", "trades.db")


def _readonly_connect(db_path: str) -> sqlite3.Connection:
    """Same discipline as scripts/accounting_shadow_report.py's own
    helper: mode=ro raises immediately for a missing/schema-less file
    rather than ever being able to create one."""
    abs_path = os.path.abspath(db_path)
    return sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True)


def _build_exchange():
    """Same construction as bot/main.py._build_exchange() — read-only use
    here (fetch_balance/fetch_deposits/fetch_withdrawals/fetch_ledger
    only, never create_order)."""
    cls = getattr(ccxt, cfg.exchange.exchange.lower())
    return cls({
        "apiKey": cfg.exchange.api_key,
        "secret": cfg.exchange.api_secret,
        "timeout": 15_000,
    })


def _load_observed_trades(conn: sqlite3.Connection, asset: str) -> "list[ObservedTrade]":
    rows = conn.execute(
        "SELECT trade_id, order_id, symbol, side, price, amount, cost, fee_cost, "
        "fee_currency, exchange_timestamp, source FROM observed_trades "
        "WHERE symbol LIKE ? ORDER BY exchange_timestamp",
        (f"{asset}/%",),
    ).fetchall()
    return [
        ObservedTrade(
            trade_id=r[0], order_id=r[1], symbol=r[2], side=r[3], price=r[4], amount=r[5],
            cost=r[6], fee_cost=r[7], fee_currency=r[8], exchange_timestamp=r[9], source=r[10],
        )
        for r in rows
    ]


def _fetch_deposits(ex, asset: str) -> "tuple[list[LedgerMovement], str]":
    try:
        raw = ex.fetch_deposits(code=asset) or []
    except Exception as exc:
        return [], f"fetch_deposits({asset!r}) FAILED: {exc}"
    movements = [
        LedgerMovement(
            entry_id=str(d.get("id") or d.get("txid") or ""), type="deposit", asset=asset,
            amount=float(d.get("amount") or 0.0),
            timestamp=datetime.fromtimestamp(d["timestamp"] / 1000, tz=timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ") if d.get("timestamp") else "",
        )
        for d in raw
    ]
    return movements, f"fetch_deposits({asset!r}) OK — {len(movements)} record(s)"


def _fetch_withdrawals(ex, asset: str) -> "tuple[list[LedgerMovement] | None, str]":
    """Returns (None, status) if the call itself failed (genuinely
    'not queried this time' — the analyzer treats None differently from an
    empty-but-real list), or (movements, status) on success, even if
    movements is empty (queried, found none — a real, positive fact)."""
    try:
        raw = ex.fetch_withdrawals(code=asset) or []
    except Exception as exc:
        return None, f"fetch_withdrawals({asset!r}) FAILED: {exc}"
    movements = [
        LedgerMovement(
            entry_id=str(w.get("id") or w.get("txid") or ""), type="withdrawal", asset=asset,
            amount=-abs(float(w.get("amount") or 0.0)),
            timestamp=datetime.fromtimestamp(w["timestamp"] / 1000, tz=timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ") if w.get("timestamp") else "",
        )
        for w in raw
    ]
    return movements, f"fetch_withdrawals({asset!r}) OK — {len(movements)} record(s)"


def _fetch_ledger(ex) -> "tuple[list[dict], str]":
    try:
        raw = ex.fetch_ledger() or []
    except Exception as exc:
        return [], (f"fetch_ledger() FAILED: {exc} — enabling the key's 'Query ledger entries' "
                     f"permission (present but disabled on the 2026-09-20 check) may unlock this "
                     f"without ever touching Withdraw")
    return raw, f"fetch_ledger() OK — {len(raw)} entries"


def _ledger_breakdown_for_asset(ledger_entries: "list[dict]", asset: str) -> str:
    """Groups raw ledger entries for `asset` by Kraken's own `type` field —
    surfaces movement kinds (e.g. 'staking') this analyzer doesn't model at
    all, so a residual explained by one isn't mistaken for an unexplained
    data-integrity gap."""
    relevant = [e for e in ledger_entries if e.get("currency") == asset]
    if not relevant:
        return f"- No ledger entries found for {asset} in this fetch_ledger() call"
    by_type: "dict[str, list[dict]]" = {}
    for e in relevant:
        by_type.setdefault(e.get("type") or "unknown", []).append(e)
    lines = [f"- {len(relevant)} ledger entries for {asset}, by type:"]
    for t, es in sorted(by_type.items()):
        total = sum(float(e.get("amount") or 0.0) for e in es)
        lines.append(f"  - `{t}`: {len(es)} entries, sum {total:.10f}")
        if t not in ("trade", "deposit", "withdrawal"):
            lines.append(
                f"    — **movement type not modeled by this analyzer** (it only understands "
                f"trades/deposits/withdrawals); if this explains a balance-agreement mismatch, "
                f"that's real evidence, not a data-integrity problem"
            )
    return "\n".join(lines)


def _fetch_balance_total(ex, asset: str) -> "float | None":
    try:
        bal = ex.fetch_balance()
        return float((bal.get(asset) or {}).get("total") or 0.0)
    except Exception:
        return None


def build_report(asset: str, conn: sqlite3.Connection, ex, db_path: str) -> str:
    trades = _load_observed_trades(conn, asset)
    deposits, deposits_status = _fetch_deposits(ex, asset)
    withdrawals, withdrawals_status = _fetch_withdrawals(ex, asset)
    ledger_entries, ledger_status = _fetch_ledger(ex)
    fresh_balance = _fetch_balance_total(ex, asset)

    lines = [f"## {asset}", ""]
    lines.append(f"- Observed trades in `{os.path.basename(db_path)}`: {len(trades)}")
    lines.append(f"- {deposits_status}")
    lines.append(f"- {withdrawals_status}")
    lines.append(f"- {ledger_status}")
    lines.append(_ledger_breakdown_for_asset(ledger_entries, asset))
    lines.append(f"- Live fetch_balance total: {fresh_balance}")
    lines.append("")

    all_timestamps = [t.exchange_timestamp for t in trades] + [d.timestamp for d in deposits]
    all_timestamps += [w.timestamp for w in (withdrawals or [])]
    coverage_window = None
    if all_timestamps:
        coverage_window = (min(all_timestamps), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    try:
        result = analyze_with_asset_movements(
            asset, trades, deposits, withdrawals=withdrawals,
            coverage_window=coverage_window, closing_balance=fresh_balance,
        )
    except ValueError as exc:
        lines.append(f"**ANALYSIS REJECTED THE INPUT — a real data problem, not a report bug:** {exc}")
        return "\n".join(lines)

    lines.append(f"- Shortfall resolved (`ok`): **{result.ok}**"
                 + ("" if result.ok else f" — unresolved shortfall {result.unresolved_shortfall_qty:.10f}"))
    lines.append(f"- Final fold quantity: {result.final_qty:.10f}")
    lines.append(f"- Unknown-cost-basis quantity remaining: {result.unknown_basis_qty_remaining:.10f}")
    lines.append(f"- Balance agreement: checked={result.balance_agreement.checked}, "
                 f"agrees={result.balance_agreement.agrees} — {result.balance_agreement.reason}")
    lines.append(f"- History coverage: declared={result.history_coverage.declared}, "
                 f"independently_verified={result.history_coverage.independently_verified} — "
                 f"{result.history_coverage.reason}")
    lines.append(f"- P&L availability: available={result.pnl_availability.available} — "
                 f"{result.pnl_availability.reason}")
    if result.sell_attributions:
        lines.append("")
        lines.append("| trade_id | qty | known | unknown | unmatched | status | pnl (known portion) |")
        lines.append("|---|---|---|---|---|---|---|")
        for a in result.sell_attributions:
            pnl_str = f"{a.realized_pnl_known:.6f}" if a.realized_pnl_known is not None else "—"
            lines.append(f"| {a.trade_id} | {a.qty:.10f} | {a.known_qty:.10f} | "
                         f"{a.unknown_qty:.10f} | {a.unmatched_qty:.10f} | {a.cost_basis_status} | {pnl_str} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", default="BTC,SOL", help="Comma-separated base assets to report on")
    parser.add_argument("--db", default=_DEFAULT_DB, help="Path to the trades.db to read observed trades from")
    args = parser.parse_args()

    conn = _readonly_connect(args.db)
    ex = _build_exchange()

    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = os.path.join(_PROJECT_ROOT, "logs", f"asset_movement_discrepancy_report_{date_str}.md")

    sections = [
        "# Asset movement discrepancy report",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} — read-only, "
        f"produced by `bot/accounting/asset_movement_analysis.py` (offline, not wired into "
        f"the live reconciliation cycle). No order placed, no production state written, "
        f"`logs/HALT` untouched.",
        "",
    ]
    for asset in assets:
        sections.append(build_report(asset, conn, ex, args.db))
        sections.append("")

    report = "\n".join(sections)
    with open(out_path, "w") as f:
        f.write(report)

    print(report)
    print(f"\nWritten to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
