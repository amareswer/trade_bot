#!/usr/bin/env python3
"""
live_stats.py — print a live trading performance summary table.

Sources (in priority order):
  logs/live_state_BTC_CAD.json   — current cash / position
  logs/live_state_XRP_CAD.json
  logs/trades.db                  — fill history (fills table, qty > 0 rows)
  logs/trade_bot.log              — PositionManager SELL lines (older trades
                                    not yet in the DB are captured here)
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

LOG_DIR   = Path("logs")
DB_PATH   = LOG_DIR / "trades.db"
LOG_PATH  = LOG_DIR / "trade_bot.log"
SYMBOLS   = ["BTC/CAD", "XRP/CAD"]
STATE_FILES = {
    "BTC/CAD": LOG_DIR / "live_state_BTC_CAD.json",
    "XRP/CAD": LOG_DIR / "live_state_XRP_CAD.json",
}
START_CAPITAL = 100.0   # CAD per symbol

# PositionManager SELL log pattern:
# "PositionManager: SELL 0.0006 @ $91433.50 | pnl=$-0.02 | realized=$-0.02 | pos=0.0000"
_PM_SELL_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?"
    r"PositionManager: SELL [\d.]+ @ \$[\d.]+ \| pnl=\$([-\d.]+)"
)


@dataclass
class Fill:
    timestamp: datetime
    pnl: float
    symbol: str
    source: str   # "db" or "log"


def _parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Load fills from trades.db
# ---------------------------------------------------------------------------

def fills_from_db(symbol: str) -> list[Fill]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT timestamp, pnl FROM fills "
        "WHERE symbol = ? AND quantity > 0 AND side = 'SELL' "
        "ORDER BY timestamp",
        (symbol,),
    )
    rows = cur.fetchall()
    conn.close()
    return [Fill(_parse_utc(r["timestamp"]), r["pnl"], symbol, "db") for r in rows]


# ---------------------------------------------------------------------------
# Load fills from log (PositionManager SELL lines, BTC/CAD only for now)
# We attach a symbol by looking at the nearest preceding CANDLE line for
# context; for simplicity we attribute all PM SELL lines to BTC/CAD unless
# the log also carries explicit symbol tags on the SELL line.
# ---------------------------------------------------------------------------

_CANDLE_RE = re.compile(r"CANDLE \[([A-Z]+/CAD)\]")
_PM_ANY_SELL_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?"
    r"PositionManager: SELL [\d.]+ @ \$[\d.]+ \| pnl=\$([-\d.]+)"
)


def fills_from_log() -> dict[str, list[Fill]]:
    """Return {symbol: [Fill, ...]} extracted from PositionManager SELL lines."""
    if not LOG_PATH.exists():
        return {}

    result: dict[str, list[Fill]] = {s: [] for s in SYMBOLS}
    # The log doesn't embed the symbol on PM lines; all live PM SELL lines seen
    # so far belong to BTC/CAD (XRP/CAD has no completed trades yet).
    # We track the most-recently active symbol from CANDLE lines.
    active_symbol = "BTC/CAD"

    with LOG_PATH.open(errors="replace") as fh:
        for line in fh:
            m_candle = _CANDLE_RE.search(line)
            if m_candle:
                active_symbol = m_candle.group(1)

            m_sell = _PM_ANY_SELL_RE.match(line)
            if m_sell:
                ts_str, pnl_str = m_sell.group(1), m_sell.group(2)
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                if active_symbol in result:
                    result[active_symbol].append(
                        Fill(ts, float(pnl_str), active_symbol, "log")
                    )
    return result


# ---------------------------------------------------------------------------
# Merge, deduplicate (DB wins when timestamps are within 60 s of each other)
# ---------------------------------------------------------------------------

def merge_fills(db_fills: list[Fill], log_fills: list[Fill]) -> list[Fill]:
    # Log timestamps are local (EDT = UTC-4); DB timestamps are UTC.
    # Same event shows up ~4 h apart between sources.  Match on same pnl AND
    # within 5 h — robust enough without hardcoding a timezone offset.
    merged: list[Fill] = list(db_fills)
    for lf in log_fills:
        already_in_db = any(
            abs(lf.pnl - df.pnl) < 0.001
            and abs((lf.timestamp - df.timestamp).total_seconds()) < 5 * 3600
            for df in db_fills
        )
        if not already_in_db:
            merged.append(lf)
    merged.sort(key=lambda f: f.timestamp)
    return merged


# ---------------------------------------------------------------------------
# Live-state JSON
# ---------------------------------------------------------------------------

def load_state(symbol: str) -> dict:
    path = STATE_FILES.get(symbol)
    if path and path.exists():
        return json.loads(path.read_text())
    return {}


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def profit_factor(fills: list[Fill]) -> str:
    gross_win  = sum(f.pnl for f in fills if f.pnl > 0)
    gross_loss = sum(abs(f.pnl) for f in fills if f.pnl < 0)
    if gross_loss == 0:
        return "∞" if gross_win > 0 else "N/A"
    return f"{gross_win / gross_loss:.2f}"


def days_since(ts: datetime) -> int:
    return (date.today() - ts.date()).days


def fmt_position(symbol: str, qty: float) -> str:
    if qty == 0:
        return "flat"
    base = symbol.split("/")[0]
    return f"{qty:.6f} {base}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log_fills_by_sym = fills_from_log()

    table_rows = []
    for sym in SYMBOLS:
        db_f  = fills_from_db(sym)
        log_f = log_fills_by_sym.get(sym, [])
        fills = merge_fills(db_f, log_f)

        n       = len(fills)
        wins    = [f for f in fills if f.pnl > 0]
        losses  = [f for f in fills if f.pnl < 0]
        win_pct = f"{100 * len(wins) / n:.0f}%" if n else "N/A"
        pnl     = sum(f.pnl for f in fills)
        pnl_str = f"{pnl:+.2f}" if n else "N/A"
        pf      = profit_factor(fills) if n else "N/A"

        state   = load_state(sym)
        pos     = fmt_position(sym, state.get("position", 0.0))

        if fills:
            last_ts   = max(f.timestamp for f in fills)
            last_str  = f"{days_since(last_ts)}d ago"
        else:
            last_str = "never"

        table_rows.append({
            "Symbol":     sym,
            "Trades":     str(n),
            "Win %":      win_pct,
            "PnL (CAD)":  pnl_str,
            "Pr. Factor": pf,
            "Position":   pos,
            "Last trade": last_str,
        })

    cols = ["Symbol", "Trades", "Win %", "PnL (CAD)", "Pr. Factor", "Position", "Last trade"]
    widths = {c: max(len(c), max(len(r[c]) for r in table_rows)) for c in cols}

    sep = "+" + "+".join("-" * (widths[c] + 2) for c in cols) + "+"
    hdr = "|" + "|".join(f" {c:{widths[c]}} " for c in cols) + "|"

    print(f"\n=== Live Trading Stats  ({date.today()}) ===\n")
    print(sep)
    print(hdr)
    print(sep)
    for r in table_rows:
        line = "|" + "|".join(f" {r[c]:{widths[c]}} " for c in cols) + "|"
        print(line)
    print(sep)

    # Footnote
    btc_fills = merge_fills(fills_from_db("BTC/CAD"), log_fills_by_sym.get("BTC/CAD", []))
    if btc_fills:
        srcs = set(f.source for f in btc_fills)
        if "log" in srcs and "db" in srcs:
            print("\nNote: BTC/CAD trade history spans log + DB (pre-DB trades recovered from log).")
    print(f"DB: {DB_PATH}  |  Log: {LOG_PATH}\n")


if __name__ == "__main__":
    main()
