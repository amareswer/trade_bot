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
Its schema is ALSO declared (byte-identical, both `IF NOT EXISTS`) in this
module's own `_SCHEMA` below — money-readiness review 2026-09-20, P1
finding: fee corrections are now written through the SAME `conn` this
module uses (via commit_observation_batch), atomically with the trade/
watermark commit, rather than through TradeLog's own separate connection
(which could let a correction persist durably even when the trades/
watermark it was detected alongside later rolled back in the SAME cycle).
Declaring the table here too means this module never depends on ordering
— store.init_db() alone is enough to make fee_adjustments writable, with
or without a TradeLog ever having been constructed against this path.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
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
CREATE TABLE IF NOT EXISTS fee_adjustments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,
    order_id      TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    delta_fee     REAL    NOT NULL,
    fee_currency  TEXT    DEFAULT '',
    adjustment_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fee_adjustments_adjustment_id
ON fee_adjustments(adjustment_id) WHERE adjustment_id IS NOT NULL AND adjustment_id != '';
CREATE TABLE IF NOT EXISTS db_identity (
    identity   TEXT NOT NULL,
    created_at TEXT NOT NULL
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


def upsert_observed_trade_nocommit(conn: sqlite3.Connection, trade: ObservedTrade) -> bool:
    """Same insert-if-new logic as upsert_observed_trade, but never opens
    its own `with conn:` — for a caller that needs to compose this into a
    LARGER atomic transaction it already owns (e.g. migration.py linking a
    multi-trade group). Python's sqlite3 `with conn:` is not a nested
    savepoint: calling a with-conn-wrapped helper from inside an outer
    with-conn block commits the OUTER block's pending work early (accounting
    review follow-up, 2026-09-20, P1 — reproduced: a crash right after the
    second of two per-trade upserts in a matched group left only the first
    trade linked, and a retry could never re-consider the second because
    store.unlinked_fills() already excludes any fill with even one link
    row). Callers owning their own transaction must call this instead of
    upsert_observed_trade and wrap the whole group in their own `with conn:`."""
    existing = conn.execute(
        "SELECT 1 FROM observed_trades WHERE trade_id = ?", (trade.trade_id,)
    ).fetchone()
    if existing is not None:
        return False
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


def upsert_observed_trade(conn: sqlite3.Connection, trade: ObservedTrade) -> bool:
    """Insert a newly-observed trade. Idempotent on trade_id (the exchange's
    own id) — a re-observation of an already-known trade with an UNCHANGED
    payload is a silent no-op (returns False). A payload that differs is
    the caller's problem to detect (see engine.detect_fee_correction /
    engine.detect_integrity_anomaly) — this function itself never silently
    overwrites price/amount/side; it only ever inserts fresh or leaves an
    existing row untouched, so a caller that wants to react to a genuine
    correction must check BEFORE calling this, not rely on it to signal one.
    Returns True if a new row was inserted, False if trade_id already existed.

    Standalone convenience wrapper (own transaction) — a caller composing
    this into a larger atomic operation must use upsert_observed_trade_nocommit
    instead, inside its own `with conn:` block; see that function's docstring."""
    with conn:
        return upsert_observed_trade_nocommit(conn, trade)


@dataclass
class FeeCorrectionWrite:
    """A computed-but-not-yet-persisted fee_adjustments row (see
    reconciliation._apply_fee_corrections' own docstring — money-readiness
    review 2026-09-20, P1: this must commit in the SAME transaction as the
    observed trades/watermark it was detected alongside, never through a
    separate connection that could leave it durable on its own)."""
    order_id: str
    symbol: str
    delta_fee: float
    fee_currency: str
    adjustment_id: str


def commit_observation_batch(
    conn: sqlite3.Connection, *, new_trades: list[ObservedTrade], retrieval_scope: str,
    window_since: Optional[str], window_until: str, checkpoint_id: str,
    fee_corrections: "list[FeeCorrectionWrite] | None" = None,
) -> None:
    """Atomically inserts every newly-observed trade, any fee corrections
    detected this cycle, AND the account-wide RETRIEVAL-WATERMARK
    checkpoint row — all in ONE transaction (money-readiness review
    2026-09-19 P1: "the account watermark is committed before the cycle
    has reconciled successfully..."; 2026-09-20 P1: "fee corrections are
    not atomic with observation commits"). Either everything here is
    durably persisted together, or none of it is — a crash or exception
    partway through can never advance the retrieval cursor past a trade
    that didn't actually get saved, and can never leave a fee correction
    durable on its own while the trade/watermark it was detected
    alongside rolled back.

    fee_corrections uses `INSERT OR IGNORE` against fee_adjustments'
    existing partial-unique-index on adjustment_id (idempotent — same
    discipline as TradeLog.log_fee_adjustment, just issued against THIS
    connection/transaction instead of TradeLog's own).

    This is intentionally a DIFFERENT checkpoint row than commit_checkpoint
    (which records a currency-scope's own balance-identity checkpoint) —
    `retrieval_scope` is the account-wide cursor scope
    (reconciliation.py's `account_scope`, "__account__"), with
    `balance_after=0.0` and `covered_trade_ids=[]` (unused for this row;
    it exists purely so recover_watermark() has something to recover)."""
    with conn:
        for t in new_trades:
            upsert_observed_trade_nocommit(conn, t)
        for fc in (fee_corrections or []):
            conn.execute(
                "INSERT OR IGNORE INTO fee_adjustments "
                "(timestamp, order_id, symbol, delta_fee, fee_currency, adjustment_id) "
                "VALUES (?,?,?,?,?,?)",
                (_now_iso(), fc.order_id, fc.symbol, fc.delta_fee, fc.fee_currency, fc.adjustment_id),
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
    linking the same (trade_id, fill_id) pair twice is a no-op.

    Single-trade convenience wrapper around link_trades_to_fill — use THAT
    directly for a multi-trade match (money-readiness review 2026-09-20,
    P1: calling this in a loop, once per trade, let a crash between calls
    leave a fills row PARTIALLY linked — and because store.unlinked_fills()
    excludes any fill with even one trade_fill_links row, that partial
    match becomes permanently invisible to future re-matching, with the
    remaining trade(s) never reconsidered)."""
    link_trades_to_fill(conn, [trade_id], fill_id)


def link_trades_to_fill_nocommit(conn: sqlite3.Connection, trade_ids: "list[str]", fill_id: int) -> None:
    """Same guarded linking logic as link_trades_to_fill, but never opens
    its own `with conn:` — for a caller that needs to compose this into a
    LARGER atomic transaction it already owns (third review pass, 2026-09-20,
    P1: "the ownership guard is not universal" — migration.py and
    live_observe.py both inserted into trade_fill_links directly, via raw
    SQL, to get atomicity with their own upsert_observed_trade_nocommit
    calls — which correctly fixed the FIRST review's atomicity finding but
    silently bypassed the SECOND review's one-fill-owner-per-trade guard
    entirely, since that guard only lived inside link_trades_to_fill's own
    `with conn:`. This is the single guarded primitive both the committing
    wrapper below AND those two callers now share — mirrors
    upsert_observed_trade/upsert_observed_trade_nocommit's split for the
    exact same reason.

    One-fill-owner-per-trade + no-dangling-fill enforcement (accounting
    review, second follow-up pass, 2026-09-20, P1: "duplicate ownership and
    dangling links still pass verification"). A real ALTER TABLE to add a
    UNIQUE constraint on trade_fill_links.trade_id was deliberately not
    done here — this table already has real linked rows in the live
    database, and rebuilding a live financial table's schema is a
    separate, riskier decision than an application-level guard every write
    to this table now goes through. Raises ValueError (not a silent skip)
    if fill_id doesn't exist in `fills`, or if any trade_id is already
    linked to a DIFFERENT fill_id — re-linking to the SAME fill_id remains
    the idempotent no-op link_trades_to_fill's own docstring describes.

    Also requires every trade_id to already exist in observed_trades
    (accounting review, fourth pass, 2026-09-20: "add the missing trade_id
    existence guard — it directly enforces the link API's contract, just
    like the existing fill_id check"). Checked for the WHOLE group before
    any row is inserted for ANY of them — one bad trade_id anywhere in a
    matched group must reject the entire group, not just skip that one
    trade while linking the rest (every real caller treats a matched group
    as one atomic economic claim; a link API that silently linked the
    valid trades and dropped the invalid one would misrepresent what was
    actually matched)."""
    if not trade_ids:
        return
    fill_exists = conn.execute("SELECT 1 FROM fills WHERE id = ?", (fill_id,)).fetchone()
    if fill_exists is None:
        raise ValueError(f"link_trades_to_fill: fill_id={fill_id} does not exist in fills")
    for trade_id in trade_ids:
        trade_exists = conn.execute(
            "SELECT 1 FROM observed_trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        if trade_exists is None:
            raise ValueError(f"link_trades_to_fill: trade_id={trade_id} does not exist in observed_trades")
    for trade_id in trade_ids:
        existing_fill_ids = {
            r[0] for r in conn.execute(
                "SELECT fill_id FROM trade_fill_links WHERE trade_id = ?", (trade_id,)
            )
        }
        if existing_fill_ids and existing_fill_ids != {fill_id}:
            raise ValueError(
                f"link_trades_to_fill: trade_id={trade_id} is already linked to "
                f"fill_id(s) {sorted(existing_fill_ids)} — one fill owner per trade, "
                f"refusing to also link fill_id={fill_id}"
            )
        conn.execute(
            "INSERT OR IGNORE INTO trade_fill_links (trade_id, fill_id, linked_at) VALUES (?,?,?)",
            (trade_id, fill_id, _now_iso()),
        )
        conn.execute(
            "UPDATE observed_trades SET ledger_written_at = COALESCE(ledger_written_at, ?) "
            "WHERE trade_id = ?",
            (_now_iso(), trade_id),
        )


def link_trades_to_fill(conn: sqlite3.Connection, trade_ids: "list[str]", fill_id: int) -> None:
    """Links EVERY trade_id in a matched multi-trade group to the SAME
    EXISTING fills.id row, and marks each ledger-written, all in ONE
    transaction (money-readiness review 2026-09-20, P1 — see
    link_trade_to_fill's docstring for the exact failure this closes: a
    crash after linking only SOME of a matched group's trades left the
    fill permanently un-reconsiderable, since store.unlinked_fills()
    treats "has any link at all" as "already resolved"). Either the WHOLE
    matched set links together, or none of it does — there is no
    partially-linked state a restart could ever observe. Idempotent per
    trade_id (INSERT OR IGNORE), so re-submitting an already-fully-linked
    group is a safe no-op.

    Standalone convenience wrapper (own transaction) — a caller composing
    this into a larger atomic operation must use link_trades_to_fill_nocommit
    instead, inside its own `with conn:` block; see that function's
    docstring for the one-fill-owner-per-trade guard both share."""
    with conn:
        link_trades_to_fill_nocommit(conn, trade_ids, fill_id)


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


def read_db_identity(conn: sqlite3.Connection) -> Optional[str]:
    """Pure SELECT — never creates anything. This is the read-only half
    used by scripts/accounting_shadow_report.py (accounting review,
    seventh pass, 2026-09-21, P1: "status evidence is not tied to the
    inspected database" — the report must be able to verify a persisted
    cycle-status file actually corresponds to THIS database, not just
    trust that whatever --db/--status paths were passed happen to agree)."""
    row = conn.execute("SELECT identity FROM db_identity LIMIT 1").fetchone()
    return row[0] if row else None


def get_or_create_db_identity(conn: sqlite3.Connection) -> str:
    """The write-capable half — used only by bot/main.py (a real writer),
    never by the read-only shadow report. Idempotent: a db_identity row,
    once created, is permanent for the lifetime of this database file —
    established the first time this function runs against it, never
    regenerated or overwritten afterward."""
    existing = read_db_identity(conn)
    if existing is not None:
        return existing
    identity = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO db_identity (identity, created_at) VALUES (?, ?)",
            (identity, _now_iso()),
        )
    return identity
