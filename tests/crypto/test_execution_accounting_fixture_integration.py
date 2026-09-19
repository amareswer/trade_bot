"""
Controlled integration testing against REAL exchange-response fixtures —
the milestone agreed after four rounds of design/prototype review on
execution_accounting_reference_model.py (see
CRYPTO_BOT_EXECUTION_ACCOUNTING_DESIGN_2026-09-19.md and its review files),
itself corrected after CRYPTO_BOT_FIXTURE_INTEGRATION_REVIEW_2026-09-19.md.

These tests load `fixtures/kraken_reconciliation_2026_07_03.json` — a
RECONSTRUCTED (not a saved raw API response) fixture built from two real
historical sources already in this repository: logs/reconciliation_
20260703.md (a real reconcile_ledger.py run against the live Kraken
account) and logs/trades.db (the bot's own real historical trade log).
The fixture's own "_provenance" block labels EVERY field's source
precisely (captured / db_recorded / report_value / placeholder /
normalized) — see it before trusting any specific number here.

KrakenFixtureSource does its OWN parsing of the raw dict-keyed-by-trade-id
shape (mirroring ccxt.kraken.fetch_my_trades's real behavior) rather than
reusing pre-parsed data. Because KrakenFixtureSource's parser and this
fixture were authored together, they can only validate each other's self-
consistency — test_kraken_ccxt_adapter_contract.py is the SEPARATE test
that validates the parser's assumptions against the real installed ccxt
library's actual behavior (offline, mocked transport).

No live exchange calls are made anywhere in this file. No production code
(bot/execution/live_executor.py, bot/main.py) is imported or touched.
HALT is untouched.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from execution_accounting_reference_model import (
    SynLedgerEntry,
    SynTrade,
    TradePage,
    assess_readiness,
    causal_order,
    check_balance_consistency,
    commit_checkpoint,
    fold_position,
    init_db,
    record_fee_revision,
    recover_position,
    refresh_ledger_projection_for_corrections,
    retrieve_with_coverage_proof,
    verify_ledger_delivery_consistency,
    write_ledger_rows,
)

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                              "kraken_reconciliation_2026_07_03.json")


def _iso_to_ms(iso: str) -> int:
    return int(round(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000))


class KrakenFixtureSource:
    """Parses the reconstructed Kraken-shaped TradesHistory fixture the
    way ccxt.kraken.fetch_my_trades actually does: the raw response is a
    dict keyed by the exchange's own trade id, each value carrying
    ordertxid/pair/time/type/price/cost/fee/vol as strings. Deliberately
    simplified relative to real ccxt (assumes `pair` is already the
    unified symbol, hardcodes CAD as fee currency, implements its own
    pagination) — those simplifications are exactly what test_kraken_
    ccxt_adapter_contract.py separately validates against real ccxt."""

    def __init__(self, fixture_path: str = _FIXTURE_PATH):
        with open(fixture_path) as f:
            self.fixture = json.load(f)
        self._trades = self._parse_trades_history()

    def _parse_trades_history(self) -> "list[SynTrade]":
        raw = self.fixture["trades_history"]["result"]["trades"]
        parsed = []
        for trade_id, t in raw.items():
            parsed.append(SynTrade(
                trade_id=trade_id,
                order_id=str(t["ordertxid"]) if t["ordertxid"] is not None else "",
                symbol=t["pair"],
                side=t["type"],
                price=float(t["price"]),
                amount=float(t["vol"]),
                cost=float(t["cost"]),
                fee_cost=float(t["fee"]),
                fee_currency="CAD",
                timestamp_ms=int(round(float(t["time"]) * 1000)),
            ))
        parsed.sort(key=lambda x: (x.timestamp_ms, x.trade_id))
        return parsed

    def fetch_my_trades_page(self, symbol, since_ms=None, offset=0, limit=50):
        matching = [t for t in self._trades
                    if t.symbol == symbol and (since_ms is None or t.timestamp_ms > since_ms)]
        total = len(matching)
        page = matching[offset:offset + limit]
        next_offset = offset + limit if offset + limit < total else None
        return TradePage(page, total, next_offset)

    def fetch_balance_total(self, asset: str) -> float:
        return float(self.fixture["balance"]["total"].get(asset, 0.0))

    def fetch_deposits(self, asset: str, since_ms=None) -> "list[SynLedgerEntry]":
        out = []
        for d in self.fixture.get("deposits", {}).get(asset, []):
            ts_ms = _iso_to_ms(d["timestamp"])
            if since_ms is not None and ts_ms <= since_ms:
                continue
            out.append(SynLedgerEntry(f"dep-{d['timestamp']}", "", "deposit", asset,
                                       d["amount"], 0.0, ts_ms))
        return out

    def external_holdings_trade(self) -> SynTrade:
        """The real 2026-06-27 external-holdings incident (a pre-existing
        BTC deposit, not bot-bought, sold due to a stale state flag —
        reconciliation_20260703.md section 9). trade_id here IS the real
        captured Kraken trade id (trades.db row id=9's notes column) —
        not a placeholder, unlike the trail-stop trade below."""
        e = self.fixture["external_holdings_incident"]
        return SynTrade(
            trade_id=e["trade_id"], order_id=e["order_id"], symbol=e["pair"],
            side=e["type"], price=float(e["price"]), amount=float(e["vol"]),
            cost=float(e["cost"]), fee_cost=float(e["fee"]), fee_currency="CAD",
            timestamp_ms=int(round(float(e["time"]) * 1000)),
        )

    def local_ledger_error_trade(self, trade_id: str) -> SynTrade:
        """The bot's OWN pre-existing local trades.db row for the SAME
        fill as trade_id in trades_history — NOT exchange data (see the
        fixture's own local_ledger_error section, sourced from
        logs/trades.db row id=1). Its fee (0.0) is a real historical
        local-ledger bug; trades_history's entry for the same trade_id
        carries the exchange's real, true fee (0.4064) throughout — the
        two were never conflated into one record."""
        e = self.fixture["local_ledger_error"]
        price = float(e["recorded_price"])
        qty = float(e["recorded_quantity"])
        return SynTrade(
            trade_id=trade_id, order_id="", symbol="BTC/CAD", side="sell",
            price=price, amount=qty, cost=price * qty,
            fee_cost=float(e["recorded_fee"]), fee_currency="CAD",
            timestamp_ms=_iso_to_ms(e["recorded_at"]),
        )


@pytest.fixture
def source() -> KrakenFixtureSource:
    return KrakenFixtureSource()


# ---------------------------------------------------------------------------
# 1. Full pipeline composes correctly against real trade-history shape
# ---------------------------------------------------------------------------

def test_full_pipeline_composes_correctly_against_real_captured_trade_history(tmp_path, source):
    """coverage -> causal ordering -> fold -> checkpoint commit -> ledger
    write -> verification -> a REAL close/reopen recovery, all against the
    6 genuine BTC/CAD trades in the fixture (3 real round trips), parsed
    from the raw Kraken response shape rather than hand-built SynTrade
    objects. This proves the pipeline composes end-to-end on realistically
    -shaped data — it does not assert a specific P&L number against
    reconcile_ledger.py's own report, which uses a DIFFERENT accounting
    method (one blended average cost across the whole window) than this
    model's sequential running-average fold; asserting equality between
    two different, legitimate methods would be a false claim, not a
    verification.

    The per-symbol BTC/CAD checkpoint committed below is a LOW-LEVEL
    persistence exercise only — see the orchestration-level assertion at
    the end of this test proving it must never be mistaken for an
    account-wide, balance-confirmed, ready checkpoint (the full account
    balance genuinely does not reconcile against this BTC/CAD-only trade
    set — see test_balance_identity_... below for the exact numbers)."""
    coverage = retrieve_with_coverage_proof(source, "BTC/CAD", since_ms=None)
    assert coverage.complete
    assert coverage.fetched_count == 6

    ordered = causal_order(coverage.trades)
    assert ordered is not None  # 3 clean BUY-then-SELL round trips, no ties
    fold = fold_position(ordered)
    # A real, independently-known property of this data (reconciliation_
    # 20260703.md section 7): cumulative bot BUYs (0.000774) slightly
    # exceed cumulative bot SELLs across these 3 round trips, leaving a
    # small residual long position — not flat.
    assert fold.final_qty > 0
    assert fold.final_qty < 0.00001  # the small residual, not a bulk leftover

    db_path = str(tmp_path / "real_fixture.db")
    conn = init_db(db_path)
    checkpoint_id = commit_checkpoint(
        conn, currency_scope="CAD", window_since_ms=None,
        window_until_ms=max(t.timestamp_ms for t in coverage.trades),
        balance_after=source.fetch_balance_total("CAD"), trades=coverage.trades,
    )
    assert checkpoint_id is not None
    write_ledger_rows(conn, ordered, fold)
    assert verify_ledger_delivery_consistency(conn, "BTC/CAD") is True

    fills = conn.execute("SELECT exec_key FROM fills ORDER BY id").fetchall()
    assert {r[0] for r in fills} == {t.trade_id for t in coverage.trades}
    conn.close()

    # Real close/reopen: reconstruction from disk alone reproduces the
    # EXACT same fold this process just computed in memory.
    conn2 = init_db(db_path)
    recovered = recover_position(conn2, "BTC/CAD")
    assert abs(recovered.final_qty - fold.final_qty) < 1e-9
    assert abs(recovered.avg_cost - fold.avg_cost) < 1e-9
    assert abs(recovered.realized_pnl - fold.realized_pnl) < 1e-9
    assert verify_ledger_delivery_consistency(conn2, "BTC/CAD") is True

    # --- Orchestration-level assertion (fixture-integration review): this
    #     BTC/CAD-only checkpoint must NOT be publishable as an account-
    #     wide ready/balance-confirmed checkpoint — the real CAD balance
    #     genuinely does not reconcile against only these 6 trades (the
    #     account also has DOGE/CAD trading and pre-existing balances this
    #     fixture doesn't cover). ---
    account_balance_check = check_balance_consistency(
        prior_balance=0.0, trades=coverage.trades,
        deposits=source.fetch_deposits("CAD", since_ms=None), withdrawals=[],
        fresh_balance=source.fetch_balance_total("CAD"),
        side_asset_is_quote=True, quote="CAD",
    )
    account_readiness = assess_readiness(
        coverage, account_balance_check, ledger_delivery_ok=True, watermark_confirmed=True,
    )
    assert account_readiness.ready is False  # cannot be published as account-wide ready
    conn2.close()


def test_balance_identity_against_the_real_captured_balance_and_deposit(source):
    """A genuine real-numbers exercise of check_balance_consistency,
    entirely separate from the bot-owned-P&L question above: the fixture's
    real CAD balance (154.1094) and real single deposit (100.00, 2026-06-
    07) are both actual captured values (report_value — see the fixture's
    own provenance block). Independently computed from this fixture's
    exact numbers (verified this session): expected_balance ~= 99.8220956
    (0.0 prior + 100.00 deposit - net BTC/CAD trade cash flow across the 6
    trades), actual 154.1094, residual ~= 54.2873044 — genuinely NOT
    consistent, because the account also holds pre-existing, non-bot ETH/
    BTC/DOGE balances and other real trading activity this fixture
    doesn't cover (reconciliation_20260703.md section 4b) — an honest,
    expected residual, not a bug in this test."""
    deposits = source.fetch_deposits("CAD", since_ms=None)
    assert len(deposits) == 1
    assert abs(deposits[0].amount - 100.0) < 1e-9

    coverage = retrieve_with_coverage_proof(source, "BTC/CAD", since_ms=None)
    result = check_balance_consistency(
        prior_balance=0.0,  # true opening balance before the deposit is not captured by this fixture
        trades=coverage.trades, deposits=deposits, withdrawals=[],
        fresh_balance=source.fetch_balance_total("CAD"),
        side_asset_is_quote=True, quote="CAD",
    )
    assert result.expected_balance == pytest.approx(99.8220956, abs=1e-6)
    assert result.actual_balance == pytest.approx(154.1094, abs=1e-6)
    assert result.residual == pytest.approx(54.2873044, abs=1e-6)
    assert result.consistent is False

    # Readiness must be blocked by this — not just the raw check.
    report = assess_readiness(coverage, result, ledger_delivery_ok=True, watermark_confirmed=True)
    assert report.ready is False
    assert "balance" in report.explain()


# ---------------------------------------------------------------------------
# 2. A genuine, naturally-occurring fee discrepancy: the bot's OWN local
#    ledger (trades.db) vs. the real exchange history — not a fabricated
#    "exchange revised its fee" scenario.
# ---------------------------------------------------------------------------

def test_real_fee_discrepancy_between_local_ledger_and_exchange_truth_corrects_via_refresh(
    tmp_path, source,
):
    """A real, naturally-occurring instance of the scenario R2-R4 built
    the fee-correction machinery for: the bot's OWN local trades.db (row
    id=1) recorded this fill's fee as 0.0 — a real historical local-ledger
    bug (predates the 2026-08-26 post-only-param fix documented in
    CLAUDE.md). trades_history (this fixture's exchange-shaped section)
    has ALWAYS carried the exchange's real true fee, 0.4064 — it is never
    framed as "the exchange revised its data," because it didn't; only
    the bot's OWN pre-existing local record was ever wrong.

    This test seeds that pre-existing (wrong) local record directly —
    exactly as a real reconciliation run would find an already-populated
    ledger from before it ran — then reconciles it against the real
    exchange trade history via record_fee_revision ->
    refresh_ledger_projection_for_corrections -> verify_ledger_delivery_
    consistency -> a real close/reopen, using only the real captured
    numbers from both sources."""
    trade_id = "TRLSTP-PLACEHOLDER-ID"
    coverage = retrieve_with_coverage_proof(source, "BTC/CAD", since_ms=None)
    true_trades = coverage.trades
    true_trail_stop = next(t for t in true_trades if t.trade_id == trade_id)
    assert abs(true_trail_stop.fee_cost - 0.4064) < 1e-9  # exchange truth, unchanged throughout

    local_wrong = source.local_ledger_error_trade(trade_id)
    assert abs(local_wrong.fee_cost - 0.0) < 1e-9  # the bot's own real historical error
    seeded_trades = [t if t.trade_id != trade_id else local_wrong for t in true_trades]

    db_path = str(tmp_path / "fee_correction_real.db")
    conn = init_db(db_path)
    ordered = causal_order(seeded_trades)
    commit_checkpoint(conn, currency_scope="CAD", window_since_ms=None,
                       window_until_ms=max(t.timestamp_ms for t in seeded_trades),
                       balance_after=source.fetch_balance_total("CAD"), trades=seeded_trades)
    write_ledger_rows(conn, ordered, fold_position(ordered))
    # Self-consistent against its OWN (wrong) local record — verification
    # has no way yet to know this disagrees with the exchange.
    assert verify_ledger_delivery_consistency(conn, "BTC/CAD") is True

    # Reconcile against the real exchange trade history.
    assert record_fee_revision(conn, trade_id, true_trail_stop.fee_cost) is True
    assert verify_ledger_delivery_consistency(conn, "BTC/CAD") is False  # fail-closed, unmaterialized

    updated = refresh_ledger_projection_for_corrections(conn, "BTC/CAD")
    assert trade_id in updated
    assert verify_ledger_delivery_consistency(conn, "BTC/CAD") is True
    conn.close()

    conn2 = init_db(db_path)
    assert verify_ledger_delivery_consistency(conn2, "BTC/CAD") is True
    row = conn2.execute("SELECT fee_cost FROM fills WHERE exec_key = ?", (trade_id,)).fetchone()
    assert abs(row[0] - 0.4064) < 1e-9
    conn2.close()


# ---------------------------------------------------------------------------
# 3. The real external-holdings incident — a narrow, specific claim only
# ---------------------------------------------------------------------------

def test_causal_order_rejects_the_real_external_holdings_trade_when_wrongly_included(source):
    """Reproduces the QUANTITY SHAPE of the real 2026-06-27 incident
    (reconciliation_20260703.md section 9): the bot sold 0.000378 BTC it
    never bought. This fixture's 3 legitimate round trips leave only a
    ~0.00000044 BTC residual position — nowhere near enough to cover an
    extra 0.000378 BTC sale.

    Narrow claim, stated explicitly (fixture-integration review finding):
    this test proves ONLY that causal_order's never-short invariant
    rejects an ordering where the external trade is wrongly included
    among bot-owned trades. It does NOT prove ownership classification,
    and it does NOT demonstrate prevention of the original live incident —
    no ownership guard and no production executor code (the real fix was
    _sync_position's ADOPT_EXTERNAL_HOLDINGS guard, in bot/execution/
    live_executor.py, never imported here) is exercised. The "clean"
    success case below EXCLUDES the external trade BY CONSTRUCTION (the
    test itself decides which trades are "legitimate") — this test is not
    evidence that any mechanism would have made that classification
    correctly in production."""
    coverage = retrieve_with_coverage_proof(source, "BTC/CAD", since_ms=None)
    legitimate_trades = coverage.trades
    external_trade = source.external_holdings_trade()

    clean_order = causal_order(legitimate_trades)
    assert clean_order is not None
    clean_fold = fold_position(clean_order)
    assert clean_fold.final_qty > 0

    contaminated = legitimate_trades + [external_trade]
    rejected = causal_order(contaminated)
    assert rejected is None  # the never-short invariant catches the impossible ordering
