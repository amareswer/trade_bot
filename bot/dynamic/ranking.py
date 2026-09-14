"""
Ranking for simultaneous BUY signals in the dynamic universe.

Point 3 of the dynamic-universe design ("Do not buy a coin merely because it
is a top mover") means the ranking key must be something the STRATEGY itself
already treats as a quality signal — not a fresh, untested score invented
just to pick winners. We use ADX (trend strength), the same metric
IndicatorStrategy already gates entries on, as the primary key, with 24h
quote volume (liquidity, already screened for eligibility) as the tiebreak.
This deliberately does not add a new tunable parameter to avoid a second,
unvalidated selection strategy hiding behind "dynamic universe" — the
existing strategy decides WHETHER to buy; this only decides, among several
simultaneous BUY signals and a limited number of free slots, WHICH ones fill
them first, using information available at that exact moment.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RankableSignal:
    symbol:      str
    adx:         float | None   # from the strategy's own last-evaluated candle
    quote_volume: float | None  # from the eligibility screen, same cycle


def rank_buy_signals(signals: list[RankableSignal]) -> list[str]:
    """
    Return symbols ordered highest-priority-first. A missing ADX or volume
    sorts last within its tier rather than crashing or being silently
    dropped — a symbol with a valid BUY signal is never excluded from
    ranking just because one auxiliary field is unavailable.
    """
    def key(s: RankableSignal) -> tuple[float, float]:
        adx = s.adx if s.adx is not None else -1.0
        vol = s.quote_volume if s.quote_volume is not None else -1.0
        return (adx, vol)

    return [s.symbol for s in sorted(signals, key=key, reverse=True)]
