"""
Improved indicator strategy — EMA trend-following with stronger filters.

Problems fixed vs original:
  - Old: bought every BULLISH EMA crossover regardless of strength → whipsawed
  - New: requires meaningful EMA separation + RSI momentum confirmation

Signal logic:
  BUY  — BULLISH trend + EMA spread > min_ema_spread_pct
          + RSI rising (momentum confirms)
          + RSI not overbought
          + price above regime EMA (bull regime confirmed)
          + regime EMA slope rising (trend not rolling over)
  SELL — BEARISH trend + EMA spread > min_ema_spread_pct
          + RSI falling (momentum confirms)
          + RSI not oversold
  HOLD — anything else (flat market, weak crossover, conflicting signals,
          bear regime detected, or regime EMA slope falling)

Key additions:
  1. EMA separation filter  — ignore weak crossovers (noise)
  2. RSI direction filter   — only trade when RSI momentum agrees with trend
  3. RSI midline filter     — on BUY, RSI must be above 45 (not just "not overbought")
                              on SELL, RSI must be below 55 (not just "not oversold")
  4. Regime EMA filter      — BUY only when price > regime_ema_period EMA
                              (filters bear market entries)
  5. Regime EMA slope filter — BUY only when EMA200 is rising (slope > 0 over 24h)
                               prevents entries during dead-cat bounces
                               (only active when regime_ema_slope_filter=True)
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional

from bot.strategy.threshold_strategy import Signal
from bot.indicators.indicators import rsi as calc_rsi, trend as calc_trend, ema as calc_ema, adx as calc_adx, macd as calc_macd
from bot.data.historical_feed import Candle

logger = logging.getLogger(__name__)


@dataclass
class IndicatorConfig:
    rsi_period:               int   = 14
    rsi_overbought:           float = 65.0
    rsi_oversold:             float = 35.0
    rsi_buy_min:              float = 45.0   # RSI must be ABOVE this to BUY  (momentum rising)
    rsi_sell_max:             float = 55.0   # RSI must be BELOW this to SELL (momentum falling)
    fast_ema_period:          int   = 9
    slow_ema_period:          int   = 21
    min_ema_spread_pct:       float = 0.002  # EMAs must be at least this far apart
    max_ema_spread_pct:       float = 0.0    # EMAs must be no more than this far apart (0 = disabled)
    adx_period:               int   = 14
    adx_threshold:            float = 25.0   # < threshold = ranging market → HOLD (0 = disabled)
    adx_max:                  float = 0.0    # > max = overextended trend → HOLD (0 = disabled)
    volume_k:                 float = 1.2    # current volume must be >= k * avg(last 3 candles); 0 = disabled
    macd_enabled:             bool  = True   # BUY only when MACD histogram is rising (momentum)
    macd_fast_period:         int   = 12
    macd_slow_period:         int   = 26
    macd_signal_period:       int   = 9
    rsi_filter_enabled:       bool  = True   # set False to bypass RSI level/direction checks
    regime_ema_period:        int   = 200    # BUY only when price > this EMA (0 = disabled)
    regime_ema_slope_filter:  bool  = False  # BUY only when EMA200 slope > 0 (rising)


class IndicatorStrategy:
    """
    Improved indicator strategy — EMA trend + RSI momentum + ADX regime filter
    + long-period regime EMA filter to block bear market entries
    + optional regime EMA slope filter to block dead-cat bounce entries.
    Interface: evaluate(candle: Candle) -> Signal
    """

    def __init__(self, config: Optional[IndicatorConfig] = None):
        self.config   = config or IndicatorConfig()
        self._warmup  = max(
            self.config.rsi_period + 1,
            self.config.slow_ema_period,
            2 * self.config.adx_period + 1,
            self.config.regime_ema_period if self.config.regime_ema_period > 0 else 0,
        ) + 2   # +2 for RSI direction comparison

        # Deque sized to hold enough candles for all indicators including regime EMA
        # +6 extra so slope comparison (6 candles = 24h) always has data
        buf = max(self._warmup + 50, self.config.regime_ema_period + 16)
        self._closes:     deque = deque(maxlen=buf)
        self._highs:      deque = deque(maxlen=buf)
        self._lows:       deque = deque(maxlen=buf)
        self._volumes:    deque = deque(maxlen=buf)
        self._last_rsi:       Optional[float] = None
        self._last_trend:     Optional[str]   = None
        self._last_adx:       Optional[float] = None
        self._prev_rsi:       Optional[float] = None   # RSI one tick ago — for direction
        self._last_macd_hist: Optional[float] = None

        self.stats: dict = {
            "candles_seen":    0,
            "warmup_rejected": 0,
            "adx_rejected":    0,
            "trend_rejected":  0,
            "ema_rejected":    0,
            "rsi_rejected":    0,
            "macd_rejected":   0,
            "regime_rejected": 0,
            "volume_rejected": 0,
            "buy_signals":     0,
            "sell_signals":    0,
            "hold_signals":    0,
        }

        logger.info(
            "IndicatorStrategy (improved) | RSI(%d) ob=%.0f os=%.0f buy_min=%.0f sell_max=%.0f"
            " | EMA(%d/%d) min_spread=%.2f%% | ADX(%d) threshold=%.0f"
            " | regime_ema=%d slope_filter=%s | warmup=%d",
            self.config.rsi_period,
            self.config.rsi_overbought, self.config.rsi_oversold,
            self.config.rsi_buy_min,    self.config.rsi_sell_max,
            self.config.fast_ema_period, self.config.slow_ema_period,
            self.config.min_ema_spread_pct * 100,
            self.config.adx_period, self.config.adx_threshold,
            self.config.regime_ema_period,
            self.config.regime_ema_slope_filter,
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
        self._volumes.append(candle.volume)
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
        adx_val: Optional[float] = None
        if len(self._highs) >= 2 * self.config.adx_period + 1:
            adx_val = calc_adx(
                list(self._highs), list(self._lows), closes,
                self.config.adx_period,
            )
        self._last_adx = adx_val

        if adx_val is None or adx_val < self.config.adx_threshold:
            self.stats["adx_rejected"] += 1
            return Signal.HOLD   # ranging market — skip
        if self.config.adx_max > 0 and adx_val > self.config.adx_max:
            self.stats["adx_rejected"] += 1
            return Signal.HOLD   # overextended trend — skip

        # ── Regime EMA filter (bull/bear macro filter) ────────────────
        # Only allow BUY when price is above the long-period EMA.
        # SELL signals are not filtered — always allow exits.
        if self.config.regime_ema_period > 0:
            regime_ema = calc_ema(closes, self.config.regime_ema_period)

            if regime_ema is not None and price < regime_ema:
                # Price is below regime EMA → bear regime → block BUY
                if trend_val == "BULLISH":
                    self.stats["regime_rejected"] += 1
                    return Signal.HOLD

            # ── Regime EMA slope filter (Project 2) ──────────────────
            # Only enter when EMA200 is rising — prevents dead-cat bounce entries.
            # Compares EMA200 now vs EMA200 24h ago (6 × 4h candles).
            # Only active when regime_ema_slope_filter=True.
            if (self.config.regime_ema_slope_filter
                    and regime_ema is not None
                    and trend_val == "BULLISH"
                    and len(closes) >= self.config.regime_ema_period + 6):
                regime_ema_prev = calc_ema(closes[:-6], self.config.regime_ema_period)
                if regime_ema_prev is not None and regime_ema < regime_ema_prev:
                    # EMA200 is still falling — regime not yet confirmed rising
                    self.stats["regime_rejected"] += 1
                    return Signal.HOLD

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

        # ── Volume confirmation ───────────────────────────────────────
        vols = list(self._volumes)
        volume_ok = True
        if self.config.volume_k > 0 and len(vols) >= 4:
            avg_vol_3 = sum(vols[-4:-1]) / 3
            curr_vol  = vols[-1]
            volume_ok = curr_vol >= self.config.volume_k * avg_vol_3
            logger.info(
                "volume  curr=%.2f  avg3=%.2f  threshold=%.2f  ok=%s",
                curr_vol, avg_vol_3, self.config.volume_k * avg_vol_3, volume_ok,
            )

        # ── MACD momentum confirmation ────────────────────────────────
        macd_val = calc_macd(closes, self.config.macd_fast_period, self.config.macd_slow_period, self.config.macd_signal_period)
        macd_hist = macd_val[2] if macd_val is not None else None
        macd_hist_rising = (
            macd_hist is not None
            and self._last_macd_hist is not None
            and macd_hist > self._last_macd_hist
        )
        self._last_macd_hist = macd_hist

        # ── BUY / SELL conditions ─────────────────────────────────────
        if trend_val == "BULLISH":
            if not ema_strong:
                self.stats["ema_rejected"] += 1
            elif self.config.rsi_filter_enabled and not (
                    rsi_rising
                    and rsi_val > self.config.rsi_buy_min
                    and rsi_val < self.config.rsi_overbought):
                self.stats["rsi_rejected"] += 1
            elif not volume_ok:
                self.stats["volume_rejected"] += 1
            elif self.config.macd_enabled and not macd_hist_rising:
                self.stats["macd_rejected"] += 1
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
            elif not volume_ok:
                self.stats["volume_rejected"] += 1
            else:
                self.stats["sell_signals"] += 1
                return Signal.SELL

        else:
            self.stats["trend_rejected"] += 1

        self.stats["hold_signals"] += 1
        return Signal.HOLD

    # ── Read-only properties used by main.py / display ───────────────

    @property
    def last_rsi(self) -> Optional[float]:
        return self._last_rsi

    @property
    def last_trend(self) -> Optional[str]:
        return self._last_trend

    @property
    def last_adx(self) -> Optional[float]:
        return self._last_adx

    @property
    def is_warmed_up(self) -> bool:
        return len(self._closes) >= self._warmup

    @property
    def tick_count(self) -> int:
        return len(self._closes)

    @property
    def last_macd_hist(self) -> Optional[float]:
        return self._last_macd_hist
