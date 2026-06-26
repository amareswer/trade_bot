"""Unit tests for CryptoUniverse — no network calls."""

from typing import Optional
from unittest.mock import MagicMock
from bot.data.crypto_universe import CryptoUniverse


def _make_exchange(tickers: dict, pairs: Optional[list] = None) -> MagicMock:
    """Build a minimal mock ccxt exchange."""
    ex = MagicMock()
    active_pairs = pairs if pairs is not None else list(tickers.keys())
    ex.load_markets.return_value = {
        s: {"active": True, "spot": True} for s in active_pairs
    }
    ex.fetch_tickers.return_value = tickers
    return ex


def _ticker(pct: float, vol: float, last: float = 100.0) -> dict:
    return {"percentage": pct, "quoteVolume": vol, "last": last}


# ── test 1 ────────────────────────────────────────────────────────────────────

def test_returns_top_n_by_score():
    """Pairs are ranked by vol×pct descending; only top n returned."""
    tickers = {
        "ETH/CAD": _ticker(5.0, 50_000),   # score 250_000
        "BTC/CAD": _ticker(2.0, 200_000),  # score 400_000  ← rank 1
        "SOL/CAD": _ticker(8.0, 30_000),   # score 240_000  ← rank 3
        "ADA/CAD": _ticker(3.0, 100_000),  # score 300_000  ← rank 2
        "DOT/CAD": _ticker(1.0, 10_000),   # score  10_000  ← rank 5
    }
    ex = _make_exchange(tickers)
    result = CryptoUniverse().get_top_movers(ex, n=3)

    assert result == ["BTC/CAD", "ADA/CAD", "ETH/CAD"], result


# ── test 2 ────────────────────────────────────────────────────────────────────

def test_filters_negative_momentum():
    """Pairs with negative or zero percentage are excluded from positive pass."""
    tickers = {
        "ETH/CAD": _ticker(-5.0, 500_000),  # negative — excluded
        "BTC/CAD": _ticker(0.0,  200_000),  # zero — excluded
        "SOL/CAD": _ticker(3.0,   10_000),  # positive ← only positive
    }
    ex = _make_exchange(tickers)
    result = CryptoUniverse().get_top_movers(ex, n=3)

    assert "SOL/CAD" in result
    assert result[0] == "SOL/CAD"
    # negative/zero pairs may appear in fill slots but not before positive ones
    for sym in result[1:]:
        assert sym in ("ETH/CAD", "BTC/CAD")


# ── test 3 ────────────────────────────────────────────────────────────────────

def test_fallback_on_fetch_error():
    """If fetch_tickers() raises, return ['ETH/CAD'] without crashing."""
    ex = MagicMock()
    ex.load_markets.return_value = {
        "BTC/CAD": {"active": True, "spot": True},
    }
    ex.fetch_tickers.side_effect = Exception("network timeout")

    result = CryptoUniverse().get_top_movers(ex, n=5)

    assert result == ["ETH/CAD"]


# ── test 4 ────────────────────────────────────────────────────────────────────

def test_fallback_fills_when_few_positive():
    """When fewer than n pairs are positive, fill remaining slots by volume."""
    tickers = {
        "ETH/CAD": _ticker( 2.0, 80_000),   # positive ← slot 1
        "BTC/CAD": _ticker(-1.0, 300_000),  # negative, highest vol ← fill slot 2
        "SOL/CAD": _ticker(-3.0,  50_000),  # negative, lower vol  ← fill slot 3
    }
    ex = _make_exchange(tickers)
    result = CryptoUniverse().get_top_movers(ex, n=3)

    assert len(result) == 3
    assert result[0] == "ETH/CAD"          # only positive first
    assert "BTC/CAD" in result             # highest vol fills slot 2
    assert "SOL/CAD" in result             # remaining vol fills slot 3
    assert result.index("BTC/CAD") < result.index("SOL/CAD")  # vol-ordered
