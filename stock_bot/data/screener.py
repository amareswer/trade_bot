"""
Technical pre-screener — rejects "boring" stocks before AI analysis, and
(added 2026-08-23) rejects stocks whose volatility/liquidity regime is far
outside anything the strategy was ever backtested on.

screen() returns (passed, reason). `reason` is only populated on rejection
by the new in-distribution filter below — the pre-existing "boring stock" /
min-price rejections stay reason=None (unchanged visibility; only the new
filter is meant to surface on the dashboard, see stock_bot/main.py).
"""
from __future__ import annotations

import logging
from typing import Optional

from stock_bot.data.price_feed       import Candle
from stock_bot.indicators.indicators import rsi as calc_rsi, macd as calc_macd, atr as calc_atr

logger = logging.getLogger(__name__)

# ── Thresholds (all configurable here, used nowhere else) ────────────────────
RSI_OVERSOLD    = 35.0    # RSI below this → potential buy signal
RSI_OVERBOUGHT  = 75.0    # RSI above this → potential sell signal (synced with AI hard rule)
PRICE_MOVE_PCT  = 3.0     # % upward move in latest candle vs previous to flag
MACD_LOOKBACK   = 3       # candles back to check for a cross
_MIN_PRICE      = 5.0     # no penny stocks — below this price, skip

# ── In-distribution volatility/liquidity filter (added 2026-08-23) ───────────
# RULE_WHITELIST no longer gates rule-based BUYs (see CLAUDE_HISTORY.md,
# 2026-08-23) — any symbol reaching the scan universe can now be bought on a
# rule signal alone. This filter is the replacement coarse sanity check: it
# rejects a symbol whose ATR-as-%-of-price or average dollar volume is far
# outside the range actually observed on the 4 symbols that PASSED walk-forward
# in logs/stock_backtest_20260710.md (MRNA, AMD, RY.TO, PLTR) — i.e. is it
# even roughly the same kind of instrument the strategy was validated on.
#
# That report itself has no ATR/volume columns (it's a trade-stats table), so
# these numbers were computed directly from live data (300-day daily candles,
# matching the live LOOKBACK_DAYS/INTERVAL config) on 2026-08-23:
#   MRNA   ATR%=10.26%  avg $vol=$1.54B/day
#   AMD    ATR%= 6.00%  avg $vol=$20.86B/day
#   RY.TO  ATR%= 1.73%  avg $vol=$0.99B/day
#   PLTR   ATR%= 4.28%  avg $vol=$9.41B/day
# Observed backtested range: ATR% [1.73%, 10.26%], avg $ volume [$0.99B, $20.86B].
BACKTESTED_ATR_PCT_MAX      = 10.26   # MRNA — highest ATR% among the 4 PASS symbols
ATR_PCT_REJECT_MULT         = 3.0     # reject above this many multiples of the above
                                       # (~30.8% ATR — well past MRNA, only catches
                                       # meme-stock/pre-earnings-blowup-level chaos)
MIN_AVG_DOLLAR_VOLUME       = 50_000_000   # USD/day — about 1/20th of RY.TO's ~$989M/day
                                       # (the thinnest of the 4 PASS symbols). Deliberately
                                       # generous: only rejects names genuinely illiquid
                                       # relative to anything backtested, not merely smaller
                                       # than AMD/PLTR's huge mega-cap volume.
# No floor on ATR% — low volatility isn't a risk this filter needs to catch
# (if anything it's safer than what was backtested); only excess volatility is.


class StockScreener:
    """
    Call screen(symbol, candles) → (True, None) to proceed to AI, or
    (False, reason) to skip. `reason` is a human-readable string only when
    the NEW in-distribution filter is what rejected the symbol; the
    pre-existing "boring stock" checks below still return (False, None).

    Passes if ANY of:
      - RSI < RSI_OVERSOLD or RSI > RSI_OVERBOUGHT
      - MACD histogram changed sign in the last MACD_LOOKBACK candles
      - Price moved ≥ PRICE_MOVE_PCT% in the most recent candle
    """

    def screen(self, symbol: str, candles: list[Candle]) -> tuple[bool, Optional[str]]:
        if not candles or len(candles) < 2:
            return True, None  # too little data — pass through rather than suppress

        latest_price = candles[-1].close
        if latest_price < _MIN_PRICE:
            logger.info("%s $%.2f — below min price $%.0f, skipping", symbol, latest_price, _MIN_PRICE)
            return False, None

        in_dist_ok, in_dist_reason = _check_in_distribution(symbol, candles, latest_price)
        if not in_dist_ok:
            logger.warning(in_dist_reason)
            return False, in_dist_reason

        if len(candles) < 26:
            return True, None  # new IPO — price momentum alone justifies full analysis

        closes = [c.close for c in candles]

        # RSI extremes
        rsi_val = calc_rsi(closes)
        if rsi_val is not None:
            if rsi_val < RSI_OVERSOLD or rsi_val > RSI_OVERBOUGHT:
                logger.debug("%s screener PASS — RSI=%.1f", symbol, rsi_val)
                return True, None

        # MACD cross in recent candles
        if _macd_cross_recent(closes, MACD_LOOKBACK):
            logger.debug("%s screener PASS — MACD cross detected", symbol)
            return True, None

        # Significant single-candle price move
        if closes[-2] != 0:
            pct = (closes[-1] - closes[-2]) / closes[-2] * 100
            if pct >= PRICE_MOVE_PCT:  # long-only: only upward momentum qualifies
                logger.debug("%s screener PASS — price move %.2f%%", symbol, pct)
                return True, None

        logger.debug("%s screener FAIL — no signal", symbol)
        return False, None


def _check_in_distribution(symbol: str, candles: list[Candle], latest_price: float) -> tuple[bool, Optional[str]]:
    """ATR%/liquidity sanity check against the backtested (MRNA/AMD/RY.TO/PLTR)
    range. Needs at least 15 candles for a 14-period ATR — with less, pass
    through (same "too little data, don't suppress" policy as the caller)."""
    if len(candles) < 15 or latest_price <= 0:
        return True, None

    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]
    closes = [c.close for c in candles]
    atr_val = calc_atr(highs, lows, closes, period=14)
    if atr_val is not None:
        atr_pct = atr_val / latest_price * 100
        reject_above = BACKTESTED_ATR_PCT_MAX * ATR_PCT_REJECT_MULT
        if atr_pct > reject_above:
            mult = atr_pct / BACKTESTED_ATR_PCT_MAX
            return False, (
                f"SCREEN_SKIP: {symbol} ATR {atr_pct:.1f}% is {mult:.1f}x the backtested "
                f"max ({BACKTESTED_ATR_PCT_MAX:.1f}%, MRNA) — outside the validated volatility regime"
            )

    avg_dollar_vol = sum(c.volume * c.close for c in candles) / len(candles)
    if avg_dollar_vol < MIN_AVG_DOLLAR_VOLUME:
        return False, (
            f"SCREEN_SKIP: {symbol} avg volume ${avg_dollar_vol:,.0f}/day is below the "
            f"${MIN_AVG_DOLLAR_VOLUME:,.0f}/day liquidity floor — outside the validated regime"
        )

    return True, None


def _macd_cross_recent(closes: list[float], lookback: int) -> bool:
    """Return True if the MACD histogram changed sign within the last `lookback` candles."""
    if len(closes) < 36:  # minimum needed: 26 (slow EMA) + 9 (signal) + 1
        return False
    signs: list[bool] = []
    for offset in range(lookback + 1):
        end = len(closes) - offset if offset > 0 else len(closes)
        result = calc_macd(closes[:end])
        if result is None:
            return False
        signs.append(result[2] > 0)  # True when histogram is positive
    return len(set(signs)) > 1  # True when sign changed at least once
