"""
Unit tests for the paper-report expectancy math (net of commissions).

The expectancy number is the product of the stock bot's paper phase: it is
what converts the paper book into an income projection. It must be net of
the IBKR Pro commission model (env-driven) — slippage is already applied to
fill prices by the paper executor and must NOT be double-counted here.

Run: python -m pytest test_paper_report.py -v
"""
from __future__ import annotations

import os
import tempfile

from stock_bot.analysis.paper_report import (
    _expectancy_stats,
    _round_trip_commission,
    generate_report,
)


def test_commission_us_minimum_applies():
    # 2 shares × $0.005 = $0.01 → minimum $1.00 per side → $2.00 round trip
    assert _round_trip_commission("DLTR", 2) == 2.00


def test_commission_tsx_per_share_above_minimum():
    # 500 shares × $0.01 = $5.00 per side → $10.00 round trip
    assert _round_trip_commission("AC.TO", 500) == 10.00


def _pair(symbol, shares, entry, exit_, exit_date="2026-07-01"):
    return {
        "symbol": symbol, "shares": shares,
        "entry_price": entry, "exit_price": exit_,
        "pnl": round((exit_ - entry) * shares, 2),
        "pnl_pct": round((exit_ - entry) / entry * 100, 2),
        "entry_date": "2026-06-24", "exit_date": exit_date,
        "exit_reason": "TP", "hold_days": 3,
    }


def test_expectancy_nets_out_commissions():
    # One US trade: +$10 gross on 2 shares → $2 round-trip commission → +$8 net
    pairs = [_pair("DLTR", 2, 100.0, 105.0)]
    exp = _expectancy_stats(pairs)
    assert exp is not None
    assert abs(exp["expectancy_usd"] - 8.0) < 1e-9
    # net % is on position value (200): 8/200 = 4%
    assert abs(exp["expectancy_pct"] - 4.0) < 1e-9
    assert exp["net_win_rate"] == 100.0


def test_expectancy_commission_can_flip_small_win_to_loss():
    # +$1.50 gross on a tiny position − $2.00 commission = −$0.50 net
    pairs = [_pair("DLTR", 1, 100.0, 101.5)]
    exp = _expectancy_stats(pairs)
    assert exp["expectancy_usd"] < 0, "commission must be able to flip small wins"
    assert exp["net_win_rate"] == 0.0


def test_expectancy_none_without_pairs():
    assert _expectancy_stats([]) is None


def test_generate_report_renders_expectancy_section():
    tmp = tempfile.mkdtemp()
    trades_csv = os.path.join(tmp, "paper_trades.csv")
    fast_csv   = os.path.join(tmp, "fast_trades.csv")   # absent — must not crash
    with open(trades_csv, "w", encoding="utf-8") as f:
        f.write(
            "timestamp,symbol,side,shares,price,total_value,cash_remaining,reason,confidence\n"
            "2026-06-24 10:00:00,DLTR,BUY,2.0000,100.0000,200.00,800.00,BUY 70% LONGTERM,70\n"
            "2026-07-01 10:00:00,DLTR,SELL,2.0000,110.0000,220.00,1020.00,TAKE_PROFIT_HIT,70\n"
        )
    report = generate_report(
        csv_path=trades_csv,
        state_path=os.path.join(tmp, "missing_state.json"),
        fast_csv_path=fast_csv,
    )
    assert "EXPECTANCY — NET OF COMMISSIONS" in report
    # $20 gross − $2 commission = $18 net on one trade
    assert "+18.00" in report
    assert "FAST VALIDATOR" in report


if __name__ == "__main__":
    import sys
    failures = 0
    for t in [
        test_commission_us_minimum_applies,
        test_commission_tsx_per_share_above_minimum,
        test_expectancy_nets_out_commissions,
        test_expectancy_commission_can_flip_small_win_to_loss,
        test_expectancy_none_without_pairs,
        test_generate_report_renders_expectancy_section,
    ]:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failures += 1
    sys.exit(failures)
