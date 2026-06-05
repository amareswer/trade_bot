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
from bot.data.historical_feed import Candle

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
    min_ema_spread_pct:  float = 0.002  # EMAs must be at least this far apart
    max_ema_spread_pct:  float = 0.0    # EMAs must be no more than this far apart (0 = disabled)
    adx_period:          int   = 14
    adx_threshold:       float = 25.0   # < threshold = ranging market → HOLD (0 = disabled)
    rsi_filter_enabled:  bool  = True   # set False to bypass RSI level/direction checks


class IndicatorStrategy:
    """
    Improved indicator strategy — EMA trend + RSI momentum + ADX regime filter.
    Interface: evaluate(candle: Candle) -> Signal
    """

    def __init__(self, config: IndicatorConfig | None = None):
        self.config   = config or IndicatorConfig()
        self._warmup  = max(
            self.config.rsi_period + 1,
            self.config.slow_ema_period,
            2 * self.config.adx_period + 1,
        ) + 2   # +2 for RSI direction comparison

        self._closes:     deque[float] = deque(maxlen=self._warmup + 50)
        self._highs:      deque[float] = deque(maxlen=self._warmup + 50)
        self._lows:       deque[float] = deque(maxlen=self._warmup + 50)
        self._last_rsi:   float | None = None
        self._last_trend: str   | None = None
        self._last_adx:   float | None = None
        self._prev_rsi:   float | None = None   # RSI one tick ago — for direction

        self.stats: dict[str, int] = {
            "candles_seen":    0,
            "warmup_rejected": 0,
            "adx_rejected":    0,
            "trend_rejected":  0,
            "ema_rejected":    0,
            "rsi_rejected":    0,
            "buy_signals":     0,
            "sell_signals":    0,
            "hold_signals":    0,
        }

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

    def evaluate(self, candle) -> Signal:
        if isinstance(candle, (int, float)):
            from datetime import datetime, timezone as _tz
            candle = Candle(
                timestamp=datetime.now(_tz.utc),
                open=float(candle), high=float(candle),
                low=float(candle), close=float(candle), volume=0.0,
            )
        price = candle.close
        self._closes.append(price)
        self._highs.append(candle.high)
        self._lows.append(candle.low)
        closes = list(self._closes)

        self.stats["candles_seen"] += 1

        if len(closes) < self._warmup:
            self.stats["warmup_rejected"] += 1
            return Signal.HOLD

        # ── Compute indicators ────────────────────────────────────────
        rsi_val   = calc_rsi(closes, self.config.rsi_period)
        trend_val = calc_trend(closes, self.config.fast_ema_period, self.config.slow_ema_period)
        fast_ema  = calc_ema(closes, self.config.fast_ema_period)
        slow_ema  = calc_ema(closes, self.config.slow_ema_period)

        if rsi_val is None or fast_ema is None or slow_ema is None:
            self.stats["warmup_rejected"] += 1
            return Signal.HOLD

        # ── ADX market regime filter ──────────────────────────────────
        adx_val: float | None = None
        if len(self._highs) >= 2 * self.config.adx_period + 1:
            adx_val = calc_adx(
                list(self._highs), list(self._lows), closes,
                self.config.adx_period,
            )
        self._last_adx = adx_val

        if adx_val is None or adx_val < self.config.adx_threshold:
            self.stats["adx_rejected"] += 1
            return Signal.HOLD   # ranging market — skip

        # ── RSI direction (is momentum rising or falling?) ────────────
        rsi_rising  = self._last_rsi is not None and rsi_val > self._last_rsi
        rsi_falling = self._last_rsi is not None and rsi_val < self._last_rsi

        self._prev_rsi   = self._last_rsi
        self._last_rsi   = rsi_val
        self._last_trend = trend_val

        # ── EMA separation filter ─────────────────────────────────────
        ema_spread_pct = abs(fast_ema - slow_ema) / slow_ema if slow_ema > 0 else 0.0
        ema_above_min  = ema_spread_pct >= self.config.min_ema_spread_pct
        ema_below_max  = (self.config.max_ema_spread_pct <= 0 or
                          ema_spread_pct <= self.config.max_ema_spread_pct)
        ema_strong     = ema_above_min and ema_below_max

        logger.info(
            "price=%.2f RSI=%.1f(%s) trend=%s EMA_spread=%.3f%% strong=%s ADX=%.1f",
            price, rsi_val,
            "↑" if rsi_rising else ("↓" if rsi_falling else "→"),
            trend_val, ema_spread_pct * 100, ema_strong, adx_val,
        )

        # ── BUY / SELL conditions ─────────────────────────────────────
        if trend_val == "BULLISH":
            if not ema_strong:
                self.stats["ema_rejected"] += 1
            elif self.config.rsi_filter_enabled and not (
                    rsi_rising
                    and rsi_val > self.config.rsi_buy_min
                    and rsi_val < self.config.rsi_overbought):
                self.stats["rsi_rejected"] += 1
            else:
                self.stats["buy_signals"] += 1
                return Signal.BUY
        elif trend_val == "BEARISH":
            if not ema_strong:
                self.stats["ema_rejected"] += 1
            elif self.config.rsi_filter_enabled and not (
                    rsi_falling
                    and rsi_val < self.config.rsi_sell_max
                    and rsi_val > self.config.rsi_oversold):
                self.stats["rsi_rejected"] += 1
            else:
                self.stats["sell_signals"] += 1
                return Signal.SELL
        else:
            self.stats["trend_rejected"] += 1

        self.stats["hold_signals"] += 1
        return Signal.HOLD

    # ── Read-only properties (same interface as original) ─────────────

    @property
    def tick_count(self) -> int:
        return len(self._closes)

    @property
    def is_warmed_up(self) -> bool:
        return len(self._closes) >= self._warmup

    @property
    def last_rsi(self) -> float | None:
        return self._last_rsi

    @property
    def last_trend(self) -> str | None:
        return self._last_trend

    @property
    def last_adx(self) -> float | None:
        return self._last_adx