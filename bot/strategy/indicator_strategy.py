"""
Improved indicator strategy — EMA trend-following with stronger filters.

Problems fixed vs original:
  - Old: bought every BULLISH EMA crossover regardless of strength → whipsawed
  - New: requires meaningful EMA separation + RSI momentum confirmation

Signal logic:
  BUY  — BULLISH trend + EMA spread > min_ema_spread_pct
          + RSI rising (momentum confirms)
          + RSI not overbought
  SELL — BEARISH trend + EMA spread > min_ema_spread_pct
          + RSI falling (momentum confirms)
          + RSI not oversold
  HOLD — anything else (flat market, weak crossover, conflicting signals)

Key additions:
  1. EMA separation filter  — ignore weak crossovers (noise)
  2. RSI direction filter   — only trade when RSI momentum agrees with trend
  3. RSI midline filter     — on BUY, RSI must be above 45 (not just "not overbought")
                              on SELL, RSI must be below 55 (not just "not oversold")
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from bot.strategy.threshold_strategy import Signal
from bot.indicators.indicators import rsi as calc_rsi, trend as calc_trend, ema as calc_ema, adx as calc_adx

logger = logging.getLogger(__name__)


@dataclass
class IndicatorConfig:
    rsi_period:          int   = 14
    rsi_overbought:      float = 65.0
    rsi_oversold:        float = 35.0
    rsi_buy_min:         float = 45.0   # RSI must be ABOVE this to BUY  (momentum rising)
    rsi_sell_max:        float = 55.0   # RSI must be BELOW this to SELL (momentum falling)
    fast_ema_period:     int   = 9
    slow_ema_period:     int   = 21
    min_ema_spread_pct:  float = 0.002  # EMAs must be 0.2% apart — filters weak crossovers
    adx_period:          int   = 14
    adx_threshold:       float = 25.0  # < threshold = ranging market → HOLD


class IndicatorStrategy:
    """
    Improved drop-in replacement for the original IndicatorStrategy.
    Same interface: evaluate(price: float) -> Signal

    Warmup: max(rsi_period + 1, slow_ema_period) + 2 extra ticks for RSI direction.
    """

    def __init__(self, config: IndicatorConfig | None = None):
        self.config   = config or IndicatorConfig()
        self._warmup  = max(
            self.config.rsi_period + 1,
            self.config.slow_ema_period,
            2 * self.config.adx_period + 1,
        ) + 2   # +2 for RSI direction comparison

        self._prices:     deque[float] = deque(maxlen=self._warmup + 50)
        self._highs:      deque[float] = deque(maxlen=self._warmup + 50)
        self._lows:       deque[float] = deque(maxlen=self._warmup + 50)
        self._last_rsi:   float | None = None
        self._last_trend: str   | None = None
        self._last_adx:   float | None = None
        self._prev_rsi:   float | None = None   # RSI one tick ago — for direction

        logger.info(
            "IndicatorStrategy (improved) | RSI(%d) ob=%.0f os=%.0f buy_min=%.0f sell_max=%.0f"
            " | EMA(%d/%d) min_spread=%.2f%% | ADX(%d) threshold=%.0f | warmup=%d",
            self.config.rsi_period,
            self.config.rsi_overbought, self.config.rsi_oversold,
            self.config.rsi_buy_min,    self.config.rsi_sell_max,
            self.config.fast_ema_period, self.config.slow_ema_period,
            self.config.min_ema_spread_pct * 100,
            self.config.adx_period, self.config.adx_threshold,
            self._warmup,
        )

    def evaluate(self, price: float, high: float | None = None, low: float | None = None) -> Signal:
        self._prices.append(price)
        if high is not None and low is not None:
            self._highs.append(high)
            self._lows.append(low)
        prices = list(self._prices)

        if len(prices) < self._warmup:
            return Signal.HOLD

        # ── Compute indicators ────────────────────────────────────────
        rsi_val   = calc_rsi(prices, self.config.rsi_period)
        trend_val = calc_trend(prices, self.config.fast_ema_period, self.config.slow_ema_period)
        fast_ema  = calc_ema(prices, self.config.fast_ema_period)
        slow_ema  = calc_ema(prices, self.config.slow_ema_period)

        if rsi_val is None or fast_ema is None or slow_ema is None:
            return Signal.HOLD

        # ── ADX market regime filter ──────────────────────────────────
        adx_val: float | None = None
        if len(self._highs) >= 2 * self.config.adx_period + 1:
            adx_val = calc_adx(
                list(self._highs), list(self._lows), prices,
                self.config.adx_period,
            )
        self._last_adx = adx_val

        if adx_val is not None and adx_val < self.config.adx_threshold:
            return Signal.HOLD   # ranging market — skip

        # ── RSI direction (is momentum rising or falling?) ────────────
        rsi_rising  = self._prev_rsi is not None and rsi_val > self._prev_rsi
        rsi_falling = self._prev_rsi is not None and rsi_val < self._prev_rsi

        self._prev_rsi   = self._last_rsi
        self._last_rsi   = rsi_val
        self._last_trend = trend_val

        # ── EMA separation filter ─────────────────────────────────────
        ema_spread_pct = abs(fast_ema - slow_ema) / slow_ema if slow_ema > 0 else 0.0
        ema_strong     = ema_spread_pct >= self.config.min_ema_spread_pct

        logger.info(
            "price=%.2f RSI=%.1f(%s) trend=%s EMA_spread=%.3f%% strong=%s ADX=%s",
            price, rsi_val,
            "↑" if rsi_rising else ("↓" if rsi_falling else "→"),
            trend_val, ema_spread_pct * 100, ema_strong,
            f"{adx_val:.1f}" if adx_val is not None else "n/a",
        )

        # ── BUY conditions ────────────────────────────────────────────
        # 1. Uptrend confirmed by EMA crossover
        # 2. EMA spread is meaningful (not a weak/noisy crossover)
        # 3. RSI is rising (momentum agrees with trend)
        # 4. RSI is above midline (not just starting from oversold bounce)
        # 5. RSI has not hit overbought (move not exhausted)
        if (trend_val == "BULLISH"
                and ema_strong
                and rsi_rising
                and rsi_val > self.config.rsi_buy_min
                and rsi_val < self.config.rsi_overbought):
            return Signal.BUY

        # ── SELL conditions ───────────────────────────────────────────
        # 1. Downtrend confirmed by EMA crossover
        # 2. EMA spread is meaningful
        # 3. RSI is falling (momentum agrees with trend)
        # 4. RSI is below midline
        # 5. RSI has not hit oversold (move not exhausted)
        if (trend_val == "BEARISH"
                and ema_strong
                and rsi_falling
                and rsi_val < self.config.rsi_sell_max
                and rsi_val > self.config.rsi_oversold):
            return Signal.SELL

        return Signal.HOLD

    # ── Read-only properties (same interface as original) ─────────────

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

    @property
    def last_adx(self) -> float | None:
        return self._last_adx