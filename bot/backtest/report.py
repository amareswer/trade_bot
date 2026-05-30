"""
Backtest report — terminal output and CSV export.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone

from bot.backtest.engine import BacktestResult
from bot.backtest.metrics import BacktestMetrics

# ANSI
_R  = "\033[0m"
_B  = "\033[1m"
_DIM= "\033[2m"
_GR = "\033[92m"
_RD = "\033[91m"
_YL = "\033[93m"
_CY = "\033[96m"
_WH = "\033[97m"


def print_report(metrics: BacktestMetrics, result: BacktestResult) -> None:
    sym = result.symbol.replace("/", "_")
    bar = "═" * 50

    def _pnl(v: float) -> str:
        color = _GR if v >= 0 else _RD
        sign  = "+" if v >= 0 else ""
        return f"{color}{sign}${v:,.2f}{_R}"

    def _pct(v: float) -> str:
        color = _GR if v >= 0 else _RD
        sign  = "+" if v >= 0 else ""
        return f"{color}{sign}{v*100:.2f}%{_R}"

    def _row(label: str, value: str, width: int = 22) -> None:
        print(f"  {_DIM}{label:<{width}}{_R}  {value}")

    print(f"\n{_B}{bar}{_R}")
    print(f"  {_B}BACKTEST REPORT{_R}")
    print(f"  {_CY}{result.symbol}{_R}  ·  {result.timeframe}  ·  {metrics.candle_count} candles")
    print(f"  {_DIM}{metrics.period_start}  →  {metrics.period_end}{_R}")
    print(f"{_DIM}{bar}{_R}")

    print(f"\n  {_B}PERFORMANCE{_R}")
    print(f"  {'─'*46}")
    _row("Starting cash",   f"${metrics.starting_cash:>12,.2f}")
    _row("Final value",     f"${metrics.final_value:>12,.2f}")
    _row("Total return",    _pct(metrics.total_return_pct))
    _row("Total fees paid", _pnl(-metrics.total_fees))

    print(f"\n  {_B}TRADES{_R}")
    print(f"  {'─'*46}")
    _row("Total trades",    str(metrics.total_trades))

    if metrics.total_trades == 0:
        print(f"  {_YL}  No trades executed — strategy produced only HOLD signals.{_R}")
        print(f"  {_YL}  Try adjusting RSI thresholds or a longer timeframe.{_R}")
    else:
        win_str = f"{metrics.win_rate*100:.1f}%  ({metrics.winning_trades}W / {metrics.losing_trades}L)"
        if metrics.breakeven_trades:
            win_str += f" / {metrics.breakeven_trades}B"
        _row("Win rate",       f"{_GR if metrics.win_rate >= 0.5 else _RD}{win_str}{_R}")

        pf_col = _GR if metrics.profit_factor >= 1.0 else _RD
        pf_str = f"{pf_col}{metrics.profit_factor:.2f}{_R}" if metrics.profit_factor != float("inf") else f"{_GR}∞{_R}"
        _row("Profit factor",  pf_str)
        _row("Avg win",        _pnl(metrics.avg_win))
        _row("Avg loss",       _pnl(metrics.avg_loss))
        _row("Best trade",     _pnl(metrics.best_trade))
        _row("Worst trade",    _pnl(metrics.worst_trade))

    print(f"\n  {_B}RISK{_R}")
    print(f"  {'─'*46}")
    _row("Max drawdown",    _pct(metrics.max_drawdown_pct))

    sr = metrics.sharpe_ratio
    sr_col = _GR if sr >= 1.0 else (_YL if sr >= 0 else _RD)
    _row("Sharpe ratio",    f"{sr_col}{sr:.2f}{_R}")

    print(f"\n{_DIM}{bar}{_R}\n")


def save_csv(result: BacktestResult, directory: str = "logs") -> str:
    """Write all fills to a CSV. Returns the file path."""
    os.makedirs(directory, exist_ok=True)
    sym       = result.symbol.replace("/", "_")
    date_str  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename  = f"backtest_{sym}_{result.timeframe}_{date_str}.csv"
    path      = os.path.join(directory, filename)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "side", "price", "quantity",
            "total_value", "pnl", "fee",
        ])
        for fill in result.fills:
            writer.writerow([
                fill.timestamp,
                fill.side,
                fill.price,
                fill.quantity,
                fill.total_value,
                fill.pnl if fill.pnl is not None else "",
                fill.fee,
            ])

    return path
