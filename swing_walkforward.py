"""
1D swing strategy walk-forward validation.

Purpose: determine if SL=4% TP=25% on 1d candles has a genuine edge
or was just riding the 2017–2026 BTC bull market.

Splits:
  TRAIN   2017-08-17 → 2022-12-31  (bear + bull + crash — 5.5 years)
  VAL_1   2023-01-01 → 2024-06-30  (recovery + ETF approval era)
  VAL_2   2024-07-01 → 2026-06-23  (recent bull + current regime)

Usage:
    python swing_walkforward.py
"""
import logging
logging.basicConfig(level=logging.WARNING)

from bot.data.historical_feed import fetch_candles_paginated, slice_candles
from bot.backtest import engine, metrics as metrics_mod

# ── Data source ───────────────────────────────────────────────────────────────
EXCHANGE    = "binance"
SYMBOL      = "BTC/USDT"
TIMEFRAME   = "1d"
TOTAL_LIMIT = 5000

# ── Walk-forward period boundaries ────────────────────────────────────────────
TRAIN_START = "2017-08-17"
TRAIN_END   = "2023-01-01"   # exclusive (slice_candles end_date is exclusive)

VAL1_START  = "2023-01-01"
VAL1_END    = "2024-07-01"   # exclusive

VAL2_START  = "2024-07-01"
VAL2_END    = None           # through latest

# ── Fixed config — best SL/TP from swing_backtest.py (do NOT read from .env) ─
FIXED = dict(
    strategy_mode           = "indicator",
    starting_cash           = 10_000.0,
    risk_per_trade_pct      = 0.10,
    fee_pct                 = 0.008,
    stop_loss_pct           = 0.04,
    take_profit_pct         = 0.25,
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
    atr_sl_mult             = 0.0,
)


def _run_period(candles, label, start, end):
    """Run engine on a date-sliced period, return (metrics, result) or (None, None)."""
    period = slice_candles(candles, start, end)
    if not period:
        print(f"  ERROR: no candles in {label} ({start} → {end or 'present'})")
        return None, None
    result = engine.run(
        candles   = period,
        symbol    = SYMBOL,
        timeframe = TIMEFRAME,
        **FIXED,
    )
    m = metrics_mod.compute(result)
    return m, result


def _verdict(pf: float, trades: int) -> str:
    if trades < 5:
        return "FAIL"   # too few trades to judge
    if pf >= 1.3:
        return "PASS"
    if pf >= 1.0:
        return "MARGINAL"
    return "FAIL"


def main():
    _B  = "\033[1m"
    _R  = "\033[0m"
    _GR = "\033[92m"
    _YL = "\033[93m"
    _RD = "\033[91m"
    _CY = "\033[96m"

    print(f"\n  Swing Walk-Forward — {SYMBOL} {TIMEFRAME}")
    print(f"  Config: SL=4%  TP=25%  ADX≥18  RSI filter ON  fee=0.8%  cash=$10,000\n")

    try:
        all_candles = fetch_candles_paginated(
            exchange_id = EXCHANGE,
            symbol      = SYMBOL,
            timeframe   = TIMEFRAME,
            total_limit = TOTAL_LIMIT,
        )
    except Exception as exc:
        print(f"\n  ERROR fetching data: {exc}\n")
        return

    print(
        f"\n  {len(all_candles)} candles loaded"
        f"  ({all_candles[0].timestamp.strftime('%Y-%m-%d')}"
        f" → {all_candles[-1].timestamp.strftime('%Y-%m-%d')})\n"
    )

    print("  Running Train period …")
    train_m, train_r = _run_period(all_candles, "Train", TRAIN_START, TRAIN_END)

    print("  Running Val_1 period …")
    val1_m,  val1_r  = _run_period(all_candles, "Val_1", VAL1_START, VAL1_END)

    print("  Running Val_2 period …")
    val2_m,  val2_r  = _run_period(all_candles, "Val_2", VAL2_START, VAL2_END)

    if any(m is None for m in (train_m, val1_m, val2_m)):
        print("\n  One or more periods returned no data — cannot print table.\n")
        return

    # ── Build rows ────────────────────────────────────────────────────────────
    def _candle_count(result):
        return len(result.equity_curve) if hasattr(result, "equity_curve") else "?"

    def _trade_count(result):
        return len([f for f in result.fills if f.side == "BUY"])

    periods = [
        ("Train 2017–2022", train_m, train_r, TRAIN_START, "2022-12-31"),
        ("Val_1 2023–mid24", val1_m, val1_r, VAL1_START, "2024-06-30"),
        ("Val_2 mid24–now",  val2_m, val2_r, VAL2_START, "2026-06-23"),
    ]

    rows = []
    for label, m, r, _s, _e in periods:
        trades  = _trade_count(r)
        candles = _candle_count(r)
        pf      = m.profit_factor
        ret     = (m.final_value / m.starting_cash - 1) * 100
        dd      = m.max_drawdown_pct * 100
        v       = _verdict(pf, trades)
        rows.append({
            "label":   label,
            "candles": candles,
            "trades":  trades,
            "pf":      pf,
            "ret":     ret,
            "dd":      dd,
            "verdict": v,
        })

    # ── Print table ───────────────────────────────────────────────────────────
    def _vcol(v: str) -> str:
        return _GR if v == "PASS" else (_YL if v == "MARGINAL" else _RD)

    BAR = "─" * 80
    print(f"\n{_B}{BAR}{_R}")
    print(f"  {_B}1D SWING WALK-FORWARD — BTC/USDT  (SL=4%  TP=25%  ADX≥18  fee=0.8%){_R}")
    print(f"{BAR}")
    hdr = (
        f"  {'Period':<20} | {'Candles':>7} | {'Trades':>6} | "
        f"{'PF':>5} | {'Return%':>8} | {'MaxDD%':>7} | Verdict"
    )
    print(f"  {_CY}{hdr.strip()}{_R}")
    print(f"{BAR}")

    for r in rows:
        pf_s  = f"{r['pf']:.2f}"  if r['pf'] != float('inf') else "∞"
        ret_s = f"{r['ret']:+.2f}%"
        dd_s  = f"{r['dd']:.2f}%"
        vc    = _vcol(r['verdict'])
        print(
            f"  {r['label']:<20} | {r['candles']:>7} | {r['trades']:>6} | "
            f"{pf_s:>5} | {ret_s:>8} | {dd_s:>7} | "
            f"{vc}{r['verdict']}{_R}"
        )

    print(f"{BAR}\n")

    # ── Conclusion ────────────────────────────────────────────────────────────
    v1 = rows[1]["verdict"]
    v2 = rows[2]["verdict"]

    val1_ok = v1 in ("PASS", "MARGINAL")
    val2_ok = v2 in ("PASS", "MARGINAL")

    if val1_ok and val2_ok:
        print(
            f"  {_GR}{_B}VALIDATED: Edge holds out-of-sample. "
            f"Safe to paper-trade alongside 4h bot.{_R}"
        )
    elif val1_ok and not val2_ok:
        print(
            f"  {_YL}{_B}PARTIAL: Edge degraded in recent regime. "
            f"Do not activate.{_R}"
        )
    else:
        print(
            f"  {_RD}{_B}FAILED: Bull market artifact. "
            f"Do not pursue 1d swing strategy.{_R}"
        )

    print()


if __name__ == "__main__":
    main()
