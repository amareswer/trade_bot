"""
Regression tests for bot/backtest/engine.py's execution model — 2026-09-14 review
findings. There was zero prior direct unit test coverage of engine.run() itself
(only metrics.compute() and the engine_kwargs_from_cfg() config-builder wiring
were tested; the actual candle-by-candle simulation loop was only ever
exercised indirectly through full runs against real market data).

Both tests use strategy_mode="threshold" (ThresholdStrategy: BUY below
buy_threshold, SELL above sell_threshold, else HOLD) since it needs no
indicator warmup — a handful of synthetic candles is enough to force a
deterministic BUY, then a deterministic stop-loss trigger, on the very next
candle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.backtest.engine import run
from bot.data.historical_feed import Candle


def _candle(ts_offset_hours: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=ts_offset_hours),
        open=o, high=h, low=l, close=c, volume=1000.0,
    )


def test_threshold_mode_does_not_crash_on_its_first_buy():
    """2026-09-14 finding: the BUY-fill snapshot block accessed
    strategy.last_atr / strategy._closes / strategy.config.*_ema_period
    unconditionally — all indicator-only attributes that don't exist on
    ThresholdStrategy (a bare buy_threshold/sell_threshold dataclass) —
    crashing with an AttributeError on strategy_mode="threshold"'s very
    first BUY fill."""
    candles = [
        _candle(0, 100.0, 101.0, 99.0, 100.0),   # closes at 100 -> BUY (< buy_threshold)
        _candle(1, 101.0, 102.0, 100.5, 101.0),  # closes at 101 -> HOLD (between thresholds)
    ]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        buy_threshold=105.0, sell_threshold=999.0,
        stop_loss_pct=0.02, take_profit_pct=0.10,
    )
    assert len(result.fills) == 1
    assert result.fills[0].side == "BUY"


def test_gap_through_stop_loss_fills_at_the_open_not_the_stale_level():
    """2026-09-14 finding: a candle whose OPEN already gapped past the
    theoretical stop level made that level itself untradable, but the engine
    filled at the stale level anyway — reproduced with the reviewer's exact
    shape: BUY at $100 with a 2% ($98) stop, next candle trading entirely
    between $75 and $85 (never touching $98). The realistic fill is the
    candle's open ($80), not $98 — mirrors the already-tested gap-handling
    pattern in stock_bot/backtest/engine.py (`min(c.open, sl_price)`)."""
    candles = [
        _candle(0, 100.0, 101.0, 99.0, 100.0),    # closes at 100 -> BUY
        _candle(1, 80.0, 85.0, 75.0, 78.0),       # gapped down, never traded near $98
    ]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        buy_threshold=105.0, sell_threshold=999.0,
        stop_loss_pct=0.02, take_profit_pct=0.0,
    )
    assert len(result.fills) == 2
    sl_fill = result.fills[1]
    assert sl_fill.side == "SELL"
    assert sl_fill.reason == "stop_loss"
    assert sl_fill.price == 80.0, (
        f"stop should fill at the gapped-through candle's open ($80), not the "
        f"stale $98 stop level the candle never actually traded at — got {sl_fill.price}"
    )
    assert sl_fill.price < 98.0, "the old bug filled exactly at the theoretical $98 level"
