"""
SQLite schema + row-level access for the execution-accounting layer.

Adds three tables to the EXISTING trades.db (bot/data/trade_log.py's
_DEFAULT_DB) — never a separate database, per design §3.1 ("one database,
one transactional boundary"):

  observed_trades   — one row per REAL exchange trade id (the durable
                       "Observed" obligation, design §3 table).
  checkpoints        — one row per committed balance-identity check (§2),
                        recording exactly which trades it covered.
  trade_fill_links    — links an observed_trades.trade_id to the `fills.id`
                        row (in the pre-existing `fills` table) that
                        represents it economically. Used for BOTH ongoing
                        live linking (item 2 of the implementation request)
                        AND one-time historical migration (item 8) — a
                        deliberate generalization of the design's
                        `legacy_links` table (see the design doc §3.1's
                        reference-model schema): rather than giving
                        newly-observed live trades their OWN fresh `fills`
                        row keyed `exec_key=trade_id` (which would create a
                        second, parallel fill-recording path alongside the
                        existing, already-hardened journal-replay writer in
                        bot/execution/live_executor.py / bot/main.py), this
                        implementation always links a real trade_id to the
                        fills row the EXISTING writer already created (via
                        an exact order_id match at fill time, or — for
                        stragglers/migration — the same conservation-based
                        matcher). This means bot/execution/live_executor.py
                        and its ~70 existing tests are never touched by this
                        package. See live_observe.py and migration.py.

fee_adjustments (existing table, bot/data/trade_log.py) is reused as-is for
trade-level fee corrections (design §4), keyed
`adjustment_id=f"{trade_id}:fee_correction:{n}"` — no new fee table here.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "trades.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observed_trades (
    trade_id              TEXT PRIMARY KEY,
    order_id              TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    side                  TEXT NOT NULL,
    price                 REAL NOT NULL,
    amount                REAL NOT NULL,
    cost                  REAL NOT NULL,
    fee_cost              REAL NOT NULL,
    fee_currency          TEXT NOT NULL,
    exchange_timestamp    TEXT NOT NULL,
    observed_at           TEXT NOT NULL,
    checkpoint_id         TEXT,
    ledger_written_at     TEXT,
    source                TEXT NOT NULL DEFAULT 'live'
);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id      TEXT PRIMARY KEY,
    currency_scope     TEXT NOT NULL,
    window_since       TEXT,
    window_until       TEXT NOT NULL,
    balance_after      REAL NOT NULL,
    covered_trade_ids  TEXT NOT NULL,
    committed_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_fill_links (
    trade_id     TEXT NOT NULL,
    fill_id      INTEGER NOT NULL,
    linked_at    TEXT NOT NULL,
    PRIMARY KEY (trade_id, fill_id)
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: str = _DEFAULT_DB) -> None:
    """Idempotent — safe to call on every process start. Creates the three
    accounting tables in the existing trades.db if they don't exist yet.
    Never drops or alters `fills`/`fee_adjustments` (bot/data/trade_log.py
    owns those)."""
    path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def connect(db_path: str = _DEFAULT_DB) -> sqlite3.Connection:
    return sqlite3.connect(os.path.abspath(db_path), timeout=10)


@dataclass
class ObservedTrade:
    trade_id: str
    order_id: str
    symbol: str
    side: str              # "buy" / "sell"
    price: float
    amount: float
    cost: float
    fee_cost: float
    fee_currency: str
    exchange_timestamp: str   # ISO 8601 UTC
    source: str = "live"       # "live" | "migration"


def upsert_observed_trade(conn: sqlite3.Connection, trade: ObservedTrade) -> bool:
    """Insert a newly-observed trade. Idempotent on trade_id (the exchange's
    own id) — a re-observation of an already-known trade with an UNCHANGED
    payload is a silent no-op (returns False). A payload that differs is
    the caller's problem to detect (see engine.detect_fee_correction /
    engine.detect_integrity_anomaly) — this function itself never silently
    overwrites price/amount/side; it only ever inserts fresh or leaves an
    existing row untouched, so a caller that wants to react to a genuine
    correction must check BEFORE calling this, not rely on it to signal one.
    Returns True if a new row was inserted, False if trade_id already existed."""
    existing = conn.execute(
        "SELECT 1 FROM observed_trades WHERE trade_id = ?", (trade.trade_id,)
    ).fetchone()
    if existing is not None:
        return False
    with conn:
        conn.execute(
            """
            INSERT INTO observed_trades
                (trade_id, order_id, symbol, side, price, amount, cost,
                 fee_cost, fee_currency, exchange_timestamp, observed_at, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (trade.trade_id, trade.order_id, trade.symbol, trade.side, trade.price,
             trade.amount, trade.cost, trade.fee_cost, trade.fee_currency,
             trade.exchange_timestamp, _now_iso(), trade.source),
        )
    return True


def commit_observation_batch(
    conn: sqlite3.Connection, *, new_trades: list[ObservedTrade], retrieval_scope: str,
    window_since: Optional[str], window_until: str, checkpoint_id: str,
) -> None:
    """Atomically inserts every newly-observed trade AND the account-wide
    RETRIEVAL-WATERMARK checkpoint row in ONE transaction (money-readiness
    review 2026-09-19, P1 finding: "the account watermark is committed
    before the cycle has reconciled successfully... later failures can
    leave a durable cursor ahead of uncommitted observations"). Either
    every trade in `new_trades` AND the watermark checkpoint are all
    durably persisted together, or NONE of them are — a crash or exception
    partway through can never advance the retrieval cursor past a trade
    that didn't actually get saved.

    This is intentionally a DIFFERENT checkpoint row than commit_checkpoint
    (which records a currency-scope's own balance-identity checkpoint) —
    `retrieval_scope` is the account-wide cursor scope
    (reconciliation.py's `account_scope`, "__account__"), with
    `balance_after=0.0` and `covered_trade_ids=[]` (unused for this row;
    it exists purely so recover_watermark() has something to recover)."""
    with conn:
        for t in new_trades:
            existing = conn.execute(
                "SELECT 1 FROM observed_trades WHERE trade_id = ?", (t.trade_id,)
            ).fetchone()
            if existing is not None:
                continue
            conn.execute(
                """
                INSERT INTO observed_trades
                    (trade_id, order_id, symbol, side, price, amount, cost,
                     fee_cost, fee_currency, exchange_timestamp, observed_at, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (t.trade_id, t.order_id, t.symbol, t.side, t.price, t.amount, t.cost,
                 t.fee_cost, t.fee_currency, t.exchange_timestamp, _now_iso(), t.source),
            )
        conn.execute(
            "INSERT INTO checkpoints (checkpoint_id, currency_scope, window_since, "
            "window_until, balance_after, covered_trade_ids, committed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (checkpoint_id, retrieval_scope, window_since, window_until, 0.0, "[]", _now_iso()),
        )


def get_observed_trade(conn: sqlite3.Connection, trade_id: str) -> Optional[ObservedTrade]:
    row = conn.execute(
        "SELECT trade_id, order_id, symbol, side, price, amount, cost, fee_cost, "
        "fee_currency, exchange_timestamp, source FROM observed_trades WHERE trade_id = ?",
        (trade_id,),
    ).fetchone()
    if row is None:
        return None
    return ObservedTrade(*row)


def load_observed_trades(conn: sqlite3.Connection, symbol: str) -> list[ObservedTrade]:
    rows = conn.execute(
        "SELECT trade_id, order_id, symbol, side, price, amount, cost, fee_cost, "
        "fee_currency, exchange_timestamp, source FROM observed_trades WHERE symbol = ?",
        (symbol,),
    ).fetchall()
    return [ObservedTrade(*row) for row in rows]


def is_ledger_represented(conn: sqlite3.Connection, trade_id: str) -> bool:
    """A trade counts as ledger-represented once it has a trade_fill_links
    row — whether that link was created by the synchronous live-fill
    observer (live_observe.py, exact order_id match) or by migration
    (migration.py, conservation match)."""
    return conn.execute(
        "SELECT 1 FROM trade_fill_links WHERE trade_id = ?", (trade_id,)
    ).fetchone() is not None


def link_trade_to_fill(conn: sqlite3.Connection, trade_id: str, fill_id: int) -> None:
    """Links trade_id to an EXISTING fills.id row and marks the observed
    trade ledger-written, in one transaction — the atomic-write invariant
    design §3.1 demands for the ledger-written obligation. Idempotent:
    linking the same (trade_id, fill_id) pair twice is a no-op."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO trade_fill_links (trade_id, fill_id, linked_at) VALUES (?,?,?)",
            (trade_id, fill_id, _now_iso()),
        )
        conn.execute(
            "UPDATE observed_trades SET ledger_written_at = COALESCE(ledger_written_at, ?) "
            "WHERE trade_id = ?",
            (_now_iso(), trade_id),
        )


def linked_fill_ids_for_trades(conn: sqlite3.Connection, trade_ids: list[str]) -> set[int]:
    if not trade_ids:
        return set()
    placeholders = ",".join("?" for _ in trade_ids)
    rows = conn.execute(
        f"SELECT DISTINCT fill_id FROM trade_fill_links WHERE trade_id IN ({placeholders})",
        trade_ids,
    ).fetchall()
    return {r[0] for r in rows}


def already_linked_trade_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT trade_id FROM trade_fill_links")}


def trade_ids_for_fill(conn: sqlite3.Connection, fill_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT trade_id FROM trade_fill_links WHERE fill_id = ?", (fill_id,)
    )]


def commit_checkpoint(
    conn: sqlite3.Connection, *, currency_scope: str, window_since: Optional[str],
    window_until: str, balance_after: float, covered_trade_ids: list[str],
    checkpoint_id: str,
) -> None:
    """Atomically: insert the checkpoint row AND stamp every covered trade's
    checkpoint_id, in ONE transaction (design §2 / reference-model
    commit_checkpoint). A caller-provided checkpoint_id (rather than
    generating one internally) lets tests inject a failure mid-transaction
    and assert nothing partially persisted, exactly as the reference model's
    fail_after_n_trade_inserts tests did."""
    with conn:
        conn.execute(
            "INSERT INTO checkpoints (checkpoint_id, currency_scope, window_since, "
            "window_until, balance_after, covered_trade_ids, committed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (checkpoint_id, currency_scope, window_since, window_until,
             balance_after, json.dumps(covered_trade_ids), _now_iso()),
        )
        for trade_id in covered_trade_ids:
            conn.execute(
                "UPDATE observed_trades SET checkpoint_id = ? WHERE trade_id = ?",
                (checkpoint_id, trade_id),
            )


def latest_checkpoint(conn: sqlite3.Connection, currency_scope: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT checkpoint_id, currency_scope, window_since, window_until, balance_after, "
        "covered_trade_ids, committed_at FROM checkpoints WHERE currency_scope = ? "
        "ORDER BY window_until DESC LIMIT 1",
        (currency_scope,),
    ).fetchone()
    if row is None:
        return None
    return {
        "checkpoint_id": row[0], "currency_scope": row[1], "window_since": row[2],
        "window_until": row[3], "balance_after": row[4],
        "covered_trade_ids": json.loads(row[5]), "committed_at": row[6],
    }


def recover_watermark(conn: sqlite3.Connection, currency_scope: str) -> Optional[str]:
    """The highest window_until among committed checkpoints for this scope —
    the ISO timestamp a restarted process must resume fetching from. None
    means no checkpoint has ever committed for this scope (fetch from the
    beginning)."""
    row = conn.execute(
        "SELECT window_until FROM checkpoints WHERE currency_scope = ? "
        "ORDER BY window_until DESC LIMIT 1",
        (currency_scope,),
    ).fetchone()
    return row[0] if row else None


def fills_row_by_exec_key(conn: sqlite3.Connection, exec_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM fills WHERE exec_key = ?", (exec_key,))
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def fee_correction_deltas_for_trade(conn: sqlite3.Connection, trade_id: str) -> list[float]:
    """Every fee_adjustments.delta_fee recorded against this trade_id, in
    id (insertion) order — fee_adjustments is the EXISTING table
    (bot/data/trade_log.py), reused as-is; adjustment_id is
    f"{trade_id}:fee_correction:{n}" (design §4)."""
    rows = conn.execute(
        "SELECT delta_fee FROM fee_adjustments WHERE adjustment_id LIKE ? ORDER BY id",
        (f"{trade_id}:fee_correction:%",),
    ).fetchall()
    return [float(r[0]) for r in rows]


def all_fee_correction_adjustment_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT adjustment_id FROM fee_adjustments WHERE adjustment_id LIKE '%:fee_correction:%'"
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def unlinked_fills(conn: sqlite3.Connection, symbol: str, source_exclude: str = "") -> list[dict]:
    """fills rows for `symbol` that have no trade_fill_links row yet — the
    candidate pool migration.py and the reconciliation straggler-matcher
    both draw from. source_exclude optionally filters out notes containing
    a given [source] tag (e.g. already-migrated legacy rows)."""
    cur = conn.execute(
        "SELECT * FROM fills WHERE symbol = ? AND id NOT IN "
        "(SELECT fill_id FROM trade_fill_links) ORDER BY id",
        (symbol,),
    )
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    out = [dict(zip(cols, row)) for row in rows]
    if source_exclude:
        out = [r for r in out if source_exclude not in (r.get("notes") or "")]
    return out
