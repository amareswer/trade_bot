#!/usr/bin/env python
"""
atr_sl_experiment.py — test whether an ATR-scaled stop-loss rescues the
near-miss alts from the 2026-07-03 USD screen.

Hypothesis:
  The 79–90% SL-exit rate on every alt tested is caused by the fixed 1.5% stop
  being too tight for alt volatility (BTC 4h ATR ~1–2% of price; alts often
  2–5%), not by the entries lacking edge. If true, an ATR-scaled stop should
  cut the SL-exit rate and lift PF on symbols whose entries were near-misses
  on PF (SYN, LINK) — and should NOT materially change BTC (control).

Design:
  - Same config as screen_universe.py (reads cfg from .env), same 3 windows
    (5000/3000/1000 × 4h), same 0.8% fee — the fixed-SL baseline here must
    reproduce the screen's numbers, which validates the harness.
  - Variants: fixed SL 1.5% (baseline) vs pure ATR SL at 1.5/2.0/2.5/3.0 ×
    ATR(14) captured at entry (stop_loss_pct=0 so only the ATR stop applies).
  - Symbols: SYN, LINK (PF near-misses), XRP (known 87% SL failure — direct
    hypothesis test), BTC (control — must stay healthy).

Pass gate (same as screen): all 3 windows PF ≥ 1.2, full-window trades ≥ 10,
full-window SL-exit rate ≤ 70%.

RESEARCH ONLY. No .env or live-config change follows from this script alone —
any adoption requires the full Validation Discipline workflow in CLAUDE.md.

Usage:
  python atr_sl_experiment.py                    # default symbol set
  ATR_EXP_SYMBOLS=SYN,LINK python atr_sl_experiment.py
"""
import logging
import os
import time
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

from config import cfg
from bot.backtest import engine, metrics as metrics_mod
from bot.data.historical_feed import fetch_candles_paginated

# ── Experiment matrix ─────────────────────────────────────────────────────────
SYMBOLS  = os.getenv("ATR_EXP_SYMBOLS", "SYN,LINK,XRP,BTC").split(",")
WINDOWS  = [5000, 3000, 1000]
VARIANTS = [
    # (label, stop_loss_pct, atr_sl_mult)
    ("fixed1.5%", 0.015, 0.0),   # baseline — must reproduce screen numbers
    ("ATRx1.5",   0.0,   1.5),
    ("ATRx2.0",   0.0,   2.0),
    ("ATRx2.5",   0.0,   2.5),
    ("ATRx3.0",   0.0,   3.0),
]

TIMEFRAME  = os.getenv("BACKTEST_TIMEFRAME", "4h")
FEE        = float(os.getenv("BACKTEST_FEE_PCT", "0.008"))
MIN_PF     = 1.2
MIN_TRADES = 10
MAX_SL     = 0.70

REPORT_PATH = os.path.join(
    "logs", f"atr_sl_experiment_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
)


def run_window(candles: list, n: int, stop_loss_pct: float, atr_sl_mult: float) -> dict:
    """Same engine call as screen_universe._run_window, with SL params swapped."""
    window = candles[-n:] if len(candles) >= n else candles
    if len(window) < 100:
        return {"trades": 0, "pf": 0.0, "sl_rate": 0.0, "win": 0.0,
                "ret": 0.0, "usable": False}

    result = engine.run(
        candles                  = window,
        symbol                   = "ATR_EXP",
        timeframe                = TIMEFRAME,
        strategy_mode            = cfg.strategy.mode,
        starting_cash            = cfg.portfolio.starting_cash,
        risk_per_trade_pct       = cfg.risk.risk_per_trade_pct,
        fee_pct                  = FEE,
        cooldown_ticks           = cfg.risk.cooldown_ticks,
        rsi_period               = cfg.strategy.rsi_period,
        rsi_oversold             = cfg.strategy.rsi_oversold,
        rsi_overbought           = cfg.strategy.rsi_overbought,
        fast_ema_period          = cfg.strategy.fast_ema_period,
        slow_ema_period          = cfg.strategy.slow_ema_period,
        adx_period               = cfg.strategy.adx_period,
        adx_threshold            = cfg.strategy.adx_threshold,
        adx_max                  = cfg.strategy.adx_max,
        min_ema_spread_pct       = cfg.strategy.min_ema_spread_pct,
        max_ema_spread_pct       = cfg.strategy.max_ema_spread_pct,
        rsi_filter_enabled       = cfg.strategy.rsi_filter_enabled,
        buy_threshold            = cfg.strategy.buy_threshold,
        sell_threshold           = cfg.strategy.sell_threshold,
        max_position_pct         = cfg.risk.max_position_pct,
        daily_loss_limit_pct     = cfg.risk.daily_loss_limit_pct,
        max_drawdown_pct         = 0.25,
        max_trades_per_day       = cfg.risk.max_trades_per_day,
        stop_loss_pct            = stop_loss_pct,
        take_profit_pct          = cfg.backtest.take_profit_pct,
        trail_stop_pct           = cfg.backtest.trail_stop_pct,
        trail_stop_activation_pct= cfg.backtest.trail_stop_activation_pct,
        partial_tp_pct           = cfg.backtest.partial_tp_pct,
        partial_tp_size          = cfg.backtest.partial_tp_size,
        regime_ema_period        = cfg.strategy.regime_ema_period,
        regime_ema_slope_filter  = cfg.strategy.regime_ema_slope_filter,
        volume_k                 = cfg.strategy.volume_k,
        atr_volatile_multiplier  = cfg.strategy.atr_volatile_multiplier,
        atr_sl_mult              = atr_sl_mult,
    )

    m        = metrics_mod.compute(result)
    sells    = [f for f in result.fills if f.side == "SELL"]
    sl_exits = [f for f in sells if f.reason == "stop_loss"]

    return {
        "trades":  m.total_trades,
        "pf":      m.profit_factor if m.profit_factor != float("inf") else 99.0,
        "sl_rate": (len(sl_exits) / len(sells)) if sells else 0.0,
        "win":     m.win_rate * 100,
        "ret":     m.total_return_pct * 100,
        "usable":  True,
    }


def verdict(rows: dict) -> tuple[str, str]:
    """rows: {window_size: result}. Returns (PASS/FAIL, reason)."""
    full = rows[WINDOWS[0]]
    if not full["usable"] or full["trades"] < MIN_TRADES:
        return "THIN", f"only {full['trades']} trades in full window"
    if full["sl_rate"] > MAX_SL:
        return "FAIL", f"SL rate {full['sl_rate']*100:.0f}% > {MAX_SL*100:.0f}%"
    low = [w for w in WINDOWS if rows[w]["usable"] and rows[w]["pf"] < MIN_PF]
    if low:
        return "FAIL", "PF < %.1f in %s" % (
            MIN_PF, ", ".join(f"{w}c ({rows[w]['pf']:.2f})" for w in low))
    return "PASS", "all windows PF ≥ %.1f, SL ≤ %.0f%%" % (MIN_PF, MAX_SL * 100)


def main() -> None:
    print(f"\nATR SL experiment — {TIMEFRAME} candles, fee {FEE*100:.2f}%")
    print(f"Symbols: {SYMBOLS}   Variants: {[v[0] for v in VARIANTS]}\n")

    report = [
        f"# ATR Stop-Loss Experiment — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        "Hypothesis: fixed 1.5% SL is too tight for alt volatility (79–90% SL-exit",
        "rates on every alt screened); an ATR-scaled stop should cut stop-outs and",
        "lift PF on entry near-misses (SYN, LINK). XRP = failed-symbol test,",
        "BTC = control.",
        "",
        f"Config: screen_universe baseline (cfg from .env), fee {FEE*100:.2f}%, "
        f"TP {cfg.backtest.take_profit_pct*100:.0f}%, windows {WINDOWS}.",
        f"Gate: PF ≥ {MIN_PF} all windows, trades ≥ {MIN_TRADES}, SL rate ≤ {MAX_SL*100:.0f}%.",
        "",
    ]

    for base in SYMBOLS:
        base = base.strip().upper()
        sym  = f"{base}/USDT"
        print(f"── {sym} ──────────────────────────────────────")
        print(f"  Fetching {WINDOWS[0]} × {TIMEFRAME} candles from Binance …", flush=True)
        try:
            candles = fetch_candles_paginated(
                exchange_id="binance", symbol=sym,
                timeframe=TIMEFRAME, total_limit=WINDOWS[0],
            )
        except Exception as exc:
            print(f"  ERROR: fetch failed — {exc}\n")
            report += [f"## {sym}", "", f"**Fetch failed:** {exc}", ""]
            continue
        print(f"  {len(candles)} candles "
              f"({candles[0].timestamp.strftime('%Y-%m-%d')} → "
              f"{candles[-1].timestamp.strftime('%Y-%m-%d')})")

        report += [
            f"## {sym}  ({len(candles)} candles, "
            f"{candles[0].timestamp.strftime('%Y-%m-%d')} → "
            f"{candles[-1].timestamp.strftime('%Y-%m-%d')})",
            "",
            "| Variant | " + " | ".join(f"{w}c PF" for w in WINDOWS)
            + " | Trades | Win% | SL rate | Return | Verdict |",
            "|---------|" + "------|" * (len(WINDOWS) + 5),
        ]

        for label, sl_pct, mult in VARIANTS:
            t0 = time.time()
            rows = {w: run_window(candles, w, sl_pct, mult) for w in WINDOWS}
            v, why = verdict(rows)
            full = rows[WINDOWS[0]]
            pf_cells = " | ".join(
                f"{rows[w]['pf']:.2f}" if rows[w]["usable"] else "—" for w in WINDOWS
            )
            line = (
                f"| {label} | {pf_cells} | {full['trades']} | {full['win']:.0f}% "
                f"| {full['sl_rate']*100:.0f}% | {full['ret']:+.2f}% | {v} — {why} |"
            )
            report.append(line)
            print(
                f"  {label:<10} "
                + "  ".join(f"{w}c PF={rows[w]['pf']:.2f}" for w in WINDOWS)
                + f"  trades={full['trades']}  SL={full['sl_rate']*100:.0f}%"
                f"  {v}  ({time.time()-t0:.0f}s)",
                flush=True,
            )
        report.append("")
        print()

    report += [
        "---",
        "",
        "Adoption path if any variant PASSes on an alt: full Validation Discipline",
        "workflow (CLAUDE.md) — this experiment alone changes nothing live.",
        "BTC control: any ATR variant that degrades BTC below the fixed-SL baseline",
        "must not be applied globally; use per-symbol exit config instead.",
    ]

    os.makedirs("logs", exist_ok=True)
    with open(REPORT_PATH, "w") as fh:
        fh.write("\n".join(report) + "\n")
    print(f"Report written → {REPORT_PATH}")


if __name__ == "__main__":
    main()
