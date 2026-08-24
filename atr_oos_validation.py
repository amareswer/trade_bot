#!/usr/bin/env python
"""
atr_oos_validation.py — out-of-sample train/validation split for a single
ATR stop-loss multiplier on a single symbol.

Why this exists: atr_sl_experiment.py (2026-07-16) found SOL/USDT ATRx2.0
PASSES the 3-window screen gate (5000c/3000c/1000c PF >= 1.2), but those
windows are NESTED slices of one fetch (1000c sits entirely inside 3000c
sits entirely inside 5000c) — a single passing multiplier with both
neighbors (ATRx1.5, ATRx2.5) failing on those same nested windows is
curve-fit risk, not proof of edge. CLAUDE.md flags a genuine OOS split
(walkforward.py-style: non-overlapping train and validation periods) as
the required next step before SOL could be considered a candidate.

This script does that: fetch once, split by DATE into two non-overlapping
halves, run the SAME atr_sl_mult on both, and compare. If PF collapses on
the validation half, the training result was curve fitting (same
interpretation rule walkforward.py uses for the core BTC strategy).

RESEARCH ONLY. No .env or live-config change follows from this script
alone — any adoption requires the full Validation Discipline workflow.

Usage:
  python atr_oos_validation.py                          # SOL/USDT, ATRx2.0 (default)
  SYMBOL=SOL/USDT ATR_MULT=2.0 python atr_oos_validation.py
  SYMBOL=BTC/USDT ATR_MULT=2.0 python atr_oos_validation.py

── ATR_RISK_SIZING (added 2026-08-24) ──────────────────────────────────────
NOT new sizing logic — bot.backtest.engine.run() has taken an atr_risk_sizing
flag since 2026-07-17 (the same day ATR_SIZING_ENABLED went live for BTC/CAD,
implementing the identical dollar-risk-cap formula as config.py's
calc_trade_qty_atr_risk() — position_size = risk_budget / stop_distance, so a
wider ATR stop sizes DOWN, never up). This script just never passed it
through, which means the 2026-07-17 SOL/BTC/SYN/LINK OOS runs in logs/
atr_oos_*_20260717.md were all evaluated under flat notional sizing — the
ATR stop distance was tested, but the paired risk-cap that makes a wider
stop NOT a bigger bet was not. Opt-in, default off, so re-running this
script with no env override reproduces the exact 2026-07-17 methodology
unchanged:
  ATR_RISK_SIZING=true python atr_oos_validation.py                # opt in
  ATR_RISK_SIZING=true ATR_RISK_SIZING_BASELINE_SL_PCT=0.015 \\
      SYMBOL=SOL/USDT python atr_oos_validation.py
"""
import logging
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

from config import cfg
from bot.backtest import engine, metrics as metrics_mod
from bot.data.historical_feed import fetch_candles_paginated, slice_candles

SYMBOL     = os.getenv("SYMBOL", "SOL/USDT")
ATR_MULT   = float(os.getenv("ATR_MULT", "2.0"))
TIMEFRAME  = os.getenv("BACKTEST_TIMEFRAME", "4h")
FEE        = float(os.getenv("BACKTEST_FEE_PCT", "0.008"))
TOTAL_LIMIT = int(os.getenv("BACKTEST_LIMIT", "5000"))

# Opt-in, default off — see module docstring. Mirrors ATR_SIZING_ENABLED /
# calc_trade_qty_atr_risk() exactly; this does not add a new sizing method,
# it exercises the one that's already live for BTC/CAD.
ATR_RISK_SIZING = os.getenv("ATR_RISK_SIZING", "false").strip().lower() in ("1", "true", "yes")
ATR_RISK_SIZING_BASELINE_SL_PCT = float(os.getenv("ATR_RISK_SIZING_BASELINE_SL_PCT", "0.015"))

MIN_PF     = 1.2
MIN_TRADES = 10
MAX_SL     = 0.70

REPORT_PATH = os.path.join(
    "logs",
    f"atr_oos_{SYMBOL.split('/')[0]}_{ATR_MULT}"
    f"{'_sized' if ATR_RISK_SIZING else ''}"
    f"_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md",
)


def run_period(candles, stop_loss_pct: float, atr_sl_mult: float) -> dict:
    if len(candles) < 100:
        return {"trades": 0, "pf": 0.0, "sl_rate": 0.0, "win": 0.0,
                "ret": 0.0, "usable": False}

    result = engine.run(
        candles                  = candles,
        symbol                   = SYMBOL,
        timeframe                = TIMEFRAME,
        strategy_mode            = cfg.strategy.mode,
        starting_cash            = cfg.portfolio.starting_cash,
        risk_per_trade_pct       = cfg.risk.risk_per_trade_pct,
        fee_pct                  = FEE,
        cooldown_ticks           = cfg.risk.cooldown_ticks,
        rsi_period                = cfg.strategy.rsi_period,
        rsi_oversold              = cfg.strategy.rsi_oversold,
        rsi_overbought            = cfg.strategy.rsi_overbought,
        fast_ema_period           = cfg.strategy.fast_ema_period,
        slow_ema_period           = cfg.strategy.slow_ema_period,
        adx_period                = cfg.strategy.adx_period,
        adx_threshold             = cfg.strategy.adx_threshold,
        adx_max                   = cfg.strategy.adx_max,
        min_ema_spread_pct        = cfg.strategy.min_ema_spread_pct,
        max_ema_spread_pct        = cfg.strategy.max_ema_spread_pct,
        rsi_filter_enabled        = cfg.strategy.rsi_filter_enabled,
        buy_threshold             = cfg.strategy.buy_threshold,
        sell_threshold            = cfg.strategy.sell_threshold,
        max_position_pct          = cfg.risk.max_position_pct,
        daily_loss_limit_pct      = cfg.risk.daily_loss_limit_pct,
        max_drawdown_pct          = 0.25,
        max_trades_per_day        = cfg.risk.max_trades_per_day,
        stop_loss_pct             = stop_loss_pct,
        take_profit_pct           = cfg.backtest.take_profit_pct,
        trail_stop_pct            = cfg.backtest.trail_stop_pct,
        trail_stop_activation_pct = cfg.backtest.trail_stop_activation_pct,
        partial_tp_pct            = cfg.backtest.partial_tp_pct,
        partial_tp_size           = cfg.backtest.partial_tp_size,
        regime_ema_period         = cfg.strategy.regime_ema_period,
        regime_ema_slope_filter   = cfg.strategy.regime_ema_slope_filter,
        volume_k                  = cfg.strategy.volume_k,
        atr_volatile_multiplier   = cfg.strategy.atr_volatile_multiplier,
        atr_sl_mult               = atr_sl_mult,
        atr_risk_sizing            = ATR_RISK_SIZING,
        atr_sizing_baseline_sl_pct = ATR_RISK_SIZING_BASELINE_SL_PCT,
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


def verdict_row(r: dict) -> str:
    if not r["usable"] or r["trades"] < MIN_TRADES:
        return f"THIN — only {r['trades']} trades"
    if r["sl_rate"] > MAX_SL:
        return f"FAIL — SL rate {r['sl_rate']*100:.0f}% > {MAX_SL*100:.0f}%"
    if r["pf"] < MIN_PF:
        return f"FAIL — PF {r['pf']:.2f} < {MIN_PF}"
    return f"PASS — PF {r['pf']:.2f} >= {MIN_PF}, SL {r['sl_rate']*100:.0f}%"


def main() -> None:
    print(f"\nATR OOS validation — {SYMBOL} ATRx{ATR_MULT}, {TIMEFRAME} candles, fee {FEE*100:.2f}%\n")

    print(f"Fetching {TOTAL_LIMIT} × {TIMEFRAME} candles from Binance …", flush=True)
    candles = fetch_candles_paginated(
        exchange_id="binance", symbol=SYMBOL,
        timeframe=TIMEFRAME, total_limit=TOTAL_LIMIT,
    )
    start_dt = candles[0].timestamp
    end_dt   = candles[-1].timestamp
    mid_dt   = start_dt + (end_dt - start_dt) / 2
    mid_date = mid_dt.strftime("%Y-%m-%d")

    print(f"  {len(candles)} candles ({start_dt:%Y-%m-%d} → {end_dt:%Y-%m-%d})")
    print(f"  Split at midpoint: {mid_date}  (train / validation, non-overlapping)\n")

    train = slice_candles(candles, start_dt.strftime("%Y-%m-%d"), mid_date)
    val   = slice_candles(candles, mid_date, None)

    print(f"  TRAIN:      {len(train)} candles ({train[0].timestamp:%Y-%m-%d} → {train[-1].timestamp:%Y-%m-%d})")
    print(f"  VALIDATION: {len(val)} candles ({val[0].timestamp:%Y-%m-%d} → {val[-1].timestamp:%Y-%m-%d})\n")

    report = [
        f"# ATR OOS Validation — {SYMBOL} ATRx{ATR_MULT} — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        f"Fetched {len(candles)} × {TIMEFRAME} candles ({start_dt:%Y-%m-%d} → {end_dt:%Y-%m-%d}), "
        f"split at midpoint {mid_date} into non-overlapping train/validation halves — "
        "unlike the nested 5000c/3000c/1000c windows in atr_sl_experiment.py, these "
        "share zero candles.",
        "",
        f"Config: cfg from .env (ADX>={cfg.strategy.adx_threshold}, "
        f"EMA spread>={cfg.strategy.min_ema_spread_pct*100:.1f}%, "
        f"RSI filter={'on' if cfg.strategy.rsi_filter_enabled else 'off'}), fee {FEE*100:.2f}%, "
        f"TP {cfg.backtest.take_profit_pct*100:.0f}%.",
        f"Sizing: "
        + (
            f"ATR risk-capped (ATR_RISK_SIZING=true, baseline_sl_pct="
            f"{ATR_RISK_SIZING_BASELINE_SL_PCT*100:.2f}%) — position size = "
            f"(cash × risk_pct × baseline_sl_pct) / (ATR × mult), min()-capped "
            f"against flat notional, on the ATRx{ATR_MULT} rows only (the "
            f"fixed1.5% rows below are always flat notional regardless of this "
            f"flag). Matches config.calc_trade_qty_atr_risk(), live for BTC/CAD "
            f"since 2026-07-17."
            if ATR_RISK_SIZING else
            f"flat notional (cash × risk_pct / price) on every row — "
            f"ATR_RISK_SIZING not set, same methodology as the original "
            f"2026-07-17 run."
        ),
        f"Gate: PF >= {MIN_PF}, trades >= {MIN_TRADES}, SL rate <= {MAX_SL*100:.0f}%.",
        "",
        "| Period | Window | Trades | Win% | PF | SL rate | Return | Verdict |",
        "|--------|--------|--------|------|-----|---------|--------|---------|",
    ]

    results = {}
    for label, period_candles in (("TRAIN", train), ("VALIDATION", val)):
        r_fixed = run_period(period_candles, stop_loss_pct=0.015, atr_sl_mult=0.0)
        r_atr   = run_period(period_candles, stop_loss_pct=0.0,   atr_sl_mult=ATR_MULT)
        results[label] = {"fixed": r_fixed, "atr": r_atr}

        for variant_label, r in (("fixed1.5%", r_fixed), (f"ATRx{ATR_MULT}", r_atr)):
            line = (
                f"| {label} | {variant_label} | {r['trades']} | {r['win']:.0f}% "
                f"| {r['pf']:.2f} | {r['sl_rate']*100:.0f}% | {r['ret']:+.2f}% "
                f"| {verdict_row(r)} |"
            )
            report.append(line)
            print(
                f"  {label:<10} {variant_label:<10} trades={r['trades']:<4} "
                f"win={r['win']:.0f}%  PF={r['pf']:.2f}  SL={r['sl_rate']*100:.0f}%  "
                f"ret={r['ret']:+.2f}%  {verdict_row(r)}"
            )

    # ── Interpretation: does the ATR-variant PF survive the OOS split? ────────
    train_pf = results["TRAIN"]["atr"]["pf"]
    val_pf   = results["VALIDATION"]["atr"]["pf"]
    val_trades = results["VALIDATION"]["atr"]["trades"]

    print(f"\n{'─'*60}")
    report += ["", "## Interpretation", ""]
    if val_trades < MIN_TRADES:
        verdict_text = (
            f"THIN — validation half only has {val_trades} trades "
            f"(< {MIN_TRADES}). Not enough out-of-sample data to confirm or "
            f"reject the in-sample PF {train_pf:.2f}. Needs more history or "
            f"a wider symbol set before treating this as validated."
        )
    elif val_pf >= MIN_PF:
        verdict_text = (
            f"HOLDS — validation PF {val_pf:.2f} >= {MIN_PF} "
            f"(train was {train_pf:.2f}). The ATRx{ATR_MULT} edge on {SYMBOL} "
            f"survives a genuine out-of-sample split — this is real evidence, "
            f"not the in-sample-only nested-window result from 2026-07-16."
        )
    elif val_pf >= 1.0:
        verdict_text = (
            f"MARGINAL — validation PF {val_pf:.2f} is above 1.0 but below "
            f"the {MIN_PF} gate (train was {train_pf:.2f}). Weak evidence; "
            f"do not promote without more data."
        )
    else:
        verdict_text = (
            f"FAILED — validation PF {val_pf:.2f} collapsed out-of-sample "
            f"(train was {train_pf:.2f}). The 2026-07-16 in-sample PASS for "
            f"{SYMBOL} ATRx{ATR_MULT} was curve fitting on the nested windows. "
            f"Do not pursue this configuration further."
        )
    print(f"  {verdict_text}")
    report.append(verdict_text)
    report += [
        "",
        "---",
        "",
        "This script changes nothing live. Any promotion requires the full "
        "Validation Discipline workflow in CLAUDE.md (walk-forward pass, hash "
        "stamp, whitelist update) in addition to this OOS check.",
    ]

    os.makedirs("logs", exist_ok=True)
    with open(REPORT_PATH, "w") as fh:
        fh.write("\n".join(report) + "\n")
    print(f"\nReport written → {REPORT_PATH}\n")


if __name__ == "__main__":
    main()
