"""
Research: is the crypto strategy too selective? Sweep the two "how picky is it"
knobs (ADX threshold, EMA-spread floor) + a few structural variants, on the
validated backtest windows. Answers "would loosening it trade more AND make
more, or just trade more and lose the edge".

Changes nothing live. Run:
    .venv/bin/python strategy_selectivity_sweep.py
Writes logs/strategy_selectivity_sweep_<date>.md
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

logging.basicConfig(level=logging.ERROR)

from config import cfg
from bot.data.historical_feed import fetch_candles_paginated, slice_candles
from bot.backtest import engine, metrics as metrics_mod
from bot.backtest.params import engine_kwargs_from_cfg

SYMBOLS = ["BTC/USDT", "SOL/USDT"]
EXCHANGE = "binance"
# validation window = the out-of-sample half walkforward.py uses
VAL_START = "2025-02-22"

ADX_GRID    = [12.0, 15.0, 18.0, 22.0, 25.0]      # 18 = live
SPREAD_GRID = [0.002, 0.003, 0.004, 0.006, 0.008]  # 0.004 = live


def _metrics(candles, symbol, **overrides):
    kw = engine_kwargs_from_cfg(cfg)
    kw.update(symbol=symbol)
    kw.update(overrides)
    m = metrics_mod.compute(engine.run(candles=candles, **kw))
    return m.total_trades, m.profit_factor, m.total_return_pct * 100, m.max_drawdown_pct * 100, m.win_rate * 100


def _pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def main():
    today = _dt.date.today().strftime("%Y%m%d")
    out = [f"# Strategy selectivity sweep — {_dt.date.today().isoformat()}\n",
           f"Out-of-sample window: {VAL_START} → present ({EXCHANGE}, 4h). Live config: "
           f"ADX≥{cfg.strategy.adx_threshold:g}, EMA spread≥{cfg.strategy.min_ema_spread_pct*100:.1f}%, "
           f"trend must be BULLISH, price>200EMA.\n"]

    for symbol in SYMBOLS:
        print(f"\n=== {symbol} ===")
        allc = fetch_candles_paginated(EXCHANGE, symbol, "4h", 5000)
        val  = slice_candles(allc, VAL_START, None)
        print(f"  {len(val)} OOS candles ({val[0].timestamp.date()} → {val[-1].timestamp.date()})")

        base = _metrics(val, symbol)
        out.append(f"\n## {symbol}  ({len(val)} OOS 4h candles)\n")
        out.append(f"**Live config baseline:** {base[0]} trades · PF {_pf(base[1])} · "
                   f"return {base[2]:+.1f}% · maxDD {base[3]:.1f}% · win {base[4]:.0f}%\n")

        # ── ADX threshold sweep (spread fixed at live) ──
        out.append("\n### ADX threshold (EMA spread fixed at live 0.4%)\n")
        out.append("| ADX≥ | trades | PF | return% | maxDD% | win% |")
        out.append("|-----:|-------:|---:|--------:|-------:|-----:|")
        for adx in ADX_GRID:
            t, pf, r, dd, w = _metrics(val, symbol, adx_threshold=adx)
            tag = "  ← live" if adx == 18.0 else ""
            out.append(f"| {adx:g}{tag} | {t} | {_pf(pf)} | {r:+.1f} | {dd:.1f} | {w:.0f} |")
            print(f"  ADX≥{adx:<4g} trades={t:3d} PF={_pf(pf):>5s} ret={r:+6.1f}% dd={dd:6.1f}%")

        # ── EMA spread sweep (ADX fixed at live) ──
        out.append("\n### EMA-spread floor (ADX fixed at live 18)\n")
        out.append("| spread≥ | trades | PF | return% | maxDD% | win% |")
        out.append("|--------:|-------:|---:|--------:|-------:|-----:|")
        for sp in SPREAD_GRID:
            t, pf, r, dd, w = _metrics(val, symbol, min_ema_spread_pct=sp)
            tag = "  ← live" if abs(sp - 0.004) < 1e-9 else ""
            out.append(f"| {sp*100:.1f}%{tag} | {t} | {_pf(pf)} | {r:+.1f} | {dd:.1f} | {w:.0f} |")
            print(f"  spread≥{sp*100:.1f}% trades={t:3d} PF={_pf(pf):>5s} ret={r:+6.1f}% dd={dd:6.1f}%")

        # ── Structural variants ──
        out.append("\n### Structural variants\n")
        out.append("| variant | trades | PF | return% | maxDD% | win% |")
        out.append("|---------|-------:|---:|--------:|-------:|-----:|")
        variants = {
            "live baseline":            {},
            "drop 200-EMA macro filter": dict(regime_ema_period=0),
            "ADX≥15 + spread≥0.3%":      dict(adx_threshold=15.0, min_ema_spread_pct=0.003),
            "ADX≥12 + spread≥0.2%":      dict(adx_threshold=12.0, min_ema_spread_pct=0.002),
        }
        for name, ov in variants.items():
            t, pf, r, dd, w = _metrics(val, symbol, **ov)
            out.append(f"| {name} | {t} | {_pf(pf)} | {r:+.1f} | {dd:.1f} | {w:.0f} |")
            print(f"  {name:28s} trades={t:3d} PF={_pf(pf):>5s} ret={r:+6.1f}%")

    path = os.path.join("logs", f"strategy_selectivity_sweep_{today}.md")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    print(f"\n  Saved → {path}")


if __name__ == "__main__":
    main()
