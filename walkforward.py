"""
Walk-forward validation.

Purpose: determine whether a promising backtest configuration
survives out-of-sample testing. This is the only way to distinguish
a genuine edge from curve fitting.

Split:
  Training   2024-02-22 → 2025-02-22  (first half)
  Validation 2025-02-22 → 2026-06-05  (second half, never seen during tuning)

Usage:
  python walkforward.py

The script runs the SAME config on both periods and compares results.
If PF degrades dramatically on the validation period, the training
result was curve fitting. If PF holds, the edge may be real.
"""
import logging
logging.basicConfig(level=logging.WARNING)

from config import cfg
from bot.data.historical_feed import fetch_candles_paginated, slice_candles
from bot.backtest import engine, metrics as metrics_mod
from bot.backtest.attribution import compute_attribution, print_attribution
from bot.backtest.params import engine_kwargs_from_cfg

# ── Split dates ───────────────────────────────────────────────────────────────
TRAIN_START = "2024-02-22"
TRAIN_END   = "2025-02-22"
VAL_START   = "2025-02-22"
VAL_END     = None           # through present

# All engine parameters come from engine_kwargs_from_cfg() — the same .env-backed
# source backtest.py uses. This script validates the ACTIVE config, nothing else.
# (Until 2026-07-17 it hand-listed a partial arg set that had drifted from live:
# volume filter on, min EMA spread 0.2%, stale 0.5% spread ceiling, no ATR keys.)


def _config_summary() -> str:
    """One-line description of the active config for the report header."""
    b, s = cfg.backtest, cfg.strategy
    sl = (f"ATRx{b.atr_sl_mult}" if b.atr_sl_mult > 0
          else f"{b.stop_loss_pct*100:.1f}%")
    _ep = b.exit_params_for(cfg.exchange.symbol)   # per-symbol TP / trailing stop
    tp = (f"trail{_ep['trail_stop_pct']*100:.0f}%" if _ep["trail_stop_pct"] > 0
          else f"{_ep['take_profit_pct']*100:.0f}%" if _ep["take_profit_pct"] > 0
          else "none")
    return (f"ADX≥{s.adx_threshold:g}  EMA≥{s.min_ema_spread_pct*100:.1f}%  "
            f"RSI={'on' if s.rsi_filter_enabled else 'off'}  "
            f"VolK={s.volume_k:g}  SL={sl}  TP={tp}  "
            f"ATRsizing={'on' if b.atr_sizing_enabled else 'off'}")


def run_period(candles, label, start, end):
    """Run a single backtest period and return metrics + result."""
    period_candles = slice_candles(candles, start, end)
    if not period_candles:
        print(f"\n  ERROR: no candles in {label} period ({start} → {end or 'present'})")
        return None, None

    print(f"\n  {label}: {len(period_candles)} candles  "
          f"({period_candles[0].timestamp.strftime('%Y-%m-%d')} → "
          f"{period_candles[-1].timestamp.strftime('%Y-%m-%d')})")

    result = engine.run(candles=period_candles, **engine_kwargs_from_cfg(cfg))
    m = metrics_mod.compute(result)
    return m, result


def print_comparison(train_m, train_r, val_m, val_r):
    """Print side-by-side comparison of training vs validation."""
    _B  = "\033[1m"
    _R  = "\033[0m"
    _GR = "\033[32m"
    _RD = "\033[31m"
    _YL = "\033[33m"
    bar = "═" * 56

    def pf_color(pf):
        if pf >= 1.2: return _GR
        if pf >= 1.0: return _YL
        return _RD

    def ret_color(r):
        return _GR if r >= 0 else _RD

    print(f"\n{_B}{bar}{_R}")
    print(f"  {_B}WALK-FORWARD VALIDATION RESULTS{_R}")
    print(f"  Config: {_config_summary()}")
    print(f"{_B}{bar}{_R}\n")

    col = 24
    print(f"  {'Metric':<{col}}  {'TRAINING':>14}  {'VALIDATION':>14}  {'VERDICT':>10}")
    print(f"  {'─'*col}  {'─'*14}  {'─'*14}  {'─'*10}")

    def row(label, tv, vv, fmt="{}", higher_better=True):
        ts = fmt.format(tv)
        vs = fmt.format(vv)
        if higher_better:
            verdict = "✓ holds" if vv >= tv * 0.8 else "✗ degrades"
            vcol    = _GR if vv >= tv * 0.8 else _RD
        else:
            verdict = "✓ holds" if vv <= tv * 1.2 else "✗ degrades"
            vcol    = _GR if vv <= tv * 1.2 else _RD
        print(f"  {label:<{col}}  {ts:>14}  {vs:>14}  {vcol}{verdict:>10}{_R}")

    train_trades = len([f for f in train_r.fills if f.side == "BUY"])
    val_trades   = len([f for f in val_r.fills if f.side == "BUY"])

    print(f"  {'Trades':<{col}}  {train_trades:>14}  {val_trades:>14}")

    pf_t = train_m.profit_factor
    pf_v = val_m.profit_factor
    pf_tc = pf_color(pf_t)
    pf_vc = pf_color(pf_v)
    verdict_pf = "✓ holds" if pf_v >= pf_t * 0.8 else "✗ degrades"
    vcol_pf    = _GR if pf_v >= pf_t * 0.8 else _RD
    print(f"  {'Profit factor':<{col}}  "
          f"{pf_tc}{pf_t:>13.2f}{_R}  "
          f"{pf_vc}{pf_v:>13.2f}{_R}  "
          f"{vcol_pf}{verdict_pf:>10}{_R}")

    wr_t = train_m.win_rate * 100
    wr_v = val_m.win_rate * 100
    print(f"  {'Win rate':<{col}}  {wr_t:>13.1f}%  {wr_v:>13.1f}%")

    ret_t = (train_m.final_value / train_m.starting_cash - 1) * 100
    ret_v = (val_m.final_value   / val_m.starting_cash   - 1) * 100
    ret_tc = ret_color(ret_t)
    ret_vc = ret_color(ret_v)
    print(f"  {'Return':<{col}}  "
          f"{ret_tc}{ret_t:>13.2f}%{_R}  "
          f"{ret_vc}{ret_v:>13.2f}%{_R}")

    sr_t = train_m.sharpe_ratio
    sr_v = val_m.sharpe_ratio
    print(f"  {'Sharpe ratio':<{col}}  {sr_t:>14.2f}  {sr_v:>14.2f}")

    dd_t = train_m.max_drawdown_pct * 100
    dd_v = val_m.max_drawdown_pct * 100
    print(f"  {'Max drawdown':<{col}}  {dd_t:>13.2f}%  {dd_v:>13.2f}%")

    # ── Interpretation ────────────────────────────────────────────────
    print(f"\n  {_B}INTERPRETATION{_R}")
    print(f"  {'─'*52}")
    if pf_v >= 1.2:
        print(f"  {_GR}Strong: PF holds above 1.2 out-of-sample.{_R}")
        print(f"  {_GR}This config may have a genuine edge worth pursuing.{_R}")
    elif pf_v >= 1.0:
        print(f"  {_YL}Marginal: PF above 1.0 out-of-sample but below 1.2.{_R}")
        print(f"  {_YL}Weak evidence of edge. Needs more data or refinement.{_R}")
    elif pf_v >= pf_t * 0.8:
        print(f"  {_YL}Degraded but not collapsed: PF below 1.0 out-of-sample.{_R}")
        print(f"  {_YL}Some overfitting but not total failure.{_R}")
    else:
        print(f"  {_RD}Failed: PF collapsed out-of-sample.{_R}")
        print(f"  {_RD}Training result was likely curve fitting.{_R}")
        print(f"  {_RD}Do not pursue this configuration further.{_R}")

    print(f"\n{_B}{bar}{_R}\n")


def main():
    print(f"\n  Walk-Forward Validation")
    print(f"  Exchange: {cfg.exchange.exchange.capitalize()}  |  "
          f"Symbol: {cfg.exchange.symbol}  |  "
          f"Timeframe: {cfg.backtest.timeframe}")
    print(f"\n  Fetching 5000 × {cfg.backtest.timeframe} candles …")

    try:
        all_candles = fetch_candles_paginated(
            exchange_id = cfg.exchange.exchange,
            symbol      = cfg.exchange.symbol,
            timeframe   = cfg.backtest.timeframe,
            total_limit = 5000,
        )
    except Exception as exc:
        print(f"\n  ERROR fetching data: {exc}\n")
        return

    print(f"  {len(all_candles)} candles loaded  "
          f"({all_candles[0].timestamp.strftime('%Y-%m-%d')} → "
          f"{all_candles[-1].timestamp.strftime('%Y-%m-%d')})\n")
    print(f"  Running training period …")
    train_m, train_r = run_period(all_candles, "Training  ", TRAIN_START, TRAIN_END)
    if train_m is None:
        return

    print(f"  Running validation period …")
    val_m,   val_r   = run_period(all_candles, "Validation", VAL_START,   VAL_END)
    if val_m is None:
        return

    print_comparison(train_m, train_r, val_m, val_r)

    # Attribution for validation period only (the one that matters)
    if val_r.fills:
        print(f"  Validation period attribution:\n")
        attr = compute_attribution(val_r)
        print_attribution(attr)


if __name__ == "__main__":
    main()
