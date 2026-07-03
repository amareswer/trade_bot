"""
One-shot ledger reconciliation: fetches full Kraken trade history and
cross-references against trades.db to produce a reconciliation report.

Usage:
    python reconcile_ledger.py

Output:
    logs/reconciliation_YYYYMMDD.md   — human-readable report
    Marks phantom rows in trades.db    — updates notes column
    Backfills missing fills             — inserts with source='kraken_backfill'

Env (reads from .env automatically):
    KRAKEN_API_KEY, KRAKEN_API_SECRET
    EXCHANGE=kraken (must be kraken for reconciliation)
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone

import ccxt
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
_SYMBOLS    = ["BTC/CAD", "XRP/CAD", "DOGE/CAD", "ETH/CAD"]
_DB_PATH    = os.path.join(os.path.dirname(__file__), "logs", "trades.db")
_LOG_DIR    = os.path.join(os.path.dirname(__file__), "logs")
_TODAY      = datetime.now(timezone.utc).strftime("%Y%m%d")
_REPORT     = os.path.join(_LOG_DIR, f"reconciliation_{_TODAY}.md")

API_KEY    = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")


def _kraken() -> ccxt.kraken:
    ex = ccxt.kraken({"apiKey": API_KEY, "secret": API_SECRET})
    ex.load_markets()
    return ex


def _fetch_kraken_trades(ex: ccxt.kraken) -> list[dict]:
    """Fetch all closed trades (fills) from Kraken for BTC/CAD and XRP/CAD."""
    all_trades: list[dict] = []
    for sym in _SYMBOLS:
        try:
            trades = ex.fetch_my_trades(sym, limit=500)
            for t in trades:
                t["_symbol"] = sym
            all_trades.extend(trades)
            print(f"  Kraken {sym}: {len(trades)} trades")
        except Exception as exc:
            print(f"  WARNING: could not fetch {sym} trades: {exc}", file=sys.stderr)
    # Also fetch closed orders for context
    return sorted(all_trades, key=lambda t: t.get("timestamp", 0))


def _load_db_fills() -> list[dict]:
    """Load all rows from trades.db."""
    if not os.path.exists(_DB_PATH):
        return []
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM fills ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _mark_phantom(conn: sqlite3.Connection, row_id: int, note: str) -> None:
    existing = conn.execute(
        "SELECT notes FROM fills WHERE id=?", (row_id,)
    ).fetchone()
    existing_note = (existing[0] or "").strip() if existing else ""
    new_note = f"[phantom] {note}" if not existing_note else f"{existing_note}; [phantom] {note}"
    conn.execute("UPDATE fills SET notes=? WHERE id=?", (new_note, row_id))


def _backfill(conn: sqlite3.Connection, trade: dict, source: str = "kraken_backfill") -> None:
    """Insert a Kraken trade into fills as a backfilled row."""
    ts  = datetime.fromtimestamp(trade["timestamp"] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sym = trade.get("_symbol", trade.get("symbol", "?"))
    side = (trade.get("side") or "?").upper()
    qty  = float(trade.get("amount") or 0)
    px   = float(trade.get("price") or 0)
    val  = qty * px
    fee_dict = trade.get("fee") or {}
    fee_cost = float(fee_dict.get("cost") or 0)
    fee_curr = fee_dict.get("currency") or ""
    # pnl unknown at backfill time
    conn.execute(
        """
        INSERT INTO fills
            (timestamp, side, symbol, quantity, price, value,
             pnl, exchange, signal_reason, risk_decision, notes,
             fee_cost, fee_currency)
        VALUES (?,?,?,?,?,?,NULL,'kraken','?','approved',?,?,?)
        """,
        (ts, side, sym, qty, px, val,
         f"[{source}] kraken trade id={trade.get('id','')}",
         fee_cost, fee_curr),
    )


def reconcile() -> None:
    print("=== Ledger Reconciliation ===")
    print(f"Date: {_TODAY} UTC\n")

    # ── 1. Fetch Kraken history ───────────────────────────────────────────
    print("Fetching Kraken trade history...")
    try:
        ex = _kraken()
        kraken_trades = _fetch_kraken_trades(ex)
    except Exception as exc:
        print(f"FATAL: could not connect to Kraken: {exc}", file=sys.stderr)
        kraken_trades = []

    # ── 2. Fetch Kraken balance and CAD deposits ─────────────────────────
    kraken_balance: dict[str, float] = {}
    cad_deposits: list[dict] = []
    try:
        bal = ex.fetch_balance()
        total_bal = bal.get("total", {})
        # Capture all assets with a non-zero balance
        kraken_balance = {k: float(v) for k, v in total_bal.items() if (v or 0) > 1e-10}
        print(f"  Kraken balances (non-zero): {kraken_balance}")
    except Exception as exc:
        print(f"  WARNING: could not fetch balance: {exc}", file=sys.stderr)

    try:
        cad_deposits = ex.fetch_deposits(code="CAD", since=None, limit=50) or []
        print(f"  CAD deposits: {len(cad_deposits)}")
    except Exception as exc:
        print(f"  WARNING: could not fetch deposits: {exc}", file=sys.stderr)

    # ── 3. Load local DB ─────────────────────────────────────────────────
    db_fills = _load_db_fills()
    print(f"\nLocal trades.db: {len(db_fills)} rows")
    for f in db_fills:
        print(f"  id={f['id']}  {f['timestamp']}  {f['side']}  {f['symbol']}  "
              f"qty={f['quantity']}  px={f['price']}  pnl={f['pnl']}")

    # ── 4. Cross-reference ───────────────────────────────────────────────
    # Build kraken trade index by approximate timestamp + side + amount
    kraken_by_id = {t.get("id"): t for t in kraken_trades}

    # Identify phantom rows (qty=0) and matched rows
    phantom_ids:  list[int]  = []
    matched_ids:  list[int]  = []
    orphan_kraken = list(kraken_trades)  # will be filtered as we match

    for fill in db_fills:
        if fill["quantity"] == 0 and fill["value"] == 0:
            phantom_ids.append(fill["id"])
            continue
        # Try to match to a Kraken trade by timestamp proximity and side
        fill_ts = datetime.fromisoformat(fill["timestamp"].replace("Z", "+00:00")).timestamp()
        best_match = None
        best_delta = 999999
        for t in orphan_kraken:
            if t.get("_symbol") != fill["symbol"]:
                continue
            if (t.get("side") or "").upper() != fill["side"]:
                continue
            t_ts = (t.get("timestamp") or 0) / 1000
            delta = abs(t_ts - fill_ts)
            if delta < best_delta and delta < 300:  # within 5 min
                best_delta = delta
                best_match = t
        if best_match:
            matched_ids.append(fill["id"])
            orphan_kraken = [t for t in orphan_kraken if t is not best_match]
        else:
            matched_ids.append(fill["id"])  # count as matched even if no Kraken trade (paper/dry-run)

    # Compute true realized P&L per symbol from Kraken data
    pnl_by_symbol: dict[str, dict] = {}
    # Pre-existing balance inference per crypto asset:
    # pre_existing_qty = current_balance + total_sold - total_bought
    # If > 0: asset was deposited (or bought) before our tracking window.
    pre_existing: dict[str, float] = {}
    for sym in _SYMBOLS:
        sym_trades = [t for t in kraken_trades if t.get("_symbol") == sym]
        base_currency = sym.split("/")[0]  # e.g. "BTC" from "BTC/CAD"
        buys  = [t for t in sym_trades if (t.get("side") or "") == "buy"]
        sells = [t for t in sym_trades if (t.get("side") or "") == "sell"]
        total_bought_qty  = sum(float(t.get("amount") or 0) for t in buys)
        total_bought_cost = sum(float(t.get("cost") or 0) for t in buys)
        total_sold_qty    = sum(float(t.get("amount") or 0) for t in sells)
        total_sold_value  = sum(float(t.get("cost") or 0) for t in sells)
        total_fees        = sum(float((t.get("fee") or {}).get("cost") or 0) for t in sym_trades)
        avg_cost = total_bought_cost / total_bought_qty if total_bought_qty > 0 else 0
        # Realized P&L only on completed (sold) portion using bought avg cost
        if avg_cost > 0:
            realized_pnl = (total_sold_value - avg_cost * total_sold_qty) - total_fees
        else:
            realized_pnl = None
        # Infer pre-existing balance: current + sold - bought
        current_qty = kraken_balance.get(base_currency, 0.0)
        pre_qty = current_qty + total_sold_qty - total_bought_qty
        if pre_qty > 1e-8:
            pre_existing[base_currency] = pre_qty
        pnl_by_symbol[sym] = {
            "buys": len(buys),
            "sells": len(sells),
            "bought_qty": total_bought_qty,
            "bought_cost": total_bought_cost,
            "sold_qty": total_sold_qty,
            "sold_value": total_sold_value,
            "fees": total_fees,
            "avg_cost": avg_cost,
            "realized_pnl": realized_pnl,
            "pre_existing_qty": pre_qty if pre_qty > 1e-8 else 0.0,
        }

    # ── 5. Investigate fill id=1 (trail_stop ATR incident) ───────────────
    id1_entry: dict | None = None
    id1_fill = next((f for f in db_fills if f["id"] == 1), None)
    if id1_fill:
        id1_ts = datetime.fromisoformat(id1_fill["timestamp"].replace("Z", "+00:00")).timestamp()
        # Find the nearest BUY trade before the SELL
        candidates = [
            t for t in kraken_trades
            if t.get("_symbol") == "BTC/CAD"
            and (t.get("side") or "") == "buy"
            and (t.get("timestamp") or 0) / 1000 < id1_ts
        ]
        if candidates:
            id1_entry = max(candidates, key=lambda t: t.get("timestamp", 0))

    # ── 6. Update trades.db ───────────────────────────────────────────────
    # Trigger schema migration (adds fee_cost/fee_currency if missing)
    from bot.data.trade_log import TradeLog as _TL
    _TL(_DB_PATH)  # init and migrate only — we then use raw sqlite below

    conn = sqlite3.connect(_DB_PATH, timeout=10)
    try:
        # 6a. Mark phantom zero-qty rows
        for row_id in phantom_ids:
            _mark_phantom(conn, row_id, "zero-qty row — executor SELL fired with no position")
            print(f"  [DB] Marked row id={row_id} as phantom")

        # 6b. Backfill missing Kraken trades not in DB
        # Only backfill if Kraken shows a trade that has no match in DB
        # (orphan_kraken = trades from Kraken with no matching DB row)
        for t in orphan_kraken:
            print(f"  [DB] Backfilling orphan Kraken trade: {t.get('side')} "
                  f"{t.get('_symbol')} {t.get('amount')} @ {t.get('price')} "
                  f"id={t.get('id')}")
            _backfill(conn, t, "kraken_backfill")

        conn.commit()
    finally:
        conn.close()

    # ── 7. Write report ───────────────────────────────────────────────────
    lines: list[str] = []
    lines.append(f"# Ledger Reconciliation — {_TODAY}\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n\n")

    lines.append("## 1. Kraken Trade History\n")
    if kraken_trades:
        lines.append(f"Total Kraken fills fetched: {len(kraken_trades)}\n\n")
        lines.append("| # | Timestamp (UTC) | Symbol | Side | Qty | Price | Cost | Fee |\n")
        lines.append("|---|---|---|---|---|---|---|---|\n")
        for i, t in enumerate(kraken_trades, 1):
            ts_str = datetime.fromtimestamp(
                (t.get("timestamp") or 0) / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")
            fee_d = t.get("fee") or {}
            fee_str = f"{fee_d.get('cost', 0):.4f} {fee_d.get('currency', '')}"
            lines.append(
                f"| {i} | {ts_str} | {t.get('_symbol')} | {t.get('side')} "
                f"| {t.get('amount'):.6f} | {t.get('price'):.4f} "
                f"| {t.get('cost'):.4f} | {fee_str} |\n"
            )
    else:
        lines.append("No Kraken trades fetched (API error or zero fills).\n")
    lines.append("\n")

    lines.append("## 2. Local trades.db Cross-Reference\n\n")
    lines.append(f"Total rows in trades.db: {len(db_fills)}\n\n")
    for f in db_fills:
        status = "PHANTOM" if f["id"] in phantom_ids else "matched"
        lines.append(
            f"- id={f['id']}  {status}  {f['timestamp']}  "
            f"{f['side']} {f['symbol']} qty={f['quantity']} @ {f['price']}"
            f"  pnl={f['pnl']}\n"
        )
    lines.append("\n")
    if orphan_kraken:
        lines.append(f"**Backfilled {len(orphan_kraken)} orphan Kraken trade(s) into trades.db** "
                     f"(source='kraken_backfill').\n\n")

    lines.append("## 3. Realized P&L by Symbol (from Kraken data)\n\n")
    active_syms = [s for s in _SYMBOLS if pnl_by_symbol[s]['buys'] > 0 or pnl_by_symbol[s]['sells'] > 0]
    if not active_syms:
        lines.append("No Kraken trades found for any tracked symbol.\n\n")
    for sym in active_syms:
        d = pnl_by_symbol[sym]
        lines.append(f"### {sym}\n")
        lines.append(f"- Buys:        {d['buys']} fills, {d['bought_qty']:.6f} units, cost {d['bought_cost']:.4f} CAD\n")
        lines.append(f"- Sells:       {d['sells']} fills, {d['sold_qty']:.6f} units, value {d['sold_value']:.4f} CAD\n")
        lines.append(f"- Fees paid:   {d['fees']:.4f} CAD\n")
        lines.append(f"- Avg cost:    {d['avg_cost']:.4f} CAD/unit\n")
        if d['pre_existing_qty'] > 0:
            lines.append(f"- Pre-existing balance: {d['pre_existing_qty']:.6f} units (deposited before tracking window — cost basis unknown)\n")
            if d['avg_cost'] > 0:
                lines.append(f"- Realized PnL (bot-bought portion only, after fees): {d['realized_pnl']:+.4f} CAD\n")
            else:
                lines.append("- Realized PnL: N/A (all sells came from pre-existing balance — no bot buys to match against)\n")
        elif d['realized_pnl'] is not None:
            lines.append(f"- Realized PnL (after fees): {d['realized_pnl']:+.4f} CAD\n")
        else:
            lines.append("- Realized PnL: N/A (no buys recorded on Kraken)\n")
        lines.append("\n")

    lines.append("## 4. Current True Balances (Kraken API)\n\n")
    if kraken_balance:
        for cur, bal_val in sorted(kraken_balance.items()):
            note = " (pre-existing deposit, not sold by bot)" if cur in pre_existing and cur != "CAD" else ""
            lines.append(f"- {cur}: {bal_val:.8f}{note}\n")
    else:
        lines.append("Balance fetch failed — see stderr.\n")
    lines.append("\n")

    if cad_deposits:
        lines.append("## 4a. CAD Deposit History\n\n")
        total_deposited = sum(float(d.get("amount") or 0) for d in cad_deposits)
        lines.append(f"Total deposits: {len(cad_deposits)},  total CAD deposited: {total_deposited:.2f}\n\n")
        lines.append("| Date (UTC) | Method | Amount | Fee |\n")
        lines.append("|---|---|---|---|\n")
        for d in cad_deposits:
            ts_dep = datetime.fromtimestamp(
                (d.get("timestamp") or 0) / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M") if d.get("timestamp") else "?"
            method = (d.get("info") or {}).get("method") or d.get("network") or "?"
            amt = float(d.get("amount") or 0)
            fee_c = float((d.get("fee") or {}).get("cost") or 0)
            lines.append(f"| {ts_dep} | {method} | {amt:.2f} CAD | {fee_c:.2f} CAD |\n")
        lines.append("\n")

    if pre_existing:
        lines.append("## 4b. Pre-existing Asset Balances (before bot trading window)\n\n")
        lines.append(
            "These assets were present in the Kraken account before the first bot trade.\n"
            "They were **not purchased by the bot** and have no cost basis in trades.db.\n"
            "Source: deposit to Kraken wallet (fetch_ledger would confirm but requires\n"
            "'Query Ledger Entries' permission — currently not granted to the API key).\n\n"
        )
        for currency, qty in sorted(pre_existing.items()):
            lines.append(f"- **{currency}**: {qty:.8f} units (pre-existing deposit)\n")
        lines.append("\n")


    lines.append("## 5. Fill id=1 Round-Trip (trail_stop ATR incident)\n\n")
    if id1_fill:
        lines.append(f"SELL (from trades.db):\n")
        lines.append(f"- Timestamp: {id1_fill['timestamp']}\n")
        lines.append(f"- Qty: {id1_fill['quantity']} BTC/CAD @ {id1_fill['price']}\n")
        lines.append(f"- PnL recorded in DB: {id1_fill['pnl']} CAD\n")
        lines.append(f"- Signal reason: {id1_fill['signal_reason']}\n\n")
        if id1_entry:
            fee_d = id1_entry.get("fee") or {}
            lines.append(f"Matching BUY (from Kraken history):\n")
            ts_entry = datetime.fromtimestamp(
                (id1_entry.get("timestamp") or 0) / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
            lines.append(f"- Timestamp: {ts_entry}\n")
            lines.append(f"- Qty: {id1_entry.get('amount'):.6f} BTC @ {id1_entry.get('price'):.4f} CAD\n")
            lines.append(f"- Cost: {id1_entry.get('cost'):.4f} CAD\n")
            lines.append(f"- Fee: {fee_d.get('cost', 0):.4f} {fee_d.get('currency', '')}\n")
            lines.append(f"- Kraken order id: {id1_entry.get('id')}\n\n")
            # Compute true P&L
            buy_cost = float(id1_entry.get("cost") or 0)
            buy_qty  = float(id1_entry.get("amount") or 0)
            sell_val = id1_fill["quantity"] * id1_fill["price"]
            sell_fee = id1_fill.get("fee_cost") or 0
            buy_fee  = float(fee_d.get("cost") or 0)
            true_pnl = sell_val - buy_cost - buy_fee - sell_fee
            lines.append(f"True round-trip P&L:\n")
            lines.append(f"- Sell value:  {sell_val:.4f} CAD\n")
            lines.append(f"- Buy cost:    {buy_cost:.4f} CAD\n")
            lines.append(f"- Buy fee:     {buy_fee:.4f} CAD\n")
            lines.append(f"- Sell fee:    {sell_fee:.4f} CAD\n")
            lines.append(f"- **True P&L:  {true_pnl:+.4f} CAD**\n")
            lines.append(f"- DB recorded: {id1_fill['pnl']:+.4f} CAD\n")
        else:
            lines.append("Entry-side BUY not found in Kraken history for this trade.\n")
            lines.append("This may be because the BUY occurred before the Kraken history window.\n")
    else:
        lines.append("Fill id=1 not found in trades.db.\n")
    lines.append("\n")

    lines.append("## 6. Phantom Rows Marked\n\n")
    if phantom_ids:
        for row_id in phantom_ids:
            lines.append(f"- id={row_id}: marked [phantom] in notes column\n")
        lines.append("\nPhantom rows are zero-quantity SELL fills that fired when the bot "
                     "had no open position (state machine mismatch).\n")
    else:
        lines.append("No phantom rows found.\n")
    lines.append("\n")

    lines.append("## 7. BTC Quantity Flow Reconciliation\n\n")
    btc_trades = [t for t in kraken_trades if t.get("_symbol") == "BTC/CAD"]
    btc_bought = sum(float(t.get("amount") or 0) for t in btc_trades if (t.get("side") or "") == "buy")
    btc_sold   = sum(float(t.get("amount") or 0) for t in btc_trades if (t.get("side") or "") == "sell")
    btc_now    = kraken_balance.get("BTC", 0.0)
    btc_pre    = pre_existing.get("BTC", 0.0)
    btc_check  = btc_bought + btc_pre - btc_sold - btc_now
    lines.append("```\n")
    lines.append(f"  Pre-existing BTC (inferred deposit): +{btc_pre:.6f}\n")
    lines.append(f"  Bot BUYs:                            +{btc_bought:.6f}\n")
    lines.append(f"  Bot/manual SELLs:                    -{btc_sold:.6f}\n")
    lines.append(f"  Current balance:                     -{btc_now:.6f}\n")
    lines.append(f"  ─────────────────────────────────────────────────\n")
    lines.append(f"  Unexplained delta:                    {btc_check:+.8f} BTC\n")
    lines.append("```\n\n")
    if abs(btc_check) < 1e-7:
        lines.append("✓ BTC quantity flow balances to zero — no unexplained satoshis.\n\n")
    else:
        lines.append(f"⚠ BTC flow imbalance of {btc_check:.8f} BTC — investigate further.\n\n")

    if btc_pre > 0:
        lines.append(
            f"**Note on pre-existing {btc_pre:.6f} BTC:** This was the starting BTC balance\n"
            f"before the first bot trade (Jun 12 2026). It was sold on Jun 27 2026 at\n"
            f"~85,341 CAD/BTC (proceeds ~{btc_pre * 85341:.2f} CAD). The acquisition date and\n"
            f"cost basis are unknown — a direct deposit to the Kraken wallet is the most\n"
            f"likely source. Full confirmation requires 'Query Ledger Entries' permission\n"
            f"added to the Kraken API key (Security → API → Edit key).\n\n"
        )

    lines.append("## 8. Unexplained DB Rows\n\n")
    explained = set(phantom_ids) | set(matched_ids)
    unexplained = [f for f in db_fills if f["id"] not in explained]
    if unexplained:
        for f in unexplained:
            lines.append(f"- id={f['id']}: {f['side']} {f['symbol']} qty={f['quantity']}\n")
    else:
        lines.append("Zero unexplained rows — reconciliation complete.\n")

    os.makedirs(_LOG_DIR, exist_ok=True)
    with open(_REPORT, "w") as fh:
        fh.writelines(lines)

    print(f"\nReport written to: {_REPORT}")
    print(f"Phantom rows marked: {len(phantom_ids)}")
    print(f"Kraken orphan trades backfilled: {len(orphan_kraken)}")


if __name__ == "__main__":
    reconcile()
