"""
Tests for the live-only BUY overlays in bot/backtest/engine.py
(`mtf_daily_closes`, `fng_by_date`) — added 2026-09-02 so a research script
(`mtf_overlay_backtest.py`) can measure whether bot/main.py's MTF daily-trend
veto and Fear&Greed veto actually help, given neither is in the validated
fingerprint.

Hermetic — a stub strategy drives BUY on chosen candles so the veto logic is
exercised exactly, with no dependence on crafting organic IndicatorStrategy
signals.

Run: python -m pytest tests/crypto/test_overlay_gates.py -q
"""
from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta, timezone

from bot.backtest import engine
from bot.data.historical_feed import Candle
from bot.strategy.threshold_strategy import Signal

_T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


class _StubStrategy:
    """Emits BUY on `buy_idx` candles, SELL on `sell_idx`, HOLD otherwise.

    Implements only the surface bot/backtest/engine.py touches.
    """

    def __init__(self, buy_idx: set[int], sell_idx: set[int]) -> None:
        self._buy_idx = buy_idx
        self._sell_idx = sell_idx
        self._i = -1
        self._closes: deque = deque(maxlen=50)
        self.stats: dict = {"buy_signals": 0, "sell_signals": 0}
        self.last_atr = 1.0
        self.last_adx = 25.0
        self.last_rsi = 50.0
        self.last_trend = "BULLISH"

        class _Cfg:
            fast_ema_period = 9
            slow_ema_period = 21

        self.config = _Cfg()

    def evaluate(self, candle) -> Signal:
        self._i += 1
        self._closes.append(candle.close)
        if self._i in self._buy_idx:
            self.stats["buy_signals"] += 1
            return Signal.BUY
        if self._i in self._sell_idx:
            self.stats["sell_signals"] += 1
            return Signal.SELL
        return Signal.HOLD

    @property
    def is_warmed_up(self) -> bool:
        return True


def _candles(n: int, start: float = 100.0) -> list[Candle]:
    out = []
    p = start
    for i in range(n):
        p *= 1.001
        out.append(Candle(timestamp=_T0 + timedelta(hours=4 * i),
                          open=p, high=p * 1.01, low=p * 0.99, close=p, volume=1.0))
    return out


def _run(candles, strat, **overlay):
    # Monkeypatch-free: engine builds its own strategy, so inject via the
    # `strategy_mode` seam is not available — patch the module symbol instead.
    import bot.backtest.engine as eng_mod
    orig = eng_mod.IndicatorStrategy
    eng_mod.IndicatorStrategy = lambda *a, **k: strat
    try:
        return eng_mod.run(
            candles=candles, symbol="TEST/USDT", timeframe="4h",
            strategy_mode="indicator", starting_cash=10_000.0,
            risk_per_trade_pct=0.01, fee_pct=0.001, cooldown_ticks=0,
            max_position_pct=0.99, stop_loss_pct=0.0, take_profit_pct=0.0,
            **overlay,
        )
    finally:
        eng_mod.IndicatorStrategy = orig


# ── daily-close helpers ──────────────────────────────────────────────────

def _daily(values: list[float]) -> list[tuple[date, float]]:
    d0 = date(2024, 6, 1)
    return [(d0 + timedelta(days=k), v) for k, v in enumerate(values)]


def _bearish_daily() -> list[tuple[date, float]]:
    return _daily([1000.0 - k for k in range(400)])   # strictly falling → trend() BEARISH


def _bullish_daily() -> list[tuple[date, float]]:
    return _daily([500.0 + k * 3 for k in range(400)])  # strictly rising → BULLISH


# ── tests ────────────────────────────────────────────────────────────────

def test_no_overlay_kwargs_is_unchanged():
    c = _candles(60)
    base = _run(c, _StubStrategy({10, 30}, {20, 40}))
    assert len(base.fills) == 4
    assert "overlay_mtf_rejected" not in base.rejection_stats
    assert "overlay_fng_rejected" not in base.rejection_stats


def test_explicit_none_overlays_match_baseline():
    c = _candles(60)
    a = _run(c, _StubStrategy({10, 30}, {20, 40}))
    b = _run(c, _StubStrategy({10, 30}, {20, 40}),
             mtf_daily_closes=None, fng_by_date=None)
    assert [(f.side, f.candle_index) for f in a.fills] == \
           [(f.side, f.candle_index) for f in b.fills]


def test_mtf_bearish_daily_vetoes_every_buy():
    c = _candles(60)
    r = _run(c, _StubStrategy({10, 30}, set()), mtf_daily_closes=_bearish_daily())
    assert r.rejection_stats["overlay_mtf_rejected"] == 2
    assert [f.side for f in r.fills] == []          # both BUYs suppressed


def test_mtf_bullish_daily_allows_buys():
    c = _candles(60)
    r = _run(c, _StubStrategy({10, 30}, {20, 40}), mtf_daily_closes=_bullish_daily())
    assert r.rejection_stats["overlay_mtf_rejected"] == 0
    assert len([f for f in r.fills if f.side == "BUY"]) == 2


def test_fng_above_threshold_vetoes_buys():
    c = _candles(60)
    hot = {date(2024, 6, 1) + timedelta(days=k): 90 for k in range(400)}
    r = _run(c, _StubStrategy({10, 30}, set()), fng_by_date=hot)
    assert r.rejection_stats["overlay_fng_rejected"] == 2
    assert r.fills == []


def test_fng_below_threshold_allows_buys():
    c = _candles(60)
    cold = {date(2024, 6, 1) + timedelta(days=k): 20 for k in range(400)}
    r = _run(c, _StubStrategy({10, 30}, {20, 40}), fng_by_date=cold)
    assert r.rejection_stats["overlay_fng_rejected"] == 0
    assert len([f for f in r.fills if f.side == "BUY"]) == 2


def test_fng_asof_uses_most_recent_prior_date():
    # FNG only published on day 0 (value 90) then a gap; a BUY on a later
    # candle must still see the stale-but-most-recent 90 and be vetoed.
    c = _candles(60)
    sparse = {date(2020, 1, 1): 90}   # far in the past, still the latest known
    r = _run(c, _StubStrategy({10}, set()), fng_by_date=sparse)
    assert r.rejection_stats["overlay_fng_rejected"] == 1


def test_fng_no_prior_data_fails_open():
    # Only future-dated FNG entries → nothing "as of" the candle date → no veto.
    c = _candles(60)
    future = {date(2099, 1, 1): 99}
    r = _run(c, _StubStrategy({10}, set()), fng_by_date=future)
    assert r.rejection_stats["overlay_fng_rejected"] == 0
    assert len([f for f in r.fills if f.side == "BUY"]) == 1


def test_mtf_takes_precedence_over_fng_in_veto_label():
    c = _candles(60)
    hot = {date(2024, 6, 1) + timedelta(days=k): 90 for k in range(400)}
    r = _run(c, _StubStrategy({10}, set()),
             mtf_daily_closes=_bearish_daily(), fng_by_date=hot)
    # MTF checked first — it owns the veto, FNG counter stays 0
    assert r.rejection_stats["overlay_mtf_rejected"] == 1
    assert r.rejection_stats["overlay_fng_rejected"] == 0


def test_insufficient_daily_history_skips_mtf_veto():
    c = _candles(60)
    r = _run(c, _StubStrategy({10}, set()),
             mtf_daily_closes=_daily([100.0, 99.0, 98.0]))   # < mtf_slow_period
    assert r.rejection_stats["overlay_mtf_rejected"] == 0
    assert len([f for f in r.fills if f.side == "BUY"]) == 1
