"""Rule-based trade signals for the position book — the validated brain.

One function, `rule_signal()`, replays daily candles through a fresh
IndicatorStrategy (the crypto bot's Mode A/B logic, imported not copied) and
returns the signal for the most recent completed candle. Stateless by design:
recomputing from scratch each scan cycle makes the live signal identical to
the backtest by construction — no state files, no drift.

`build_indicator_config()` is THE parameter set. stock_backtest.py imports it
and so does the live bot: what was validated is what trades, always.

Why rules instead of AI verdicts (decided 2026-07-10): AI confidence numbers
are uncalibrated and verdicts flip on unchanged data (AMD: BUY 58 → SELL 60 →
HOLD 58 → BUY 68 → SELL 62 within ~10 min). Rules are reproducible, therefore
backtestable, therefore gateable. AI is advisory display only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig
from bot.strategy.threshold_strategy import Signal
from bot.data.historical_feed import Candle as StrategyCandle


def build_indicator_config() -> IndicatorConfig:
    """The validated parameter family (walk-forward pass 2026-07-10 on
    MRNA/AMD/RY.TO/PLTR — logs/stock_backtest_20260710.md). Same values the
    crypto bot validated on BTC 4h. Change nothing here without re-running
    stock_backtest.py and updating RULE_WHITELIST."""
    return IndicatorConfig(
        adx_threshold=18.0,
        min_ema_spread_pct=0.004,
        rsi_filter_enabled=True,
        volume_k=0.0,
        rsi_oversold=30.0,
        rsi_overbought=70.0,
    )


@dataclass(frozen=True)
class RuleVerdict:
    signal:     str              # "BUY" | "SELL" | "HOLD"
    rsi:        Optional[float]
    adx:        Optional[float]
    trend:      Optional[str]
    regime:     Optional[str]
    warmed_up:  bool
    candles_used: int


def rule_signal(candles: list, drop_last: bool = False) -> RuleVerdict:
    """Replay `candles` (oldest→newest, any object with OHLCV + timestamp)
    and return the verdict of the last candle fed.

    drop_last=True excludes the final candle — pass this while the symbol's
    market is OPEN, because today's daily candle is still forming and the
    backtest only ever saw completed candles. Acting on a half-formed candle
    is a live-vs-backtest divergence."""
    if drop_last:
        candles = candles[:-1]

    strategy = IndicatorStrategy(build_indicator_config())
    sig = Signal.HOLD
    for c in candles:
        sig = strategy.evaluate(StrategyCandle(
            timestamp=c.timestamp, open=c.open, high=c.high,
            low=c.low, close=c.close, volume=c.volume,
        ))

    return RuleVerdict(
        signal=sig.name,
        rsi=strategy.last_rsi,
        adx=strategy.last_adx,
        trend=strategy.last_trend,
        regime=strategy.last_regime,
        warmed_up=strategy.is_warmed_up,
        candles_used=len(candles),
    )
