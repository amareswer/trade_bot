"""
Live vs Backtest comparison tool.

Loads live fills from logs/trades.db and compares realized performance
(PF, win rate, Sharpe) against the validated backtest baseline.

Usage:
    python live_comparison.py
    python live_comparison.py --db logs/trades.db --min_trades 5
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Validated backtest baseline — the CURRENT canonical strategy fingerprint
# (CLAUDE.md, hash b30f2f9e769c8d41, re-stamped 2026-08-20 after the
# self-referential ATR regime-baseline fix; numbers reproduced by running
# `EXCHANGE=binance SYMBOL=BTC/USDT python backtest.py` on 2026-08-25).
# This block had drifted: it still carried the 2026-06-19 result (58 trades,
# PF 1.79) through four subsequent strategy-hash changes — corrected 2026-08-25.
# If the canonical fingerprint in CLAUDE.md changes again, update this too.
# ---------------------------------------------------------------------------
_BASELINE = {
    "symbol":      "BTC/USDT",
    "timeframe":   "4h",
    "candles":     5000,
    "trades":      31,
    "win_rate":    0.387,
    "pf":          2.19,
    "max_dd_pct":  -1.74,
    "return_pct":  -0.08,
    "fee_pct":     0.8,
    "stop_loss":   1.5,
    "take_profit": 10.0,
    "validated":   "2026-08-20",
}

# The baseline above is BTC-only (Binance BTC/USDT is the standing walk-forward
# proxy for live Kraken BTC/CAD — see CLAUDE.md "Exchange Setup"). With more
# than one live symbol (SOL/CAD added 2026-08-25), blending all fills into one
# metric set would compare a mixed book against a BTC-only baseline. Fills are
# therefore filtered to this base asset by default (--base to override).
_BASELINE_BASE = "BTC"

_GR = "\033[92m"
_RD = "\033[91m"
_YL = "\033[93m"
_DIM = "\033[2m"
_R  = "\033[0m"
_BD = "\033[1m"


def _col(val: float, thresh_green: float, thresh_yellow: float, higher_is_better: bool = True) -> str:
    if higher_is_better:
        if val >= thresh_green:
            return _GR
        if val >= thresh_yellow:
            return _YL
        return _RD
    else:
        if val <= thresh_green:
            return _GR
        if val <= thresh_yellow:
            return _YL
        return _RD


def _load_fills(db_path: str) -> list[dict]:
    if not os.path.exists(db_path):
        print(f"{_RD}No database found at {db_path}{_R}")
        print("Run the live bot first to accumulate fills.")
        sys.exit(0)

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT side, symbol, quantity, price, value, pnl, timestamp, exchange, "
        "fee_cost, fee_currency FROM fills ORDER BY id"
    ).fetchall()
    conn.close()

    return [
        {
            "side":         r[0],
            "symbol":       r[1],
            "quantity":     r[2],
            "price":        r[3],
            "value":        r[4],
            "pnl":          r[5],
            "timestamp":    r[6],
            "exchange":     r[7],
            "fee_cost":     r[8] or 0.0,
            "fee_currency": r[9] or "",
        }
        for r in rows
    ]


def _quote_ccy(symbol: str) -> str:
    parts = (symbol or "").split("/")
    return parts[1].upper() if len(parts) == 2 else ""


def _compute_live_metrics(fills: list[dict]) -> dict:
    """
    2026-09-18 review finding: this used to compute PF/win-rate/total P&L
    entirely from the stored `pnl` column, which PositionManager.on_sell()
    never subtracts fees from — a price-profitable trade that lost money
    after real trading costs was counted as a win. Fixed to mirror the same
    gross-vs-net convention bot/backtest/metrics.py adopted 2026-09-12
    (CLAUDE.md: "PF/win-rate are NET of fees"): `pf`/`win_rate` below are
    now NET (the trustworthy, primary figures); `gross_pf`/`gross_win_rate`
    are kept alongside for comparison only, never for gating.

    `net_pnl` is an EXACT identity — total realized gross P&L minus every
    fee actually paid (BUY entry + SELL exit) across the whole loaded
    fill set. Per-trade `pf`/`win_rate` are a documented APPROXIMATION —
    each SELL's own exit fee is attributed to it, but the matching entry
    BUY's fee is not (this flat fills table has no cost-basis linkage
    between a BUY and the SELL(s) that later close it — a true per-trade
    allocation is the "shared closed-position accounting component" the
    review calls for as future work, matching the backtest engine's own
    already-built position-tracking machinery). A fee whose currency
    doesn't match its own fill's quote currency is excluded rather than
    silently mixed into a differently-denominated total — counted in
    `unmatched_fee_ct` so the report can say so.
    """
    sells    = [f for f in fills if f["side"] == "SELL" and f["pnl"] is not None]
    n        = len(sells)
    if n == 0:
        return {}

    total_fees       = 0.0
    unmatched_fee_ct = 0
    for f in fills:
        fee = f.get("fee_cost") or 0.0
        if fee <= 0:
            continue
        cur = (f.get("fee_currency") or "").upper()
        if cur and cur != _quote_ccy(f["symbol"]):
            unmatched_fee_ct += 1
            continue
        total_fees += fee

    def _net_pnl(f: dict) -> float:
        fee = f.get("fee_cost") or 0.0
        cur = (f.get("fee_currency") or "").upper()
        if cur and cur != _quote_ccy(f["symbol"]):
            return f["pnl"]   # unknown basis for this trade — report gross rather than guess
        return f["pnl"] - fee

    pnls     = [f["pnl"] for f in sells]
    net_pnls = [_net_pnl(f) for f in sells]

    wins,     losses     = [p for p in pnls if p > 0],     [p for p in pnls if p < 0]
    net_wins, net_losses = [p for p in net_pnls if p > 0], [p for p in net_pnls if p < 0]
    gross_win_rate = len(wins) / n
    net_win_rate   = len(net_wins) / n

    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses)) if losses else 0.0
    gross_pf     = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    net_profit = sum(net_wins)
    net_loss   = abs(sum(net_losses)) if net_losses else 0.0
    net_pf     = net_profit / net_loss if net_loss > 0 else float("inf")

    # Equity curve for Sharpe (cumulative NET PnL per trade). This is a
    # trade-level statistic, not a regularly sampled account-return series
    # — it is NOT directly comparable to a backtest's own (per-period)
    # Sharpe. 2026-09-18 review finding: labeled explicitly in the report
    # rather than implying comparability.
    equity = [0.0]
    for p in net_pnls:
        equity.append(equity[-1] + p)
    returns = [equity[i + 1] - equity[i] for i in range(len(equity) - 1)]
    mean_r = sum(returns) / len(returns)
    std_r  = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / len(returns)) if len(returns) > 1 else 0.0
    sharpe = round((mean_r / std_r) * math.sqrt(n), 2) if std_r > 0 else 0.0

    # Max drawdown from cumulative (net) equity
    peak = 0.0
    max_dd = 0.0
    for e in equity:
        peak   = max(peak, e)
        max_dd = min(max_dd, e - peak)

    return {
        "n_trades":         n,
        "win_rate":         net_win_rate,
        "pf":               net_pf,
        "gross_win_rate":   gross_win_rate,
        "gross_pf":         gross_pf,
        "total_pnl":        sum(pnls),
        "net_pnl":          sum(pnls) - total_fees,
        "unmatched_fee_ct": unmatched_fee_ct,
        "avg_win":          net_profit / len(net_wins) if net_wins else 0.0,
        "avg_loss":         sum(net_losses) / len(net_losses) if net_losses else 0.0,
        "sharpe":           sharpe,
        "max_dd":           max_dd,
        "first_trade":      sells[0]["timestamp"],
        "last_trade":       sells[-1]["timestamp"],
        "exchanges":        list({f["exchange"] for f in fills if f["exchange"]}),
        "symbols":          list({f["symbol"] for f in fills if f["symbol"]}),
    }


def _print_report(metrics: dict, min_trades: int) -> None:
    n = metrics.get("n_trades", 0)
    w = 52

    print(f"\n{_BD}{'─' * w}{_R}")
    print(f"{_BD}  LIVE vs BACKTEST COMPARISON{_R}")
    print(f"{'─' * w}")

    def _row(label: str, live_val: str, base_val: str) -> None:
        print(f"  {label:<20} {live_val:<22} {base_val}")

    print(f"  {'':20} {'LIVE':^22} {'BACKTEST BASELINE':^18}")
    print(f"  {'':20} {'─'*20} {'─'*18}")
    _row("Trades",   f"{n}",                                  str(_BASELINE["trades"]))
    _row("Symbol",   metrics.get("symbols", ["—"])[0],        _BASELINE["symbol"])
    _row("Exchange", metrics.get("exchanges", ["—"])[0] if metrics.get("exchanges") else "—", "binance")
    print(f"  {'─'*20}")

    print(f"\n  {_YL}⚠ Baseline predates the 2026-09-12 net-of-fee accounting fix — it is a{_R}")
    print(f"  {_YL}  GROSS-only figure. Compare against gross_pf/gross_win_rate below, not{_R}")
    print(f"  {_YL}  the primary (net) numbers, until the baseline is regenerated net of fees.{_R}")

    if n < min_trades:
        print(f"\n  {_YL}Only {n} live trade(s) — need {min_trades} for statistical relevance.{_R}")
        print(f"  {_DIM}Baseline (gross): {_BASELINE['trades']} trades, PF {_BASELINE['pf']}, "
              f"win rate {_BASELINE['win_rate']*100:.1f}%{_R}")
        print(f"\n{'─' * w}\n")
        return

    # ── Profit Factor (NET of fees — 2026-09-18 fix; gross kept alongside) ─
    pf      = metrics["pf"]
    gpf     = metrics["gross_pf"]
    pf_c    = _col(pf, 1.5, 1.0)
    pf_b    = _BASELINE["pf"]   # gross baseline — see warning above
    diff_c  = _GR if pf >= pf_b * 0.8 else _YL if pf >= pf_b * 0.5 else _RD
    _row("Profit factor (net)",
         f"{pf_c}{pf:.2f}{_R}",
         f"{pf_b:.2f} (gross)  {diff_c}{'▲' if pf >= pf_b else '▼'}{abs(pf-pf_b):.2f}{_R}")
    _row("  gross_pf",          f"{_DIM}{gpf:.2f}{_R}", "")

    # ── Win rate ─────────────────────────────────────────────────────────
    wr     = metrics["win_rate"] * 100
    gwr    = metrics["gross_win_rate"] * 100
    wr_c   = _col(wr, 40, 30)
    wr_b   = _BASELINE["win_rate"] * 100
    _row("Win rate (net)",
         f"{wr_c}{wr:.1f}%{_R}",
         f"{wr_b:.1f}% (gross)")
    _row("  gross_win_rate",    f"{_DIM}{gwr:.1f}%{_R}", "")

    if metrics.get("unmatched_fee_ct"):
        print(f"  {_YL}⚠ {metrics['unmatched_fee_ct']} fill(s) had a fee in a currency that "
              f"didn't match the symbol's quote — excluded from net figures rather than "
              f"mixed in; net numbers above may understate real cost.{_R}")

    # ── Sharpe ───────────────────────────────────────────────────────────
    # Trade-level (cumulative per-trade P&L), NOT a regularly sampled
    # account-return series — not directly comparable to a backtest's own
    # per-period Sharpe. 2026-09-18 review finding: labeled explicitly.
    sh     = metrics["sharpe"]
    sh_c   = _col(sh, 1.0, 0.0)
    _row("Sharpe (trade, not comparable to backtest)",  f"{sh_c}{sh:.2f}{_R}", "—")

    # ── Total P&L ────────────────────────────────────────────────────────
    tp     = metrics["total_pnl"]
    npl    = metrics["net_pnl"]
    tp_c   = _GR if tp > 0 else _RD
    npl_c  = _GR if npl > 0 else _RD
    _row("Total P&L (net, all fees)", f"{npl_c}${npl:+.2f}{_R}", "—")
    _row("  gross (pre-fee)",         f"{_DIM}${tp:+.2f}{_R}",   "")

    # ── Avg win / loss ───────────────────────────────────────────────────
    aw = metrics["avg_win"]
    al = metrics["avg_loss"]
    _row("Avg win",  f"${aw:+.2f}", "")
    _row("Avg loss", f"${al:.2f}",  "")

    # ── Max drawdown ─────────────────────────────────────────────────────
    dd     = metrics["max_dd"]
    dd_c   = _col(dd, -5, -10, higher_is_better=False)
    _row("Max DD ($)",      f"{dd_c}${dd:.2f}{_R}", f"-{abs(_BASELINE['max_dd_pct']):.2f}% of cash")

    print(f"  {'─'*20}")
    _row("Period",
         f"{metrics['first_trade'][:10]} → {metrics['last_trade'][:10]}",
         f"{_BASELINE['validated']} (validated)")
    print(f"  {_DIM}Baseline: fee={_BASELINE['fee_pct']}% taker  "
          f"SL={_BASELINE['stop_loss']}%  TP={_BASELINE['take_profit']}%{_R}")

    print(f"\n{'─' * w}")
    if n < 30:
        print(f"  {_YL}Statistical note: {n} trades is below the ~30-trade minimum for{_R}")
        print(f"  {_YL}reliable PF/win rate estimates. Keep accumulating live data.{_R}")
    else:
        print(f"  {_GR}Sample size ({n} trades) is statistically meaningful.{_R}")

    print(f"{'─' * w}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live vs Backtest comparison")
    parser.add_argument("--db",         default="logs/trades.db", help="SQLite database path")
    parser.add_argument("--min_trades", type=int, default=10,     help="Minimum fills before showing comparison")
    parser.add_argument("--base",       default=_BASELINE_BASE,
                        help="Base asset to compare (fills for other symbols are "
                             "excluded — the baseline is single-symbol)")
    args = parser.parse_args()

    all_fills = _load_fills(args.db)
    base      = args.base.strip().upper()
    fills     = [f for f in all_fills
                 if (f["symbol"] or "").split("/")[0].upper() == base]
    excluded  = len(all_fills) - len(fills)
    metrics   = _compute_live_metrics(fills)

    print(f"\n  Database:    {os.path.abspath(args.db)}")
    print(f"  Total fills: {len(all_fills)}  (BUY + SELL)")
    if excluded:
        _other = sorted({f["symbol"] for f in all_fills
                         if (f["symbol"] or "").split("/")[0].upper() != base})
        print(f"  {_DIM}Comparing {base} fills only ({len(fills)}) — {excluded} fill(s) "
              f"for other symbols excluded ({', '.join(_other)}): the baseline is "
              f"{_BASELINE['symbol']}-only. Use --base to compare another symbol.{_R}")
    _print_report(metrics, args.min_trades)


if __name__ == "__main__":
    main()
