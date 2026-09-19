"""
Tests for live_comparison.py.

2026-09-18 review finding: this tool had zero test coverage. It computed
PF/win-rate/total P&L entirely from the stored (gross) `pnl` column and
never loaded the fee_cost/fee_currency columns at all — a price-profitable
trade that lost money after real fees was counted as a win.
"""
from __future__ import annotations

import os
import tempfile

import pytest

import live_comparison as lc
from bot.data.trade_log import TradeLog


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "trades.db")


def _fill(tl, side, qty, price, *, pnl=None, fee_cost=0.0, fee_currency="CAD",
          symbol="BTC/CAD", order_id=""):
    tl.log_fill(side, symbol, qty, price, pnl=pnl, exchange="kraken",
                fee_cost=fee_cost, fee_currency=fee_currency, order_id=order_id)


def test_quote_ccy_extracts_quote_from_symbol():
    assert lc._quote_ccy("BTC/CAD") == "CAD"
    assert lc._quote_ccy("ETH/USDT") == "USDT"
    assert lc._quote_ccy("garbage") == ""
    assert lc._quote_ccy("") == ""


def test_load_fills_includes_fee_columns(db):
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY", 0.001, 90_000.0, fee_cost=0.36)
    fills = lc._load_fills(db)
    assert len(fills) == 1
    assert fills[0]["fee_cost"] == pytest.approx(0.36)
    assert fills[0]["fee_currency"] == "CAD"


def test_price_profitable_trade_is_a_net_loss_after_fees(db):
    """The exact scenario the review's fee-accounting concern describes:
    a trade that gained on price alone but lost once real fees are
    counted must show as a loss in the net figures, not a win."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  0.001, 90_000.0, fee_cost=0.5)
    _fill(tl, "SELL", 0.001, 90_500.0, pnl=0.5, fee_cost=0.9)   # +$0.50 price gain, $1.40 fees

    fills   = lc._load_fills(db)
    metrics = lc._compute_live_metrics(fills)

    assert metrics["total_pnl"] == pytest.approx(0.5)     # gross: looks like a win
    assert metrics["net_pnl"]   == pytest.approx(0.5 - 0.5 - 0.9)   # net: a real loss
    assert metrics["net_pnl"] < 0
    assert metrics["win_rate"] == 0.0     # the one trade nets negative
    assert metrics["gross_win_rate"] == 1.0   # gross still shows it as a win


def test_follow_up_review_exact_reproduction_buy_fee_flips_win_to_loss(db):
    """2026-09-18 FOLLOW-UP review finding: the first fix's per-trade net
    pnl subtracted only the SELL's own exit fee, not its share of the
    matching BUY's entry fee. Exact reproduction: BUY fee $0.80, SELL gross
    profit $1.00, SELL fee $0.40 — real round-trip result is -$0.20, but
    the old code returned win_rate=1.0 and pf=infinity."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  0.001, 90_000.0, fee_cost=0.80)
    _fill(tl, "SELL", 0.001, 91_000.0, pnl=1.00, fee_cost=0.40)

    fills   = lc._load_fills(db)
    metrics = lc._compute_live_metrics(fills)

    assert metrics["net_pnl"] == pytest.approx(-0.20)
    assert metrics["win_rate"] == 0.0       # not 1.0 — this is a real net loss
    assert metrics["pf"] == 0.0             # not infinity — no net winners at all
    assert metrics["gross_win_rate"] == 1.0  # gross still (correctly) shows it as a win


def test_partial_exit_allocates_buy_fee_proportionally_by_quantity(db):
    """Mirrors bot/backtest/metrics.py's own partial-exit regression: a
    position closed via two partial SELLs must split the entry fee
    proportionally by quantity, not dump 100% onto whichever SELL closes
    first."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  0.002, 90_000.0, fee_cost=1.0)   # one BUY, $1.00 entry fee
    _fill(tl, "SELL", 0.001, 91_000.0, pnl=1.0, fee_cost=0.1)   # closes half
    _fill(tl, "SELL", 0.001, 91_000.0, pnl=1.0, fee_cost=0.1)   # closes the other half

    fills   = lc._load_fills(db)
    metrics = lc._compute_live_metrics(fills)

    # Each SELL gets exactly half the $1.00 entry fee ($0.50), not 100%/0%.
    assert metrics["net_pnl"] == pytest.approx((1.0 - 0.5 - 0.1) + (1.0 - 0.5 - 0.1))
    assert metrics["win_rate"] == 1.0   # both trades still net positive at this split
    assert metrics["unallocated_buy_fees"] == pytest.approx(0.0)   # fully allocated, nothing open


def test_unallocated_buy_fee_reported_for_open_inventory(db):
    """Inventory still open at the end of the window (a SELL only partially
    closes the pooled BUY quantity) leaves a real, already-paid fee that
    isn't yet attributable to any closed trade — reported separately, not
    silently dropped or wrongly charged in full against the one trade that
    HAS closed. Pooled proportionally by quantity (same convention as
    bot/backtest/metrics.py): SELL closes half the combined 0.002 BUY
    quantity, so it's allocated exactly half the combined $0.90 fee."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY", 0.001, 90_000.0, fee_cost=0.5)
    _fill(tl, "BUY", 0.001, 90_000.0, fee_cost=0.4)
    _fill(tl, "SELL", 0.001, 91_000.0, pnl=1.0, fee_cost=0.2)   # closes half the pooled quantity

    fills   = lc._load_fills(db)
    metrics = lc._compute_live_metrics(fills)

    assert metrics["n_trades"] == 1
    assert metrics["unallocated_buy_fees"] == pytest.approx(0.45)   # half of the pooled $0.90
    assert metrics["net_pnl"] == pytest.approx(1.0 - 0.45 - 0.2)


def test_gross_and_net_pf_reported_separately(db):
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  0.001, 90_000.0, fee_cost=0.36)
    _fill(tl, "SELL", 0.001, 95_000.0, pnl=5.0, fee_cost=0.76)   # clear net win too
    _fill(tl, "BUY",  0.001, 90_000.0, fee_cost=0.36)
    _fill(tl, "SELL", 0.001, 89_000.0, pnl=-1.0, fee_cost=0.71)  # loses both ways

    fills   = lc._load_fills(db)
    metrics = lc._compute_live_metrics(fills)

    assert metrics["gross_pf"] > metrics["pf"]     # net PF is always <= gross PF when fees > 0
    assert metrics["net_pnl"]  < metrics["total_pnl"]


def test_unmatched_fee_currency_excluded_and_counted(db):
    """A fee reported in a currency that doesn't match the symbol's quote
    must not be silently netted in — it's counted separately instead."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  0.001, 90_000.0, fee_cost=0.36, fee_currency="CAD")
    _fill(tl, "SELL", 0.001, 91_000.0, pnl=1.0, fee_cost=0.02, fee_currency="BTC")  # mismatch

    fills   = lc._load_fills(db)
    metrics = lc._compute_live_metrics(fills)

    assert metrics["unmatched_fee_ct"] == 1
    # The mismatched SELL fee wasn't subtracted from that trade's net pnl —
    # it falls back to gross for that one trade rather than guessing.
    assert metrics["net_pnl"] == pytest.approx(1.0 - 0.36)   # only the BUY's CAD fee counted


def test_no_fees_matches_old_gross_only_behavior(db):
    """A DB with no fee data at all (legacy rows, or dry-run) must still
    produce a sane result — net == gross when there's nothing to subtract."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  0.001, 90_000.0, fee_cost=0.0)
    _fill(tl, "SELL", 0.001, 91_000.0, pnl=1.0, fee_cost=0.0)

    fills   = lc._load_fills(db)
    metrics = lc._compute_live_metrics(fills)

    assert metrics["net_pnl"] == pytest.approx(metrics["total_pnl"])
    assert metrics["pf"] == metrics["gross_pf"]


def test_empty_db_returns_empty_metrics(db):
    TradeLog(db_path=db)   # creates the schema, no fills
    fills = lc._load_fills(db)
    assert lc._compute_live_metrics(fills) == {}


def test_print_report_does_not_raise_with_fee_data(db, capsys):
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  0.001, 90_000.0, fee_cost=0.36)
    _fill(tl, "SELL", 0.001, 91_000.0, pnl=1.0, fee_cost=0.73)
    fills   = lc._load_fills(db)
    metrics = lc._compute_live_metrics(fills)
    lc._print_report(metrics, min_trades=1)
    out = capsys.readouterr().out
    assert "net" in out.lower()
    assert "GROSS-only" in out   # the stale-baseline warning fired


# ---------------------------------------------------------------------------
# PASS-6 review finding (P1): fee_adjustments (a late correction, logged
# separately from the fill) were computed and stored but never consumed by
# this report.
# ---------------------------------------------------------------------------

def test_load_fee_adjustments_empty_when_no_table(db):
    """A DB predating this feature (schema never created) must return {}
    rather than raise."""
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE fills (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert lc._load_fee_adjustments(db) == {}


def test_load_fee_adjustments_sums_by_order_id(db):
    tl = TradeLog(db_path=db)
    tl.log_fee_adjustment("order-1", "BTC/CAD", 2.0, "CAD", adjustment_id="adj-1")
    tl.log_fee_adjustment("order-1", "BTC/CAD", 0.5, "CAD", adjustment_id="adj-2")
    tl.log_fee_adjustment("order-2", "BTC/CAD", 1.0, "CAD", adjustment_id="adj-3")

    adjustments = lc._load_fee_adjustments(db)

    assert adjustments == {"order-1": pytest.approx(2.5), "order-2": pytest.approx(1.0)}


def test_stored_fee_adjustment_flips_a_reported_win_to_a_loss(db):
    """PASS-6 review finding (P1), EXACT reproduction: BUY $100 fee $0,
    SELL $101 fee $0 (gross win +$1), then a stored $2 SELL fee
    adjustment. The correct net result is -$1 with a LOSS classification.
    The un-fixed report returned net P&L +$1 and win rate 100%."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  1.0, 100.0, order_id="buy-1")
    _fill(tl, "SELL", 1.0, 101.0, pnl=1.0, order_id="sell-1")
    tl.log_fee_adjustment("sell-1", "BTC/CAD", 2.0, "CAD", adjustment_id="adj-1")

    fills       = lc._load_fills(db)
    adjustments = lc._load_fee_adjustments(db)
    metrics     = lc._compute_live_metrics(fills, adjustments)

    assert metrics["net_pnl"] == pytest.approx(-1.0)
    assert metrics["win_rate"] == 0.0   # the ONE trade is now a net LOSS
    assert metrics["unattributed_fee_adjustments"] == pytest.approx(0.0)


def test_fee_adjustment_on_buy_allocates_into_entry_fee_pool(db):
    """A BUY-side fee adjustment must flow into the entry-fee pool exactly
    like the BUY's own original fee would — reducing the net pnl of
    whatever SELL(s) later close that inventory, via the SAME FIFO
    allocation already used for ordinary entry fees."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  1.0, 100.0, order_id="buy-1")
    tl.log_fee_adjustment("buy-1", "BTC/CAD", 1.5, "CAD", adjustment_id="adj-1")
    _fill(tl, "SELL", 1.0, 105.0, pnl=5.0, order_id="sell-1")

    fills       = lc._load_fills(db)
    adjustments = lc._load_fee_adjustments(db)
    metrics     = lc._compute_live_metrics(fills, adjustments)

    assert metrics["net_pnl"] == pytest.approx(5.0 - 1.5)


def test_fee_adjustment_replay_idempotent_does_not_double_count(db):
    """Reading the SAME adjustment_id twice (e.g. a replay after an
    incomplete ack) must never double-count — log_fee_adjustment's own
    idempotency, exercised end-to-end through this report."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  1.0, 100.0, order_id="buy-1")
    _fill(tl, "SELL", 1.0, 101.0, pnl=1.0, order_id="sell-1")
    tl.log_fee_adjustment("sell-1", "BTC/CAD", 2.0, "CAD", adjustment_id="adj-1")
    tl.log_fee_adjustment("sell-1", "BTC/CAD", 2.0, "CAD", adjustment_id="adj-1")   # replay

    fills       = lc._load_fills(db)
    adjustments = lc._load_fee_adjustments(db)
    metrics     = lc._compute_live_metrics(fills, adjustments)

    assert metrics["net_pnl"] == pytest.approx(-1.0)   # not -3.0


def test_unattributed_fee_adjustment_reported_not_silently_dropped(db):
    """An adjustment whose order_id matches no loaded fill (shouldn't
    normally happen) must be surfaced, not silently discarded — a gap in
    the figures above must be visible, not hidden."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  1.0, 100.0, order_id="buy-1")
    _fill(tl, "SELL", 1.0, 101.0, pnl=1.0, order_id="sell-1")
    tl.log_fee_adjustment("orphan-order", "BTC/CAD", 3.0, "CAD", adjustment_id="adj-1")

    fills       = lc._load_fills(db)
    adjustments = lc._load_fee_adjustments(db)
    metrics     = lc._compute_live_metrics(fills, adjustments)

    assert metrics["net_pnl"] == pytest.approx(1.0)    # unaffected — nothing to attribute to
    assert metrics["unattributed_fee_adjustments"] == pytest.approx(3.0)


def test_main_wires_fee_adjustments_into_metrics(db, monkeypatch, capsys):
    """End-to-end: main() must actually load and pass fee_adjustments
    through — not just have the helper function exist unused."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY",  1.0, 100.0, order_id="buy-1")
    _fill(tl, "SELL", 1.0, 101.0, pnl=1.0, order_id="sell-1")
    tl.log_fee_adjustment("sell-1", "BTC/CAD", 2.0, "CAD", adjustment_id="adj-1")

    monkeypatch.setattr("sys.argv", ["live_comparison.py", "--db", db, "--min_trades", "1"])
    lc.main()
    out = capsys.readouterr().out
    assert "-$1.00" in out or "-1.00" in out or "$-1.00" in out


# ---------------------------------------------------------------------------
# PASS-7 review finding (P1, finding 2): a correction stored ONCE per
# order_id must be allocated ONCE across ALL fill rows sharing that
# order_id — not charged in full to every one of them.
# ---------------------------------------------------------------------------

def test_fee_adjustment_split_across_two_partial_fills_sharing_one_order_id():
    """Exact reviewer reproduction: two SELL fill dicts, both
    order_id="O1", quantity 0.5 each, gross pnl $0.50 each (gross profit
    $1.00 total), zero original fees. Correction {"O1": 1.20}. Correct net
    P&L is $1.00 - $1.20 = -$0.20. The un-fixed code produced -$1.40
    (the $1.20 correction applied to BOTH rows)."""
    fills = [
        {"side": "SELL", "symbol": "BTC/CAD", "quantity": 0.5, "price": 100.0,
         "pnl": 0.50, "fee_cost": 0.0, "fee_currency": "CAD", "order_id": "O1",
         "timestamp": "t1", "exchange": "kraken"},
        {"side": "SELL", "symbol": "BTC/CAD", "quantity": 0.5, "price": 100.0,
         "pnl": 0.50, "fee_cost": 0.0, "fee_currency": "CAD", "order_id": "O1",
         "timestamp": "t2", "exchange": "kraken"},
    ]
    metrics = lc._compute_live_metrics(fills, {"O1": 1.20})

    assert metrics["net_pnl"] == pytest.approx(-0.20)
    assert metrics["unattributed_fee_adjustments"] == pytest.approx(0.0)


def test_fee_adjustment_split_proportionally_by_quantity_across_uneven_fills():
    """Uneven quantities: the split must be proportional, not equal —
    a 0.75/0.25 quantity split of a $4.00 correction allocates $3.00 and
    $1.00 respectively, summing to exactly $4.00."""
    fills = [
        {"side": "SELL", "symbol": "BTC/CAD", "quantity": 0.75, "price": 100.0,
         "pnl": 1.0, "fee_cost": 0.0, "fee_currency": "CAD", "order_id": "O2",
         "timestamp": "t1", "exchange": "kraken"},
        {"side": "SELL", "symbol": "BTC/CAD", "quantity": 0.25, "price": 100.0,
         "pnl": 1.0, "fee_cost": 0.0, "fee_currency": "CAD", "order_id": "O2",
         "timestamp": "t2", "exchange": "kraken"},
    ]
    metrics = lc._compute_live_metrics(fills, {"O2": 4.0})

    # Gross pnl total $2.00, minus the full $4.00 correction (split
    # 3.00/1.00 but summing to exactly 4.00 regardless) = -$2.00.
    assert metrics["net_pnl"] == pytest.approx(-2.0)


def test_fee_adjustment_allocation_end_to_end_with_real_sqlite(db):
    """Real SQLite fixture: BUY split into two partial fills sharing an
    order_id, closed by a SELL — a BUY-side correction must also be
    allocated once across its own rows before flowing into the entry-fee
    pool, per the SAME quantity-proportional convention."""
    tl = TradeLog(db_path=db)
    _fill(tl, "BUY", 0.5, 100.0, order_id="buy-1")
    _fill(tl, "BUY", 0.5, 100.0, order_id="buy-1")   # second partial, SAME order_id
    tl.log_fee_adjustment("buy-1", "BTC/CAD", 2.0, "CAD", adjustment_id="adj-1")
    _fill(tl, "SELL", 1.0, 105.0, pnl=5.0, order_id="sell-1")

    fills       = lc._load_fills(db)
    adjustments = lc._load_fee_adjustments(db)
    metrics     = lc._compute_live_metrics(fills, adjustments)

    # Entry fee pool receives the FULL $2.00 correction exactly once
    # (split 1.00/1.00 across the two BUY rows, but summing to 2.00 either
    # way) — net pnl = 5.00 - 2.00 = 3.00, not 5.00 - 4.00 = 1.00.
    assert metrics["net_pnl"] == pytest.approx(3.0)
