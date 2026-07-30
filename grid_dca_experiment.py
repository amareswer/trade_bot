#!/usr/bin/env python
"""
grid_dca_experiment.py — does a grid or DCA strategy clear the project's own
PF >= 1.0 / >=10-trades-per-window walk-forward bar on BTC/CAD?

RESEARCH ONLY. Touches no live code, no bot/strategy/* files, no .env, no
bot/main.py, no CapitalPool, no live executor. A PASS here authorizes nothing
by itself — any promotion still requires the full Validation Discipline
workflow in CLAUDE.md.

Why this is a SEPARATE engine, not bot/backtest/engine.py (matching the
convention documented in bot/backtest/params.py for why engine.run()'s kwarg
list is centralized): that engine's SL/TP-at-CLOSE model was validated
specifically for the existing 4h RSI/EMA/ADX trend strategy, where entries
and exits are driven by indicator state that is itself only known at candle
close. Grid and DCA are structurally different — a grid line or a DCA
safety-order trigger is a bare price level that can be crossed and recrossed
intra-candle, so fills here are checked against candle high/low, not just
close (the same reasoning CLAUDE.md already documents for why the live bot's
intra-candle SL/TP block checks the Kraken ticker every 30s rather than
waiting for a 4h close — see "Intra-Candle SL/TP" in CLAUDE_HISTORY.md).
Reusing engine.run() would have silently forced a close-only fill model onto
a strategy family where that model is wrong.

Data note: same BTC/CAD-via-Binance-BTC/USDT-proxy convention used by every
other validation script in this repo (CLAUDE.md "Exchange Setup": Kraken's
OHLCV history is capped at ~720 candles, Binance has 5000+, confirmed price
diff 0.048% — negligible). "python backtest.py" runs the exact same proxy.

Methodology note on parameter choice (step 4 of the brief): every config
below is fixed in the source, chosen from round, defensible numbers (10%/
20%/35% grid half-width, DCA deviations "-2/-4/-6...%"-style ladders) BEFORE
any window's results were inspected. The grid's low/high range for a given
window is anchored to that window's OWN opening candle (the price at
hypothetical grid-setup time) — never to the window's realized high/low,
which would be lookahead. No parameter here was tuned against the walk-
forward output; if none of these configs pass, the honest answer is "these
configs don't pass," not "try until one does."

Usage:  .venv/bin/python grid_dca_experiment.py
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

from bot.data.historical_feed import Candle, fetch_candles_paginated

SYMBOL      = "BTC/USDT"     # Binance proxy for BTC/CAD — see module docstring
TIMEFRAME   = os.getenv("BACKTEST_TIMEFRAME", "4h")
FEE         = float(os.getenv("BACKTEST_FEE_PCT", "0.008"))   # 0.8%, live Kraken finding — do not lower
WINDOWS     = [5000, 3000, 1000]
MIN_TRADES  = 10             # per-window sample floor, matching the project's screen gate
PASS_PF     = 1.0            # per the brief — "our own PF >= 1.0 bar"

RESEARCH_CAPITAL = 1000.0    # independent of live $77 CAD slot sizing — grid/DCA are a
                              # structurally different capital model; disclosed, not hidden

REPORT_PATH = os.path.join(
    "logs", f"grid_dca_experiment_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
)


# ─────────────────────────────────────────────────────────────────────────
# Shared trade-stats helper (same PF convention as bot/backtest/metrics.py:
# profit_factor = gross_profit / gross_loss; inf if no losses)
# ─────────────────────────────────────────────────────────────────────────

def _pf_stats(pnls: list[float], starting_cash: float) -> dict:
    if not pnls:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "pf": 0.0, "ret_pct": 0.0}
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp     = sum(wins)
    gl     = abs(sum(losses))
    pf     = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {
        "trades":  len(pnls),
        "wins":    len(wins),
        "win_rate": len(wins) / len(pnls) * 100,
        "pf":      pf,
        "ret_pct": sum(pnls) / starting_cash * 100 if starting_cash else 0.0,
    }


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


# ─────────────────────────────────────────────────────────────────────────
# STEP 1 — Grid strategy backtester
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class GridConfig:
    name:             str
    range_pct:        float   # half-width around the window's opening price
    grid_levels:      int
    floor_buffer_pct: float = 0.05   # non-negotiable floor stop, 5% below low_price


@dataclass
class GridResult:
    pnls:        list[float] = field(default_factory=list)
    floor_stops: int = 0
    low_price:   float = 0.0
    high_price:  float = 0.0
    # Subset of `pnls` specifically from floor-stop forced closes (also
    # included in `pnls` itself, unchanged) — lets a caller report the
    # circuit breaker's own cumulative damage separately from ordinary
    # grid-line round trips. Added 2026-07-30 for the crash-period stress
    # test; purely additive, no existing behavior changed.
    floor_stop_pnls: list[float] = field(default_factory=list)


def run_grid_backtest(candles: list[Candle], cfg: GridConfig,
                       capital: float = RESEARCH_CAPITAL, fee_pct: float = FEE) -> GridResult:
    """Simple grid simulator. Fills are checked against candle high/low (a
    grid line can be crossed intra-candle) — see module docstring for why
    this differs from the close-only convention in bot/backtest/engine.py.

    Range is anchored to the window's OWN first candle (no lookahead).
    Lines are evenly spaced; each of the `grid_levels` slots i tracks a BUY
    at line[i] whose matching SELL is at line[i+1] (the line above).

    Non-negotiable floor stop: if a candle CLOSES below
    low_price * (1 - floor_buffer_pct), every open slot is closed at that
    candle's close, and the grid halts (no new fills) until a later candle
    closes back at/above low_price. Deliberately CLOSE-based (not high/low)
    — unlike ordinary grid fills, this is a circuit breaker, not a price
    touch, and should not trip on a wick.
    """
    if not candles:
        return GridResult()

    p0    = candles[0].close
    low   = p0 * (1 - cfg.range_pct)
    high  = p0 * (1 + cfg.range_pct)
    floor = low * (1 - cfg.floor_buffer_pct)

    n_lines = cfg.grid_levels
    lines   = [low + i * (high - low) / n_lines for i in range(n_lines + 1)]
    capital_per_slot = capital / n_lines

    open_slots: dict[int, dict] = {}   # slot i -> {"qty": float, "cost": float}
    halted = False
    result = GridResult(low_price=low, high_price=high)

    for candle in candles:
        if halted:
            if candle.close >= low:
                halted = False
            else:
                continue

        # Floor stop — check BEFORE any normal fill this candle.
        if candle.close < floor:
            for slot in list(open_slots.values()):
                proceeds = slot["qty"] * candle.close * (1 - fee_pct)
                pnl = proceeds - slot["cost"]
                result.pnls.append(pnl)
                result.floor_stop_pnls.append(pnl)
            open_slots.clear()
            result.floor_stops += 1
            halted = True
            continue

        # SELLs first (close positions on the way up), then BUYs (open on
        # the way down) — a per-candle simplification since only OHLC (not
        # tick data) is available; documented, not hidden.
        for i in sorted(open_slots.keys()):
            sell_line = lines[i + 1]
            if candle.low <= sell_line <= candle.high:
                slot = open_slots.pop(i)
                proceeds = slot["qty"] * sell_line * (1 - fee_pct)
                result.pnls.append(proceeds - slot["cost"])

        for i in range(n_lines):
            if i in open_slots:
                continue
            buy_line = lines[i]
            if candle.low <= buy_line <= candle.high:
                cost = capital_per_slot * (1 + fee_pct)
                qty  = capital_per_slot / buy_line
                open_slots[i] = {"qty": qty, "cost": cost}

    return result


# ─────────────────────────────────────────────────────────────────────────
# STEP 2 — DCA strategy backtester
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class DCAConfig:
    name:               str
    initial_order:      float
    safety_multiplier:  float          # each successive safety order is this much bigger
    deviations:         list[float]    # e.g. [0.02, 0.04, 0.06, ...] — cumulative % below
                                        # the BASE order's fill price (not chained from the
                                        # previous safety order — see module note below)
    max_safety_orders:  int
    take_profit_pct:    float          # on AVERAGE cost


@dataclass
class DCAResult:
    pnls:          list[float] = field(default_factory=list)
    skipped_cash:  int = 0   # safety orders that would have fired but cash ran out


def run_dca_backtest(candles: list[Candle], cfg: DCAConfig,
                      starting_cash: float = RESEARCH_CAPITAL, fee_pct: float = FEE) -> DCAResult:
    """Simple DCA (safety-order averaging) simulator.

    Deviation triggers are measured from the BASE (initial) order's fill
    price, not chained order-to-order — the brief's own example
    ("-2%, -4%, -6%...") reads as a fixed ladder below one anchor, and this
    is the simpler, more conservative reading (chaining from the previous
    fill would trigger safety orders closer together in a fast drop).
    Documented here rather than silently assumed.

    Cash-constrained, no leverage: a safety order that would exceed
    remaining cash is skipped (not partially filled, not borrowed) — the
    cycle stays open and can still close via take-profit later.

    A cycle always restarts on the candle immediately after a take-profit
    close ("always re-enter" — the simplest default a person would plausibly
    run, not tuned to this data). If a cycle is still open when the window
    ends, it is marked-to-market at the final candle's close and counted as
    a trade — otherwise an underwater cycle that never recovered within the
    window would simply vanish from the stats, inflating PF. This is a real
    risk specific to DCA (a deep, un-recovered drawdown can sit open through
    an entire window) and is called out again in the report.
    """
    if not candles:
        return DCAResult()

    cash = starting_cash
    qty = 0.0
    cost_basis = 0.0   # total $ cost of the open cycle (incl. fees), not per-unit
    safety_filled = 0
    base_price = 0.0
    cycle_open = False
    result = DCAResult()
    # Cash only decreases within an open cycle (no inflow until TP closes it),
    # so once trigger index k is unaffordable it stays unaffordable for the
    # rest of this cycle — track counted indices so skipped_cash counts
    # unique missed orders, not "still missed" on every subsequent candle.
    _skipped_this_cycle: set[int] = set()

    def _open_cycle(price: float) -> None:
        nonlocal cash, qty, cost_basis, safety_filled, base_price, cycle_open
        size = cfg.initial_order
        if size > cash:
            return   # cash-constrained — cannot even open; cycle stays closed this candle
        fee = size * fee_pct
        cash -= (size + fee)
        qty = size / price
        cost_basis = size + fee
        base_price = price
        safety_filled = 0
        cycle_open = True
        _skipped_this_cycle.clear()

    for candle in candles:
        if not cycle_open:
            _open_cycle(candle.close)
            continue

        avg_cost = cost_basis / qty if qty > 0 else 0.0
        tp_price = avg_cost * (1 + cfg.take_profit_pct)

        if candle.low <= tp_price <= candle.high:
            proceeds = qty * tp_price * (1 - fee_pct)
            result.pnls.append(proceeds - cost_basis)
            cash += proceeds
            qty = 0.0
            cost_basis = 0.0
            cycle_open = False
            continue

        if safety_filled < cfg.max_safety_orders:
            trigger_price = base_price * (1 - cfg.deviations[safety_filled])
            if candle.low <= trigger_price <= candle.high:
                order_size = cfg.initial_order * (cfg.safety_multiplier ** (safety_filled + 1))
                fee = order_size * fee_pct
                if order_size + fee > cash:
                    if safety_filled not in _skipped_this_cycle:
                        _skipped_this_cycle.add(safety_filled)
                        result.skipped_cash += 1
                else:
                    cash -= (order_size + fee)
                    qty += order_size / trigger_price
                    cost_basis += order_size + fee
                    safety_filled += 1

    # Mark-to-market any still-open cycle at the final close — see docstring.
    if cycle_open and candles:
        final_price = candles[-1].close
        proceeds = qty * final_price * (1 - fee_pct)
        result.pnls.append(proceeds - cost_basis)

    return result


# ─────────────────────────────────────────────────────────────────────────
# STEP 4 — 3-window walk-forward (5000/3000/1000 trailing candles)
# ─────────────────────────────────────────────────────────────────────────

GRID_CONFIGS = [
    GridConfig(name="tight_10pct", range_pct=0.10, grid_levels=10),
    GridConfig(name="medium_20pct", range_pct=0.20, grid_levels=12),
    GridConfig(name="wide_35pct",  range_pct=0.35, grid_levels=14),
]

DCA_CONFIGS = [
    DCAConfig(
        name="conservative", initial_order=100.0, safety_multiplier=1.5,
        deviations=[0.03, 0.06, 0.09, 0.12, 0.15], max_safety_orders=5,
        take_profit_pct=0.03,
    ),
    DCAConfig(
        name="aggressive", initial_order=50.0, safety_multiplier=2.0,
        deviations=[0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.14], max_safety_orders=7,
        take_profit_pct=0.015,
    ),
]


def _verdict(window_stats: list[dict]) -> str:
    """PASS / MARGINAL / FAILED — see report methodology section for the
    exact rule. window_stats is one dict per window (5000/3000/1000c), each
    with 'trades' and 'pf'.
    """
    counted = [w for w in window_stats if w["trades"] >= MIN_TRADES]
    if not counted:
        return "FAILED — no window reached the 10-trade sample floor"
    all_pass = all(w["pf"] >= PASS_PF for w in counted)
    if not all_pass:
        failing = [w for w in counted if w["pf"] < PASS_PF]
        return (
            "FAILED — PF < 1.0 in "
            + ", ".join(f"{w['window']}c ({_fmt_pf(w['pf'])})" for w in failing)
        )
    if len(counted) < len(window_stats):
        under = [w for w in window_stats if w["trades"] < MIN_TRADES]
        return (
            "MARGINAL — PF >= 1.0 in every window with >=10 trades, but "
            + ", ".join(f"{w['window']}c only had {w['trades']}" for w in under)
        )
    return "PASS — all windows PF >= 1.0, >=10 trades each"


def main() -> None:
    print(f"\nGrid/DCA experiment — {SYMBOL} (BTC/CAD proxy) {TIMEFRAME}, "
          f"fee {FEE*100:.2f}%, capital ${RESEARCH_CAPITAL:.0f}\n")
    print(f"Fetching {WINDOWS[0]} × {TIMEFRAME} candles from Binance …", flush=True)
    all_candles = fetch_candles_paginated(
        exchange_id="binance", symbol=SYMBOL, timeframe=TIMEFRAME, total_limit=WINDOWS[0],
    )
    print(f"  {len(all_candles)} candles "
          f"({all_candles[0].timestamp:%Y-%m-%d} → {all_candles[-1].timestamp:%Y-%m-%d})\n")

    report = [
        f"# Grid / DCA Experiment — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        "Question: would a grid or DCA strategy pass this project's own PF >= 1.0 "
        "walk-forward bar on BTC/CAD? Research only — see module docstring in "
        "grid_dca_experiment.py for the full methodology.",
        "",
        f"Data: {SYMBOL} via Binance (BTC/CAD proxy — Kraken OHLCV history capped at "
        "~720 candles, price diff confirmed 0.048% per CLAUDE.md 'Exchange Setup'). "
        f"{TIMEFRAME} candles, fee {FEE*100:.2f}% (live Kraken finding, not the smaller "
        "modeled rate already proven wrong once for this strategy family). "
        f"Research capital ${RESEARCH_CAPITAL:.0f} per config (independent of live "
        "sizing — grid/DCA are a structurally different capital model).",
        "",
        f"Gate: PF >= {PASS_PF} in every window with >= {MIN_TRADES} trades (matching "
        "CLAUDE.md's Validation Discipline screen-gate shape). Windows: "
        f"{'/'.join(str(w) for w in WINDOWS)} trailing candles, same shape as "
        "atr_walkforward.py / the existing screen tables.",
        "",
        "**Grid ranges are anchored to each window's OWN opening candle** (no "
        "lookahead into that window's realized high/low). **DCA deviation triggers "
        "are measured from the base order's fill price**, not chained order-to-order. "
        "Both documented in-code. No config below was tuned against any window's "
        "output — every one was fixed before results were inspected.",
        "",
        "Every grid config also runs the non-negotiable floor stop-loss (5% below "
        "the range low, CLOSE-triggered, closes all open slots and halts until price "
        "re-enters the range) — this is the guard against unbounded breakout risk.",
        "",
    ]

    print("═" * 70)
    print("GRID")
    print("═" * 70)
    report += ["## Grid", "", "| Config | Window | Trades | PF | Win% | Return | Floor stops |",
               "|--------|--------|--------|-----|------|--------|-------------|"]

    for gcfg in GRID_CONFIGS:
        window_stats = []
        for w in WINDOWS:
            window = all_candles[-w:] if len(all_candles) >= w else all_candles
            gr = run_grid_backtest(window, gcfg)
            stats = _pf_stats(gr.pnls, RESEARCH_CAPITAL)
            stats["window"] = w
            window_stats.append(stats)
            report.append(
                f"| {gcfg.name} | {w}c | {stats['trades']} | {_fmt_pf(stats['pf'])} "
                f"| {stats['win_rate']:.0f}% | {stats['ret_pct']:+.2f}% | {gr.floor_stops} |"
            )
            print(f"  {gcfg.name:<14} {w:>5}c  trades={stats['trades']:<4} "
                  f"PF={_fmt_pf(stats['pf']):<5} win={stats['win_rate']:.0f}%  "
                  f"ret={stats['ret_pct']:+.2f}%  floor_stops={gr.floor_stops}")
        verdict = _verdict(window_stats)
        report.append(f"| **{gcfg.name} verdict** | | | | | | **{verdict}** |")
        print(f"  → {verdict}\n")

    print("═" * 70)
    print("DCA")
    print("═" * 70)
    report += ["", "## DCA", "", "| Config | Window | Trades | PF | Win% | Return | Cash-skipped orders |",
               "|--------|--------|--------|-----|------|--------|----------------------|"]

    for dcfg in DCA_CONFIGS:
        window_stats = []
        for w in WINDOWS:
            window = all_candles[-w:] if len(all_candles) >= w else all_candles
            dr = run_dca_backtest(window, dcfg)
            stats = _pf_stats(dr.pnls, RESEARCH_CAPITAL)
            stats["window"] = w
            window_stats.append(stats)
            report.append(
                f"| {dcfg.name} | {w}c | {stats['trades']} | {_fmt_pf(stats['pf'])} "
                f"| {stats['win_rate']:.0f}% | {stats['ret_pct']:+.2f}% | {dr.skipped_cash} |"
            )
            print(f"  {dcfg.name:<14} {w:>5}c  trades={stats['trades']:<4} "
                  f"PF={_fmt_pf(stats['pf']):<5} win={stats['win_rate']:.0f}%  "
                  f"ret={stats['ret_pct']:+.2f}%  cash_skipped={dr.skipped_cash}")
        verdict = _verdict(window_stats)
        report.append(f"| **{dcfg.name} verdict** | | | | | | **{verdict}** |")
        print(f"  → {verdict}\n")

    report += [
        "",
        "## Caveats",
        "",
        "- DCA cycles still open at a window's end are marked-to-market at the final "
        "close and counted as a trade — otherwise a deep, un-recovered drawdown "
        "sitting open through the whole window would simply vanish from the stats "
        "and inflate PF. Watch the trade count: a config with very few trades in a "
        "window likely means most of the window was spent in one long open cycle.",
        "- Grid capital is split evenly across `grid_levels` slots up front "
        f"(${RESEARCH_CAPITAL:.0f} / N) — a real deployment would size slots to the "
        "chosen range and account balance, not this fixed research capital.",
        "- Fills are checked against candle high/low, which assumes a grid line or "
        "DCA trigger touched intra-candle actually fills at that exact price — real "
        "slippage and partial fills are not modeled here (same simplification level "
        "as the rest of this repo's non-execution-layer backtests).",
        "",
        "---",
        "",
        "This script changes nothing live. Any promotion requires the full "
        "Validation Discipline workflow in CLAUDE.md in addition to this check.",
        "",
    ]

    os.makedirs("logs", exist_ok=True)
    with open(REPORT_PATH, "w") as fh:
        fh.write("\n".join(report) + "\n")
    print(f"\nReport written → {REPORT_PATH}\n")


if __name__ == "__main__":
    main()
