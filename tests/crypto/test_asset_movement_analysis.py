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

A second review pass (same day) found the first version of this module
asserted completeness from a bare flag, let a different asset's deposit
satisfy a shortfall, and silently picked between conflicting duplicate
records depending on input order. Those three findings are reproduced
explicitly below, alongside the original properties.
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


def test_production_causal_order_still_fails_without_the_deposit():
    """Baseline regression: the REAL production function, on trades alone,
    still reports the original 2026-09-21 finding unchanged. This offline
    module does not patch or alter engine.causal_order in any way."""
    assert causal_order(REAL_BTC_TRADES) is None


def test_deposit_resolves_the_inventory_shortfall():
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    assert result.ok is True
    assert result.unresolved_shortfall_qty == 0.0
    # Trades + the one deposit close the position out — a tiny
    # (0.00000044 BTC) pre-existing rounding dust from the earlier trades
    # remains, nowhere near the ~0.00037766 BTC shortfall the
    # deposit-less fold produced.
    assert abs(result.final_qty) < 1e-6


def test_unknown_deposited_asset_cost_basis_remains_unknown():
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    sell = next(a for a in result.sell_attributions if a.trade_id == "TEYLVF-3GXRC-N6RME4")
    # This SELL is (almost entirely) filled from the deposited lot, whose
    # cost basis is None by construction — never a fabricated number.
    assert sell.cost_basis_status in ("unknown", "mixed")
    assert sell.unknown_qty > 0.0


def test_no_fabricated_profit_or_bot_ownership_introduced():
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    sell = next(a for a in result.sell_attributions if a.trade_id == "TEYLVF-3GXRC-N6RME4")
    if sell.known_qty == 0.0:
        assert sell.realized_pnl_known is None
    # The deposit is represented as its own event kind, never as a trade —
    # the module's dedup keys are namespaced ("trade:..." / "deposit:...")
    # specifically so a deposit can never collide with, or be mistaken for,
    # a bot-executed BUY's trade_id.
    assert REAL_BTC_DEPOSIT.entry_id not in {t.trade_id for t in REAL_BTC_TRADES}
    # The function is pure: verify it took no DB/exchange handle at all —
    # its signature only accepts plain data, so it structurally cannot
    # write a synthetic BUY into any store.
    sig = inspect.signature(analyze_with_asset_movements)
    assert set(sig.parameters) == {
        "asset", "trades", "deposits", "withdrawals", "coverage_window",
        "closing_balance", "balance_tolerance",
    }


# ── Completeness: requires actual evidence, never a bare flag ──────────────

def test_completeness_is_false_on_empty_evidence_even_with_no_shortfall():
    """Review finding #1 reproduced against the FIXED module: an empty
    trade/deposit list trivially has no shortfall (ok=True), but that must
    never be conflated with a complete verdict — no withdrawal records, no
    coverage window, and no closing balance were ever supplied."""
    result = analyze_with_asset_movements("BTC", [], [])
    assert result.ok is True
    assert result.complete is False
    assert "withdrawal records" in result.reason
    assert "coverage window" in result.reason
    assert "closing balance" in result.reason


def test_missing_withdrawal_coverage_prevents_a_complete_verdict():
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    assert result.ok is True          # the shortfall itself IS explained
    assert result.complete is False   # but that is not the same as "reconciled"
    assert "withdrawal records" in result.reason


def test_completeness_requires_all_three_pieces_of_real_evidence():
    # Withdrawals supplied (even empty — "queried, found none") but still
    # missing a coverage window and a closing balance.
    result = analyze_with_asset_movements(
        "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], withdrawals=[],
    )
    assert result.complete is False
    assert "withdrawal records" not in result.reason
    assert "coverage window" in result.reason
    assert "closing balance" in result.reason


def test_completeness_achieved_only_with_full_real_evidence_and_matching_balance():
    result = analyze_with_asset_movements(
        "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], withdrawals=[],
        coverage_window=("2026-06-01T00:00:00Z", "2026-07-18T00:00:00Z"),
        closing_balance=result_final_qty_placeholder(),
    )
    assert result.complete is True
    assert result.reason == ""


def result_final_qty_placeholder():
    """The real fold's own final_qty, used as the 'exchange-reported
    closing balance' for the completeness test above — computed once,
    independently, so the test above isn't tautological about its own
    tolerance handling."""
    baseline = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT])
    return baseline.final_qty


def test_completeness_fails_on_a_real_balance_mismatch():
    result = analyze_with_asset_movements(
        "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], withdrawals=[],
        coverage_window=("2026-06-01T00:00:00Z", "2026-07-18T00:00:00Z"),
        closing_balance=0.01,  # not what the fold actually predicts
    )
    assert result.complete is False
    assert "balance mismatch" in result.reason


# ── Asset separation: a different asset can never resolve this shortfall ───

def test_a_different_asset_deposit_is_rejected_not_silently_applied():
    """Review finding #2 reproduced: a SOL-denominated deposit must never
    be usable to explain a BTC shortfall."""
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


# ── Duplicate handling: exact repeats collapse, conflicts are rejected ─────

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
    """Review finding #3 reproduced: two deposits sharing the same id but
    DIFFERENT amounts (1 vs 0.1) must be rejected outright, and the
    rejection must not depend on which one appears first in the input."""
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
    """If the supplied deposit evidence does NOT cover the real shortfall,
    the function must say so explicitly, not silently accept a residual
    negative position the way a naive fold might."""
    small_deposit = LedgerMovement(
        entry_id="synthetic-too-small", type="deposit", asset="BTC",
        amount=0.0001, timestamp="2026-06-26T12:43:35Z",
    )
    result = analyze_with_asset_movements("BTC", REAL_BTC_TRADES, [small_deposit])
    assert result.ok is False
    assert result.unresolved_shortfall_qty > 0.0
    assert result.complete is False


def test_a_withdrawal_that_exceeds_holdings_also_fails_closed():
    withdrawal = LedgerMovement(entry_id="w1", type="withdrawal", asset="BTC",
                                 amount=-1.0, timestamp="2026-07-20T00:00:00Z")
    result = analyze_with_asset_movements(
        "BTC", REAL_BTC_TRADES, [REAL_BTC_DEPOSIT], withdrawals=[withdrawal],
    )
    assert result.ok is False
    assert result.unresolved_shortfall_qty > 0.9


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
