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


def test_trailing_stop_cannot_fire_on_the_same_candle_that_activates_it():
    """2026-09-15 finding: a trailing stop activated (or raised) by THIS
    candle's own high was then checked against THIS SAME candle's low and,
    if breached, filled at THIS candle's open — a timing impossibility (the
    stop couldn't have been resting at the open; it didn't exist until the
    high, sometime later in the same candle, activated it). Reproduced
    exactly as reported: entry $100, next candle opens $100 and reaches $120
    (activating a 10% trail at $108), old code sold at the earlier $100 open
    even though the trail had no chance to exist at that price yet.

    Fixed behavior: candle 1 (activates the trail using its own high) must
    NOT exit, regardless of how low candle 1's own low goes. The trail only
    protects starting the FOLLOWING candle, once it has genuinely had a
    chance to be a resting order — candle 2 here, gap-aware fill at its open."""
    candles = [
        _candle(0, 100.0, 101.0, 99.0, 100.0),   # closes at 100 -> BUY
        _candle(1, 100.0, 120.0, 95.0, 110.0),   # activates a 10% trail at $120 -> $108,
                                                  # but its own low ($95) must NOT trigger an exit here
        _candle(2, 105.0, 106.0, 90.0, 95.0),    # NOW the $108 trail (from candle 1's peak) is
                                                  # genuinely resting — low $90 breaches it
    ]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        buy_threshold=105.0, sell_threshold=999.0,
        stop_loss_pct=0.0, take_profit_pct=0.0,
        trail_stop_pct=0.10, trail_stop_activation_pct=0.0,
    )
    assert len(result.fills) == 2, (
        f"expected exactly BUY + one trail-stop SELL, got {[(f.side, f.reason, f.price) for f in result.fills]}"
    )
    buy, sell = result.fills
    assert buy.side == "BUY"
    assert sell.side == "SELL"
    assert sell.reason == "trail_stop"
    assert sell.candle_index == 2, (
        "the trail-stop exit must happen on candle 2 (the first candle where "
        "the $108 level, set by candle 1's peak, was genuinely already "
        "resting at the open) — not candle 1, which is what activated it"
    )
    assert sell.price == 105.0, (
        f"candle 2's open ($105) is below the $108 trail level -> gap-aware "
        f"fill at the open, not the stale $108 level — got {sell.price}"
    )
