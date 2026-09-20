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
"""
from __future__ import annotations

import logging

from bot.accounting import engine, store
from bot.accounting.engine import ExchangeAdapter

logger = logging.getLogger(__name__)


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
    matches = [t for t in coverage.trades if t.order_id == order_id and t.symbol == symbol]
    if not matches:
        logger.info(
            "live_observe: no real trade yet visible for order_id=%s symbol=%s "
            "(coverage.complete=%s) — will be picked up by reconciliation later",
            order_id, symbol, coverage.complete,
        )
        return False
    linked_any = False
    for t in matches:
        store.upsert_observed_trade(conn, t)
        if not store.is_ledger_represented(conn, t.trade_id):
            store.link_trade_to_fill(conn, t.trade_id, fill_id)
            linked_any = True
    if linked_any:
        logger.info(
            "live_observe: linked %d real trade(s) to fill_id=%s (order_id=%s)",
            len(matches), fill_id, order_id,
        )
    return linked_any
