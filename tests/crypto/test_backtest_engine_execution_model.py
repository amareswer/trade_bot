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

import pytest

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
        fill_model="close",   # premise: entered at the $100 signal close (next_open would enter at $80)
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


def test_partial_tp_deferred_when_same_candle_also_stops_out():
    """2026-09-18 review finding: a single candle whose high touches the
    partial-TP level AND whose low touches the stop-loss level used to bank
    the partial profit first, then stop out the remainder — OHLC data alone
    cannot establish that the TP genuinely happened before the SL within
    that candle. Conservative fix: assume the SL happened first and skip
    the partial TP entirely, closing the FULL (undiminished) position at
    the stop level instead.

    Entry $100, partial TP at +5% ($105), stop-loss at -2% ($98). The next
    candle's high ($106) clears the TP level and its low ($95) clears the
    SL level in the same bar."""
    candles = [
        _candle(0, 100.0, 101.0, 99.0, 100.0),   # closes at 100 -> BUY
        _candle(1, 100.0, 106.0, 95.0, 100.0),   # both partial-TP ($105) and SL ($98) touched
    ]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        buy_threshold=105.0, sell_threshold=999.0,
        stop_loss_pct=0.02, take_profit_pct=0.0,
        partial_tp_pct=0.05, partial_tp_size=0.5,
    )
    assert len(result.fills) == 2, (
        f"expected exactly BUY + one full stop-loss SELL (no partial_tp "
        f"fill), got {[(f.side, f.reason, f.price, f.quantity) for f in result.fills]}"
    )
    buy, sell = result.fills
    assert buy.side == "BUY"
    assert sell.side == "SELL"
    assert sell.reason == "stop_loss"
    assert sell.quantity == buy.quantity, (
        "the FULL position must close at the stop — a partial_tp fill "
        "before it would have left only half this quantity"
    )
    assert sell.price == 98.0   # theoretical level — candle's open (100) didn't gap through it


# ── fill_model="next_open" (2026-09-26 review finding) ─────────────────────

def test_next_open_fills_strategy_buy_at_the_following_candles_open():
    candles = [
        _candle(0, 100.0, 101.0, 99.0, 100.0),    # closes at 100 -> BUY decided here
        _candle(1, 100.5, 102.0, 100.0, 101.0),   # ...and filled at THIS open
    ]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        buy_threshold=100.5, sell_threshold=999.0,
        stop_loss_pct=0.0, take_profit_pct=0.0, fill_model="next_open",
    )
    assert len(result.fills) == 1
    assert result.fills[0].candle_index == 1
    assert result.fills[0].price == 100.5


def test_next_open_signal_on_the_last_candle_is_never_filled():
    candles = [_candle(0, 101.0, 101.0, 99.0, 100.0)]     # BUY on the final candle
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        buy_threshold=105.0, sell_threshold=999.0, fill_model="next_open",
    )
    assert result.fills == [], "no later candle exists to trade at"


def test_next_open_entry_can_be_stopped_out_on_its_own_entry_candle():
    """The position exists from the open, so that same candle's low can hit
    the stop — the gap-aware SL logic still applies to the new entry."""
    candles = [
        _candle(0, 100.0, 101.0, 99.0, 100.0),    # BUY decided
        _candle(1, 80.0, 81.0, 70.0, 75.0),       # entry at 80, stop 78.4 hit same candle
    ]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        buy_threshold=105.0, sell_threshold=999.0,
        stop_loss_pct=0.02, take_profit_pct=0.0, fill_model="next_open",
    )
    assert [f.side for f in result.fills] == ["BUY", "SELL"]
    assert result.fills[0].price == 80.0
    assert result.fills[1].reason == "stop_loss"
    assert result.fills[1].price == 78.4


def test_next_open_strategy_sell_exits_at_next_open():
    candles = [
        _candle(0, 100.0, 101.0, 99.0, 100.0),    # BUY decided
        _candle(1, 100.0, 111.0, 99.5, 110.0),    # BUY filled @100; closes 110 -> SELL decided
        _candle(2, 108.0, 109.0, 107.0, 108.5),   # SELL filled @108 (open)
    ]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        buy_threshold=100.5, sell_threshold=105.0, cooldown_ticks=0,
        stop_loss_pct=0.0, take_profit_pct=0.0, fill_model="next_open",
    )
    assert [(f.side, f.candle_index, f.price) for f in result.fills] == [
        ("BUY", 1, 100.0), ("SELL", 2, 108.0),
    ]


def test_unknown_fill_model_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        run([_candle(0, 1, 1, 1, 1)], symbol="T", timeframe="4h", fill_model="bogus")


# ── next_open execution-time risk handling (review of 22635de, 2026-09-26) ──

def _flat(hours: int, px: float, low: float | None = None) -> Candle:
    return _candle(hours, px, px, px if low is None else low, px)


_CAP_KW = dict(
    symbol="TEST/USD", timeframe="4h", strategy_mode="threshold", fill_model="next_open",
    fee_pct=0.0, cooldown_ticks=0, buy_threshold=101.0, sell_threshold=999.0,
    stop_loss_pct=0.02, take_profit_pct=0.0, max_trades_per_day=2,
)


def test_cross_midnight_pending_fill_counts_toward_the_execution_day():
    """Review repro: BUY decided Jan 1 20:00 fills at the Jan 2 00:00 open and
    is stopped out on that same candle — both fills belong to Jan 2, so with
    max_trades_per_day=2 no further BUY may happen on Jan 2. The old code
    counted the BUY on Jan 1, then the date reset erased it."""
    candles = [
        _flat(20, 100.0),               # Jan 1 20:00 — BUY decided
        _flat(24, 100.0, low=97.0),     # Jan 2 00:00 — BUY @100 open, SL @98
        _flat(28, 100.0),               # Jan 2 04:00
        _flat(32, 100.0),               # Jan 2 08:00
    ]
    result = run(candles, **_CAP_KW)
    assert [(f.side, f.candle_index, f.reason) for f in result.fills] == [
        ("BUY", 1, "strategy"), ("SELL", 1, "stop_loss"),
    ], "the third BUY must be blocked by Jan 2's cap"


def test_same_day_cap_still_enforced():
    candles = [_flat(0, 100.0), _flat(4, 100.0, low=97.0), _flat(8, 100.0), _flat(12, 100.0)]
    result = run(candles, **_CAP_KW)
    assert [f.side for f in result.fills] == ["BUY", "SELL"]


def test_protective_stop_fires_even_when_the_daily_cap_is_used_up():
    candles = [_flat(20, 100.0), _flat(24, 100.0, low=97.0), _flat(28, 100.0)]
    result = run(candles, **{**_CAP_KW, "max_trades_per_day": 1})
    assert [(f.side, f.reason) for f in result.fills] == [("BUY", "strategy"), ("SELL", "stop_loss")]


def test_pending_buy_rejected_when_the_open_gaps_past_the_position_cap():
    """Review repro: 1 unit approved at $100 (10% of $1000) must not fill at a
    $200 open, where it would be 20% of the account — rejected, not resized."""
    candles = [_flat(0, 100.0), _flat(4, 200.0)]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        fill_model="next_open", starting_cash=1000.0, risk_per_trade_pct=0.10,
        max_position_pct=0.10, fee_pct=0.0, buy_threshold=101.0, sell_threshold=999.0,
        stop_loss_pct=0.0, take_profit_pct=0.0,
    )
    assert result.fills == []


def test_pending_buy_within_the_cap_after_a_small_gap_still_fills():
    candles = [_flat(0, 100.0), _flat(4, 99.0)]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        fill_model="next_open", starting_cash=1000.0, risk_per_trade_pct=0.10,
        max_position_pct=0.10, fee_pct=0.0, buy_threshold=101.0, sell_threshold=999.0,
        stop_loss_pct=0.0, take_profit_pct=0.0,
    )
    assert [(f.side, f.price) for f in result.fills] == [("BUY", 99.0)]


def test_pending_buy_rejected_when_cash_cannot_cover_notional_plus_fee():
    """All cash sized at $100; at a $100 open the 1% fee makes it unaffordable.
    The old code filled it and drove cash negative."""
    candles = [_flat(0, 100.0), _flat(4, 100.0)]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        fill_model="next_open", starting_cash=1000.0, risk_per_trade_pct=1.0,
        max_position_pct=1.0, fee_pct=0.01, buy_threshold=101.0, sell_threshold=999.0,
        stop_loss_pct=0.0, take_profit_pct=0.0,
    )
    assert result.fills == []
    assert result.final_value == 1000.0


def test_pending_buy_slippage_applied_exactly_once():
    candles = [_flat(0, 100.0), _flat(4, 100.0)]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        fill_model="next_open", starting_cash=100_000.0, risk_per_trade_pct=0.01,
        max_position_pct=0.5, fee_pct=0.0, slippage_pct=0.01,
        buy_threshold=101.0, sell_threshold=999.0, stop_loss_pct=0.0, take_profit_pct=0.0,
    )
    assert len(result.fills) == 1
    assert result.fills[0].price == pytest.approx(101.0), "100 × 1.01 once — not 102.01"


def test_pending_strategy_sell_executes_despite_buy_only_breakers():
    """A deep drawdown trips BUY-only breakers; the strategy exit decided at
    the prior close must still reach the market at the next open."""
    candles = [
        _flat(0, 100.0),     # BUY decided
        _flat(4, 100.0),     # BUY @100
        _flat(8, 1000.0),    # price rockets -> close > sell_threshold -> SELL decided
        _flat(12, 900.0),    # SELL @900 open
    ]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        fill_model="next_open", starting_cash=1000.0, risk_per_trade_pct=0.5,
        max_position_pct=1.0, fee_pct=0.0, cooldown_ticks=0, max_drawdown_pct=0.01,
        buy_threshold=101.0, sell_threshold=500.0, stop_loss_pct=0.0, take_profit_pct=0.0,
    )
    assert [(f.side, f.price) for f in result.fills] == [("BUY", 100.0), ("SELL", 900.0)]


def test_close_model_unchanged_by_the_pending_path():
    candles = [_flat(0, 100.0), _flat(4, 200.0)]
    result = run(
        candles, symbol="TEST/USD", timeframe="4h", strategy_mode="threshold",
        fill_model="close", starting_cash=1000.0, risk_per_trade_pct=0.10,
        max_position_pct=0.10, fee_pct=0.0, buy_threshold=101.0, sell_threshold=999.0,
        stop_loss_pct=0.0, take_profit_pct=0.0,
    )
    assert [(f.side, f.price, f.candle_index) for f in result.fills] == [("BUY", 100.0, 0)]
