"""
Regression test for the 2026-09-12 finding: IndicatorStrategy (the shared
strategy both bots trade on — see bot/strategy/indicator_strategy.py) could
classify FALLING MACD momentum as "rising".

Root cause: _last_macd_hist was only updated inside _trend_signal(), AFTER
its ADX-threshold rejection could already return HOLD. A candle rejected by
ADX therefore never updated _last_macd_hist, so the next eligible candle's
"rising" check compared against a stale value from before the rejected
candle instead of the immediately preceding one. Reproduced with histogram
values 1 -> 5 -> 3 (middle candle ADX-rejected): the old code read the final
candle's 3 > 1 as "rising" despite real momentum having fallen from 5 to 3.

Fixed by moving the MACD computation and _last_macd_hist update into
evaluate(), unconditionally on every completed candle (same rule already
used for RSI), before any gate can early-return.

Indicator internals (calc_adx, calc_macd, calc_rsi, calc_trend, calc_ema)
are mocked so this test exercises only the control-flow bug — whether
_last_macd_hist gets updated on an ADX-rejected candle — independent of the
real indicator math, which is already covered by tests/shared/test_indicators.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bot.strategy.indicator_strategy as strat_mod
from bot.strategy.indicator_strategy import IndicatorConfig, IndicatorStrategy
from bot.strategy.threshold_strategy import Signal
from bot.data.historical_feed import Candle


def _candle(i: int) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i),
        open=100.0, high=100.5, low=99.5, close=100.0, volume=1_000_000,
    )


def _make_strategy() -> IndicatorStrategy:
    """Config stripped of every gate irrelevant to the MACD-history bug —
    isolates a BUY to depend purely on macd_hist_rising via Mode A."""
    return IndicatorStrategy(IndicatorConfig(
        regime_ema_period=0,        # skip the 200-EMA macro filter entirely
        rsi_filter_enabled=False,   # Mode A's RSI range check always passes
        volume_k=0,                 # no volume confirmation gate
        min_ema_spread_pct=0.0,     # EMA-spread gate always passes
        macd_enabled=True,
        adx_threshold=25.0,
        breakout_adx_threshold=999.0,  # isolate to Mode A — Mode B's macd_b_ok
                                        # (hist > 0, no "rising" requirement) would
                                        # otherwise fire independently and mask
                                        # the bug under test
    ))


def test_adx_rejected_candle_still_updates_macd_history():
    """The exact reviewer reproduction: histogram 1 -> 5(ADX-rejected) -> 3.
    The final candle's real momentum FELL (5 -> 3) and must not fire a
    pullback BUY."""
    strat = _make_strategy()

    # calc_adx: passes (30) for every candle except the deliberately
    # ADX-rejected middle test candle (10); calc_macd: flat histogram (0.0)
    # through warmup, then the exact 1 -> 5 -> 3 sequence for the 3 test
    # candles. calc_rsi/calc_trend/calc_ema are fixed, valid, gate-passing
    # constants — irrelevant to the bug under test.
    adx_queue  = [30.0, 10.0, 30.0]     # candle A, B (rejected), C
    macd_queue = [1.0, 5.0, 3.0]

    def _adx_side_effect(*a, **k):
        return adx_queue.pop(0) if adx_queue else 30.0

    def _macd_side_effect(*a, **k):
        hist = macd_queue.pop(0) if macd_queue else 0.0
        return (0.0, 0.0, hist)

    with patch.object(strat_mod, "calc_rsi", return_value=50.0), \
         patch.object(strat_mod, "calc_trend", return_value="BULLISH"), \
         patch.object(strat_mod, "calc_ema", return_value=100.0), \
         patch.object(strat_mod, "calc_adx", side_effect=_adx_side_effect), \
         patch.object(strat_mod, "calc_macd", side_effect=_macd_side_effect):

        # evaluate()'s warmup gate (`len(closes) < self._warmup`) returns
        # HOLD without computing ANY indicator, for every call up to and
        # including closes-length == _warmup - 1. Feed exactly that many
        # candles so the very next 3 calls are the controlled A/B/C
        # sequence, with the queues untouched until then.
        i = 0
        for i in range(strat._warmup - 1):
            strat.evaluate(_candle(i))

        # Candle A: histogram=1, ADX=30 (passes) -> _last_macd_hist becomes 1.
        sig_a = strat.evaluate(_candle(i + 1))
        assert strat.last_macd_hist == 1.0

        # Candle B: histogram=5, ADX=10 (REJECTED). The fix requires
        # _last_macd_hist to become 5 regardless of the ADX rejection.
        sig_b = strat.evaluate(_candle(i + 2))
        assert sig_b == Signal.HOLD   # confirms the rejection actually happened
        assert strat.last_macd_hist == 5.0, (
            "an ADX-rejected candle must still update MACD history — this "
            "is the exact bug: the old code left it stale at 1.0 here"
        )

        # Candle C: histogram=3. Real momentum fell 5 -> 3 (falling), so
        # this must NOT read as "rising" and must NOT fire a pullback BUY —
        # the old code compared 3 against the stale 1.0 (3 > 1 = "rising")
        # and fired here.
        sig_c = strat.evaluate(_candle(i + 3))
        assert sig_c == Signal.HOLD, (
            "momentum fell from 5 to 3 — must not fire a pullback BUY on a "
            "false 'rising' reading against a stale pre-rejection value"
        )
