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
    fee_currency  TEXT    DEFAULT '',
    exec_key      TEXT
)
"""
# Partial unique index (SQLite): only enforces uniqueness among NON-NULL/
# non-empty exec_key values, so every pre-existing row (exec_key NULL) and
# every ordinary log_fill() call that doesn't pass one are completely
# unaffected — only callers that opt into idempotent replay (2026-09-18
# follow-up review finding: journal replay could duplicate a fill if a
# crash landed between the DB insert succeeding and the ack being
# persisted) get the uniqueness guarantee.
_CREATE_EXEC_KEY_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_exec_key
ON fills(exec_key) WHERE exec_key IS NOT NULL AND exec_key != ''
"""

# 2026-09-19 PASS-5 review finding (P1): a late fee correction (native-stop
# or ordinary order) updates the executor's own cash/fees_paid, but had
# nothing durable for TradeLog/reporting — once the fill's own row was
# already logged and acknowledged, the correction never reached net-of-fee
# reports. A SEPARATE table, not a zero-quantity row in `fills` (log_fill
# itself refuses quantity<=0 — this is deliberately never a fabricated
# trade). adjustment_id is the idempotency key, same discipline as
# fills.exec_key.
_CREATE_FEE_ADJUSTMENTS = """
CREATE TABLE IF NOT EXISTS fee_adjustments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,
    order_id      TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    delta_fee     REAL    NOT NULL,
    fee_currency  TEXT    DEFAULT '',
    adjustment_id TEXT
)
"""
_CREATE_FEE_ADJUSTMENTS_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_fee_adjustments_adjustment_id
ON fee_adjustments(adjustment_id) WHERE adjustment_id IS NOT NULL AND adjustment_id != ''
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
        exec_key:      str             = "",
        timestamp:     Optional[str]   = None,
    ) -> None:
        """
        exec_key (2026-09-18 follow-up review finding): pass a stable,
        globally-unique key to make this insert IDEMPOTENT — a second call
        with the same exec_key is a confirmed no-op (logged, not raised),
        not a duplicate row. Exists for crash-recovery replay (a crash
        between the insert succeeding and the caller's own ack being
        persisted must not duplicate the row on a retried replay); ordinary
        fills leave it empty and are unaffected (multiple empty-exec_key
        rows are explicitly allowed — see the partial unique index).

        timestamp: override the row's timestamp with an ISO string instead
        of "now" — for replaying a fill whose ORIGINAL execution time is
        known (the journal's own filled_at), so a recovered row doesn't
        masquerade as having happened at replay time.
        """
        if quantity <= 0:
            raise ValueError(
                f"log_fill called with quantity={quantity} for {side.upper()} {symbol}"
                f" — would write a phantom row. Caller must provide a real fill quantity."
            )
        if exec_key:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT id FROM fills WHERE exec_key = ?", (exec_key,)
                ).fetchone()
            if existing is not None:
                logger.info(
                    "TradeLog: exec_key=%s already recorded (row id=%s) — "
                    "skipping duplicate insert", exec_key, existing[0],
                )
                return
        value = quantity * price
        ts    = timestamp or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        # Prepend source tag to notes if provided
        if source:
            notes = f"[{source}] {notes}".strip()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO fills
                        (timestamp, side, symbol, quantity, price, value,
                         pnl, exchange, signal_reason, risk_decision, notes,
                         fee_cost, fee_currency, exec_key)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (ts, side.upper(), symbol, quantity, price, value,
                     pnl, exchange, signal_reason, risk_decision, notes,
                     fee_cost, fee_currency, exec_key or None),
                )
        except sqlite3.IntegrityError:
            # A concurrent/retried call raced this exact exec_key between
            # the SELECT check above and this INSERT — the other writer
            # won; treat it identically to the pre-check finding it,
            # rather than raising and losing an otherwise-successful replay.
            logger.info(
                "TradeLog: exec_key=%s inserted concurrently — skipping "
                "duplicate", exec_key,
            )
            return
        logger.debug("TradeLog: %s %s qty=%.6f @ %.2f pnl=%s fee=%.6f %s",
                     side, symbol, quantity, price, pnl, fee_cost, fee_currency)

    def log_fee_adjustment(
        self,
        order_id:      str,
        symbol:        str,
        delta_fee:     float,
        fee_currency:  str           = "",
        adjustment_id: str           = "",
        timestamp:     Optional[str] = None,
    ) -> None:
        """Records a late fee correction (a native-stop or ordinary order
        whose fee was updated/finalized AFTER its original quantity fill
        was already logged) as its own event — never a fabricated zero-
        quantity row in `fills` (log_fill refuses quantity<=0 for exactly
        this reason). adjustment_id makes this idempotent, identical
        discipline to log_fill's exec_key: a second call with the same
        adjustment_id is a confirmed no-op, not a duplicate row."""
        if adjustment_id:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT id FROM fee_adjustments WHERE adjustment_id = ?",
                    (adjustment_id,),
                ).fetchone()
            if existing is not None:
                logger.info(
                    "TradeLog: fee adjustment_id=%s already recorded (row "
                    "id=%s) — skipping duplicate insert", adjustment_id, existing[0],
                )
                return
        ts = timestamp or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO fee_adjustments
                        (timestamp, order_id, symbol, delta_fee, fee_currency, adjustment_id)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (ts, order_id, symbol, delta_fee, fee_currency, adjustment_id or None),
                )
        except sqlite3.IntegrityError:
            logger.info(
                "TradeLog: fee adjustment_id=%s inserted concurrently — "
                "skipping duplicate", adjustment_id,
            )
            return
        logger.debug(
            "TradeLog: fee adjustment order=%s symbol=%s delta=%.6f %s",
            order_id, symbol, delta_fee, fee_currency,
        )

    def total_fee_adjustments(self, symbol: Optional[str] = None) -> float:
        """Sum of every logged fee adjustment (optionally scoped to one
        symbol) — the correction live_comparison.py's net-of-fee math must
        add on top of `fills.fee_cost` to be complete."""
        with self._connect() as conn:
            if symbol:
                row = conn.execute(
                    "SELECT COALESCE(SUM(delta_fee), 0.0) FROM fee_adjustments WHERE symbol = ?",
                    (symbol,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(delta_fee), 0.0) FROM fee_adjustments"
                ).fetchone()
        return float(row[0]) if row else 0.0

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
        Returns trade count, a GROSS win rate, and both gross and net-of-
        fee realized P&L.

        2026-09-18 review finding: this reported ONLY gross `pnl` (the
        stored value never has fees subtracted — see
        PositionManager.on_sell) with no fee awareness at all. `net_pnl`
        here is an exact identity: total realized gross P&L across every
        SELL, minus every fee (BUY entry + SELL exit) actually paid across
        the whole logged history — a true accounting total.

        `win_rate` here is still classified from GROSS pnl (unchanged,
        never claimed otherwise) — it does NOT do the finer per-symbol
        FIFO entry-fee allocation live_comparison.py's _compute_live_metrics()
        implements (2026-09-18 follow-up review finding fixed there: a
        trade can be a real net loss yet show a gross win, which flips
        win-rate/PF classification, not just the total). This method has
        no production caller today (a quick/manual summary only) — use
        live_comparison.py for any decision that depends on a correct NET
        win rate or profit factor.
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
            if "exec_key" not in existing:
                conn.execute("ALTER TABLE fills ADD COLUMN exec_key TEXT")
                logger.info("TradeLog: migrated — added exec_key column")
            conn.execute(_CREATE_EXEC_KEY_INDEX)
            conn.execute(_CREATE_FEE_ADJUSTMENTS)
            conn.execute(_CREATE_FEE_ADJUSTMENTS_INDEX)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, timeout=10)
