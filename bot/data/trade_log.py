"""
Persistent SQLite trade log.

Creates logs/trades.db on first use.
One row per fill — BUY or SELL.

Usage:
    tl = TradeLog()
    tl.log_fill("BUY", "BTC/CAD", 0.001, 98000.0, 98.0, exchange="kraken", reason="RSI+ADX")
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "trades.db")
_CREATE = """
CREATE TABLE IF NOT EXISTS fills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,
    side          TEXT    NOT NULL,   -- BUY / SELL
    symbol        TEXT    NOT NULL,
    quantity      REAL    NOT NULL,
    price         REAL    NOT NULL,
    value         REAL    NOT NULL,   -- quantity * price
    pnl           REAL,               -- realized P&L on SELL, NULL on BUY
    exchange      TEXT,
    signal_reason TEXT,
    risk_decision TEXT,
    notes         TEXT,
    fee_cost      REAL    DEFAULT 0.0,
    fee_currency  TEXT    DEFAULT ''
)
"""


class TradeLog:
    """Thread-safe (write-serialized) SQLite trade log."""

    def __init__(self, db_path: str = _DEFAULT_DB) -> None:
        self._db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._init_db()
        logger.info("TradeLog ready — %s", self._db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_fill(
        self,
        side:          str,
        symbol:        str,
        quantity:      float,
        price:         float,
        pnl:           Optional[float] = None,
        exchange:      str             = "",
        signal_reason: str             = "",
        risk_decision: str             = "approved",
        notes:         str             = "",
        fee_cost:      float           = 0.0,
        fee_currency:  str             = "",
        source:        str             = "",
    ) -> None:
        if quantity <= 0:
            raise ValueError(
                f"log_fill called with quantity={quantity} for {side.upper()} {symbol}"
                f" — would write a phantom row. Caller must provide a real fill quantity."
            )
        value = quantity * price
        ts    = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        # Prepend source tag to notes if provided
        if source:
            notes = f"[{source}] {notes}".strip()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fills
                    (timestamp, side, symbol, quantity, price, value,
                     pnl, exchange, signal_reason, risk_decision, notes,
                     fee_cost, fee_currency)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (ts, side.upper(), symbol, quantity, price, value,
                 pnl, exchange, signal_reason, risk_decision, notes,
                 fee_cost, fee_currency),
            )
        logger.debug("TradeLog: %s %s qty=%.6f @ %.2f pnl=%s fee=%.6f %s",
                     side, symbol, quantity, price, pnl, fee_cost, fee_currency)

    def recent(self, limit: int = 20) -> list[dict]:
        """Return the most recent fills as a list of dicts.

        2026-09-18 review finding: this used to zip a hardcoded 12-column
        list against a `SELECT *` row that actually has 14 columns
        (fee_cost/fee_currency were added later) — zip() silently truncates
        to the shorter list, so every caller of recent() lost the fee
        columns without any error. Columns are now read from the cursor's
        own description, so this can never drift from the real schema
        again, whatever columns get added next."""
        with self._connect() as conn:
            cursor = conn.execute("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    def summary(self) -> dict:
        """
        Quick performance summary from logged fills.
        Returns trade count, win rate, and both gross and net-of-fee
        realized P&L.

        2026-09-18 review finding: this reported ONLY gross `pnl` (the
        stored value never has fees subtracted — see
        PositionManager.on_sell) with no fee awareness at all. `net_pnl`
        here is an exact identity, not an approximation: total realized
        gross P&L across every SELL, minus every fee (BUY entry + SELL
        exit) actually paid across the whole logged history — a true
        accounting total, even though no single trade's fee is currently
        attributed as "this trade's entry fee" in this flat schema (that
        finer per-trade allocation is a separate, larger effort — see
        live_comparison.py's own per-trade net calculation and its
        docstring for the same caveat).
        """
        with self._connect() as conn:
            sell_rows = conn.execute(
                "SELECT pnl FROM fills WHERE side='SELL' AND pnl IS NOT NULL"
            ).fetchall()
            fee_row = conn.execute(
                "SELECT COALESCE(SUM(fee_cost), 0.0) FROM fills"
            ).fetchone()
        if not sell_rows:
            return {"trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "net_pnl": 0.0}
        pnls        = [r[0] for r in sell_rows]
        wins        = sum(1 for p in pnls if p > 0)
        total_pnl   = sum(pnls)
        total_fees  = fee_row[0] if fee_row else 0.0
        return {
            "trades":    len(pnls),
            "win_rate":  round(wins / len(pnls), 4),
            "total_pnl": round(total_pnl, 4),
            "net_pnl":   round(total_pnl - total_fees, 4),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE)
            # Migrate: add fee columns if this is a pre-existing DB
            existing = {r[1] for r in conn.execute("PRAGMA table_info(fills)")}
            if "fee_cost" not in existing:
                conn.execute("ALTER TABLE fills ADD COLUMN fee_cost REAL DEFAULT 0.0")
                logger.info("TradeLog: migrated — added fee_cost column")
            if "fee_currency" not in existing:
                conn.execute("ALTER TABLE fills ADD COLUMN fee_currency TEXT DEFAULT ''")
                logger.info("TradeLog: migrated — added fee_currency column")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, timeout=10)
