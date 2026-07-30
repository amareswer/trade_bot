#!/usr/bin/env python
"""
grid_stress_test.py — does the wide-range grid config that PASSED the
standard 3-window walk-forward (grid_dca_experiment.py, wide_35pct,
logs/grid_dca_experiment_20260729.md) survive real historical crash periods?

Why this is a separate script from grid_dca_experiment.py: the 3-window
walk-forward (5000/3000/1000 trailing candles, all ending at present) never
tested a genuine multi-month drawdown of the size BTC has actually produced
— the windows happened to sample a period where the wide grid's floor stop
was never even triggered. That's a "got lucky with the sample," not
"survives a crash," distinction — the same reasoning atr_oos_validation.py
applies to a nested-window PASS (real evidence requires testing on data the
original result didn't get to see). This script runs the EXACT config that
passed, UNCHANGED, against two real, non-overlapping, pre-2024 crash
periods the 3-window walk-forward never touched:

  1. The 2022 crash  (2021-11-01 -> 2022-12-31): BTC ~$69k peak (Nov 2021)
     to ~$15.5k trough (Nov 2022), ~-77% drawdown, a slow multi-month grind.
  2. The COVID crash (2020-01-01 -> 2020-12-31): includes the March 2020
     crash, ~-50% in weeks — a much faster, sharper shape than 2022's.

Config source of truth: logs/grid_dca_experiment_20260729.md, "wide_35pct"
row — range_pct=0.35, grid_levels=14, floor_buffer_pct=0.05 (the
GridConfig default), capital=$1000, fee=0.8%. Pulled directly from
grid_dca_experiment.GRID_CONFIGS by name, not retyped, so this script
cannot silently drift from the config that actually passed. NOT retuned
for this test — the point is whether the passing config survives, not
whether some other config would have done better here.

Data: BTC/USDT via Binance (BTC/CAD proxy — same convention as every other
validation script in this repo; Kraken OHLCV history doesn't reach back to
2020 or 2021 at all).

RESEARCH ONLY. Touches no live code, no bot/strategy/* files, no .env, no
bot/main.py, no CapitalPool, no live executor.

Usage:  .venv/bin/python grid_stress_test.py
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

from bot.data.historical_feed import Candle, fetch_candles_paginated
from grid_dca_experiment import FEE, GRID_CONFIGS, RESEARCH_CAPITAL, _fmt_pf, _pf_stats, run_grid_backtest

TIMEFRAME = os.getenv("BACKTEST_TIMEFRAME", "4h")

# Exact config under test — pulled by name from the module that already
# validated it, not retyped here.
_WIDE_CFG = next(c for c in GRID_CONFIGS if c.name == "wide_35pct")

# Loss-severity bar for the MARGINAL/FAILED split — a single-period stress
# test doesn't have "windows" to average across like the walk-forward gate,
# so PF alone doesn't capture magnitude (PF 0.99 and PF 0.1 are both "below
# 1.0" but very different outcomes). 20% of capital is the same order of
# magnitude as the live max-drawdown breaker (RISK_MAX_DRAWDOWN=0.05 is
# tighter, but that's a halt-new-BUYs breaker, not a "this strategy failed"
# bar) — chosen and fixed before running either period, not tuned after
# seeing the results.
_MARGINAL_LOSS_PCT = 0.20

CRASH_PERIODS = [
    # (label, start_date, end_date)
    ("2022 Crash",   "2021-11-01", "2022-12-31"),
    ("COVID Crash",  "2020-01-01", "2020-12-31"),
]

REPORT_PATH = os.path.join(
    "logs", f"grid_stress_test_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
)


def _period_to_ms(start_date: str, end_date: str) -> tuple[int, int]:
    """Parse 'YYYY-MM-DD' strings to (since_ms, until_ms) for
    fetch_candles_paginated's pinned mode."""
    since = int(datetime.strptime(start_date, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timestamp() * 1000)
    until = int(datetime.strptime(end_date, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timestamp() * 1000)
    return since, until


def buy_and_hold_pnl(candles: list[Candle], capital: float, fee_pct: float) -> float:
    """Buy at the period's first close, sell at the period's last close —
    same two-leg fee convention run_grid_backtest() uses (fee added on top
    of cost to buy, deducted from proceeds to sell), so the comparison is
    apples-to-apples with the grid's own realized P&L."""
    if not candles:
        return 0.0
    entry_price = candles[0].close
    exit_price  = candles[-1].close
    qty  = capital / entry_price
    cost = capital * (1 + fee_pct)
    proceeds = qty * exit_price * (1 - fee_pct)
    return proceeds - cost


def classify_verdict(pf: float, total_pnl: float, capital: float) -> str:
    """PASS / MARGINAL / FAILED for a single-period stress result.

    PASS:     PF >= 1.0 — net profitable through the period, whether or not
              the floor stop fired along the way.
    MARGINAL: PF < 1.0 but the loss is contained (< _MARGINAL_LOSS_PCT of
              capital) — lost money, didn't get wiped out.
    FAILED:   PF < 1.0 and loss >= _MARGINAL_LOSS_PCT of capital — severe.
    """
    if pf >= 1.0:
        return f"PASS — PF {_fmt_pf(pf)} >= 1.0"
    loss_pct = -total_pnl / capital if capital else 0.0
    if loss_pct < _MARGINAL_LOSS_PCT:
        return (f"MARGINAL — PF {_fmt_pf(pf)} < 1.0 but loss "
                f"{loss_pct*100:.1f}% of capital is contained "
                f"(< {_MARGINAL_LOSS_PCT*100:.0f}%)")
    return (f"FAILED — PF {_fmt_pf(pf)} < 1.0, loss {loss_pct*100:.1f}% of "
            f"capital >= {_MARGINAL_LOSS_PCT*100:.0f}% — severe, under real crash stress")


def main() -> None:
    print(f"\nGrid stress test — wide_35pct config UNCHANGED "
          f"(range_pct={_WIDE_CFG.range_pct}, grid_levels={_WIDE_CFG.grid_levels}, "
          f"floor_buffer_pct={_WIDE_CFG.floor_buffer_pct}), "
          f"capital ${RESEARCH_CAPITAL:.0f}, fee {FEE*100:.2f}%\n")

    report = [
        f"# Grid Stress Test — wide_35pct under real crash conditions — "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        "Question: the wide-range grid config (`wide_35pct`) PASSED the standard "
        "3-window trailing walk-forward in `grid_dca_experiment.py` "
        "(`logs/grid_dca_experiment_20260729.md`) with zero losing round trips and "
        "the floor stop never triggering in any of the 3 windows. Those windows never "
        "sampled a real multi-month crash. Does the SAME config, unchanged, survive one?",
        "",
        f"Config under test (pulled by name from `grid_dca_experiment.GRID_CONFIGS`, not "
        f"retyped — cannot silently drift from what actually passed): "
        f"range_pct={_WIDE_CFG.range_pct}, grid_levels={_WIDE_CFG.grid_levels}, "
        f"floor_buffer_pct={_WIDE_CFG.floor_buffer_pct}, capital=${RESEARCH_CAPITAL:.0f}, "
        f"fee={FEE*100:.2f}%. Not retuned for this test — the point is whether the "
        "passing config survives, not whether a different config would do better here.",
        "",
        "Data: BTC/USDT via Binance (BTC/CAD proxy — Kraken has no history back to "
        "2020/2021 at all). Grid range is anchored to each period's OWN opening "
        "candle, same as every walk-forward window (no lookahead).",
        "",
        f"Gate: PF >= 1.0 = PASS. Below 1.0: loss < {_MARGINAL_LOSS_PCT*100:.0f}% of "
        f"capital = MARGINAL, loss >= {_MARGINAL_LOSS_PCT*100:.0f}% = FAILED. "
        "Buy-and-hold BTC over the same period is reported alongside every result, "
        "not the grid number in isolation.",
        "",
        "| Period | Dates | Candles | Trades | PF | Floor stops | Floor-stop loss | "
        "Grid P&L | Buy&Hold P&L | Verdict |",
        "|--------|-------|---------|--------|-----|--------------|------------------|"
        "----------|---------------|---------|",
    ]

    print("═" * 78)
    for label, start_date, end_date in CRASH_PERIODS:
        since_ms, until_ms = _period_to_ms(start_date, end_date)
        print(f"\n{label}  ({start_date} → {end_date})")
        print(f"  Fetching BTC/USDT {TIMEFRAME} candles from Binance …", flush=True)
        candles = fetch_candles_paginated(
            exchange_id="binance", symbol="BTC/USDT", timeframe=TIMEFRAME,
            total_limit=3000, since_ms=since_ms, until_ms=until_ms,
        )
        if not candles:
            print(f"  ERROR: no candles returned for {label}")
            report.append(f"| {label} | {start_date} → {end_date} | 0 | — | — | — | — | — | — | ERROR — no data |")
            continue
        print(f"  {len(candles)} candles ({candles[0].timestamp:%Y-%m-%d} → "
              f"{candles[-1].timestamp:%Y-%m-%d})")

        gr = run_grid_backtest(candles, _WIDE_CFG, capital=RESEARCH_CAPITAL, fee_pct=FEE)
        stats = _pf_stats(gr.pnls, RESEARCH_CAPITAL)
        total_pnl = sum(gr.pnls)
        floor_loss = sum(gr.floor_stop_pnls)
        bh_pnl = buy_and_hold_pnl(candles, RESEARCH_CAPITAL, FEE)
        verdict = classify_verdict(stats["pf"], total_pnl, RESEARCH_CAPITAL)

        print(f"  Trades={stats['trades']}  PF={_fmt_pf(stats['pf'])}  "
              f"Floor stops={gr.floor_stops} (loss ${floor_loss:+.2f})  "
              f"Grid P&L=${total_pnl:+.2f}  Buy&Hold P&L=${bh_pnl:+.2f}")
        print(f"  → {verdict}")

        report.append(
            f"| {label} | {start_date} → {end_date} | {len(candles)} | {stats['trades']} "
            f"| {_fmt_pf(stats['pf'])} | {gr.floor_stops} | ${floor_loss:+.2f} "
            f"| ${total_pnl:+.2f} | ${bh_pnl:+.2f} | {verdict} |"
        )

    report += [
        "",
        "## Interpretation",
        "",
        "Buy-and-hold BTC uses the SAME two-leg fee convention as the grid's own "
        "round trips (fee added on entry cost, deducted from exit proceeds) — bought "
        "at the period's first close, sold at the period's last close, no rebalancing.",
        "",
        "---",
        "",
        "This script changes nothing live. Any promotion requires the full "
        "Validation Discipline workflow in CLAUDE.md in addition to this check.",
    ]

    os.makedirs("logs", exist_ok=True)
    with open(REPORT_PATH, "w") as fh:
        fh.write("\n".join(report) + "\n")
    print(f"\n{'─'*78}\nReport written → {REPORT_PATH}\n")


if __name__ == "__main__":
    main()
