"""
Indicator-based strategy: EMA trend-following filtered by RSI.

Signal logic (fully deterministic, no AI):
  BUY  — EMA trend is BULLISH  AND RSI has not yet reached overbought territory
  SELL — EMA trend is BEARISH  AND RSI has not yet reached oversold territory
  HOLD — during warmup, flat/neutral trend, or RSI at an extreme against the trend

Rationale: follow the confirmed trend (EMA crossover) but skip entries when
momentum is already exhausted — i.e., don't chase a move when RSI > rsi_overbought,
and don't short a move when RSI < rsi_oversold.

The strategy maintains an internal price buffer.  Callers only call evaluate(price)
each tick — identical interface to ThresholdStrategy.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

from bot.strategy.threshold_strategy import Signal
from bot.indicators.indicators import rsi as calc_rsi, trend as calc_trend

logger = logging.getLogger(__name__)


@dataclass
class IndicatorConfig:
    rsi_period:      int   = 14
    rsi_overbought:  float = 70.0   # skip BUY when RSI is above this (momentum exhausted)
    rsi_oversold:    float = 30.0   # skip SELL when RSI is below this (momentum exhausted)
    fast_ema_period: int   = 9
    slow_ema_period: int   = 21


class IndicatorStrategy:
    """
    Drop-in replacement for ThresholdStrategy.
    Implements: evaluate(price: float) -> Signal

    Warmup period: max(rsi_period + 1, slow_ema_period) ticks.
    Returns HOLD during warmup so no trades fire before indicators are valid.
    """

    def __init__(self, config: IndicatorConfig | None = None):
        self.config = config or IndicatorConfig()
        self._warmup = max(
            self.config.rsi_period + 1,
            self.config.slow_ema_period,
        )
        self._prices: deque[float] = deque(maxlen=self._warmup + 50)
        self._last_rsi:   float | None = None
        self._last_trend: str   | None = None
        logger.info(
            "IndicatorStrategy ready | RSI(%d) overbought=%.0f oversold=%.0f"
            " | EMA(%d/%d) | warmup=%d ticks",
            self.config.rsi_period,
            self.config.rsi_overbought,
            self.config.rsi_oversold,
            self.config.fast_ema_period,
            self.config.slow_ema_period,
            self._warmup,
        )

    def evaluate(self, price: float) -> Signal:
        self._prices.append(price)
        prices = list(self._prices)

        if len(prices) < self._warmup:
            logger.debug("Warmup: %d/%d ticks", len(prices), self._warmup)
            return Signal.HOLD

        rsi_val   = calc_rsi(prices, self.config.rsi_period)
        trend_val = calc_trend(prices, self.config.fast_ema_period, self.config.slow_ema_period)
        self._last_rsi   = rsi_val
        self._last_trend = trend_val

        logger.info(
            "Indicators | price=%.2f | RSI(%.0f)=%.2f | trend=%s",
            price,
            self.config.rsi_period,
            rsi_val if rsi_val is not None else float("nan"),
            trend_val,
        )

        if rsi_val is None:
            return Signal.HOLD

        # BUY: uptrend confirmed by EMA crossover, momentum not yet overbought
        if trend_val == "BULLISH" and rsi_val < self.config.rsi_overbought:
            return Signal.BUY

        # SELL: downtrend confirmed by EMA crossover, momentum not yet oversold
        if trend_val == "BEARISH" and rsi_val > self.config.rsi_oversold:
            return Signal.SELL

        return Signal.HOLD

    @property
    def tick_count(self) -> int:
        return len(self._prices)

    @property
    def is_warmed_up(self) -> bool:
        return len(self._prices) >= self._warmup

    @property
    def last_rsi(self) -> float | None:
        return self._last_rsi

    @property
    def last_trend(self) -> str | None:
        return self._last_trend
