"""
Research: is the crypto EXIT logic leaving money on the table?

Entry selectivity was swept 2026-09-02 (strategy_selectivity_sweep.py) — filters
are well-calibrated. This sweeps the OTHER half: the exits. Live config is a
static ATR×2.0 stop + a hard 10% take-profit, no trailing, no partial TP. The
walk-forward attribution shows winners are held ~11 days and mostly exit at the
10% TP — so a trailing stop or higher TP *might* capture more of the runners.

Same OOS window as walkforward.py. Changes nothing live. Run:
    .venv/bin/python strategy_exit_sweep.py
Writes logs/strategy_exit_sweep_<date>.md
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import statistics

logging.basicConfig(level=logging.ERROR)

from config import cfg
from bot.data.historical_feed import fetch_candles_paginated, slice_candles
from bot.backtest import engine, metrics as metrics_mod
from bot.backtest.params import engine_kwargs_from_cfg

SYMBOLS = ["BTC/USDT", "SOL/USDT"]
EXCHANGE = "binance"
VAL_START = "2025-02-22"


def _run(candles, symbol, **ov):
    kw = engine_kwargs_from_cfg(cfg)
    kw.update(symbol=symbol)
    kw.update(ov)
    res = engine.run(candles=candles, **kw)
    m = metrics_mod.compute(res)
    # avg holding period (candles between a BUY fill and the next SELL fill)
    holds = []
    buy_i = None
    for f in res.fills:
        if f.side == "BUY":
            buy_i = f.candle_index
        elif f.side == "SELL" and buy_i is not None:
            holds.append(f.candle_index - buy_i)
            buy_i = None
    avg_hold_h = statistics.mean(holds) * 4 if holds else 0.0   # 4h candles
    return (m.total_trades, m.profit_factor, m.total_return_pct * 100,
            m.max_drawdown_pct * 100, m.win_rate * 100, avg_hold_h)


def _pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def _row(label, r):
    return (f"| {label} | {r[0]} | {_pf(r[1])} | {r[2]:+.1f} | {r[3]:.1f} | "
            f"{r[4]:.0f} | {r[5]:.0f}h |")


def main():
    today = _dt.date.today().strftime("%Y%m%d")
    out = [f"# Crypto exit-logic sweep — {_dt.date.today().isoformat()}\n",
           f"OOS window {VAL_START}→present ({EXCHANGE}, 4h). Live exit: ATR×2.0 stop + "
           f"hard 10% TP, no trail, no partial. Columns: trades · PF · return% · maxDD% · "
           f"win% · avg hold.\n"]

    for symbol in SYMBOLS:
        print(f"\n=== {symbol} ===")
        allc = fetch_candles_paginated(EXCHANGE, symbol, "4h", 5000)
        val  = slice_candles(allc, VAL_START, None)
        print(f"  {len(val)} OOS candles")

        base = _run(val, symbol)
        out.append(f"\n## {symbol}\n")
        out.append("| config | trades | PF | return% | maxDD% | win% | hold |")
        out.append("|--------|-------:|---:|--------:|-------:|-----:|-----:|")
        out.append(_row("**LIVE (ATR2.0 + 10% TP)**", base))

        # ── Take-profit level (ATR stop unchanged) ──
        for tp in (0.06, 0.08, 0.12, 0.15, 0.20, 0.0):
            lbl = f"TP {tp*100:.0f}%" if tp > 0 else "TP off (stop/strategy only)"
            out.append(_row(lbl, _run(val, symbol, take_profit_pct=tp)))

        # ── Trailing stop (replaces ATR stop when it arms; activation 3%) ──
        for tr in (0.03, 0.05, 0.08):
            out.append(_row(f"trail {tr*100:.0f}% (act 3%), keep 10% TP",
                            _run(val, symbol, trail_stop_pct=tr,
                                 trail_stop_activation_pct=0.03)))
        # trailing + no hard TP (let winners run to the trail)
        for tr in (0.05, 0.08):
            out.append(_row(f"trail {tr*100:.0f}%, NO hard TP",
                            _run(val, symbol, trail_stop_pct=tr,
                                 trail_stop_activation_pct=0.03, take_profit_pct=0.0)))

        # ── Partial TP: take half at X, let the rest ride to 10% ──
        for ptp in (0.04, 0.05, 0.07):
            out.append(_row(f"partial 50% @ {ptp*100:.0f}%, rest to 10% TP",
                            _run(val, symbol, partial_tp_pct=ptp, partial_tp_size=0.5)))

        # ── ATR stop multiplier ──
        for mult in (1.5, 2.5, 3.0):
            out.append(_row(f"ATR stop ×{mult} (keep 10% TP)",
                            _run(val, symbol, atr_sl_mult=mult)))

        for line in out[-25:]:
            if line.startswith("|") and "trades" not in line and "---" not in line:
                print("  " + line)

    path = os.path.join("logs", f"strategy_exit_sweep_{today}.md")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    print(f"\n  Saved → {path}")


if __name__ == "__main__":
    main()
