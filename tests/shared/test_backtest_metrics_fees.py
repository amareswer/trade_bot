"""
Regression test for the 2026-09-12 finding: bot/backtest/metrics.compute()
computed profit_factor/win_rate/avg_win/avg_loss/best_trade/worst_trade from
FillRecord.pnl, which is pure price-difference P&L — position_manager.
on_sell() never subtracts fees, and engine.py only deducts them from cash,
never from this per-trade figure. A trade gaining $1 before $1.608 in fees
was counted as a WINNING trade, and every validation gate that reads
m.profit_factor (walkforward.py, screen_universe.py, validate_symbol.py, ...)
was approving strategies on gross trading profit, not real edge.

Independently reproduced on the real saved 2026-09-12 backtest CSVs:
BTC/USDT's pinned-window profit factor was 1.87 gross, 0.82 (a net LOSS)
once entry+exit fees are correctly attributed per trade.

There was zero prior unit test coverage of metrics.compute() at all.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot.backtest.engine import BacktestResult, FillRecord
from bot.backtest.metrics import compute
from bot.data.historical_feed import Candle


def _candle(i: int) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), open=100.0,
        high=101.0, low=99.0, close=100.0, volume=1000.0,
    )


def _result(fills: list[FillRecord], total_fees: float = 0.0) -> BacktestResult:
    return BacktestResult(
        symbol="TEST/USD", timeframe="4h", fee_pct=0.001,
        starting_cash=1000.0, final_value=1000.0 - total_fees,
        total_fees=total_fees, fills=fills,
        equity_curve=[1000.0, 1000.0 - total_fees],
        candles=[_candle(0), _candle(1)], warmup_ticks=0,
    )


def test_a_dollar_gain_swamped_by_fees_is_a_net_loss_not_a_win():
    """The reviewer's exact reproduction: gross +$1, but $1.608 in combined
    entry+exit fees — a real net loss of $0.608 that must NOT be counted
    as a win."""
    fills = [
        FillRecord(0, "t0", "BUY",  100.0, 1.0, 100.0, None, fee=0.804, reason="strategy"),
        FillRecord(1, "t1", "SELL", 101.0, 1.0, 101.0, pnl=1.0, fee=0.804, reason="strategy"),
    ]
    m = compute(_result(fills, total_fees=1.608))

    assert m.total_trades == 1
    assert m.winning_trades == 0
    assert m.losing_trades == 1, "a $1 gain eaten by $1.608 in fees is a net LOSS"
    assert m.profit_factor == 0.0   # no wins, one loss
    assert m.worst_trade == pytest.approx(1.0 - 1.608)   # -0.608
    # Gross figures still show the (misleading, pre-fee) picture, kept for
    # comparison only — never used to judge pass/fail.
    assert m.gross_profit_factor == float("inf")   # gross: 1 win, 0 losses
    assert m.gross_win_rate == 1.0


def test_profit_factor_matches_independent_recomputation_from_real_saved_csv():
    """Loads the real 2026-09-12 pinned-window BTC/USDT backtest CSV
    (checked into logs/) and confirms compute()'s net profit_factor matches
    an independent, from-scratch recomputation — not just internal
    self-consistency."""
    import csv
    import os

    path = "logs/backtest_BTC_USDT_4h_20260912_1417.csv"   # pinned-window run
    if not os.path.exists(path):
        pytest.skip("saved backtest CSV not present in this checkout")

    rows = list(csv.DictReader(open(path)))
    fills = []
    for i, r in enumerate(rows):
        pnl = float(r["pnl"]) if r["pnl"] else None
        fills.append(FillRecord(
            i, r["timestamp"], r["side"], float(r["price"]), float(r["quantity"]),
            float(r["total_value"]), pnl, float(r["fee"]), r["reason"],
        ))
    m = compute(_result(fills))

    # Independent recomputation, mirroring exactly how a human would check
    # this by hand from the CSV (accumulate BUY fees, apply on the next SELL).
    net_pnls = []
    pending_fee = 0.0
    for r in rows:
        fee = float(r["fee"]) if r["fee"] else 0.0
        if r["side"] == "BUY":
            pending_fee = fee
        elif r["side"] == "SELL":
            net_pnls.append(float(r["pnl"]) - pending_fee - fee)
            pending_fee = 0.0
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    expected_pf = sum(wins) / abs(sum(losses))

    assert m.total_trades == len(net_pnls)
    assert m.profit_factor == pytest.approx(expected_pf, abs=0.01)
    # The specific number the reviewer/finding is anchored on:
    assert m.profit_factor < 1.0, (
        "this pinned window's net-of-fees PF is a documented net loss (~0.82) "
        "— if this ever passes 1.0, the saved CSV or the fix has changed"
    )
