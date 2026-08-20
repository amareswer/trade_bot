"""
Trend-following indicator strategy with dual entry modes.

Regime classification runs first on every candle:
  VOLATILE  — ATR > 1.5× 20-period ATR average      → sit flat (HOLD)
  TRENDING  — ADX ≥ adx_threshold                   → trend-following (EMA/RSI/ADX)
  RANGING   — ADX < adx_threshold, not volatile      → HOLD (no signal)

BUY signals come from two modes (either can fire):
  Mode A (pullback)    — RSI in [PULLBACK_RSI_MIN, PULLBACK_RSI_MAX], MACD hist rising
  Mode B (breakout)    — RSI in [BREAKOUT_RSI_MIN, BREAKOUT_RSI_MAX], ADX ≥ breakout_adx_threshold,
                         MACD hist > 0, price within MAX_PRICE_EXTENSION_PCT of BREAKOUT_LOOKBACK high

Both modes require: trend BULLISH, ADX ≥ adx_threshold, EMA spread ≥ min_ema_spread_pct.

SELL signals are unchanged from the original trend logic.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
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
    atr_volatile_multiplier:  float = 1.5    # ATR > multiplier × avg ATR → VOLATILE
    # ── Entry mode parameters ─────────────────────────────────────────
    pullback_rsi_min:         float = 38.0   # Mode A: RSI lower bound
    pullback_rsi_max:         float = 58.0   # Mode A: RSI upper bound
    breakout_rsi_min:         float = 50.0   # Mode B: RSI lower bound
    breakout_rsi_max:         float = 72.0   # Mode B: RSI upper bound
    breakout_lookback:        int   = 20     # Mode B: N-candle high for breakout check
    max_price_extension_pct:  float = 0.03   # Mode B: max % above N-candle high (anti-chase)
    breakout_adx_threshold:   float = 22.0   # Mode B: stricter ADX requirement


class IndicatorStrategy:
    """
    Dual-regime indicator strategy with Mode A (pullback) and Mode B (breakout).
    Interface: evaluate(candle: Candle) -> Signal
    """

    def __init__(self, config: Optional[IndicatorConfig] = None):
        self.config  = config or IndicatorConfig()
        self._warmup = max(
            self.config.rsi_period + 1,
            self.config.slow_ema_period,
            2 * self.config.adx_period + 1,
            self.config.regime_ema_period if self.config.regime_ema_period > 0 else 0,
        ) + 2

        buf = max(self._warmup + 50, self.config.regime_ema_period + 16)
        self._closes:     deque = deque(maxlen=buf)
        self._highs:      deque = deque(maxlen=buf)
        self._lows:       deque = deque(maxlen=buf)
        self._volumes:    deque = deque(maxlen=buf)
        self._atr_history: deque = deque(maxlen=20)   # rolling ATR for volatile detection

        self._last_rsi:          Optional[float]  = None
        self._last_trend:        Optional[str]    = None
        self._last_adx:          Optional[float]  = None
        self._last_atr:          Optional[float]  = None
        self._last_regime:       Optional[Regime] = None
        self._prev_rsi:          Optional[float]  = None
        self._last_macd_hist:    Optional[float]  = None
        self._last_buy_block_gate: Optional[str]  = None  # first gate blocking BUY this candle
        self._last_entry_mode:   Optional[str]    = None  # "A", "B", or None

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
            "buy_signals":     0,
            "sell_signals":    0,
            "hold_signals":    0,
            "mode_a_signals":  0,
            "mode_b_signals":  0,
        }

        logger.info(
            "IndicatorStrategy | RSI(%d) ob=%.0f os=%.0f"
            " | EMA(%d/%d) | ADX(%d) thr=%.0f break_adx=%.0f"
            " | volatile_mult=%.1f | warmup=%d"
            " | ModeA RSI[%.0f-%.0f] | ModeB RSI[%.0f-%.0f] lookback=%d ext=%.0f%%",
            self.config.rsi_period,
            self.config.rsi_overbought, self.config.rsi_oversold,
            self.config.fast_ema_period, self.config.slow_ema_period,
            self.config.adx_period, self.config.adx_threshold,
            self.config.breakout_adx_threshold,
            self.config.atr_volatile_multiplier,
            self._warmup,
            self.config.pullback_rsi_min, self.config.pullback_rsi_max,
            self.config.breakout_rsi_min, self.config.breakout_rsi_max,
            self.config.breakout_lookback,
            self.config.max_price_extension_pct * 100,
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
        self._last_buy_block_gate = None
        self._last_entry_mode     = None

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
        self._last_atr = atr_val

        # ── Regime classification ─────────────────────────────────────
        # _classify_regime() compares atr_val against the mean of
        # self._atr_history — that must be the mean of PRIOR candles only.
        # Do NOT append atr_val to _atr_history before this call: appending
        # first would let the current bar's own value pull up the baseline
        # it's being tested against (self-referential bias, found in a
        # 2026-08-19 deep-verification pass — not a lookahead bug, since
        # nothing future is used, but it made a genuine volatility spike
        # slightly harder to detect than comparing against the strictly-
        # prior history, worth ~1/len(history) of the spike itself). The
        # append below happens strictly after classification, so atr_val
        # only becomes part of the baseline for FUTURE candles' comparisons
        # — the normal rolling-average-of-history shape.
        regime = self._classify_regime(adx_val, atr_val)
        self._last_regime = regime
        if atr_val is not None:
            self._atr_history.append(atr_val)

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

        # ── Volatile regime: sit flat ─────────────────────────────────
        if regime == Regime.VOLATILE:
            self.stats["volatile_skipped"] += 1
            self.stats["hold_signals"] += 1
            if trend_val == "BULLISH":
                self._last_buy_block_gate = "regime"
            return Signal.HOLD

        # RANGING → _trend_signal rejects via adx_rejected (ADX < threshold)
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

    def _is_near_breakout(self, closes: list[float], price: float) -> bool:
        """True when price is at or within MAX_PRICE_EXTENSION_PCT above the prior
        BREAKOUT_LOOKBACK-candle high (confirming breakout without chasing)."""
        lb = self.config.breakout_lookback
        if len(closes) < lb + 1:
            return False
        prior_high = max(closes[-(lb + 1):-1])   # exclude current candle
        max_ext    = self.config.max_price_extension_pct
        return prior_high <= price <= prior_high * (1.0 + max_ext)

    def _compute_buy_block_gate(
        self,
        rsi_val:          float,
        adx_val:          Optional[float],
        ema_spread_pct:   float,
        macd_hist:        Optional[float],
        macd_hist_rising: bool,
        closes:           list[float],
        price:            float,
        rsi_rising:       bool = False,
    ) -> str:
        """Return the first gate (in priority order) that blocks a BUY.
        Priority: RSI → RSI_DIRECTION → ADX → EMA_spread → MACD
        Returns empty string if BUY should fire (should not happen when called from HOLD path)."""
        cfg = self.config

        # RSI range: blocks if NEITHER mode's range is satisfied
        rsi_a_range = not cfg.rsi_filter_enabled or cfg.pullback_rsi_min <= rsi_val <= cfg.pullback_rsi_max
        rsi_b_ok    = not cfg.rsi_filter_enabled or cfg.breakout_rsi_min <= rsi_val <= cfg.breakout_rsi_max
        if not rsi_a_range and not rsi_b_ok:
            return "RSI"

        # RSI direction: Mode A requires rsi_rising; if RSI is in Mode A range but not
        # rising, and Mode B range is also not satisfied, direction is the blocker.
        if rsi_a_range and not rsi_rising and not rsi_b_ok:
            return "RSI_DIRECTION"

        # ADX: below Mode A threshold means both modes are blocked at ADX level
        if adx_val is None or adx_val < cfg.adx_threshold:
            return "ADX"

        # EMA_spread: both modes require this
        if ema_spread_pct < cfg.min_ema_spread_pct:
            return "EMA_spread"

        # MACD: check if both modes are blocked by MACD conditions
        macd_a_ok = not cfg.macd_enabled or macd_hist_rising
        macd_b_ok = not cfg.macd_enabled or (macd_hist is not None and macd_hist > 0)

        mode_a_fires    = rsi_a_range and rsi_rising and macd_a_ok
        mode_b_breakout = adx_val >= cfg.breakout_adx_threshold and self._is_near_breakout(closes, price)
        mode_b_fires    = rsi_b_ok and mode_b_breakout and macd_b_ok

        if not mode_a_fires and not mode_b_fires:
            return "MACD"   # RSI/ADX/EMA passed → MACD or breakout is the residual blocker

        return ""  # should not reach here when called from a HOLD outcome

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
        """
        Trend-following logic with Mode A (pullback) and Mode B (breakout).
        SELL logic is unchanged from the original single-mode implementation.
        """
        # ADX threshold (always checked; RANGING → called here, adx_val < threshold fires)
        if adx_val is None or adx_val < self.config.adx_threshold:
            self.stats["adx_rejected"] += 1
            if trend_val == "BULLISH":
                self._last_buy_block_gate = "ADX"
            return Signal.HOLD
        if self.config.adx_max > 0 and adx_val > self.config.adx_max:
            self.stats["adx_rejected"] += 1
            return Signal.HOLD

        # Regime EMA (macro bull/bear filter)
        if self.config.regime_ema_period > 0:
            regime_ema = calc_ema(closes, self.config.regime_ema_period)
            if regime_ema is not None and price < regime_ema and trend_val == "BULLISH":
                self.stats["regime_rejected"] += 1
                self._last_buy_block_gate = "regime"
                return Signal.HOLD
            if (self.config.regime_ema_slope_filter
                    and regime_ema is not None
                    and trend_val == "BULLISH"
                    and len(closes) >= self.config.regime_ema_period + 6):
                regime_ema_prev = calc_ema(closes[:-6], self.config.regime_ema_period)
                if regime_ema_prev is not None and regime_ema < regime_ema_prev:
                    self.stats["regime_rejected"] += 1
                    self._last_buy_block_gate = "regime"
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
                self._last_buy_block_gate = "EMA_spread"
            elif not volume_ok:
                self.stats["volume_rejected"] += 1
                self._last_buy_block_gate = "EMA_spread"  # volume treated as EMA_spread tier
            else:
                # ── Mode A: Pullback entry ────────────────────────────
                # rsi_rising confirms the dip is recovering (not still in descent)
                rsi_a_ok   = (not self.config.rsi_filter_enabled or
                              (self.config.pullback_rsi_min <= rsi_val <= self.config.pullback_rsi_max
                               and rsi_rising))
                macd_a_ok  = not self.config.macd_enabled or macd_hist_rising
                mode_a_ok  = rsi_a_ok and macd_a_ok

                # ── Mode B: Breakout continuation ─────────────────────
                adx_b_ok      = adx_val >= self.config.breakout_adx_threshold
                rsi_b_ok      = (not self.config.rsi_filter_enabled or
                                 self.config.breakout_rsi_min <= rsi_val <= self.config.breakout_rsi_max)
                macd_b_ok     = not self.config.macd_enabled or (macd_hist is not None and macd_hist > 0)
                breakout_ok   = self._is_near_breakout(closes, price)
                mode_b_ok     = adx_b_ok and rsi_b_ok and macd_b_ok and breakout_ok

                if mode_a_ok:
                    self.stats["buy_signals"]    += 1
                    self.stats["mode_a_signals"] += 1
                    self._last_entry_mode = "A"
                    return Signal.BUY
                elif mode_b_ok:
                    self.stats["buy_signals"]    += 1
                    self.stats["mode_b_signals"] += 1
                    self._last_entry_mode = "B"
                    return Signal.BUY
                else:
                    # Compute first blocking gate in priority order for diagnostics
                    self._last_buy_block_gate = self._compute_buy_block_gate(
                        rsi_val, adx_val, ema_spread_pct,
                        macd_hist, macd_hist_rising, closes, price,
                        rsi_rising=rsi_rising,
                    ) or "MACD"
                    if not rsi_a_ok and not rsi_b_ok:
                        self.stats["rsi_rejected"] += 1
                    else:
                        self.stats["macd_rejected"] += 1

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
            self._last_buy_block_gate = "trend"

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
    def last_buy_block_gate(self) -> Optional[str]:
        return self._last_buy_block_gate

    @property
    def last_entry_mode(self) -> Optional[str]:
        return self._last_entry_mode

    @property
    def is_warmed_up(self) -> bool:
        return len(self._closes) >= self._warmup

    @property
    def tick_count(self) -> int:
        return len(self._closes)

    @property
    def last_macd_hist(self) -> Optional[float]:
        return self._last_macd_hist
