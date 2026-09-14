"""Unit tests for bot.dynamic.ranking.rank_buy_signals."""
from bot.dynamic.ranking import RankableSignal, rank_buy_signals


def test_ranks_by_adx_descending():
    signals = [
        RankableSignal("A/CAD", adx=20.0, quote_volume=1_000_000),
        RankableSignal("B/CAD", adx=35.0, quote_volume=500_000),
        RankableSignal("C/CAD", adx=25.0, quote_volume=2_000_000),
    ]
    assert rank_buy_signals(signals) == ["B/CAD", "C/CAD", "A/CAD"]


def test_ties_broken_by_volume():
    signals = [
        RankableSignal("A/CAD", adx=25.0, quote_volume=1_000_000),
        RankableSignal("B/CAD", adx=25.0, quote_volume=5_000_000),
    ]
    assert rank_buy_signals(signals) == ["B/CAD", "A/CAD"]


def test_missing_adx_sorts_last_not_dropped():
    signals = [
        RankableSignal("A/CAD", adx=None, quote_volume=1_000_000),
        RankableSignal("B/CAD", adx=15.0, quote_volume=100.0),
    ]
    result = rank_buy_signals(signals)
    assert set(result) == {"A/CAD", "B/CAD"}   # nothing silently dropped
    assert result == ["B/CAD", "A/CAD"]


def test_empty_list():
    assert rank_buy_signals([]) == []
