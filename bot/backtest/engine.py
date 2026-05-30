"""
Backtest engine.

Runs the full strategy pipeline on historical candles — same layers as main.py,
no live network calls, no real money.

Pipeline per candle:
  candle.close
    → IndicatorStrategy.evaluate()
    → TradingStateMachine.filter_signal()
    → RiskManager.evaluate()
    → PaperExecutor.execute()
    → PositionManager.on_buy / on_sell()
    → equity recorded
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from bot.data.historical_feed import Candle
from bot.strategy.threshold_strategy import ThresholdStrategy, Signal
from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig
from bot.execution.executor import PaperExecutor, OrderStatus, OrderSide
from bot.risk.risk_manager import RiskManager, RiskConfig
from bot.state.trade_state import TradingStateMachine
from bot.portfolio.position_manager import PositionManager

logger = logging.getLogger(__name__)


@dataclass
class FillRecord:
    candle_index: int
    timestamp:    str
    side:         str
    price:        float
    quantity:     float
    total_value:  float
    pnl:          float | None   # None for BUY; realized PnL for SELL
    fee:          float
    reason:       str = "strategy"   # "strategy" | "stop_loss" | "take_profit"


@dataclass
class BacktestResult:
    symbol:        str
    timeframe:     str
    fee_pct:       float
    starting_cash: float
    final_value:   float
    total_fees:    float
    fills:         list[FillRecord]
    equity_curve:  list[float]     # portfolio value after each candle
    candles:       list[Candle]
    warmup_ticks:  int


def run(
    candles:              list[Candle],
    symbol:               str,
    timeframe:            str,
    strategy_mode:        str   = "indicator",
    starting_cash:        float = 10_000.0,
    risk_per_trade_pct:   float = 0.01,    # dynamic sizing: % of cash per trade
    fee_pct:              float = 0.001,
    cooldown_ticks:       int   = 10,
    # Indicator config
    rsi_period:           int   = 14,
    rsi_oversold:         float = 30.0,
    rsi_overbought:       float = 70.0,
    fast_ema_period:      int   = 9,
    slow_ema_period:      int   = 21,
    adx_period:           int   = 14,
    adx_threshold:        float = 25.0,
    # Threshold config
    buy_threshold:        float = 0.0,
    sell_threshold:       float = 0.0,
    # Risk config
    max_position_pct:     float = 0.05,
    daily_loss_limit_pct: float = 0.02,
    max_drawdown_pct:     float = 0.10,
    max_trades_per_day:   int   = 5,
    # Exit rules
    stop_loss_pct:        float = 0.02,   # exit if price drops this % from entry (0 = disabled)
    take_profit_pct:      float = 0.04,   # exit if price rises this % from entry (0 = disabled)
) -> BacktestResult:
    """Run a full backtest and return the result."""

    # ── Build components (same as main.py) ───────────────────────────
    if strategy_mode == "indicator":
        strategy = IndicatorStrategy(IndicatorConfig(
            rsi_period      = rsi_period,
            rsi_oversold    = rsi_oversold,
            rsi_overbought  = rsi_overbought,
            fast_ema_period = fast_ema_period,
            slow_ema_period = slow_ema_period,
            adx_period      = adx_period,
            adx_threshold   = adx_threshold,
        ))
        is_indicator = True
    else:
        strategy = ThresholdStrategy(
            buy_threshold  = buy_threshold,
            sell_threshold = sell_threshold,
        )
        is_indicator = False

    executor         = PaperExecutor(symbol=symbol, starting_cash=starting_cash)
    risk             = RiskManager(RiskConfig(
        max_position_pct      = max_position_pct,
        daily_loss_limit_pct  = daily_loss_limit_pct,
        max_drawdown_pct      = max_drawdown_pct,
        max_trades_per_day    = max_trades_per_day,
    ))
    state_machine    = TradingStateMachine(cooldown_ticks=cooldown_ticks)
    position_manager = PositionManager()

    equity_curve: list[float] = []
    fills:        list[FillRecord] = []
    total_fees    = 0.0
    warmup_ticks  = 0
    entry_price:  float = 0.0   # tracks BUY fill price for SL/TP

    # ── Main loop ─────────────────────────────────────────────────────
    for i, candle in enumerate(candles):
        price = candle.close

        state_machine.tick()

        raw_signal = strategy.evaluate(candle.close, high=candle.high, low=candle.low)

        if is_indicator and not strategy.is_warmed_up:
            warmup_ticks += 1
            equity_curve.append(executor.portfolio.total_value(price))
            continue

        # ── Stop-loss / take-profit override ─────────────────────────
        exit_reason = "strategy"
        if executor.position > 0 and entry_price > 0:
            if stop_loss_pct > 0 and price <= entry_price * (1 - stop_loss_pct):
                raw_signal  = Signal.SELL
                exit_reason = "stop_loss"
            elif take_profit_pct > 0 and price >= entry_price * (1 + take_profit_pct):
                raw_signal  = Signal.SELL
                exit_reason = "take_profit"

        filtered_signal, _ = state_machine.filter_signal(raw_signal)

        # Dynamic sizing: BUY = % of cash; SELL = close full position
        if filtered_signal == Signal.SELL:
            trade_qty = executor.position
        else:
            trade_qty = round(executor.cash * risk_per_trade_pct / price, 6) if price > 0 else 0.0

        approval = risk.evaluate(filtered_signal, price, executor.portfolio, trade_qty, candle.timestamp.date())

        if approval:
            order = executor.execute(filtered_signal, price, quantity=trade_qty)
            if order and order.status == OrderStatus.FILLED:
                risk.record_fill()
                state_machine.on_fill(filtered_signal, order.price)

                fee  = round(order.total_value * fee_pct, 4)
                total_fees += fee
                executor._portfolio.cash -= fee

                pnl = None
                if order.side == OrderSide.BUY:
                    position_manager.on_buy(order.price, order.quantity)
                    entry_price = order.price
                else:
                    pnl = position_manager.on_sell(order.price, order.quantity)
                    entry_price = 0.0

                fills.append(FillRecord(
                    candle_index = i,
                    timestamp    = candle.timestamp.strftime("%Y-%m-%d %H:%M"),
                    side         = order.side.value,
                    price        = order.price,
                    quantity     = order.quantity,
                    total_value  = order.total_value,
                    pnl          = pnl,
                    fee          = fee,
                    reason       = exit_reason if order.side == OrderSide.SELL else "strategy",
                ))

        equity_curve.append(executor.portfolio.total_value(price))

    # ── Close any open position at last price (mark-to-market) ───────
    last_price = candles[-1].close if candles else 0.0
    final_value = executor.portfolio.total_value(last_price)

    logger.info(
        "Backtest complete | candles=%d warmup=%d fills=%d final=$%.2f fees=$%.2f",
        len(candles), warmup_ticks, len(fills), final_value, total_fees,
    )

    return BacktestResult(
        symbol        = symbol,
        timeframe     = timeframe,
        fee_pct       = fee_pct,
        starting_cash = starting_cash,
        final_value   = final_value,
        total_fees    = total_fees,
        fills         = fills,
        equity_curve  = equity_curve,
        candles       = candles,
        warmup_ticks  = warmup_ticks,
    )
