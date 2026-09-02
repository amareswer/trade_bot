"""
Research: do the two live-only BUY overlays earn their keep?

bot/main.py vetoes a strategy BUY on top of the validated IndicatorStrategy with:
  - the MTF gate      — 1D 9/21-EMA trend must not be BEARISH  (section 2c)
  - the external gate  — Fear & Greed index must be <= 75       (section 2d)

Neither is in the validated fingerprint — both were bolted on AFTER the
walk-forward that established the edge, and the backtest engine never modelled
them. So live has been strictly more conservative than the thing that was
proven to have an edge, with no measurement of whether that helps or hurts.

This script runs the SAME candle set four ways per symbol
(baseline / +MTF / +FNG / +both) with the real live strategy config
(engine_kwargs_from_cfg(cfg)) and compares trades / PF / return / drawdown /
Sharpe. It changes nothing in the live system.

    .venv/bin/python mtf_overlay_backtest.py

Writes logs/mtf_overlay_backtest_<YYYYMMDD>.md.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

logging.basicConfig(level=logging.ERROR)

from config import cfg
from bot.data.historical_feed import fetch_candles_paginated
from bot.backtest import engine, metrics as metrics_mod
from bot.backtest.params import engine_kwargs_from_cfg

SYMBOLS       = ["BTC/USDT", "SOL/USDT"]   # BTC = canonical fingerprint pair; SOL is live
EXCHANGE      = "binance"
TF_4H         = "4h"
CANDLE_LIMIT  = int(os.environ.get("OVERLAY_LIMIT", "5000"))
DAILY_LIMIT   = 1500
FNG_URL       = "https://api.alternative.me/fng/?limit=0&format=json"


def _fetch_fng_history() -> dict[_dt.date, int]:
    """{date: Fear&Greed value} for the whole alternative.me history (~2018→now)."""
    import requests

    r = requests.get(FNG_URL, timeout=15)
    r.raise_for_status()
    out: dict[_dt.date, int] = {}
    for e in r.json().get("data", []):
        d = _dt.datetime.utcfromtimestamp(int(e["timestamp"])).date()
        out[d] = int(e["value"])
    return out


def _daily_closes(symbol: str) -> list[tuple[_dt.date, float]]:
    candles = fetch_candles_paginated(EXCHANGE, symbol, "1d", DAILY_LIMIT)
    return [(c.timestamp.date(), c.close) for c in candles]


def _run(candles, symbol, **overlay_kwargs):
    kw = engine_kwargs_from_cfg(cfg)
    kw.update(symbol=symbol, fee_pct=cfg.backtest.fee_pct)
    kw.update(overlay_kwargs)
    result = engine.run(candles=candles, **kw)
    m = metrics_mod.compute(result)
    rs = result.rejection_stats
    return {
        "trades":   m.total_trades,
        "win":      m.win_rate * 100,
        "pf":       m.profit_factor,
        "ret":      m.total_return_pct * 100,
        "dd":       m.max_drawdown_pct * 100,
        "sharpe":   m.sharpe_ratio,
        "mtf_veto": rs.get("overlay_mtf_rejected", 0),
        "fng_veto": rs.get("overlay_fng_rejected", 0),
    }


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def main() -> None:
    fng = _fetch_fng_history()
    print(f"  Fear & Greed history: {len(fng)} days "
          f"({min(fng)} → {max(fng)})")

    lines: list[str] = []
    today = _dt.date.today().strftime("%Y%m%d")
    lines.append(f"# MTF + Fear&Greed overlay backtest — {_dt.date.today().isoformat()}\n")
    lines.append(
        "Same candle set run 4 ways with the live strategy config "
        f"(`engine_kwargs_from_cfg`). Exchange {EXCHANGE}, {CANDLE_LIMIT}×4h rolling.\n\n"
        "- **baseline** — validated strategy only (what walk-forward proved)\n"
        "- **+MTF** — plus the 1D 9/21-EMA BEARISH veto\n"
        "- **+FNG** — plus the Fear&Greed > 75 veto\n"
        "- **+both** — both overlays, as live runs today\n"
    )

    for symbol in SYMBOLS:
        print(f"\n=== {symbol} ===")
        candles = fetch_candles_paginated(EXCHANGE, symbol, TF_4H, CANDLE_LIMIT)
        daily   = _daily_closes(symbol)
        span    = f"{candles[0].timestamp.date()} → {candles[-1].timestamp.date()}"
        print(f"  {len(candles)} × 4h  ({span}) | {len(daily)} daily closes")

        configs = {
            "baseline": {},
            "+MTF":     dict(mtf_daily_closes=daily),
            "+FNG":     dict(fng_by_date=fng),
            "+both":    dict(mtf_daily_closes=daily, fng_by_date=fng),
        }
        rows = {name: _run(candles, symbol, **kw) for name, kw in configs.items()}

        lines.append(f"\n## {symbol}  ({len(candles)}×4h, {span})\n")
        lines.append("| config | trades | win% | PF | return% | maxDD% | Sharpe | MTF vetoes | FNG vetoes |")
        lines.append("|--------|-------:|-----:|---:|--------:|-------:|-------:|-----------:|-----------:|")
        for name, r in rows.items():
            lines.append(
                f"| {name} | {r['trades']} | {r['win']:.1f} | {_fmt_pf(r['pf'])} | "
                f"{r['ret']:+.1f} | {r['dd']:.1f} | {r['sharpe']:.2f} | "
                f"{r['mtf_veto']} | {r['fng_veto']} |"
            )
            print(f"  {name:10s} trades={r['trades']:3d}  PF={_fmt_pf(r['pf']):>5s}  "
                  f"ret={r['ret']:+7.1f}%  DD={r['dd']:6.1f}%  Sharpe={r['sharpe']:.2f}  "
                  f"(MTF veto {r['mtf_veto']}, FNG veto {r['fng_veto']})")

        b = rows["baseline"]
        verdict = []
        for name in ("+MTF", "+FNG", "+both"):
            r = rows[name]
            better_pf = r["pf"] > b["pf"] if b["pf"] != float("inf") else False
            better_dd = r["dd"] > b["dd"]          # less negative = shallower
            better_sh = r["sharpe"] > b["sharpe"]
            tag = "HELPS" if (better_sh and (better_pf or better_dd)) else (
                  "NEUTRAL" if r["trades"] == b["trades"] else "HURTS")
            verdict.append(f"- **{name}**: {tag} "
                           f"(ΔPF {r['pf']-b['pf']:+.2f}, ΔSharpe {r['sharpe']-b['sharpe']:+.2f}, "
                           f"ΔmaxDD {r['dd']-b['dd']:+.1f}pp, Δtrades {r['trades']-b['trades']:+d})")
        lines.append("\n" + "\n".join(verdict) + "\n")

    lines.append(
        "\n## Reading this\n\n"
        "Position sizing is the live ATR-risk-capped model, so absolute return / "
        "Sharpe are compressed — **PF, trade count and maxDD carry the signal**. "
        "Each config runs the identical candle set; only the overlay-vetoed subset "
        "of BUYs differs, so ΔPF answers \"do the trades the overlay removes have a "
        "better or worse win/loss ratio than the ones it keeps?\"\n\n"
        "Single ~2.3-year window, not walk-forward — treat as directional, and "
        "re-run on a second window before acting if the deltas are small.\n"
    )

    out_path = os.path.join("logs", f"mtf_overlay_backtest_{today}.md")
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n  Saved → {out_path}")


if __name__ == "__main__":
    main()
