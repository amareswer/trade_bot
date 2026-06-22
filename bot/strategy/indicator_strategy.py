"""
Dual-regime indicator strategy.

Regime classification runs first on every candle:
  VOLATILE  — ATR > 1.5× 20-period ATR average      → sit flat (HOLD)
  TRENDING  — ADX ≥ adx_threshold                   → trend-following (EMA/RSI/ADX)
  RANGING   — ADX < adx_threshold, not volatile      → mean-reversion (BB + RSI)

When regime_enabled=False the strategy falls back to the original behaviour:
ADX < threshold = HOLD (no mean-reversion module fires).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from bot.strategy.threshold_strategy import Signal
from bot.indicators.indicators import (
    rsi  as calc_rsi,
    trend as calc_trend,
    ema   as calc_ema,
    adx   as calc_adx,
    atr   as calc_atr,
    macd  as calc_macd,
    bollinger_bands,
)
from bot.data.historical_feed import Candle

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    TRENDING = "TRENDING"
    RANGING  = "RANGING"
    VOLATILE = "VOLATILE"


@dataclass
class IndicatorConfig:
    # ── Core trend indicators ─────────────────────────────────────────
    rsi_period:               int   = 14
    rsi_overbought:           float = 65.0
    rsi_oversold:             float = 35.0
    rsi_buy_min:              float = 45.0
    rsi_sell_max:             float = 55.0
    fast_ema_period:          int   = 9
    slow_ema_period:          int   = 21
    min_ema_spread_pct:       float = 0.002
    max_ema_spread_pct:       float = 0.0
    adx_period:               int   = 14
    adx_threshold:            float = 25.0
    adx_max:                  float = 0.0
    volume_k:                 float = 1.2
    macd_enabled:             bool  = True
    macd_fast_period:         int   = 12
    macd_slow_period:         int   = 26
    macd_signal_period:       int   = 9
    rsi_filter_enabled:       bool  = True
    regime_ema_period:        int   = 200
    regime_ema_slope_filter:  bool  = False
    # ── Dual-regime additions ─────────────────────────────────────────
    regime_enabled:           bool  = True   # False = original single-strategy behaviour
    bb_period:                int   = 20     # Bollinger Band period
    bb_std_dev:               float = 2.0    # Bollinger Band standard deviation multiplier
    mr_rsi_oversold:          float = 35.0   # mean-reversion BUY: RSI must be below this
    mr_rsi_overbought:        float = 65.0   # mean-reversion SELL: RSI must be above this
    atr_volatile_multiplier:  float = 1.5    # ATR > multiplier × avg ATR → VOLATILE


class IndicatorStrategy:
    """
    Dual-regime indicator strategy.
    Interface: evaluate(candle: Candle) -> Signal
    """

    def __init__(self, config: Optional[IndicatorConfig] = None):
        self.config  = config or IndicatorConfig()
        self._warmup = max(
            self.config.rsi_period + 1,
            self.config.slow_ema_period,
            2 * self.config.adx_period + 1,
            self.config.regime_ema_period if self.config.regime_ema_period > 0 else 0,
            self.config.bb_period,
        ) + 2

        buf = max(self._warmup + 50, self.config.regime_ema_period + 16)
        self._closes:     deque = deque(maxlen=buf)
        self._highs:      deque = deque(maxlen=buf)
        self._lows:       deque = deque(maxlen=buf)
        self._volumes:    deque = deque(maxlen=buf)
        self._atr_history: deque = deque(maxlen=20)   # rolling ATR for volatile detection

        self._last_rsi:       Optional[float]  = None
        self._last_trend:     Optional[str]    = None
        self._last_adx:       Optional[float]  = None
        self._last_atr:       Optional[float]  = None
        self._last_regime:    Optional[Regime] = None
        self._last_bb_upper:  Optional[float]  = None
        self._last_bb_middle: Optional[float]  = None
        self._last_bb_lower:  Optional[float]  = None
        self._prev_rsi:       Optional[float]  = None
        self._last_macd_hist: Optional[float]  = None

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
            "volatile_skipped": 0,
            "ranging_buy":     0,
            "ranging_sell":    0,
            "buy_signals":     0,
            "sell_signals":    0,
            "hold_signals":    0,
        }

        logger.info(
            "IndicatorStrategy (dual-regime) | RSI(%d) ob=%.0f os=%.0f"
            " | EMA(%d/%d) | ADX(%d) thr=%.0f"
            " | BB(%d,%.1f) mr_os=%.0f mr_ob=%.0f"
            " | regime=%s volatile_mult=%.1f | warmup=%d",
            self.config.rsi_period,
            self.config.rsi_overbought, self.config.rsi_oversold,
            self.config.fast_ema_period, self.config.slow_ema_period,
            self.config.adx_period, self.config.adx_threshold,
            self.config.bb_period, self.config.bb_std_dev,
            self.config.mr_rsi_oversold, self.config.mr_rsi_overbought,
            "ON" if self.config.regime_enabled else "OFF",
            self.config.atr_volatile_multiplier,
            self._warmup,
        )

    # ── Public interface ──────────────────────────────────────────────

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

        # ── Core indicators ───────────────────────────────────────────
        rsi_val   = calc_rsi(closes, self.config.rsi_period)
        trend_val = calc_trend(closes, self.config.fast_ema_period, self.config.slow_ema_period)
        fast_ema  = calc_ema(closes, self.config.fast_ema_period)
        slow_ema  = calc_ema(closes, self.config.slow_ema_period)

        if rsi_val is None or fast_ema is None or slow_ema is None:
            self.stats["warmup_rejected"] += 1
            return Signal.HOLD

        # ── ADX ───────────────────────────────────────────────────────
        adx_val: Optional[float] = None
        highs  = list(self._highs)
        lows   = list(self._lows)
        if len(highs) >= 2 * self.config.adx_period + 1:
            adx_val = calc_adx(highs, lows, closes, self.config.adx_period)
        self._last_adx = adx_val

        # ── ATR (volatile detection) ──────────────────────────────────
        atr_val: Optional[float] = None
        if len(highs) >= 28:
            atr_val = calc_atr(highs, lows, closes, 14)
        if atr_val is not None:
            self._atr_history.append(atr_val)
        self._last_atr = atr_val

        # ── Bollinger Bands (ranging detection + mean-reversion) ──────
        bb_result = bollinger_bands(closes, self.config.bb_period, self.config.bb_std_dev)
        if bb_result is not None:
            self._last_bb_upper, self._last_bb_middle, self._last_bb_lower = bb_result

        # ── Regime classification ─────────────────────────────────────
        regime = self._classify_regime(adx_val, atr_val)
        self._last_regime = regime

        # ── RSI direction ─────────────────────────────────────────────
        rsi_rising  = self._last_rsi is not None and rsi_val > self._last_rsi
        rsi_falling = self._last_rsi is not None and rsi_val < self._last_rsi
        self._prev_rsi = self._last_rsi
        self._last_rsi = rsi_val
        self._last_trend = trend_val

        # ── EMA separation ────────────────────────────────────────────
        ema_spread_pct = abs(fast_ema - slow_ema) / slow_ema if slow_ema > 0 else 0.0

        logger.info(
            "price=%.2f RSI=%.1f(%s) trend=%s EMA_spread=%.3f%% ADX=%.1f regime=%s",
            price, rsi_val,
            "↑" if rsi_rising else ("↓" if rsi_falling else "→"),
            trend_val, ema_spread_pct * 100, adx_val or 0.0,
            regime.value,
        )

        # ── Route by regime ───────────────────────────────────────────
        if regime == Regime.VOLATILE:
            self.stats["volatile_skipped"] += 1
            self.stats["hold_signals"] += 1
            return Signal.HOLD

        if self.config.regime_enabled and regime == Regime.RANGING:
            return self._ranging_signal(price, rsi_val, bb_result)

        # TRENDING (or regime_enabled=False) → existing trend logic ───
        return self._trend_signal(
            price, rsi_val, rsi_rising, rsi_falling,
            trend_val, fast_ema, slow_ema, ema_spread_pct, closes, adx_val,
        )

    # ── Private helpers ───────────────────────────────────────────────

    def _classify_regime(self, adx_val: Optional[float], atr_val: Optional[float]) -> Regime:
        # Volatile: current ATR > multiplier × rolling average ATR
        if (atr_val is not None
                and len(self._atr_history) >= 10
                and atr_val > self.config.atr_volatile_multiplier
                       * (sum(self._atr_history) / len(self._atr_history))):
            return Regime.VOLATILE

        if adx_val is None or adx_val < self.config.adx_threshold:
            return Regime.RANGING

        if self.config.adx_max > 0 and adx_val > self.config.adx_max:
            return Regime.VOLATILE

        return Regime.TRENDING

    def _ranging_signal(
        self,
        price:     float,
        rsi_val:   float,
        bb_result: tuple | None,
    ) -> Signal:
        """
        Mean-reversion BUY only.
        Exits are handled by SL/TP or the trend SELL module — never by this path.
        Reason: ranging SELL fires on profitable trending positions (upper-BB touch
        during a rising trend), cutting winners short and lowering PF.
        """
        if bb_result is None or rsi_val is None:
            self.stats["hold_signals"] += 1
            return Signal.HOLD

        _upper, _middle, lower = bb_result

        # BUY: price touches lower band + RSI oversold
        if price <= lower * 1.001 and rsi_val < self.config.mr_rsi_oversold:
            self.stats["ranging_buy"] += 1
            self.stats["buy_signals"] += 1
            return Signal.BUY

        self.stats["hold_signals"] += 1
        return Signal.HOLD

    def _trend_signal(
        self,
        price:         float,
        rsi_val:       float,
        rsi_rising:    bool,
        rsi_falling:   bool,
        trend_val:     str,
        fast_ema:      float,
        slow_ema:      float,
        ema_spread_pct: float,
        closes:        list[float],
        adx_val:       Optional[float],
    ) -> Signal:
        """Original trend-following logic — EMA/RSI/ADX/regime-EMA/volume/MACD."""
        # ADX threshold (always checked here; when regime_enabled=True this is only
        # reached for TRENDING candles, so adx_val >= threshold is already guaranteed)
        if adx_val is None or adx_val < self.config.adx_threshold:
            self.stats["adx_rejected"] += 1
            return Signal.HOLD
        if self.config.adx_max > 0 and adx_val > self.config.adx_max:
            self.stats["adx_rejected"] += 1
            return Signal.HOLD

        # Regime EMA (macro bull/bear filter)
        if self.config.regime_ema_period > 0:
            regime_ema = calc_ema(closes, self.config.regime_ema_period)
            if regime_ema is not None and price < regime_ema and trend_val == "BULLISH":
                self.stats["regime_rejected"] += 1
                return Signal.HOLD
            if (self.config.regime_ema_slope_filter
                    and regime_ema is not None
                    and trend_val == "BULLISH"
                    and len(closes) >= self.config.regime_ema_period + 6):
                regime_ema_prev = calc_ema(closes[:-6], self.config.regime_ema_period)
                if regime_ema_prev is not None and regime_ema < regime_ema_prev:
                    self.stats["regime_rejected"] += 1
                    return Signal.HOLD

        ema_above_min = ema_spread_pct >= self.config.min_ema_spread_pct
        ema_below_max = (self.config.max_ema_spread_pct <= 0 or
                         ema_spread_pct <= self.config.max_ema_spread_pct)
        ema_strong    = ema_above_min and ema_below_max

        # Volume confirmation
        vols = list(self._volumes)
        volume_ok = True
        if self.config.volume_k > 0 and len(vols) >= 4:
            avg_vol_3 = sum(vols[-4:-1]) / 3
            volume_ok = vols[-1] >= self.config.volume_k * avg_vol_3

        # MACD momentum
        macd_val = calc_macd(
            closes,
            self.config.macd_fast_period,
            self.config.macd_slow_period,
            self.config.macd_signal_period,
        )
        macd_hist = macd_val[2] if macd_val is not None else None
        macd_hist_rising = (
            macd_hist is not None
            and self._last_macd_hist is not None
            and macd_hist > self._last_macd_hist
        )
        self._last_macd_hist = macd_hist

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

    # ── Read-only properties ──────────────────────────────────────────

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
    def last_atr(self) -> Optional[float]:
        return self._last_atr

    @property
    def last_regime(self) -> Optional[str]:
        return self._last_regime.value if self._last_regime else None

    @property
    def last_bb_upper(self) -> Optional[float]:
        return self._last_bb_upper

    @property
    def last_bb_lower(self) -> Optional[float]:
        return self._last_bb_lower

    @property
    def is_warmed_up(self) -> bool:
        return len(self._closes) >= self._warmup

    @property
    def tick_count(self) -> int:
        return len(self._closes)

    @property
    def last_macd_hist(self) -> Optional[float]:
        return self._last_macd_hist
