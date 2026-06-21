"""
Backtest performance metrics.

compute() takes a BacktestResult and returns a BacktestMetrics with all
statistics needed for the report.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from bot.backtest.engine import BacktestResult, FillRecord
from bot.data.historical_feed import ANNUALISATION


@dataclass
class BacktestMetrics:
    # Period
    period_start:    str
    period_end:      str
    candle_count:    int
    tradeable_count: int   # candles after warmup

    # Performance
    starting_cash:   float
    final_value:     float
    total_return_pct: float
    total_fees:      float

    # Trades
    total_trades:    int
    winning_trades:  int
    losing_trades:   int
    breakeven_trades: int
    win_rate:        float   # 0.0 – 1.0
    profit_factor:   float   # gross_profit / gross_loss; inf if no losses
    avg_win:         float
    avg_loss:        float   # negative number
    best_trade:      float
    worst_trade:     float

    # Risk
    max_drawdown_pct:   float  # negative number e.g. -0.032
    sharpe_ratio:       float
    sortino_ratio:      float  # Sharpe using only downside deviation
    calmar_ratio:       float  # annualized_return / abs(max_drawdown)
    annualized_return:  float  # total_return scaled to 1 year


def compute(result: BacktestResult) -> BacktestMetrics:
    candles   = result.candles
    fills     = result.fills
    equity    = result.equity_curve
    timeframe = result.timeframe

    # ── Period ────────────────────────────────────────────────────────
    period_start = candles[0].timestamp.strftime("%Y-%m-%d %H:%M") if candles else "—"
    period_end   = candles[-1].timestamp.strftime("%Y-%m-%d %H:%M") if candles else "—"
    tradeable    = len(equity)

    # ── Return ────────────────────────────────────────────────────────
    total_return_pct = (
        (result.final_value - result.starting_cash) / result.starting_cash
        if result.starting_cash else 0.0
    )

    # ── Trade stats (only SELL fills carry realized P&L) ─────────────
    closed_pnls = [f.pnl for f in fills if f.side == "SELL" and f.pnl is not None]
    total_trades  = len(closed_pnls)
    wins          = [p for p in closed_pnls if p > 0]
    losses        = [p for p in closed_pnls if p < 0]
    breakevens    = [p for p in closed_pnls if p == 0]

    win_rate      = len(wins) / total_trades if total_trades else 0.0
    gross_profit  = sum(wins)
    gross_loss    = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    avg_win       = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss      = sum(losses) / len(losses) if losses else 0.0
    best_trade    = max(closed_pnls) if closed_pnls else 0.0
    worst_trade   = min(closed_pnls) if closed_pnls else 0.0

    # ── Max drawdown ──────────────────────────────────────────────────
    max_drawdown_pct = 0.0
    if equity:
        peak = equity[0]
        for v in equity:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (v - peak) / peak
                if dd < max_drawdown_pct:
                    max_drawdown_pct = dd

    # ── Sharpe / Sortino / Calmar / Annualized return ────────────────
    sharpe_ratio      = 0.0
    sortino_ratio     = 0.0
    calmar_ratio      = 0.0
    annualized_return = 0.0
    periods_per_year  = ANNUALISATION.get(timeframe, 365)

    if len(equity) >= 2:
        returns = [
            (equity[i] - equity[i - 1]) / equity[i - 1]
            for i in range(1, len(equity))
            if equity[i - 1] > 0
        ]
        if len(returns) >= 2:
            mean_r   = sum(returns) / len(returns)
            variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
            std_r    = math.sqrt(variance)
            if std_r > 0:
                sharpe_ratio = round((mean_r / std_r) * math.sqrt(periods_per_year), 2)

            # Sortino: downside deviation = sqrt(mean of min(r,0)^2 for all r)
            downside_sq = sum(min(r, 0.0) ** 2 for r in returns) / len(returns)
            downside_std = math.sqrt(downside_sq)
            if downside_std > 0:
                sortino_ratio = round((mean_r / downside_std) * math.sqrt(periods_per_year), 2)

    # Annualized return: compound the total return over the observed period
    n_candles = len(equity)
    if n_candles > 0 and result.starting_cash > 0:
        holding_periods = n_candles          # tradeable candles
        power = periods_per_year / holding_periods if holding_periods > 0 else 1.0
        annualized_return = round(
            (result.final_value / result.starting_cash) ** power - 1.0, 4
        )

    # Calmar: annualized return / abs(max drawdown)
    if max_drawdown_pct < 0:
        calmar_ratio = round(annualized_return / abs(max_drawdown_pct), 2)

    return BacktestMetrics(
        period_start     = period_start,
        period_end       = period_end,
        candle_count     = len(candles),
        tradeable_count  = tradeable,
        starting_cash    = result.starting_cash,
        final_value      = result.final_value,
        total_return_pct = total_return_pct,
        total_fees       = result.total_fees,
        total_trades     = total_trades,
        winning_trades   = len(wins),
        losing_trades    = len(losses),
        breakeven_trades = len(breakevens),
        win_rate         = win_rate,
        profit_factor    = profit_factor,
        avg_win          = avg_win,
        avg_loss         = avg_loss,
        best_trade       = best_trade,
        worst_trade      = worst_trade,
        max_drawdown_pct  = max_drawdown_pct,
        sharpe_ratio      = sharpe_ratio,
        sortino_ratio     = sortino_ratio,
        calmar_ratio      = calmar_ratio,
        annualized_return = annualized_return,
    )
