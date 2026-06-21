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
    notes         TEXT
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
    ) -> None:
        value = quantity * price
        ts    = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fills
                    (timestamp, side, symbol, quantity, price, value,
                     pnl, exchange, signal_reason, risk_decision, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (ts, side.upper(), symbol, quantity, price, value,
                 pnl, exchange, signal_reason, risk_decision, notes),
            )
        logger.debug("TradeLog: %s %s qty=%.6f @ %.2f pnl=%s", side, symbol, quantity, price, pnl)

    def recent(self, limit: int = 20) -> list[dict]:
        """Return the most recent fills as a list of dicts."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        cols = [
            "id", "timestamp", "side", "symbol", "quantity", "price",
            "value", "pnl", "exchange", "signal_reason", "risk_decision", "notes",
        ]
        return [dict(zip(cols, row)) for row in rows]

    def summary(self) -> dict:
        """
        Quick performance summary from logged fills.
        Returns trade count, win rate, total realized P&L.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pnl FROM fills WHERE side='SELL' AND pnl IS NOT NULL"
            ).fetchall()
        if not rows:
            return {"trades": 0, "win_rate": 0.0, "total_pnl": 0.0}
        pnls      = [r[0] for r in rows]
        wins      = sum(1 for p in pnls if p > 0)
        total_pnl = sum(pnls)
        return {
            "trades":    len(pnls),
            "win_rate":  round(wins / len(pnls), 4),
            "total_pnl": round(total_pnl, 4),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, timeout=10)
