"""stock_bot rule signals — live/backtest parity guarantees.

The live bot and stock_backtest.py must produce the same signal for the same
candles or the walk-forward validation means nothing. These tests pin:
- rule_signal() == a direct IndicatorStrategy replay (same config, same feed)
- drop_last excludes the still-forming candle
- build_indicator_config() stays on the validated parameter set
"""

from datetime import datetime, timedelta
import math

from bot.strategy.indicator_strategy import IndicatorStrategy
from bot.strategy.threshold_strategy import Signal
from bot.data.historical_feed import Candle as StrategyCandle
from stock_bot.data.price_feed import Candle
from stock_bot.strategy.rules import build_indicator_config, rule_signal


def _synthetic_candles(n: int = 320) -> list[Candle]:
    """Deterministic wavy uptrend — enough bars to pass the ~204-candle warmup."""
    t0 = datetime(2025, 1, 1)
    out = []
    px = 100.0
    for i in range(n):
        px *= 1.0 + 0.002 + 0.01 * math.sin(i / 7.0)
        o = px * 0.998
        h = px * 1.012
        l = px * 0.988
        out.append(Candle(timestamp=t0 + timedelta(days=i), open=o, high=h,
                          low=l, close=px, volume=1_000_000 + 1000 * i))
    return out


def test_rule_signal_matches_direct_strategy_replay():
    candles = _synthetic_candles()
    verdict = rule_signal(candles)

    strategy = IndicatorStrategy(build_indicator_config())
    last = Signal.HOLD
    for c in candles:
        last = strategy.evaluate(StrategyCandle(
            timestamp=c.timestamp, open=c.open, high=c.high,
            low=c.low, close=c.close, volume=c.volume,
        ))

    assert verdict.signal == last.name
    assert verdict.rsi == strategy.last_rsi
    assert verdict.adx == strategy.last_adx
    assert verdict.warmed_up == strategy.is_warmed_up


def test_drop_last_excludes_forming_candle():
    candles = _synthetic_candles()
    with_last    = rule_signal(candles, drop_last=False)
    without_last = rule_signal(candles, drop_last=True)
    on_shorter   = rule_signal(candles[:-1], drop_last=False)
    assert without_last == on_shorter          # dataclass equality
    assert with_last.candles_used == without_last.candles_used + 1


def test_rule_signal_is_deterministic():
    candles = _synthetic_candles()
    assert rule_signal(candles) == rule_signal(candles)


def test_not_warmed_up_on_short_history():
    verdict = rule_signal(_synthetic_candles(100))
    assert not verdict.warmed_up
    assert verdict.signal == "HOLD"            # warmup always HOLDs


def test_validated_parameter_set_pinned():
    """If this fails, someone changed the strategy parameters — RULE_WHITELIST
    is invalid until stock_backtest.py is re-run and the whitelist re-derived."""
    cfg = build_indicator_config()
    assert cfg.adx_threshold == 18.0
    assert cfg.min_ema_spread_pct == 0.004
    assert cfg.rsi_filter_enabled is True
    assert cfg.volume_k == 0.0
    assert cfg.rsi_oversold == 30.0
    assert cfg.rsi_overbought == 70.0
    assert cfg.regime_ema_period == 200
