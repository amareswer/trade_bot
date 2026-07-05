#!/usr/bin/env python
"""
session_edge_experiment.py — does the validated BTC strategy's edge depend on
WHEN trades are entered (day of week / time of day)?

PRE-REGISTERED HYPOTHESES (committed before looking at any results — exactly
two, coarse buckets only, because ~39 in-sample trades cannot support finer
slicing without manufacturing fake patterns):

  H1 (weekend):   trades ENTERED on Saturday/Sunday UTC underperform weekday
                  entries (thin weekend liquidity → chop → stop-outs).
  H2 (overnight): trades ENTERED on the 00:00/04:00 UTC 4h candles underperform
                  entries on the 08/12/16/20 UTC candles (Asia/overnight
                  session vs EU+US hours where trends carry).

SUPPORT CRITERIA (also pre-registered):
  A hypothesis is SUPPORTED only if ALL of:
    1. blocked-bucket PF < 1.0 while kept-bucket PF ≥ baseline PF (in-sample)
    2. blocked bucket holds ≥ 8 trades in-sample (sample floor)
    3. the same direction repeats out-of-sample (2019–2021) — the discipline
       that separated the real EMA-spread filter (+0.17 PF, held OOS) from
       the fake ATR-alpha (vanished OOS)
  Anything else → NOT SUPPORTED or INSUFFICIENT DATA. No cherry-picking.

METHOD NOTE — post-hoc removal is an approximation: dropping a trade after the
fact ignores state-machine knock-on effects (a blocked entry frees the bot for
a different later trade). A SUPPORTED verdict therefore does NOT authorize a
live change — it authorizes building an engine-level session gate and running
the full Validation Discipline workflow (CLAUDE.md).

Windows:
  IN-SAMPLE:      2024-03-07 → 2026-06-20 (canonical pinned window; the
                  baseline must reproduce the 39-trade / PF≈1.77–1.79
                  fingerprint, which validates this harness)
  OUT-OF-SAMPLE:  2019-01-01 → 2021-12-31 (same OOS period used to validate
                  the EMA-spread filter on 2026-06-27)

RESEARCH ONLY. Touches no live code, no bot/strategy/* files, no .env.

Usage:  .venv/bin/python session_edge_experiment.py
"""
import logging
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

from config import cfg
from bot.backtest import engine, metrics as metrics_mod
from bot.data.historical_feed import fetch_candles_paginated

SYMBOL    = "BTC/USDT"
TIMEFRAME = os.getenv("BACKTEST_TIMEFRAME", "4h")
FEE       = float(os.getenv("BACKTEST_FEE_PCT", "0.008"))

_MS = 1000
PERIODS = [
    # (label, since, until)
    ("IN-SAMPLE 2024-03-07→2026-06-20",
     int(datetime(2024, 3, 7,  tzinfo=timezone.utc).timestamp() * _MS),
     int(datetime(2026, 6, 20, tzinfo=timezone.utc).timestamp() * _MS)),
    ("OUT-OF-SAMPLE 2019-01-01→2021-12-31",
     int(datetime(2019, 1, 1,  tzinfo=timezone.utc).timestamp() * _MS),
     int(datetime(2022, 1, 1,  tzinfo=timezone.utc).timestamp() * _MS)),
]

MIN_BLOCKED_TRADES = 8      # pre-registered sample floor for the blocked bucket

REPORT_PATH = os.path.join(
    "logs", f"session_edge_experiment_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
)


def run_backtest(candles: list) -> "engine.BacktestResult":
    """Validated live config — identical engine call to atr_sl_experiment.py
    baseline (fixed SL, ATR mult 0)."""
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


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def pair_round_trips(fills: list) -> list[dict]:
    """FIFO-pair BUYs with the SELLs that realize their P&L.
    Each round trip carries the ENTRY timestamp (hypotheses are about entries)."""
    trips: list[dict] = []
    open_entries: list[datetime] = []
    for f in fills:
        if f.side == "BUY":
            open_entries.append(_parse_ts(f.timestamp))
        elif f.side == "SELL" and f.pnl is not None and open_entries:
            entry_dt = open_entries.pop(0)
            trips.append({
                "entry_dt": entry_dt,
                "pnl":      f.pnl,
                "reason":   f.reason,
            })
    return trips


def bucket_stats(trips: list[dict]) -> dict:
    if not trips:
        return {"n": 0, "wins": 0, "pf": None, "net": 0.0, "sl": 0}
    wins  = [t for t in trips if t["pnl"] > 0]
    gp    = sum(t["pnl"] for t in trips if t["pnl"] > 0)
    gl    = abs(sum(t["pnl"] for t in trips if t["pnl"] < 0))
    return {
        "n":    len(trips),
        "wins": len(wins),
        "pf":   (gp / gl) if gl > 0 else (float("inf") if gp > 0 else None),
        "net":  sum(t["pnl"] for t in trips),
        "sl":   sum(1 for t in trips if t["reason"] == "stop_loss"),
    }


def _fmt_pf(pf) -> str:
    if pf is None:
        return "—"
    return "∞" if pf == float("inf") else f"{pf:.2f}"


HYPOTHESES = [
    # (name, description, predicate: trip is in the BLOCKED bucket)
    ("H1 weekend",
     "entries on Sat/Sun UTC underperform",
     lambda t: t["entry_dt"].weekday() >= 5),
    ("H2 overnight",
     "entries on 00:00/04:00 UTC candles underperform",
     lambda t: t["entry_dt"].hour in (0, 4)),
]


def evaluate_period(trips: list[dict], baseline_pf: float) -> list[dict]:
    """Per-hypothesis stats for one period."""
    out = []
    for name, desc, blocked_pred in HYPOTHESES:
        blocked = [t for t in trips if blocked_pred(t)]
        kept    = [t for t in trips if not blocked_pred(t)]
        b, k    = bucket_stats(blocked), bucket_stats(kept)
        out.append({
            "name": name, "desc": desc,
            "blocked": b, "kept": k,
            "baseline_pf": baseline_pf,
            # in-sample support conditions 1+2 (condition 3 = OOS repeat,
            # checked across periods in main)
            "direction_holds": (
                b["n"] > 0 and k["n"] > 0
                and (b["pf"] is not None and b["pf"] < 1.0)
                and (k["pf"] is not None and k["pf"] >= baseline_pf)
            ),
            "sample_ok": b["n"] >= MIN_BLOCKED_TRADES,
        })
    return out


def main() -> None:
    print(f"\nSession-edge experiment — {SYMBOL} {TIMEFRAME}, fee {FEE*100:.2f}%")
    print("Pre-registered: H1 weekend entries, H2 overnight (00/04 UTC) entries\n")

    report = [
        f"# Session-Edge Experiment — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        "Question: is the validated strategy's edge concentrated in specific entry",
        "sessions? Two pre-registered hypotheses (see script docstring): H1 weekend,",
        "H2 overnight (00/04 UTC candles). Coarse buckets only — small samples.",
        "",
        f"Config: validated live config from .env (fee {FEE*100:.2f}%, "
        f"SL {cfg.backtest.stop_loss_pct*100:.1f}%, TP {cfg.backtest.take_profit_pct*100:.0f}%).",
        "Post-hoc trade removal — approximation; adoption requires engine-level",
        "gate + full Validation Discipline workflow.",
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

        result = run_backtest(candles)
        m      = metrics_mod.compute(result)
        base_pf = m.profit_factor if m.profit_factor != float("inf") else 99.0
        trips  = pair_round_trips(result.fills)
        print(f"  Baseline: {m.total_trades} trades  PF {base_pf:.2f}  "
              f"return {m.total_return_pct*100:+.2f}%")

        report += [
            f"## {label}",
            "",
            f"Baseline: **{m.total_trades} trades, PF {base_pf:.2f}**, "
            f"return {m.total_return_pct*100:+.2f}%, {len(candles)} candles.",
            "",
            "| Hypothesis | Bucket | Trades | Wins | SL exits | Net PnL | PF |",
            "|------------|--------|--------|------|----------|---------|----|",
        ]

        evals = evaluate_period(trips, base_pf)
        period_results[label] = evals
        for e in evals:
            b, k = e["blocked"], e["kept"]
            report.append(
                f"| {e['name']} | blocked ({e['desc'].split(' entries')[0].split(' ')[-1] if False else 'in window'}) "
                f"| {b['n']} | {b['wins']} | {b['sl']} | {b['net']:+.2f} | {_fmt_pf(b['pf'])} |"
            )
            report.append(
                f"| {e['name']} | kept (outside window) "
                f"| {k['n']} | {k['wins']} | {k['sl']} | {k['net']:+.2f} | {_fmt_pf(k['pf'])} |"
            )
            print(
                f"  {e['name']:<13} blocked: n={b['n']:<3} PF={_fmt_pf(b['pf']):<5} "
                f"net={b['net']:+8.2f} | kept: n={k['n']:<3} PF={_fmt_pf(k['pf']):<5} "
                f"net={k['net']:+8.2f}"
            )
        report.append("")
        print()

    # ── Verdicts (pre-registered criteria) ────────────────────────────────
    is_label, oos_label = PERIODS[0][0], PERIODS[1][0]
    report += ["## Verdicts (pre-registered criteria)", ""]
    print("── Verdicts ─────────────────────────────")
    for i, (name, desc, _) in enumerate(HYPOTHESES):
        ins = period_results[is_label][i]
        oos = period_results[oos_label][i]
        if not ins["sample_ok"]:
            verdict = (f"INSUFFICIENT DATA — blocked bucket has "
                       f"{ins['blocked']['n']} in-sample trades "
                       f"(< {MIN_BLOCKED_TRADES} floor)")
        elif not ins["direction_holds"]:
            verdict = "NOT SUPPORTED in-sample — no PF separation in the predicted direction"
        elif not oos["direction_holds"]:
            verdict = ("NOT SUPPORTED — in-sample direction did not repeat "
                       "out-of-sample (the ATR-alpha failure mode)")
        else:
            verdict = ("SUPPORTED on both periods — next step: engine-level "
                       "session gate + full Validation Discipline workflow "
                       "before ANY live change")
        report.append(f"- **{name}** ({desc}): {verdict}")
        print(f"  {name}: {verdict}")

    report += [
        "",
        "---",
        "No live config or strategy change follows from this script alone.",
    ]

    os.makedirs("logs", exist_ok=True)
    with open(REPORT_PATH, "w") as fh:
        fh.write("\n".join(report) + "\n")
    print(f"\nReport written → {REPORT_PATH}")


if __name__ == "__main__":
    main()
