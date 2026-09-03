"""
Backtest entry point.

All settings come from config.py / .env — no hardcoded values here.
To change settings, edit .env or config.py.

Run:
    python backtest.py

Date filtering (for regime testing):
    Set BEFORE_DATE and/or AFTER_DATE below to slice candles by date.
    Both default to None (full dataset).

    Old half (Mar 2024 → Apr 2025):  BEFORE_DATE = "2025-04-23", AFTER_DATE = None
    New half (Apr 2025 → Jun 2026):  BEFORE_DATE = None,         AFTER_DATE = "2025-04-23"
    Bear stress test (Nov 2021–May 2023): BEFORE_DATE = "2023-06-01", AFTER_DATE = None
    Full dataset:                     BEFORE_DATE = None,         AFTER_DATE = None
"""
import argparse
import logging
import os
from datetime import datetime, timezone
logging.basicConfig(level=logging.WARNING)

from config import cfg
from bot.data.historical_feed import fetch_candles_paginated
from bot.backtest import engine, metrics as metrics_mod, report
from bot.backtest.params import engine_kwargs_from_cfg
from bot.backtest.attribution import compute_attribution, print_attribution, save_attribution_csv
from bot.strategy.fingerprint import compute_strategy_hash

# ── Pinned-window reproducibility (ISO dates) ─────────────────────────────────
# When set, fetches exactly that calendar window instead of rolling most-recent-N.
# Use these to reproduce a specific historical validation run.
#
#   BACKTEST_SINCE="2024-03-07" BACKTEST_UNTIL="2026-06-20"
#     → reproduces the 2026-06-19 validation window (~5000 × 4h candles)
#
# BACKTEST_SINCE changes the FETCH start; BACKTEST_UNTIL trims at the end.
# Both must be "YYYY-MM-DD" UTC dates.  Either may be omitted independently.
_SINCE_STR = os.environ.get("BACKTEST_SINCE") or None
_UNTIL_STR = os.environ.get("BACKTEST_UNTIL") or None

def _date_to_ms(date_str: str) -> int:
    """Convert "YYYY-MM-DD" UTC date string to Unix milliseconds."""
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

BACKTEST_SINCE_MS = _date_to_ms(_SINCE_STR) if _SINCE_STR else None
BACKTEST_UNTIL_MS = _date_to_ms(_UNTIL_STR) if _UNTIL_STR else None

# ── Regime-slice filter for half-period testing (post-fetch) ──────────────────
# Set to "YYYY-MM-DD" to slice the already-fetched candles by date.
# Override via env: BACKTEST_START (lower bound) / BACKTEST_END (upper bound).
# These narrow a window AFTER fetching; they cannot reach before BACKTEST_SINCE.
BEFORE_DATE = os.environ.get("BACKTEST_END")   or None  # keep candles BEFORE this date
AFTER_DATE  = os.environ.get("BACKTEST_START") or None  # keep candles FROM this date onward
# ─────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Run crypto backtest")
    # Default None → keep engine_kwargs_from_cfg()'s per-symbol exit value
    # (TAKE_PROFIT_PCT_<BASE> etc.). Only a value explicitly passed on the CLI
    # overrides it. Was `default=cfg.backtest.*` which always clobbered the
    # per-symbol resolution with the shared value (found 2026-09-03).
    parser.add_argument("--stop_loss",    type=float, default=None)
    parser.add_argument("--take_profit",  type=float, default=None)
    parser.add_argument("--fee",          type=float, default=cfg.backtest.fee_pct)
    parser.add_argument("--limit",        type=int,   default=cfg.backtest.limit)
    parser.add_argument("--max_drawdown", type=float, default=0.25)
    args = parser.parse_args()

    _strategy_hash = compute_strategy_hash()
    print(f"\n  Exchange: {cfg.exchange.exchange.capitalize()}  |  Symbol: {cfg.exchange.symbol}  |  Timeframe: {cfg.backtest.timeframe}  |  Strategy hash: {_strategy_hash}")

    try:
        candles = fetch_candles_paginated(
            exchange_id = cfg.exchange.exchange,
            symbol      = cfg.exchange.symbol,
            timeframe   = cfg.backtest.timeframe,
            total_limit = args.limit,
            since_ms    = BACKTEST_SINCE_MS,
            until_ms    = BACKTEST_UNTIL_MS,
        )
    except Exception as exc:
        print(f"\n  ERROR fetching data: {exc}\n")
        return

    print(
        f"  {len(candles)} candles loaded"
        f"  ({candles[0].timestamp.strftime('%Y-%m-%d')}"
        f" → {candles[-1].timestamp.strftime('%Y-%m-%d')})"
    )

    # ── Apply date filter if set ──────────────────────────────────────────────
    if BEFORE_DATE:
        candles = [c for c in candles if c.timestamp.strftime('%Y-%m-%d') < BEFORE_DATE]
        print(f"  Date filter: keeping candles BEFORE {BEFORE_DATE} → {len(candles)} candles remain")
    if AFTER_DATE:
        candles = [c for c in candles if c.timestamp.strftime('%Y-%m-%d') >= AFTER_DATE]
        print(f"  Date filter: keeping candles FROM {AFTER_DATE} → {len(candles)} candles remain")

    if not candles:
        print(f"\n  ERROR: no candles remain after date filter. Check BEFORE_DATE / AFTER_DATE.\n")
        return

    print(
        f"  Running on: {candles[0].timestamp.strftime('%Y-%m-%d')}"
        f" → {candles[-1].timestamp.strftime('%Y-%m-%d')}\n"
    )
    print(f"  Running backtest …\n")

    run_kwargs = engine_kwargs_from_cfg(cfg)
    run_kwargs.update(
        fee_pct          = args.fee,
        max_drawdown_pct = args.max_drawdown,
    )
    if args.stop_loss is not None:
        run_kwargs["stop_loss_pct"] = args.stop_loss
    if args.take_profit is not None:
        run_kwargs["take_profit_pct"] = args.take_profit
    result = engine.run(candles=candles, **run_kwargs)

    m = metrics_mod.compute(result)
    report.print_report(m, result)

    rs = result.rejection_stats
    if rs:
        tradeable = rs.get("candles_seen", 0) - rs.get("warmup_rejected", 0)

        def pct(n: int) -> str:
            return f"  ({n / tradeable * 100:.1f}%)" if tradeable > 0 else ""

        print()
        print("  SIGNAL FILTER BREAKDOWN")
        print("  " + "─" * 46)
        print(f"  Candles examined          {rs.get('candles_seen', 0):>6}")
        print(f"  Warmup (skipped)          {rs.get('warmup_rejected', 0):>6}")
        print(f"  Tradeable candles         {tradeable:>6}")
        print(f"  ─────────────────────────────────────────")
        adx_n      = rs.get("adx_rejected", 0)
        trend_n    = rs.get("trend_rejected", 0)
        ema_n      = rs.get("ema_rejected", 0)
        rsi_n      = rs.get("rsi_rejected", 0)
        regime_n   = rs.get("regime_rejected", 0)
        vol_n      = rs.get("volume_rejected", 0)
        volatile_n = rs.get("volatile_skipped", 0)
        print(f"  ADX rejected              {adx_n:>6}{pct(adx_n)}")
        print(f"  Trend rejected (NEUTRAL)  {trend_n:>6}{pct(trend_n)}")
        print(f"  EMA spread rejected       {ema_n:>6}{pct(ema_n)}")
        print(f"  RSI rejected              {rsi_n:>6}{pct(rsi_n)}")
        print(f"  Regime EMA rejected       {regime_n:>6}{pct(regime_n)}")
        print(f"  Volume rejected           {vol_n:>6}{pct(vol_n)}")
        print(f"  Volatile (flat)           {volatile_n:>6}{pct(volatile_n)}")
        print(f"  ─────────────────────────────────────────")
        buy_n  = rs.get("buy_signals", 0)
        sell_n = rs.get("sell_signals", 0)
        print(f"  BUY  signals              {buy_n:>6}{pct(buy_n)}")
        print(f"  SELL signals              {sell_n:>6}{pct(sell_n)}")
        print()

    csv_path = report.save_csv(result)
    print(f"  Saved → {csv_path}\n")

    # ── Trade attribution analysis ────────────────────────────────────────────
    if result.fills:
        attr_report = compute_attribution(result)
        print_attribution(attr_report)
        attr_path = save_attribution_csv(
            attr_report.records,
            symbol    = cfg.exchange.symbol,
            timeframe = cfg.backtest.timeframe,
        )
        print(f"  Attribution CSV → {attr_path}\n")


if __name__ == "__main__":
    main()