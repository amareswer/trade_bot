"""
Synchronous, per-fill real-trade-id observation (implementation item 2).

Called from bot/main.py right AFTER an ordinary live fill has already been
logged via the EXISTING trade_log.log_fill() (unchanged — see store.py's
module docstring for why this package never becomes a second fills-table
writer). Looks up the REAL exchange trade id(s) for that order_id — an
EXACT match, not ambiguous, because the order_id is already known — and
links them via trade_fill_links.

This is a best-effort fast path, not the safety net: if the real trade
isn't visible yet (a fetch_my_trades propagation delay), or the call fails
outright (network blip), this simply does nothing and logs at INFO — the
periodic reconciliation cycle (reconciliation.py) will pick up the same
trade later via its own coverage-proof pull and link it through the
straggler matcher (engine.match_legacy_fill) instead. Never raises into the
caller — an accounting-observation failure must never interrupt or reject
an already-confirmed, already-logged fill.

Quantity conservation (accounting review follow-up, 2026-09-20, P1): an
order_id match alone is exact for WHICH order, but not for which of that
order's executions belong to THIS local fill row — an order can be worked
across more than one local delta-fill row (e.g. two partial fills each
logged separately by the executor). The old code linked EVERY visible
execution of the order to whichever single fill_id happened to be observed
first, so a second local row for the same order could never claim its own
execution (reproduced: two quantity-1 executions under one order, one
quantity-1 local fill row, observe_fill(quantity=1) linked BOTH executions
to that one row). Fixed by requiring the FULL remaining (unlinked)
candidate set for this order_id to conserve THIS row's own quantity before
linking anything — the same conservation discipline match_legacy_fill uses,
scoped to an already-exact order_id match rather than a time-window
combinatoric search. If it doesn't conserve (this row is only part of the
order, or visibility is incomplete), this does nothing and leaves the row
for the periodic reconciliation cycle's straggler matcher, which considers
ALL local rows and ALL candidate trades together rather than guessing which
one this fast path happened to see first.
"""
from __future__ import annotations

import logging

from bot.accounting import engine, store
from bot.accounting.engine import ExchangeAdapter

logger = logging.getLogger(__name__)

_QTY_TOLERANCE = 1e-6


def observe_fill(
    exchange: ExchangeAdapter, conn, *, order_id: str, symbol: str, fill_id: int,
    since: "str | None", side: str, quantity: float, page_size: int = 50,
) -> bool:
    """Returns True if at least one real trade was linked to fill_id this
    call, False otherwise (not an error — see module docstring)."""
    if not order_id:
        logger.info("live_observe: no order_id for fill_id=%s — nothing to link", fill_id)
        return False
    try:
        coverage = engine.retrieve_with_coverage_proof(exchange, None, since=since, page_size=page_size)
    except Exception as exc:
        logger.info("live_observe: fetch failed for fill_id=%s order_id=%s: %s — "
                    "leaving to the periodic reconciliation cycle", fill_id, order_id, exc)
        return False
    matches = [
        t for t in coverage.trades
        if t.order_id == order_id and t.symbol == symbol
        and not store.is_ledger_represented(conn, t.trade_id)
    ]
    if not matches:
        logger.info(
            "live_observe: no real trade yet visible for order_id=%s symbol=%s "
            "(coverage.complete=%s) — will be picked up by reconciliation later",
            order_id, symbol, coverage.complete,
        )
        return False
    total_qty = sum(t.amount for t in matches)
    if abs(total_qty - quantity) >= _QTY_TOLERANCE:
        logger.info(
            "live_observe: order_id=%s symbol=%s has %d unlinked execution(s) totalling qty=%s, "
            "which does not conserve against this fill's own qty=%s — this row is likely only PART "
            "of the order (another local fill row owns the rest) or visibility is incomplete; "
            "leaving fill_id=%s for the periodic reconciliation cycle's conservation-based matcher "
            "rather than guessing which execution(s) belong to it",
            order_id, symbol, len(matches), total_qty, quantity, fill_id,
        )
        return False
    with conn:
        for t in matches:
            store.upsert_observed_trade_nocommit(conn, t)
        for t in matches:
            conn.execute(
                "INSERT OR IGNORE INTO trade_fill_links (trade_id, fill_id, linked_at) VALUES (?,?,?)",
                (t.trade_id, fill_id, engine.now_iso()),
            )
            conn.execute(
                "UPDATE observed_trades SET ledger_written_at = COALESCE(ledger_written_at, ?) "
                "WHERE trade_id = ?",
                (engine.now_iso(), t.trade_id),
            )
    logger.info(
        "live_observe: linked %d real trade(s) to fill_id=%s (order_id=%s)",
        len(matches), fill_id, order_id,
    )
    return True
