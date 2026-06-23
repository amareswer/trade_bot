"""
1D swing trading parameter sweep — BTC/USDT on Binance.

Fetches 5000 × 1d candles and sweeps six SL/TP combinations.
All strategy params are hardcoded here for reproducibility — does not read
from .env so this sweep is always comparable run-to-run.

Usage:
    python swing_backtest.py
"""
import csv
import logging
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

from bot.data.historical_feed import fetch_candles_paginated
from bot.backtest import engine, metrics as metrics_mod

# ── Fetch once, sweep many times ─────────────────────────────────────────────
EXCHANGE    = "binance"
SYMBOL      = "BTC/USDT"
TIMEFRAME   = "1d"
TOTAL_LIMIT = 5000

# ── Fixed strategy settings (hardcoded — do NOT read from .env) ───────────────
FIXED = dict(
    strategy_mode           = "indicator",
    starting_cash           = 10_000.0,
    risk_per_trade_pct      = 0.10,
    fee_pct                 = 0.008,
    cooldown_ticks          = 3,
    adx_period              = 14,
    adx_threshold           = 18.0,
    rsi_period              = 14,
    rsi_oversold            = 30.0,
    rsi_overbought          = 70.0,
    fast_ema_period         = 9,
    slow_ema_period         = 21,
    rsi_filter_enabled      = True,
    max_position_pct        = 0.50,
    daily_loss_limit_pct    = 0.10,
    max_drawdown_pct        = 0.25,
    max_trades_per_day      = 999,
    regime_enabled          = True,
    bb_period               = 20,
    bb_std_dev              = 2.0,
    mr_rsi_oversold         = 35.0,
    mr_rsi_overbought       = 65.0,
    atr_volatile_multiplier = 1.5,
    volume_k                = 0.0,
    macd_enabled            = True,
    regime_ema_period       = 200,
    regime_ema_slope_filter = False,
    slippage_pct            = 0.0,
    trail_stop_pct          = 0.0,
    partial_tp_pct          = 0.0,
    partial_tp_size         = 0.5,
    atr_sl_enabled          = False,
    atr_sl_multiplier       = 2.0,
)

# ── SL/TP sweep combinations ─────────────────────────────────────────────────
SWEEP = [
    (0.02, 0.10),
    (0.03, 0.15),
    (0.03, 0.20),
    (0.04, 0.20),
    (0.04, 0.25),
    (0.05, 0.25),
]


def verdict(pf: float, trades: int) -> str:
    if pf >= 1.3 and trades >= 10:
        return "PASS"
    if pf >= 1.0 and trades >= 10:
        return "MARGINAL"
    return "FAIL"


def main():
    print(f"\n  Swing Backtest — {SYMBOL} {TIMEFRAME} × {TOTAL_LIMIT} candles")
    print(f"  Exchange: {EXCHANGE}  |  Fee: {FIXED['fee_pct']*100:.1f}%  |  Cash: ${FIXED['starting_cash']:,.0f}\n")

    try:
        candles = fetch_candles_paginated(
            exchange_id = EXCHANGE,
            symbol      = SYMBOL,
            timeframe   = TIMEFRAME,
            total_limit = TOTAL_LIMIT,
        )
    except Exception as exc:
        print(f"\n  ERROR fetching data: {exc}\n")
        return

    print(
        f"\n  {len(candles)} candles loaded"
        f"  ({candles[0].timestamp.strftime('%Y-%m-%d')}"
        f" → {candles[-1].timestamp.strftime('%Y-%m-%d')})\n"
    )

    rows = []

    for sl_pct, tp_pct in SWEEP:
        result = engine.run(
            candles         = candles,
            symbol          = SYMBOL,
            timeframe       = TIMEFRAME,
            stop_loss_pct   = sl_pct,
            take_profit_pct = tp_pct,
            **FIXED,
        )
        m = metrics_mod.compute(result)

        v = verdict(m.profit_factor, m.total_trades)
        rows.append({
            "sl_pct":    sl_pct,
            "tp_pct":    tp_pct,
            "trades":    m.total_trades,
            "win_rate":  m.win_rate,
            "pf":        m.profit_factor,
            "max_dd":    m.max_drawdown_pct,
            "ret":       m.total_return_pct,
            "verdict":   v,
        })

    # ── Print table ───────────────────────────────────────────────────────────
    _B  = "\033[1m"
    _R  = "\033[0m"
    _GR = "\033[92m"
    _YL = "\033[93m"
    _RD = "\033[91m"
    _CY = "\033[96m"

    def _vcol(v: str) -> str:
        return _GR if v == "PASS" else (_YL if v == "MARGINAL" else _RD)

    header = f"{'SL%':>5}  {'TP%':>5}  {'Trades':>6}  {'Win%':>6}  {'PF':>6}  {'MaxDD%':>7}  {'Return%':>8}  {'Verdict'}"
    print(f"\n{_B}{'─'*70}{_R}")
    print(f"  {_B}1D SWING BACKTEST — BTC/USDT{_R}  (fee={FIXED['fee_pct']*100:.1f}%  ADX≥18  RSI filter ON)")
    print(f"{'─'*70}")
    print(f"  {_CY}{header}{_R}")
    print(f"{'─'*70}")

    for r in rows:
        sl_s  = f"{r['sl_pct']*100:.0f}%"
        tp_s  = f"{r['tp_pct']*100:.0f}%"
        win_s = f"{r['win_rate']*100:.1f}%"
        pf_s  = f"{r['pf']:.2f}" if r['pf'] != float('inf') else "∞"
        dd_s  = f"{r['max_dd']*100:.2f}%"
        ret_s = f"{r['ret']*100:.2f}%"
        vc    = _vcol(r['verdict'])
        print(
            f"  {sl_s:>5}  {tp_s:>5}  {r['trades']:>6}  "
            f"{win_s:>6}  {pf_s:>6}  {dd_s:>7}  {ret_s:>8}  "
            f"{vc}{r['verdict']}{_R}"
        )

    print(f"{'─'*70}\n")

    # ── Best config ───────────────────────────────────────────────────────────
    pass_rows = [r for r in rows if r["verdict"] == "PASS"]
    if pass_rows:
        best = max(pass_rows, key=lambda r: r["pf"])
        print(
            f"  {_GR}{_B}BEST CONFIG (highest PF among PASS):{_R}"
            f"  SL={best['sl_pct']*100:.0f}%  TP={best['tp_pct']*100:.0f}%"
            f"  PF={best['pf']:.2f}  Trades={best['trades']}"
            f"  Return={best['ret']*100:.2f}%  MaxDD={best['max_dd']*100:.2f}%\n"
        )
    else:
        marginal = [r for r in rows if r["verdict"] == "MARGINAL"]
        if marginal:
            best = max(marginal, key=lambda r: r["pf"])
            print(
                f"  {_YL}No PASS configs found. Best MARGINAL:"
                f"  SL={best['sl_pct']*100:.0f}%  TP={best['tp_pct']*100:.0f}%"
                f"  PF={best['pf']:.2f}  Trades={best['trades']}{_R}\n"
            )
        else:
            print(f"  {_RD}No PASS or MARGINAL configs — 1d swing strategy does not validate at these params.{_R}\n")

    # ── Save CSV ──────────────────────────────────────────────────────────────
    os.makedirs("logs", exist_ok=True)
    date_str  = datetime.now(timezone.utc).strftime("%Y%m%d")
    csv_path  = os.path.join("logs", f"swing_backtest_1d_{date_str}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["sl_pct", "tp_pct", "trades", "win_pct", "profit_factor",
                         "max_dd_pct", "return_pct", "verdict"])
        for r in rows:
            writer.writerow([
                f"{r['sl_pct']:.3f}",
                f"{r['tp_pct']:.3f}",
                r["trades"],
                f"{r['win_rate']*100:.2f}",
                f"{r['pf']:.4f}",
                f"{r['max_dd']*100:.4f}",
                f"{r['ret']*100:.4f}",
                r["verdict"],
            ])
    print(f"  Saved → {csv_path}\n")


if __name__ == "__main__":
    main()
