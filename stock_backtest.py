"""Stock strategy walk-forward backtest — the gate before any rule trades paper.

Replays the crypto bot's validated IndicatorStrategy (Mode A/B) over daily
candles for every watchlist symbol, across multiple walk-forward windows,
with the paper book's real cost model (15 bps slippage per fill + IBKR
commissions) and its real SL/TP (-5% / +15%).

Rewritten 2026-07-10 — replaces the 2026-06-23 one-off backtester, which
filled on the signal candle's own close (look-ahead), checked SL/TP on
closes only (missed intra-candle stops), used a 0.5% notional commission
(not the IBKR model the paper report uses), had no walk-forward windows,
and shared cash across symbols serially (results depended on symbol order).
Engine now lives in stock_bot/backtest/engine.py with its own test file
(test_stock_backtest_engine.py).

Run:    .venv/bin/python stock_backtest.py
Output: console table + logs/stock_backtest_<date>.md (dated historical record) +
        logs/stock_backtest_latest.json (fixed path, overwritten every run — machine-
        readable per-symbol verdicts, read by LiveTradingGate.check_gate1() in
        stock_bot/analysis/accuracy_tracker.py; added 2026-08-20 so Gate 1 can validate
        the CURRENT strategy instead of stock_bot/backtest.py's stale, disconnected
        --walkforward output)

Pass gate per symbol (same discipline as the crypto USD screen):
  - full-window completed trades >= 10
  - PF >= 1.2 in EVERY window that produced >= 3 trades
  - SL-exit rate <= 70% on the full window

Env overrides:
  STOCK_BT_SYMBOLS=AAPL,NVDA   (default: WATCHLIST from stock_bot/.env)
  STOCK_BT_DAYS=1500           (fetch depth)
  STOCK_BT_NOTIONAL=1000       ($ per trade)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timezone

# Ensure project root importable when run as a script
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from bot.atomic_json import atomic_write_json
from stock_bot.config import load as load_stock_config
from stock_bot.data.price_feed import fetch_candles
from stock_bot.backtest.engine import (
    StockBacktestConfig,
    BacktestResult,
    run_symbol,
)
from stock_bot.strategy.rules import build_indicator_config

# Fixed path, overwritten every run — see module docstring. Gate 1 always reads
# exactly this file, no dated-filename parsing needed.
_LATEST_JSON_PATH = os.path.join("logs", "stock_backtest_latest.json")

logging.basicConfig(level=logging.WARNING)
logging.getLogger("bot.strategy").setLevel(logging.ERROR)

# Walk-forward windows in trading days (0 = full history)
WINDOWS = [0, 750, 500, 250]

# Windows below this trade count are shown but cannot pass/fail the gate
MIN_TRADES_FOR_VERDICT = 3
MIN_TRADES_FULL_WINDOW = 10
MAX_SL_EXIT_RATE       = 70.0
MIN_PF                 = 1.2


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def run() -> int:
    cfg_stock = load_stock_config()
    symbols = [
        s.strip().upper()
        for s in os.getenv("STOCK_BT_SYMBOLS", ",".join(cfg_stock.watchlist)).split(",")
        if s.strip()
    ]
    days     = int(os.getenv("STOCK_BT_DAYS", "1500"))
    notional = float(os.getenv("STOCK_BT_NOTIONAL", "1000"))

    bt_cfg = StockBacktestConfig(
        notional=notional,
        slippage_bps=cfg_stock.paper_slippage_bps,
        stop_loss_pct=cfg_stock.paper_stop_loss_pct,
        take_profit_pct=cfg_stock.paper_take_profit_pct,
        indicator=build_indicator_config(),
    )

    print(f"\nSTOCK WALK-FORWARD BACKTEST — {len(symbols)} symbols · {days}d fetch")
    print(f"Costs: {bt_cfg.slippage_bps} bps slippage/fill + IBKR commissions · "
          f"SL {bt_cfg.stop_loss_pct*100:.0f}% / TP {bt_cfg.take_profit_pct*100:.0f}% · "
          f"${notional:,.0f}/trade\n")

    report_lines = [
        f"# Stock strategy walk-forward — {date.today().isoformat()}",
        "",
        f"Strategy: crypto IndicatorStrategy (Mode A/B) on daily candles · "
        f"ADX≥18 · EMA spread≥0.4% · SL {bt_cfg.stop_loss_pct*100:.0f}% / "
        f"TP {bt_cfg.take_profit_pct*100:.0f}% · {bt_cfg.slippage_bps} bps slippage + IBKR commissions",
        "",
        "| Symbol | Window | Trades | Win rate | PF | Net P&L | SL rate | Verdict |",
        "|--------|--------|--------|----------|-----|---------|---------|---------|",
    ]

    passes: list[str] = []
    fails:  list[str] = []
    json_results: list[dict] = []

    for sym in symbols:
        candles = fetch_candles(sym, "1d", days)
        if not candles or len(candles) < 400:
            print(f"{sym:<10} SKIP — insufficient history ({len(candles) if candles else 0} candles)")
            report_lines.append(f"| {sym} | — | — | — | — | — | — | SKIP (thin history) |")
            json_results.append({
                "symbol":  sym,
                "verdict": "SKIP",
                "reason":  "insufficient history",
                "candles": len(candles) if candles else 0,
                "windows": [],
            })
            continue

        sym_ok = True
        full_res: BacktestResult | None = None
        sym_windows: list[dict] = []
        for w in WINDOWS:
            start_idx = 0 if w == 0 else max(0, len(candles) - w)
            res = run_symbol(sym, candles, bt_cfg, trade_start_idx=start_idx)
            label = "full" if w == 0 else f"{w}d"
            n, pf, wr, sl = res.n_trades, res.profit_factor, res.win_rate, res.sl_exit_rate

            if w == 0:
                full_res = res
                if n < MIN_TRADES_FULL_WINDOW:
                    sym_ok = False
                if sl > MAX_SL_EXIT_RATE:
                    sym_ok = False
            if n >= MIN_TRADES_FOR_VERDICT and pf < MIN_PF:
                sym_ok = False

            note = "" if n >= MIN_TRADES_FOR_VERDICT else " (low sample)"
            print(f"{sym:<10} {label:<6} trades={n:<3} WR={wr:5.1f}%  PF={_fmt_pf(pf):<5} "
                  f"net=${res.total_net_pnl:+8.2f}  SL={sl:4.1f}%{note}")
            report_lines.append(
                f"| {sym} | {label} | {n} | {wr:.1f}% | {_fmt_pf(pf)} | "
                f"${res.total_net_pnl:+.2f} | {sl:.1f}% |{note or ' '}|"
            )
            sym_windows.append({
                "window_days":   w,
                "label":         label,
                "n_trades":      n,
                "win_rate":      wr,
                # inf isn't valid JSON — represented as null, same convention
                # backtest_results.json used ("null = ∞ → always pass", see
                # accuracy_tracker.py's old check_gate1 comment).
                "profit_factor": None if pf == float("inf") else pf,
                "net_pnl":       res.total_net_pnl,
                "sl_exit_rate":  sl,
                "low_sample":    n < MIN_TRADES_FOR_VERDICT,
            })

        verdict = "PASS" if sym_ok else "FAIL"
        (passes if sym_ok else fails).append(sym)
        print(f"{sym:<10} → {verdict}")
        print()
        report_lines.append(f"| **{sym}** | | | | | | | **{verdict}** |")
        json_results.append({
            "symbol":  sym,
            "verdict": verdict,
            "windows": sym_windows,
        })

    report_lines += [
        "",
        "## Summary",
        f"- PASS ({len(passes)}): {', '.join(passes) if passes else '—'}",
        f"- FAIL ({len(fails)}): {', '.join(fails) if fails else '—'}",
        "",
        f"Gate: full-window trades ≥ {MIN_TRADES_FULL_WINDOW}, PF ≥ {MIN_PF} in every "
        f"window with ≥ {MIN_TRADES_FOR_VERDICT} trades, SL-exit rate ≤ {MAX_SL_EXIT_RATE:.0f}%.",
    ]

    os.makedirs("logs", exist_ok=True)
    out = f"logs/stock_backtest_{date.today().strftime('%Y%m%d')}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines) + "\n")

    # Machine-readable snapshot — fixed path, always overwritten. See module
    # docstring and _LATEST_JSON_PATH comment.
    atomic_write_json(_LATEST_JSON_PATH, {
        "run_at":   datetime.now(timezone.utc).isoformat(),
        "windows":  WINDOWS,
        "gate_criteria": {
            "min_trades_full_window":  MIN_TRADES_FULL_WINDOW,
            "min_trades_for_verdict":  MIN_TRADES_FOR_VERDICT,
            "max_sl_exit_rate":        MAX_SL_EXIT_RATE,
            "min_pf":                  MIN_PF,
        },
        "results":  json_results,
    })

    print("=" * 70)
    print(f"PASS ({len(passes)}): {', '.join(passes) if passes else '—'}")
    print(f"FAIL ({len(fails)}): {', '.join(fails) if fails else '—'}")
    print(f"Report: {out}")
    print(f"JSON:   {_LATEST_JSON_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
