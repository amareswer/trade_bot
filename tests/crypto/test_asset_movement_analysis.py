"""
Regression tests for bot/accounting/asset_movement_analysis.py, an offline
tool (not wired into the live reconciliation path — see the source guard
below) built from a real 2026-09-21 finding: BTC/CAD's observed-trade
history alone could not be causally ordered (a SELL on 2026-06-27 needed
more BTC than the trade history had accumulated), and enabling the Kraken
key's read-only Deposit permission (Withdraw stays disabled) surfaced the
missing piece — a real deposit of exactly the missing quantity, one day
before the SELL. These tests use that REAL captured evidence (the exact
trade rows from logs/trades.db's observed_trades table, and the exact
deposit record returned by a live, read-only fetch_deposits('BTC') call)
as fixtures, not synthetic data.

Two review passes (same day) found real gaps in earlier drafts:
  - pass 2: a bare completeness flag, no asset separation, order-dependent
    duplicate-conflict resolution;
  - pass 3: `coverage_window` checked for presence only (not parsed or
    enforced), a NaN `closing_balance` silently passing the mismatch check,
    and different quote currencies (CAD vs USD) netting together into a
    fabricated "known P&L". All three passes' findings are reproduced
    explicitly below, on top of the original properties.
"""
import inspect

import pytest

from bot.accounting.asset_movement_analysis import analyze_with_asset_movements
from bot.accounting.engine import LedgerMovement, causal_order
from bot.accounting.store import ObservedTrade


def _t(trade_id, side, ts, amount, price, cost, fee_cost, symbol="BTC/CAD",
       fee_currency="CAD", order_id=None):
    return ObservedTrade(
        trade_id=trade_id, order_id=order_id or f"O-{trade_id}", symbol=symbol, side=side,
        price=price, amount=amount, cost=cost, fee_cost=fee_cost, fee_currency=fee_currency,
        exchange_timestamp=ts, source="live",
    )


# ── Real BTC/CAD evidence, verbatim from logs/trades.db's observed_trades
#    table (queried read-only during the 2026-09-21 investigation) ──────────
REAL_BTC_TRADES = [
    _t("TZA7XB-ANR7K-BJPM6G", "buy",  "2026-06-12T00:00:05Z", 0.000113,   88870.30,   10.04234, 0.08034),
    _t("TDCRFZ-MWTNB-2NVHO6", "sell", "2026-06-14T03:56:20Z", 0.00011,    90280.10,    9.93081, 0.03972),
    _t("TANQSU-QH4KF-P62EAP", "buy",  "2026-06-15T11:00:16Z", 0.000108,   92050.90,    9.94150, 0.07953),
    _t("TTOXVM-N4YMU-QT5Y4S", "sell", "2026-06-16T20:00:03Z", 0.000108,   91884.50,    9.92353, 0.07939),
    _t("TCLP47-3J4Q6-XNYYDG", "buy",  "2026-06-20T01:00:17Z", 0.000553,   89985.10,   49.76176, 0.39809),
    _t("TGPVSQ-VRIAF-RCCYVS", "sell", "2026-06-22T16:36:14Z", 0.00055556, 91433.56253, 50.79683, 0.40637),
    _t("TEYLVF-3GXRC-N6RME4", "sell", "2026-06-27T20:00:02Z", 0.00037766, 85341.30,   32.229995358, 0.25784),
    _t("TKSLTA-5KS5A-NSJNOS", "buy",  "2026-07-07T00:00:11Z", 0.000085,   91018.20,    7.73655, 0.06189),
    _t("TSO4MU-FFRTI-ZLHNWB", "buy",  "2026-07-15T16:00:10Z", 0.000084,   92003.00,    7.72825, 0.06183),
    _t("TCRZIZ-OVU6S-REHX5K", "sell", "2026-07-17T12:15:00Z", 0.000169,   88696.80,   14.98976, 0.11992),
]

# The real deposit found via a live, read-only fetch_deposits('BTC') call
# (2026-09-21, after enabling the key's Deposit-only permission): amount
# matches the shortfall to 8 decimal places, one day before the SELL that
# needed it.
REAL_BTC_DEPOSIT = LedgerMovement(
    entry_id="bde604f7268e2edec96adfacc4707cbc6d5acb219d33e88bd5162bb480c5c6bb",
    type="deposit", asset="BTC", amount=0.00037766, timestamp="2026-06-26T12:43:35Z",
)

REAL_SOL_TRADES = [
    _t("T4ZXHY-FILDO-FHOLHI", "buy",  "2026-08-26T20:00:33Z", 0.080808, 134.02, 10.82989, 0.08664,
       symbol="SOL/CAD"),
    _t("TGUCXA-JBM4G-6KEUXC", "sell", "2026-08-27T16:23:12Z", 0.080808, 149.72, 12.09857, 0.09679,
       symbol="SOL/CAD"),
]

REAL_COVERAGE_WINDOW = ("2026-06-01T00:00:00Z", "2026-07-18T00:00:00Z")

# The real, pagination-verified Kraken ledger's BTC-denominated fee on
# TDCRFZ-MWTNB-2NVHO6, established via scripts/ledger_reconciliation_audit.py
# and cross-checked against that trade's CAD leg (see that script and its
# tests) — an explicit, externally-established fact, not inferred here.
REAL_BTC_BASE_FEE_QTY = {"TDCRFZ-MWTNB-2NVHO6": 0.00000044}

# The real 4 SOL staking-reward ledger entries (net of Kraken's own staking
# fee, already applied by Kraken before crediting `amount` here — see
# scripts/ledger_reconciliation_audit.py's raw dump: amount=0.0000051019,
# fee=0.0000015305 nets to the SAME balance delta as amount-fee applied
# once; represented here as the single net credit per entry).
REAL_SOL_REWARDS = [
    LedgerMovement(entry_id="ELDY5NC-MWJ4N-ENASSE", type="reward", asset="SOL",
                   amount=0.0000051019 - 0.0000015305, timestamp="2026-08-28T04:32:39Z"),
    LedgerMovement(entry_id="ELRMKQS-EEZEM-FOWGRT", type="reward", asset="SOL",
                   amount=0.0000000021 - 0.0000000006, timestamp="2026-09-04T04:32:38Z"),
    LedgerMovement(entry_id="ELJDZNW-WLFT6-OZP7PJ", type="reward", asset="SOL",
                   amount=0.0000000022 - 0.0000000006, timestamp="2026-09-11T04:32:44Z"),
    LedgerMovement(entry_id="ELSYCZH-O5LB6-GGQLMZ", type="reward", asset="SOL",
                   amount=0.0000000018 - 0.0000000005, timestamp="2026-09-18T04:32:49Z"),
]


def _real_final_qty() -> float:
    return analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT]).final_qty


def test_production_causal_order_still_fails_without_the_deposit():
    """Baseline regression: the REAL production function, on trades alone,
    still reports the original 2026-09-21 finding unchanged. This offline
    module does not patch or alter engine.causal_order in any way."""
    assert causal_order(REAL_BTC_TRADES) is None


def test_deposit_resolves_the_inventory_shortfall():
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    assert result.ok is True
    assert result.unresolved_shortfall_qty == 0.0
    assert abs(result.final_qty) < 1e-6


def test_unknown_deposited_asset_cost_basis_remains_unknown():
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    sell = next(a for a in result.sell_attributions if a.trade_id == "TEYLVF-3GXRC-N6RME4")
    assert sell.cost_basis_status in ("unknown", "mixed")
    assert sell.unknown_qty > 0.0


def test_no_fabricated_profit_or_bot_ownership_introduced():
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    sell = next(a for a in result.sell_attributions if a.trade_id == "TEYLVF-3GXRC-N6RME4")
    if sell.known_qty == 0.0:
        assert sell.realized_pnl_known is None
    assert REAL_BTC_DEPOSIT.entry_id not in {t.trade_id for t in REAL_BTC_TRADES}
    sig = inspect.signature(analyze_with_asset_movements)
    assert set(sig.parameters) == {
        "asset", "trades", "deposits", "withdrawals", "rewards", "base_currency_fee_qty",
        "coverage_window", "closing_balance", "balance_tolerance",
    }


# ── Review pass 4, finding 1: an unmatched sale must not report "known" ────

def test_a_sell_against_zero_inventory_is_unmatched_not_known():
    """A SELL with no preceding inventory at all previously fell through to
    known_qty=0, unknown_qty=0 -> cost_basis_status='known' and
    pnl_availability.available=True — exactly backwards for a sale this
    analysis cannot explain at all."""
    sell_only = _t("SELL-NO-INV", "sell", "2026-06-01T00:00:00Z", 1.0, 100.0, 100.0, 0.0)
    result = analyze_with_asset_movements("BTC", [sell_only], [])
    assert result.ok is False
    sell = result.sell_attributions[0]
    assert sell.known_qty == 0.0
    assert sell.unknown_qty == 0.0
    assert sell.unmatched_qty == 1.0
    assert sell.cost_basis_status == "unmatched"
    assert sell.realized_pnl_known is None
    assert result.pnl_availability.available is False


def test_a_partially_unmatched_sell_also_makes_pnl_unavailable():
    buy = _t("BUY-PARTIAL", "buy", "2026-06-01T00:00:00Z", 0.4, 100.0, 40.0, 0.0)
    sell = _t("SELL-PARTIAL", "sell", "2026-06-02T00:00:00Z", 1.0, 100.0, 100.0, 0.0)
    result = analyze_with_asset_movements("BTC", [buy, sell], [])
    assert result.ok is False
    attributed = result.sell_attributions[0]
    assert attributed.known_qty == pytest.approx(0.4)
    assert attributed.unmatched_qty == pytest.approx(0.6)
    assert attributed.cost_basis_status == "mixed"
    assert result.pnl_availability.available is False


# ── Review pass 4, finding 2: chronological order must use real UTC instants ─

def test_timezone_offset_timestamps_sort_by_real_utc_instant_not_text():
    """The exact scenario reported: a BUY written with a -05:00 offset
    (05:30Z in real time) text-sorts BEFORE a SELL written as 02:00Z, even
    though the SELL happens almost four hours EARLIER in real time. Sorting
    by raw text would let the SELL succeed against inventory that, in real
    time, doesn't exist yet — sorting by parsed UTC instant must correctly
    detect the shortfall instead."""
    buy = _t("BUY-TZ", "buy", "2026-06-01T00:30:00-05:00", 1.0, 100.0, 100.0, 0.0)
    sell = _t("SELL-TZ", "sell", "2026-06-01T02:00:00Z", 1.0, 100.0, 100.0, 0.0)
    result = analyze_with_asset_movements("BTC", [buy, sell], [])
    assert result.ok is False
    assert result.unresolved_shortfall_qty == pytest.approx(1.0)


def test_simultaneous_deposit_and_sell_apply_inflow_before_outflow():
    """Documented, deterministic tie-break for the exact same real instant:
    inflows (deposits/BUYs) are applied before outflows (SELLs/withdrawals).
    This is a stated ASSUMPTION, not a proven-safe default — if the true
    order was actually outflow-before-inflow, this convention would hide
    a real shortfall rather than reveal one. This test only proves the
    convention is applied consistently, not that it is always correct."""
    deposit = LedgerMovement(entry_id="tie-deposit", type="deposit", asset="BTC",
                              amount=1.0, timestamp="2026-06-01T00:00:00Z")
    sell = _t("TIE-SELL", "sell", "2026-06-01T00:00:00Z", 1.0, 100.0, 100.0, 0.0)
    result = analyze_with_asset_movements("BTC", [sell], [deposit])
    assert result.ok is True
    assert result.unresolved_shortfall_qty == 0.0


def test_malformed_trade_timestamp_is_rejected_even_without_a_coverage_window():
    """Timestamp parsing for chronological ordering is unconditional now —
    an earlier draft only parsed timestamps when a coverage_window happened
    to be supplied, leaving the sort itself always naive-string-based."""
    bad = _t("BAD-TS", "buy", "not-a-timestamp", 0.0001, 90000.0, 9.0, 0.01)
    with pytest.raises(ValueError, match="ISO-8601"):
        analyze_with_asset_movements("BTC", [bad], [])


# ── Three separate verdicts — none may stand in for another ────────────────

def test_balance_agreement_unchecked_without_a_closing_balance():
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    assert result.balance_agreement.checked is False
    assert result.balance_agreement.agrees is None


def test_balance_agreement_true_when_it_matches():
    result = analyze_with_asset_movements(
        "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], closing_balance=_real_final_qty(),
    )
    assert result.balance_agreement.checked is True
    assert result.balance_agreement.agrees is True


def test_balance_agreement_false_on_a_real_mismatch():
    result = analyze_with_asset_movements(
        "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], closing_balance=0.01,
    )
    assert result.balance_agreement.checked is True
    assert result.balance_agreement.agrees is False
    assert "diff" in result.balance_agreement.reason or "0.01" in result.balance_agreement.reason


def test_history_coverage_not_declared_without_a_window():
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    assert result.history_coverage.declared is False
    assert result.history_coverage.independently_verified is False


def test_history_coverage_declared_but_never_independently_verified():
    """Even a perfectly valid, evidence-covering window is only ever a
    caller declaration in this offline tool — never proof."""
    result = analyze_with_asset_movements(
        "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], coverage_window=REAL_COVERAGE_WINDOW,
    )
    assert result.history_coverage.declared is True
    assert result.history_coverage.independently_verified is False
    assert "not independently verified" in result.history_coverage.reason


def test_pnl_availability_false_when_a_sale_has_unknown_basis():
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    assert result.pnl_availability.available is False
    assert result.pnl_availability.quote_currency == "CAD"


def test_pnl_availability_true_when_every_sale_is_fully_known():
    # Trades alone (no deposit needed) for a clean BUY/SELL pair — every
    # sale's cost basis is known.
    result = analyze_with_asset_movements("SOL", REAL_SOL_TRADES, [])
    assert result.pnl_availability.available is True
    assert result.pnl_availability.quote_currency == "CAD"


def test_pnl_availability_false_with_no_sells_at_all():
    buy_only = [REAL_BTC_TRADES[0]]
    result = analyze_with_asset_movements("BTC", buy_only, [])
    assert result.pnl_availability.available is False
    assert "no sell events" in result.pnl_availability.reason


# ── Review pass 3, finding 1: coverage window presence != validity ─────────

def test_an_unparseable_coverage_window_is_rejected_not_silently_accepted():
    with pytest.raises(ValueError, match="ISO-8601"):
        analyze_with_asset_movements(
            "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT],
            coverage_window=("invalid", "invalid"),
        )


def test_a_coverage_window_with_since_after_until_is_rejected():
    with pytest.raises(ValueError, match="strictly before"):
        analyze_with_asset_movements(
            "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT],
            coverage_window=("2026-07-18T00:00:00Z", "2026-06-01T00:00:00Z"),
        )


def test_a_coverage_window_that_excludes_supplied_evidence_is_rejected():
    """A window that doesn't even cover the trades/deposit it was given
    alongside contradicts the evidence — must not be treated as valid
    coverage just because it parses."""
    too_narrow = ("2026-07-01T00:00:00Z", "2026-07-18T00:00:00Z")  # excludes June trades
    with pytest.raises(ValueError, match="falls outside"):
        analyze_with_asset_movements(
            "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], coverage_window=too_narrow,
        )


# ── Review pass 3, finding 2: non-finite values must be rejected ───────────

def test_nan_closing_balance_is_rejected_not_silently_passed():
    with pytest.raises(ValueError, match="finite"):
        analyze_with_asset_movements(
            "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], closing_balance=float("nan"),
        )


def test_infinite_closing_balance_is_rejected():
    with pytest.raises(ValueError, match="finite"):
        analyze_with_asset_movements(
            "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], closing_balance=float("inf"),
        )


def test_nan_trade_amount_is_rejected():
    bad = _t("BAD-NAN", "buy", "2026-06-12T00:00:05Z", float("nan"), 90000.0, 9.0, 0.01)
    with pytest.raises(ValueError, match="finite"):
        analyze_with_asset_movements("BTC", [bad], [])


def test_nan_deposit_amount_is_rejected():
    bad_deposit = LedgerMovement(entry_id="bad-nan-deposit", type="deposit", asset="BTC",
                                  amount=float("nan"), timestamp="2026-06-26T12:43:35Z")
    with pytest.raises(ValueError, match="finite"):
        analyze_with_asset_movements("BTC", [], [bad_deposit])


# ── Review pass 3, finding 3: mixed quote currencies must not net together ──

def test_mixed_quote_currencies_are_rejected_not_netted():
    """Buying 1 BTC for CAD 100 then selling for USD 100 must never produce
    a numeric 'known P&L = 0' — CAD and USD are not the same number."""
    buy_cad = _t("BUY-CAD", "buy", "2026-06-01T00:00:00Z", 1.0, 100.0, 100.0, 0.0,
                 symbol="BTC/CAD", fee_currency="CAD")
    sell_usd = _t("SELL-USD", "sell", "2026-06-02T00:00:00Z", 1.0, 100.0, 100.0, 0.0,
                  symbol="BTC/USD", fee_currency="USD")
    with pytest.raises(ValueError, match="quote currenc"):
        analyze_with_asset_movements("BTC", [buy_cad, sell_usd], [])


# ── Asset separation (review pass 2, finding 2) ─────────────────────────────

def test_a_different_asset_deposit_is_rejected_not_silently_applied():
    wrong_asset_deposit = LedgerMovement(
        entry_id="wrong-asset-deposit", type="deposit", asset="SOL",
        amount=1.0, timestamp="2026-06-26T12:43:35Z",
    )
    with pytest.raises(ValueError, match="SOL"):
        analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [wrong_asset_deposit])


def test_a_trade_on_a_different_symbol_base_is_rejected():
    with pytest.raises(ValueError, match="SOL/CAD"):
        analyze_with_asset_movements("BTC", REAL_SOL_TRADES, [])


def test_a_trade_fee_in_the_wrong_currency_is_rejected():
    bad = _t("BAD1", "buy", "2026-06-12T00:00:05Z", 0.0001, 90000.0, 9.0, 0.01,
             fee_currency="USD")
    with pytest.raises(ValueError, match="fee_currency"):
        analyze_with_asset_movements("BTC", [bad], [])


def test_a_withdrawal_typed_record_in_the_deposits_list_is_rejected():
    mislabeled = LedgerMovement(
        entry_id="actually-a-withdrawal", type="withdrawal", asset="BTC",
        amount=-0.0001, timestamp="2026-06-26T12:43:35Z",
    )
    with pytest.raises(ValueError, match="expected 'deposit'"):
        analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [mislabeled])


def test_sol_trades_alone_already_close_out_no_deposit_needed():
    """SOL/CAD's real discrepancy is a different shape entirely: the two
    trades net to exactly zero, so nothing here is negative or unresolved —
    the live wallet's tiny (~0.0000036 SOL) EXCESS over the trade-only fold
    is not a shortfall this tool is designed to explain, and it must not
    pretend otherwise by fabricating an unaccounted-for deposit."""
    result = analyze_with_asset_movements("SOL", REAL_SOL_TRADES, [])
    assert result.ok is True
    assert abs(result.final_qty) < 1e-9
    assert result.unknown_basis_qty_remaining == 0.0


# ── Duplicate handling (review pass 2, finding 3) ───────────────────────────

def test_duplicate_deposit_does_not_double_count():
    once = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    twice = analyze_with_asset_movements(
        "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT, REAL_BTC_DEPOSIT],
    )
    assert twice.final_qty == once.final_qty
    assert twice.duplicate_ids_collapsed == [f"deposit:{REAL_BTC_DEPOSIT.entry_id}"]


def test_duplicate_trade_does_not_double_count():
    dup_trades = REAL_BTC_TRADES + [REAL_BTC_TRADES[0]]
    result = analyze_with_asset_movements("BTC", dup_trades, [REAL_BTC_DEPOSIT])
    baseline = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    assert result.final_qty == baseline.final_qty
    assert result.duplicate_ids_collapsed == [f"trade:{REAL_BTC_TRADES[0].trade_id}"]


def test_conflicting_duplicate_deposit_is_rejected_regardless_of_order():
    big = LedgerMovement(entry_id="dup-id", type="deposit", asset="BTC",
                          amount=1.0, timestamp="2026-06-26T12:43:35Z")
    small = LedgerMovement(entry_id="dup-id", type="deposit", asset="BTC",
                            amount=0.1, timestamp="2026-06-26T12:43:35Z")

    with pytest.raises(ValueError, match="conflicting records"):
        analyze_with_asset_movements("BTC", [], [big, small])

    with pytest.raises(ValueError, match="conflicting records"):
        analyze_with_asset_movements("BTC", [], [small, big])


def test_conflicting_duplicate_trade_is_rejected():
    v1 = _t("dup-trade", "buy", "2026-06-12T00:00:05Z", 0.001, 90000.0, 90.0, 0.1)
    v2 = _t("dup-trade", "buy", "2026-06-12T00:00:05Z", 0.002, 90000.0, 180.0, 0.2)
    with pytest.raises(ValueError, match="conflicting records"):
        analyze_with_asset_movements("BTC", [v1, v2], [])


def test_repeated_calls_are_reproducible_a_restart_replaying_evidence_is_safe():
    """The function holds no state between calls — a restart re-reading the
    identical persisted evidence a second time must reproduce the exact
    same result, never an accumulating one."""
    first = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    second = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    assert first == second


def test_an_insufficient_deposit_still_fails_closed():
    small_deposit = LedgerMovement(
        entry_id="synthetic-too-small", type="deposit", asset="BTC",
        amount=0.0001, timestamp="2026-06-26T12:43:35Z",
    )
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [small_deposit])
    assert result.ok is False
    assert result.unresolved_shortfall_qty > 0.0


def test_a_withdrawal_that_exceeds_holdings_also_fails_closed():
    withdrawal = LedgerMovement(entry_id="w1", type="withdrawal", asset="BTC",
                                 amount=-1.0, timestamp="2026-07-20T00:00:00Z")
    result = analyze_with_asset_movements(
        "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], withdrawals=[withdrawal],
    )
    assert result.ok is False
    assert result.unresolved_shortfall_qty > 0.9


# ── Review pass 5: real BTC base-currency-fee correction + SOL rewards ──────
# (scripts/ledger_reconciliation_audit.py found these via a pagination-
# proven, Decimal-exact ledger walk — see that script's own tests for the
# raw evidence and the cross-currency check that ruled out a second,
# separate CAD fee before this correction was added here.)

def test_base_currency_fee_qty_makes_the_real_btc_chain_reconcile_exactly():
    result = analyze_with_asset_movements(
        "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT],
        base_currency_fee_qty=REAL_BTC_BASE_FEE_QTY, closing_balance=0.0,
    )
    assert result.ok is True
    assert result.final_qty == pytest.approx(0.0, abs=1e-12)
    assert result.balance_agreement.agrees is True


def test_base_currency_fee_qty_correctly_raises_the_affected_sells_pnl():
    """A first, buggy draft of this feature assumed fee_cost was still a
    real cash deduction and silently discarded the fee-consumed quantity's
    cost basis — understating the real economic loss. The corrected
    formula (a) stops subtracting fee_cost from proceeds (the CAD leg was
    never actually debited) and (b) recognizes the fee-consumed quantity's
    own cost-basis loss — both push TDCRFZ's reported P&L UP relative to
    the old (wrong) formula, not leave it unchanged."""
    before = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    after = analyze_with_asset_movements(
        "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], base_currency_fee_qty=REAL_BTC_BASE_FEE_QTY,
    )
    before_pnl = next(a for a in before.sell_attributions if a.trade_id == "TDCRFZ-MWTNB-2NVHO6")
    after_pnl = next(a for a in after.sell_attributions if a.trade_id == "TDCRFZ-MWTNB-2NVHO6")
    assert after_pnl.fee_consumed_qty == pytest.approx(0.00000044)
    assert after_pnl.fee_consumed_known_qty == pytest.approx(0.00000044)
    assert after_pnl.fee_unit_basis_unresolved is False
    assert after_pnl.realized_pnl_known != before_pnl.realized_pnl_known


def test_complete_round_trip_pnl_equals_real_cash_change_with_a_base_fee():
    """The core conservation property this fix exists to satisfy: ending
    flat, with NO external flows (no deposits/withdrawals/rewards), total
    reported P&L must equal the actual quote-cash change — not silently
    discard the fee-consumed quantity's cost basis. BUY 1.0 for $100 cash;
    SELL reports 0.9 for $90 cash (fee_cost=0, matching the real pattern
    where the CAD leg is never actually debited); an extra 0.1 units leave
    as a base-currency fee, drawn from the SAME known-cost BUY lot. Real
    cash change: -100 + 90 = -10."""
    buy = _t("RT-BUY", "buy", "2026-06-01T00:00:00Z", 1.0, 100.0, 100.0, 0.0)
    sell = _t("RT-SELL", "sell", "2026-06-02T00:00:00Z", 0.9, 100.0, 90.0, 0.0)
    result = analyze_with_asset_movements(
        "BTC", [buy, sell], [], base_currency_fee_qty={"RT-SELL": 0.1},
    )
    assert result.ok is True
    assert result.final_qty == pytest.approx(0.0, abs=1e-9)   # fully flat
    total_pnl = sum(a.realized_pnl_known or 0.0 for a in result.sell_attributions)
    real_cash_change = -100.0 + 90.0
    assert total_pnl == pytest.approx(real_cash_change)
    attr = result.sell_attributions[0]
    assert attr.fee_consumed_known_qty == pytest.approx(0.1)
    assert attr.fee_unit_basis_unresolved is False
    assert result.pnl_availability.available is True


def test_complete_round_trip_baseline_without_any_fee_override_already_conserves():
    """Positive baseline: for an ordinary trade (no base-currency-fee
    override), the existing fee_cost-as-cash-deduction formula already
    conserves cash exactly — proving this property held before this fix
    and still holds for the common case untouched by it."""
    buy = _t("RT2-BUY", "buy", "2026-06-01T00:00:00Z", 1.0, 100.0, 100.0, 2.0)   # $102 cash out
    sell = _t("RT2-SELL", "sell", "2026-06-02T00:00:00Z", 1.0, 120.0, 120.0, 3.0)  # $117 cash in
    result = analyze_with_asset_movements("BTC", [buy, sell], [])
    assert result.final_qty == pytest.approx(0.0, abs=1e-9)
    total_pnl = sum(a.realized_pnl_known or 0.0 for a in result.sell_attributions)
    real_cash_change = -(100.0 + 2.0) + (120.0 - 3.0)
    assert total_pnl == pytest.approx(real_cash_change)


def test_real_sol_round_trip_already_conserves_cash_exactly():
    """The same conservation property, checked against the real SOL/CAD
    round trip (no override involved) — the $1.085250 net figure reported
    earlier is not an arbitrary number, it IS the real cash change."""
    result = analyze_with_asset_movements("SOL", REAL_SOL_TRADES, [])
    assert result.final_qty == pytest.approx(0.0, abs=1e-9)
    total_pnl = sum(a.realized_pnl_known or 0.0 for a in result.sell_attributions)
    buy, sell = REAL_SOL_TRADES
    real_cash_change = -(buy.cost + buy.fee_cost) + (sell.cost - sell.fee_cost)
    assert total_pnl == pytest.approx(real_cash_change)


def test_fee_consumed_from_an_unknown_cost_lot_stays_explicitly_unresolved():
    """If the fee-consumed quantity draws from a deposit/reward lot instead
    of a known BUY, its economic impact must NOT be guessed at, defaulted
    to zero, or silently folded into the known P&L number."""
    deposit = LedgerMovement(entry_id="rt3-deposit", type="deposit", asset="BTC",
                             amount=1.0, timestamp="2026-06-01T00:00:00Z")
    sell = _t("RT3-SELL", "sell", "2026-06-02T00:00:00Z", 0.9, 100.0, 90.0, 0.0)
    result = analyze_with_asset_movements(
        "BTC", [sell], [deposit], base_currency_fee_qty={"RT3-SELL": 0.1},
    )
    attr = result.sell_attributions[0]
    assert attr.fee_consumed_unknown_qty == pytest.approx(0.1)
    assert attr.fee_unit_basis_unresolved is True
    assert result.pnl_availability.available is False


def test_an_entirely_unmatched_fee_still_makes_pnl_unavailable():
    """Exact review reproduction: buy 1 unit, sell that entire unit (the
    main sale is fully known and fully matched), then charge an additional
    0.01-unit base fee with NO inventory left at all to cover it. Before
    this fix: ok=False, shortfall=0.01, but pnl_availability.available was
    STILL True and fee_unit_basis_unresolved was STILL False — the fee's
    own unmatched portion was counted toward the shortfall but excluded
    from the P&L-availability check entirely."""
    buy = _t("UNMFEE-BUY", "buy", "2026-06-01T00:00:00Z", 1.0, 100.0, 100.0, 0.0)
    sell = _t("UNMFEE-SELL", "sell", "2026-06-02T00:00:00Z", 1.0, 100.0, 100.0, 0.0)
    result = analyze_with_asset_movements(
        "BTC", [buy, sell], [], base_currency_fee_qty={"UNMFEE-SELL": 0.01},
    )
    assert result.ok is False
    assert result.unresolved_shortfall_qty == pytest.approx(0.01)
    attr = result.sell_attributions[0]
    assert attr.known_qty == pytest.approx(1.0)          # the main sale itself is fully explained
    assert attr.fee_consumed_unmatched_qty == pytest.approx(0.01)
    assert attr.fee_unit_basis_unresolved is True
    assert result.pnl_availability.available is False


def test_a_partially_covered_fee_splits_known_and_unmatched_correctly():
    """A fee that is PARTIALLY covered by remaining inventory and partially
    not must split cleanly across fee_consumed_known_qty and
    fee_consumed_unmatched_qty — neither swallowing the other."""
    buy = _t("PARTFEE-BUY", "buy", "2026-06-01T00:00:00Z", 1.0, 100.0, 100.0, 0.0)
    sell = _t("PARTFEE-SELL", "sell", "2026-06-02T00:00:00Z", 0.99, 100.0, 99.0, 0.0)
    # After the main sale consumes 0.99, only 0.01 unit remains in the BUY
    # lot — a 0.03-unit fee can only be PARTIALLY covered (0.01 known,
    # 0.02 unmatched).
    result = analyze_with_asset_movements(
        "BTC", [buy, sell], [], base_currency_fee_qty={"PARTFEE-SELL": 0.03},
    )
    assert result.ok is False
    assert result.unresolved_shortfall_qty == pytest.approx(0.02)
    attr = result.sell_attributions[0]
    assert attr.fee_consumed_known_qty == pytest.approx(0.01)
    assert attr.fee_consumed_unmatched_qty == pytest.approx(0.02)
    assert attr.fee_unit_basis_unresolved is True
    assert result.pnl_availability.available is False


def test_base_currency_fee_qty_referencing_an_unknown_trade_id_is_rejected():
    with pytest.raises(ValueError, match="not in `trades`"):
        analyze_with_asset_movements(
            "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT],
            base_currency_fee_qty={"NOT-A-REAL-TRADE-ID": 0.0001},
        )


def test_base_currency_fee_qty_must_be_non_negative():
    with pytest.raises(ValueError, match=">= 0"):
        analyze_with_asset_movements(
            "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT],
            base_currency_fee_qty={"TDCRFZ-MWTNB-2NVHO6": -0.0001},
        )


def test_a_buys_base_currency_fee_reduces_the_net_lot_not_the_cost():
    """On a BUY, the true net quantity retained shrinks by the fee, but the
    TOTAL dollar cost (cost + fee_cost) is unchanged — only its per-unit
    spread changes, since it's now divided over fewer real units."""
    buy = _t("BUY-BASEFEE", "buy", "2026-06-01T00:00:00Z", 1.0, 100.0, 100.0, 0.0)
    sell = _t("SELL-BASEFEE", "sell", "2026-06-02T00:00:00Z", 0.9, 100.0, 90.0, 0.0)
    result = analyze_with_asset_movements(
        "BTC", [buy, sell], [], base_currency_fee_qty={"BUY-BASEFEE": 0.1},
    )
    # Net lot was 1.0 - 0.1 = 0.9, cost_per_unit = 100/0.9 — the 0.9-unit
    # sell exactly drains it, entirely from a known-cost lot.
    sell_attr = result.sell_attributions[0]
    assert sell_attr.cost_basis_status == "known"
    assert sell_attr.known_qty == pytest.approx(0.9)
    assert result.final_qty == pytest.approx(0.0, abs=1e-9)


def test_sol_rewards_reconcile_exactly_to_the_real_live_balance():
    result = analyze_with_asset_movements(
        "SOL", REAL_SOL_TRADES, [], rewards=REAL_SOL_REWARDS, closing_balance=0.0000035758,
    )
    assert result.ok is True
    assert result.final_qty == pytest.approx(0.0000035758, abs=1e-12)
    assert result.balance_agreement.agrees is True


def test_a_reward_sourced_sell_has_unknown_cost_basis_no_fabricated_ownership():
    """Rewards carry no acquisition cost and are not bot-attributable —
    selling from a reward-sourced lot must behave exactly like selling from
    a deposit-sourced one: unknown cost basis, no fabricated P&L."""
    reward = LedgerMovement(entry_id="rwd1", type="reward", asset="BTC",
                            amount=0.0001, timestamp="2026-06-01T00:00:00Z")
    sell = _t("SELL-REWARD", "sell", "2026-06-02T00:00:00Z", 0.0001, 100.0, 10.0, 0.0)
    result = analyze_with_asset_movements("BTC", [sell], [], rewards=[reward])
    assert result.ok is True
    attr = result.sell_attributions[0]
    assert attr.cost_basis_status == "unknown"
    assert attr.realized_pnl_known is None
    assert result.pnl_availability.available is False


def test_a_deposit_typed_entry_in_rewards_is_rejected():
    mislabeled = LedgerMovement(entry_id="not-a-reward", type="deposit", asset="BTC",
                                amount=0.0001, timestamp="2026-06-01T00:00:00Z")
    with pytest.raises(ValueError, match="expected 'reward'"):
        analyze_with_asset_movements("BTC", [], [], rewards=[mislabeled])


def test_a_reward_in_a_different_asset_is_rejected():
    wrong_asset = LedgerMovement(entry_id="rwd2", type="reward", asset="ETH",
                                 amount=0.0001, timestamp="2026-06-01T00:00:00Z")
    with pytest.raises(ValueError, match="ETH"):
        analyze_with_asset_movements("BTC", [], [], rewards=[wrong_asset])


def test_duplicate_reward_does_not_double_count():
    once = analyze_with_asset_movements("SOL", [], [], rewards=[REAL_SOL_REWARDS[0]])
    twice = analyze_with_asset_movements(
        "SOL", [], [], rewards=[REAL_SOL_REWARDS[0], REAL_SOL_REWARDS[0]],
    )
    assert once.final_qty == twice.final_qty
    assert twice.duplicate_ids_collapsed == [f"reward:{REAL_SOL_REWARDS[0].entry_id}"]


def test_offline_module_is_not_imported_by_any_production_reconciliation_path():
    """Source guard: this module must stay a standalone analysis tool. If
    this ever starts failing, something wired it into the live cycle
    without an explicit, separate decision to do so."""
    import bot.main as main_mod
    from bot.accounting import four_way as four_way_mod
    from bot.accounting import reconciliation as reconciliation_mod

    for mod in (main_mod, four_way_mod, reconciliation_mod):
        src = inspect.getsource(mod)
        assert "asset_movement_analysis" not in src, f"{mod.__name__} must not import this offline tool"
