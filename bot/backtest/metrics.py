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

    # Trades — NET of entry+exit fees (2026-09-12 finding: these were
    # computed from FillRecord.pnl, which is pure price-difference P&L —
    # position_manager.on_sell() never subtracts fees, and engine.py only
    # deducts them from cash, not from this per-trade figure. A trade
    # gaining $1 before $1.608 in fees was counted as a win, and the
    # walk-forward gate approved strategies on gross profit factor.
    # Reproduced on saved 2026-09-12 backtests: BTC/USDT pinned-window PF
    # was 1.87 gross, 0.82 (a net LOSS) once fees are correctly attributed
    # per trade. win_rate/profit_factor/avg_win/avg_loss/best_trade/
    # worst_trade below are now NET — this is what every validation gate
    # (walkforward.py, screen_universe.py, validate_symbol.py, ...) reads
    # as `m.profit_factor`, so the fix applies everywhere via this one
    # change. Gross versions kept alongside for comparison/transparency.
    total_trades:    int
    winning_trades:  int
    losing_trades:   int
    breakeven_trades: int
    win_rate:        float   # 0.0 – 1.0, NET of fees
    profit_factor:   float   # NET gross_profit / gross_loss; inf if no losses
    avg_win:         float   # NET
    avg_loss:        float   # NET, negative number
    best_trade:      float   # NET
    worst_trade:     float   # NET
    gross_profit_factor: float   # pre-fee — comparison only, never gates validation
    gross_win_rate:      float   # pre-fee — comparison only, never gates validation

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
    # NET pnl per closed trade = the SELL's gross pnl, minus its proportional
    # share of the entry fee(s) that opened the position it's closing, minus
    # the SELL's own fee. The entry fee is allocated by QUANTITY, not dumped
    # entirely onto whichever SELL happens to close the position first — with
    # a single full-position exit (partial_tp_pct=0, the only mode live today)
    # that's the same number either way, but a partial exit (partial TP,
    # engine.py's opt-in partial_tp_pct) previously charged 100% of the entry
    # fee to the FIRST partial SELL and 0% to the rest. Reviewer-reproduced:
    # two economically-losing partial exits were classified as one loss + one
    # win (total P&L unchanged, but win_rate/profit_factor distorted) —
    # confirmed dormant against the currently-live full-exit config, but a
    # real bug for anyone turning partial_tp_pct on.
    closed_pnls: list[float] = []   # NET — drives every statistic below
    gross_pnls:  list[float] = []   # pre-fee — comparison only
    _pending_buy_fee = 0.0
    _pending_buy_qty = 0.0
    for f in fills:
        if f.side == "BUY":
            _pending_buy_fee += f.fee
            _pending_buy_qty += f.quantity
        elif f.side == "SELL" and f.pnl is not None:
            gross_pnls.append(f.pnl)
            if _pending_buy_qty > 0:
                frac = min(f.quantity / _pending_buy_qty, 1.0)
                allocated_fee = _pending_buy_fee * frac
            else:
                allocated_fee = 0.0
            closed_pnls.append(f.pnl - allocated_fee - f.fee)
            _pending_buy_fee -= allocated_fee
            _pending_buy_qty -= f.quantity
            if _pending_buy_qty <= 1e-9:   # fully closed — clear any float dust
                _pending_buy_fee = 0.0
                _pending_buy_qty = 0.0

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

    # Pre-fee versions — for transparency/comparison only, never used to
    # gate validation (see gross_profit_factor's own field docstring).
    _g_wins   = [p for p in gross_pnls if p > 0]
    _g_losses = [p for p in gross_pnls if p < 0]
    gross_win_rate = len(_g_wins) / len(gross_pnls) if gross_pnls else 0.0
    _g_gp = sum(_g_wins)
    _g_gl = abs(sum(_g_losses))
    gross_profit_factor = _g_gp / _g_gl if _g_gl > 0 else (float("inf") if _g_gp > 0 else 0.0)

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
        gross_profit_factor = gross_profit_factor,
        gross_win_rate       = gross_win_rate,
        max_drawdown_pct  = max_drawdown_pct,
        sharpe_ratio      = sharpe_ratio,
        sortino_ratio     = sortino_ratio,
        calmar_ratio      = calmar_ratio,
        annualized_return = annualized_return,
    )
