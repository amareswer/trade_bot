"""
Technical pre-screener — rejects "boring" stocks before AI analysis.

Returns True (pass) if the symbol shows ANY of the configured signals.
Returns False (reject) if none apply → skip research + AI for that symbol.
"""
from __future__ import annotations

import logging

from stock_bot.data.price_feed       import Candle
from stock_bot.indicators.indicators import rsi as calc_rsi, macd as calc_macd

logger = logging.getLogger(__name__)

# ── Thresholds (all configurable here, used nowhere else) ────────────────────
RSI_OVERSOLD    = 35.0    # RSI below this → potential buy signal
RSI_OVERBOUGHT  = 75.0    # RSI above this → potential sell signal (synced with AI hard rule)
PRICE_MOVE_PCT  = 3.0     # % upward move in latest candle vs previous to flag
MACD_LOOKBACK   = 3       # candles back to check for a cross
_MIN_PRICE      = 5.0     # no penny stocks — below this price, skip


class StockScreener:
    """
    Call screen(symbol, candles) → True to proceed to AI, False to skip.

    Passes if ANY of:
      - RSI < RSI_OVERSOLD or RSI > RSI_OVERBOUGHT
      - MACD histogram changed sign in the last MACD_LOOKBACK candles
      - Price moved ≥ PRICE_MOVE_PCT% in the most recent candle
    """

    def screen(self, symbol: str, candles: list[Candle]) -> bool:
        if not candles or len(candles) < 2:
            return True  # too little data — pass through rather than suppress

        latest_price = candles[-1].close
        if latest_price < _MIN_PRICE:
            logger.info("%s $%.2f — below min price $%.0f, skipping", symbol, latest_price, _MIN_PRICE)
            return False

        if len(candles) < 26:
            return True  # new IPO — price momentum alone justifies full analysis

        closes = [c.close for c in candles]

        # RSI extremes
        rsi_val = calc_rsi(closes)
        if rsi_val is not None:
            if rsi_val < RSI_OVERSOLD or rsi_val > RSI_OVERBOUGHT:
                logger.debug("%s screener PASS — RSI=%.1f", symbol, rsi_val)
                return True

        # MACD cross in recent candles
        if _macd_cross_recent(closes, MACD_LOOKBACK):
            logger.debug("%s screener PASS — MACD cross detected", symbol)
            return True

        # Significant single-candle price move
        if closes[-2] != 0:
            pct = (closes[-1] - closes[-2]) / closes[-2] * 100
            if pct >= PRICE_MOVE_PCT:  # long-only: only upward momentum qualifies
                logger.debug("%s screener PASS — price move %.2f%%", symbol, pct)
                return True

        logger.debug("%s screener FAIL — no signal", symbol)
        return False


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
