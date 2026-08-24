"""
In-distribution ATR%/liquidity filter (stock_bot/data/screener.py, added
2026-08-23) — the replacement safety net after RULE_WHITELIST stopped gating
rule-based BUYs. Rejects a symbol whose volatility or liquidity is far
outside the range observed on the 4 backtested-PASS symbols (MRNA/AMD/
RY.TO/PLTR).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from stock_bot.data.price_feed import Candle
from stock_bot.data.screener import (
    StockScreener,
    BACKTESTED_ATR_PCT_MAX,
    ATR_PCT_REJECT_MULT,
    MIN_AVG_DOLLAR_VOLUME,
)


def _make_candles(n, price=100.0, daily_range=1.0, volume=10_000_000.0):
    """n synthetic daily candles, flat price, constant range/volume."""
    base = datetime(2026, 1, 1)
    candles = []
    for i in range(n):
        candles.append(Candle(
            timestamp = base + timedelta(days=i),
            open      = price,
            high      = price + daily_range / 2,
            low       = price - daily_range / 2,
            close     = price,
            volume    = volume,
        ))
    return candles


def test_normal_volatility_and_liquidity_passes():
    # ATR% ~1%, avg $vol ~$1B/day — well inside the backtested range
    candles = _make_candles(30, price=100.0, daily_range=1.0, volume=10_000_000.0)
    passed, reason = StockScreener().screen("TEST", candles)
    assert reason is None
    # (passed may still be True/False depending on the RSI/MACD/move checks —
    # what matters here is the in-distribution gate did NOT reject it)


def test_extreme_atr_rejected_with_reason():
    # Daily range wildly larger than price → ATR% far above the 30.8% cutoff
    candles = _make_candles(30, price=50.0, daily_range=40.0, volume=10_000_000.0)
    passed, reason = StockScreener().screen("WILD", candles)
    assert passed is False
    assert reason is not None
    assert "SCREEN_SKIP" in reason
    assert "ATR" in reason
    assert "WILD" in reason


def test_illiquid_symbol_rejected_with_reason():
    # Tiny volume → avg $ volume far below the $50M/day floor
    candles = _make_candles(30, price=10.0, daily_range=0.1, volume=1_000.0)
    passed, reason = StockScreener().screen("THIN", candles)
    assert passed is False
    assert reason is not None
    assert "SCREEN_SKIP" in reason
    assert "volume" in reason.lower()
    assert "THIN" in reason


def test_insufficient_candles_passes_through():
    # Fewer than 15 candles — not enough for a 14-period ATR, don't suppress
    candles = _make_candles(10, price=50.0, daily_range=40.0, volume=1_000.0)
    passed, reason = StockScreener().screen("NEWIPO", candles)
    assert reason is None  # in-distribution filter did not fire


def test_backtested_symbols_actually_pass_the_filter():
    """Sanity check the thresholds against the real observed range (2026-08-23
    probe) so the 4 originally-backtested symbols themselves are never
    rejected by their own reference filter."""
    observed = {
        "MRNA":  (10.26, 1_538_005_261),
        "AMD":   (6.00,  20_857_514_911),
        "RY.TO": (1.73,  989_340_975),
        "PLTR":  (4.28,  9_410_961_874),
    }
    reject_above_atr = BACKTESTED_ATR_PCT_MAX * ATR_PCT_REJECT_MULT
    for sym, (atr_pct, avg_dollar_vol) in observed.items():
        assert atr_pct <= reject_above_atr, f"{sym} ATR% would be rejected by its own reference range"
        assert avg_dollar_vol >= MIN_AVG_DOLLAR_VOLUME, f"{sym} volume would be rejected by its own reference range"
