"""
One-time historical migration (design §3.3, implementation item 8).

Links every REAL Kraken trade already covered by an existing `fills` row
(inserted, historically, with a synthetic UUID exec_key — bot/execution/
executor.py's Order.exec_key default) to that row via `trade_fill_links`,
using EXACT conservation matching (quantity + fee sum within tolerance),
never timestamp proximity alone (reconcile_ledger.py's existing matcher
checks only timestamp+side+symbol, no amount check — too weak to trust for
a one-time historical link that can never be un-migrated safely).

Ambiguous rows (no exact match, or more than one disjoint subset matching
equally well) are NOT linked — they are reported for manual review, per
item 8's explicit requirement ("do not silently match ambiguous legacy
rows"). A trade with no matching fills row at all is inserted as a fresh
observed_trades row AND a fresh fills row (via trade_log.log_fill with a
migration-tagged source), since it represents real economics this database
never captured.

Safety: run_migration() takes a db_path explicitly and does not default to
the live trades.db. The companion CLI (migrate_legacy_fills.py, repo root)
copies the live database to a timestamped path first and operates on the
COPY, printing the report; a human reviews it and only then decides whether
to point this at the real trades.db. Never touches logs/HALT, never places
an order, never calls anything in bot/execution or bot/main.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bot.accounting import engine, store
from bot.accounting.engine import MatchResult, ObservedTrade, UnlinkedFill
from bot.data.trade_log import TradeLog


@dataclass
class MigrationReport:
    linked: "list[MatchResult]" = field(default_factory=list)
    blocked: "list[MatchResult]" = field(default_factory=list)
    orphan_trades_inserted: "list[str]" = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"linked={len(self.linked)} blocked={len(self.blocked)} "
            f"orphans_backfilled={len(self.orphan_trades_inserted)}"
        )


def run_migration(
    db_path: str, all_trades: "list[ObservedTrade]", *, symbols: "list[str]",
    window_s: float = 300.0,
) -> MigrationReport:
    """all_trades: the FULL historical trade set for `symbols`, already
    retrieved with a coverage proof (design §3.3 step 1 — the caller is
    responsible for the paginated full-history pull; this function is
    pure matching + linking logic, no network access of its own, so it can
    be tested and re-run offline against a fixed trade list)."""
    store.init_db(db_path)
    conn = store.connect(db_path)
    trade_log = TradeLog(db_path=db_path)
    report = MigrationReport()
    try:
        already_linked = store.already_linked_trade_ids(conn)
        for sym in symbols:
            sym_trades = [t for t in all_trades if t.symbol == sym]
            unlinked_fills_rows = store.unlinked_fills(conn, sym)
            for row in unlinked_fills_rows:
                unlinked = UnlinkedFill.from_fills_row(row, window_s=window_s)
                result = engine.match_legacy_fill(
                    unlinked, sym_trades, already_linked=already_linked,
                )
                if result.blocked:
                    report.blocked.append(result)
                    continue
                matched = [t for t in sym_trades if t.trade_id in result.matched_trade_ids]
                with conn:
                    for t in matched:
                        store.upsert_observed_trade(conn, t)
                        conn.execute(
                            "INSERT OR IGNORE INTO trade_fill_links (trade_id, fill_id, linked_at) "
                            "VALUES (?, ?, ?)",
                            (t.trade_id, result.fill_id, engine.now_iso()),
                        )
                        conn.execute(
                            "UPDATE observed_trades SET ledger_written_at = COALESCE(ledger_written_at, ?) "
                            "WHERE trade_id = ?",
                            (engine.now_iso(), t.trade_id),
                        )
                already_linked = already_linked | set(result.matched_trade_ids)
                report.linked.append(result)

            # Orphans: real trades with no fills row representation at all
            # (never inserted historically — a genuine gap, not just an
            # exec_key mismatch). Backfill both a fresh observed_trades row
            # AND a fresh fills row, tagged as a migration backfill.
            #
            # A trade that was a CANDIDATE in a blocked (ambiguous / no
            # exact match) row must NOT be silently backfilled as an
            # "orphan" here — that would effectively make the ambiguity
            # decision by omission (bug caught while writing this test
            # suite: a trade excluded from linking because its match was
            # ambiguous still sailed through this loop and got its own
            # fresh fills row, contradicting the whole point of blocking).
            # It stays unrepresented until a human resolves the blocked row.
            represented = {tid for r in report.linked for tid in r.matched_trade_ids}
            blocked_candidates = {tid for r in report.blocked for tid in r.candidate_trade_ids}
            for t in sym_trades:
                if t.trade_id in already_linked or t.trade_id in represented:
                    continue
                if t.trade_id in blocked_candidates:
                    continue
                existing_observed = store.get_observed_trade(conn, t.trade_id)
                if existing_observed is not None:
                    # Restart recovery (money-readiness review 2026-09-20,
                    # follow-up finding): the four writes below
                    # (upsert_observed_trade, log_fill, fills_row_by_exec_key,
                    # link_trade_to_fill) are NOT one transaction — log_fill
                    # uses TradeLog's own separate connection. A crash after
                    # upsert_observed_trade succeeds but before
                    # link_trade_to_fill completes used to strand this trade
                    # forever: `get_observed_trade` would find the row on
                    # every retry and `continue` past it, never reaching the
                    # idempotent steps that would actually finish the job.
                    # Every step here (upsert_observed_trade on trade_id,
                    # log_fill on exec_key, link_trade_to_fill on the
                    # (trade_id, fill_id) pair) is independently idempotent —
                    # so a trade THIS migration already created (source=
                    # "migration") is safe to simply resume rather than skip.
                    # A trade observed by a DIFFERENT source (e.g. "live",
                    # still unlinked for its own reasons) is deliberately
                    # left alone — backfilling a synthetic migration fills
                    # row on top of a genuinely live-observed trade would be
                    # a real double-count, not a recovery.
                    if existing_observed.source == "migration":
                        row = store.fills_row_by_exec_key(conn, f"migration:{t.trade_id}")
                        if row is None:
                            trade_log.log_fill(
                                side=t.side.upper(), symbol=t.symbol, quantity=t.amount,
                                price=t.price, exchange="kraken",
                                signal_reason="migration_backfill", risk_decision="n/a",
                                fee_cost=t.fee_cost, fee_currency=t.fee_currency,
                                source="migration_backfill", exec_key=f"migration:{t.trade_id}",
                                timestamp=t.exchange_timestamp, order_id=t.order_id,
                            )
                            row = store.fills_row_by_exec_key(conn, f"migration:{t.trade_id}")
                        if row is not None:
                            store.link_trade_to_fill(conn, t.trade_id, row["id"])
                            report.orphan_trades_inserted.append(t.trade_id)
                    continue
                store.upsert_observed_trade(conn, engine.ObservedTrade(
                    trade_id=t.trade_id, order_id=t.order_id, symbol=t.symbol, side=t.side,
                    price=t.price, amount=t.amount, cost=t.cost, fee_cost=t.fee_cost,
                    fee_currency=t.fee_currency, exchange_timestamp=t.exchange_timestamp,
                    source="migration",
                ))
                trade_log.log_fill(
                    side=t.side.upper(), symbol=t.symbol, quantity=t.amount, price=t.price,
                    exchange="kraken", signal_reason="migration_backfill",
                    risk_decision="n/a", fee_cost=t.fee_cost, fee_currency=t.fee_currency,
                    source="migration_backfill", exec_key=f"migration:{t.trade_id}",
                    timestamp=t.exchange_timestamp, order_id=t.order_id,
                )
                row = store.fills_row_by_exec_key(conn, f"migration:{t.trade_id}")
                if row is not None:
                    store.link_trade_to_fill(conn, t.trade_id, row["id"])
                report.orphan_trades_inserted.append(t.trade_id)
    finally:
        conn.close()
    return report
