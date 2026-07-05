#!/usr/bin/env python
"""
vol_regime_experiment.py — do the validated BTC strategy's winners concentrate
in a measurable trend-strength / volatility band at ENTRY?

PRE-REGISTERED HYPOTHESES (committed before looking at results — two only):

  H1 (weak trend):      entries taken with ADX < 25 (the 18–25 "weak trend"
                        zone above the strategy's own 18 floor) underperform
                        entries with ADX ≥ 25 — weak trends lack follow-through.
  H2 (excess volatility): entries where ATR(14)/price > 1.5% — i.e. one
                        average-range candle exceeds the fixed 1.5% stop
                        distance — underperform lower-volatility entries.
                        Mechanical rationale: when ATR > stop distance, pure
                        noise can hit the stop (same pathology the ATR-SL
                        experiment measured on alts: 79–90% stop-out rates).

SUPPORT CRITERIA (pre-registered, identical to session_edge_experiment):
  SUPPORTED only if ALL of:
    1. blocked-bucket PF < 1.0 AND kept-bucket PF ≥ baseline PF (in-sample)
    2. blocked bucket ≥ 8 trades in-sample
    3. same direction out-of-sample (2019–2021)
  Post-hoc removal is an approximation — a SUPPORTED verdict authorizes
  building an engine-level gate + full Validation Discipline workflow, not
  a live change.

Windows: identical to session_edge_experiment (in-sample pinned canonical
window — baseline must reproduce 39 trades / PF≈1.77 — plus 2019–2021 OOS).

RESEARCH ONLY. Touches no live code, no bot/strategy/* files, no .env.

Usage:  .venv/bin/python vol_regime_experiment.py
"""
import logging
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

from config import cfg
from bot.backtest import engine, metrics as metrics_mod
from bot.data.historical_feed import fetch_candles_paginated
from bot.indicators.indicators import atr as calc_atr

SYMBOL    = "BTC/USDT"
TIMEFRAME = os.getenv("BACKTEST_TIMEFRAME", "4h")
FEE       = float(os.getenv("BACKTEST_FEE_PCT", "0.008"))

ADX_STRONG   = 25.0    # H1 boundary: 18–25 = weak trend, ≥ 25 = strong
ATR_PCT_MAX  = 0.015   # H2 boundary: ATR/price above the 1.5% stop distance
ATR_PERIOD   = 14

_MS = 1000
PERIODS = [
    ("IN-SAMPLE 2024-03-07→2026-06-20",
     int(datetime(2024, 3, 7,  tzinfo=timezone.utc).timestamp() * _MS),
     int(datetime(2026, 6, 20, tzinfo=timezone.utc).timestamp() * _MS)),
    ("OUT-OF-SAMPLE 2019-01-01→2021-12-31",
     int(datetime(2019, 1, 1,  tzinfo=timezone.utc).timestamp() * _MS),
     int(datetime(2022, 1, 1,  tzinfo=timezone.utc).timestamp() * _MS)),
]

MIN_BLOCKED_TRADES = 8

REPORT_PATH = os.path.join(
    "logs", f"vol_regime_experiment_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
)


def run_backtest(candles: list) -> "engine.BacktestResult":
    """Validated live config — identical engine call to session_edge_experiment."""
    return engine.run(
        candles                  = candles,
        symbol                   = SYMBOL,
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
        stop_loss_pct            = cfg.backtest.stop_loss_pct,
        take_profit_pct          = cfg.backtest.take_profit_pct,
        trail_stop_pct           = cfg.backtest.trail_stop_pct,
        trail_stop_activation_pct= cfg.backtest.trail_stop_activation_pct,
        partial_tp_pct           = cfg.backtest.partial_tp_pct,
        partial_tp_size          = cfg.backtest.partial_tp_size,
        regime_ema_period        = cfg.strategy.regime_ema_period,
        regime_ema_slope_filter  = cfg.strategy.regime_ema_slope_filter,
        volume_k                 = cfg.strategy.volume_k,
        atr_volatile_multiplier  = cfg.strategy.atr_volatile_multiplier,
        atr_sl_mult              = 0.0,
    )


def atr_pct_at(candles: list, idx: int) -> float | None:
    """ATR(14)/close at candle idx, from the same indicator module the
    strategy uses. Needs 2×period+1 candles of history."""
    lo = max(0, idx - 120)
    highs  = [c.high  for c in candles[lo:idx + 1]]
    lows   = [c.low   for c in candles[lo:idx + 1]]
    closes = [c.close for c in candles[lo:idx + 1]]
    if len(closes) < 2 * ATR_PERIOD + 1:
        return None
    a = calc_atr(highs, lows, closes, ATR_PERIOD)
    px = candles[idx].close
    return (a / px) if (a is not None and px > 0) else None


def pair_round_trips(result, candles) -> list[dict]:
    """FIFO-pair BUYs with realizing SELLs; attach entry ADX (engine snapshot)
    and entry ATR% (computed at the BUY candle)."""
    snaps = list(result.entry_snapshots)   # appended once per BUY, in order
    trips: list[dict] = []
    open_entries: list[dict] = []
    buy_i = 0
    for f in result.fills:
        if f.side == "BUY":
            snap = snaps[buy_i] if buy_i < len(snaps) else {}
            buy_i += 1
            open_entries.append({
                "adx":     snap.get("adx"),
                "atr_pct": atr_pct_at(candles, f.candle_index),
            })
        elif f.side == "SELL" and f.pnl is not None and open_entries:
            e = open_entries.pop(0)
            trips.append({**e, "pnl": f.pnl, "reason": f.reason})
    return trips


def bucket_stats(trips: list[dict]) -> dict:
    if not trips:
        return {"n": 0, "wins": 0, "pf": None, "net": 0.0, "sl": 0}
    gp = sum(t["pnl"] for t in trips if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trips if t["pnl"] < 0))
    return {
        "n":    len(trips),
        "wins": sum(1 for t in trips if t["pnl"] > 0),
        "pf":   (gp / gl) if gl > 0 else (float("inf") if gp > 0 else None),
        "net":  sum(t["pnl"] for t in trips),
        "sl":   sum(1 for t in trips if t["reason"] == "stop_loss"),
    }


def _fmt_pf(pf) -> str:
    if pf is None:
        return "—"
    return "∞" if pf == float("inf") else f"{pf:.2f}"


HYPOTHESES = [
    ("H1 weak-trend",
     f"entries with ADX < {ADX_STRONG:.0f} underperform",
     lambda t: t["adx"] is not None and t["adx"] < ADX_STRONG),
    ("H2 excess-vol",
     f"entries with ATR% > {ATR_PCT_MAX*100:.1f}% (ATR exceeds stop distance) underperform",
     lambda t: t["atr_pct"] is not None and t["atr_pct"] > ATR_PCT_MAX),
]


def evaluate_period(trips: list[dict], baseline_pf: float) -> list[dict]:
    out = []
    for name, desc, blocked_pred in HYPOTHESES:
        known   = [t for t in trips if blocked_pred(t) is not None]
        blocked = [t for t in known if blocked_pred(t)]
        kept    = [t for t in known if not blocked_pred(t)]
        b, k = bucket_stats(blocked), bucket_stats(kept)
        out.append({
            "name": name, "desc": desc, "blocked": b, "kept": k,
            "direction_holds": (
                b["n"] > 0 and k["n"] > 0
                and (b["pf"] is not None and b["pf"] < 1.0)
                and (k["pf"] is not None and k["pf"] >= baseline_pf)
            ),
            "sample_ok": b["n"] >= MIN_BLOCKED_TRADES,
        })
    return out


def main() -> None:
    print(f"\nVolatility-regime experiment — {SYMBOL} {TIMEFRAME}, fee {FEE*100:.2f}%")
    print(f"Pre-registered: H1 ADX<{ADX_STRONG:.0f} entries, "
          f"H2 ATR%>{ATR_PCT_MAX*100:.1f}% entries\n")

    report = [
        f"# Volatility-Regime Experiment — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        "Question: do winners concentrate in a trend-strength / volatility band",
        "at entry? Pre-registered: H1 weak-trend (ADX 18–25), H2 excess-volatility",
        f"(ATR(14)/price > stop distance {ATR_PCT_MAX*100:.1f}%).",
        "",
        f"Config: validated live config (fee {FEE*100:.2f}%, "
        f"SL {cfg.backtest.stop_loss_pct*100:.1f}%, TP {cfg.backtest.take_profit_pct*100:.0f}%).",
        "Post-hoc removal — adoption requires engine-level gate + full validation.",
        "",
    ]

    period_results: dict[str, list[dict]] = {}

    for label, since_ms, until_ms in PERIODS:
        print(f"── {label} ─────────────────────────")
        print("  Fetching candles from Binance …", flush=True)
        candles = fetch_candles_paginated(
            exchange_id="binance", symbol=SYMBOL, timeframe=TIMEFRAME,
            total_limit=8000, since_ms=since_ms, until_ms=until_ms,
        )
        print(f"  {len(candles)} candles "
              f"({candles[0].timestamp.strftime('%Y-%m-%d')} → "
              f"{candles[-1].timestamp.strftime('%Y-%m-%d')})")

        result  = run_backtest(candles)
        m       = metrics_mod.compute(result)
        base_pf = m.profit_factor if m.profit_factor != float("inf") else 99.0
        trips   = pair_round_trips(result, candles)
        print(f"  Baseline: {m.total_trades} trades  PF {base_pf:.2f}  "
              f"return {m.total_return_pct*100:+.2f}%")

        report += [
            f"## {label}",
            "",
            f"Baseline: **{m.total_trades} trades, PF {base_pf:.2f}**, "
            f"return {m.total_return_pct*100:+.2f}%.",
            "",
            "| Hypothesis | Bucket | Trades | Wins | SL exits | Net PnL | PF |",
            "|------------|--------|--------|------|----------|---------|----|",
        ]

        evals = evaluate_period(trips, base_pf)
        period_results[label] = evals
        for e in evals:
            b, k = e["blocked"], e["kept"]
            report.append(f"| {e['name']} | blocked (in band) | {b['n']} | {b['wins']} "
                          f"| {b['sl']} | {b['net']:+.2f} | {_fmt_pf(b['pf'])} |")
            report.append(f"| {e['name']} | kept (outside band) | {k['n']} | {k['wins']} "
                          f"| {k['sl']} | {k['net']:+.2f} | {_fmt_pf(k['pf'])} |")
            print(
                f"  {e['name']:<14} blocked: n={b['n']:<3} PF={_fmt_pf(b['pf']):<5} "
                f"net={b['net']:+8.2f} | kept: n={k['n']:<3} PF={_fmt_pf(k['pf']):<5} "
                f"net={k['net']:+8.2f}"
            )
        report.append("")
        print()

    is_label, oos_label = PERIODS[0][0], PERIODS[1][0]
    report += ["## Verdicts (pre-registered criteria)", ""]
    print("── Verdicts ─────────────────────────────")
    for i, (name, desc, _) in enumerate(HYPOTHESES):
        ins = period_results[is_label][i]
        oos = period_results[oos_label][i]
        if not ins["sample_ok"]:
            verdict = (f"INSUFFICIENT DATA — blocked bucket has "
                       f"{ins['blocked']['n']} in-sample trades (< {MIN_BLOCKED_TRADES})")
        elif not ins["direction_holds"]:
            verdict = "NOT SUPPORTED in-sample — no PF separation in the predicted direction"
        elif not oos["direction_holds"]:
            verdict = ("NOT SUPPORTED — in-sample direction did not repeat "
                       "out-of-sample (the ATR-alpha failure mode)")
        else:
            verdict = ("SUPPORTED on both periods — next step: engine-level "
                       "gate + full Validation Discipline workflow before ANY live change")
        report.append(f"- **{name}** ({desc}): {verdict}")
        print(f"  {name}: {verdict}")

    report += ["", "---", "No live config or strategy change follows from this script alone."]

    os.makedirs("logs", exist_ok=True)
    with open(REPORT_PATH, "w") as fh:
        fh.write("\n".join(report) + "\n")
    print(f"\nReport written → {REPORT_PATH}")


if __name__ == "__main__":
    main()
