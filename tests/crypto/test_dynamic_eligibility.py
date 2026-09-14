"""Unit tests for bot.dynamic.eligibility.DynamicUniverseScreener.

All exchange interaction is against a hermetic FakeExchange (no network) —
same pattern this repo already uses for FakeIB in the IBKR executor tests.
"""
import json

import pytest

from bot.dynamic.eligibility import DynamicUniverseScreener
from config import DynamicUniverseConfig


class FakeExchange:
    def __init__(self, markets: dict, tickers: dict, books: dict, ohlcv: dict,
                 fail_load_markets: bool = False, fail_tickers: bool = False):
        self._markets = markets
        self._tickers = tickers
        self._books = books
        self._ohlcv = ohlcv
        self._fail_load_markets = fail_load_markets
        self._fail_tickers = fail_tickers

    def load_markets(self):
        if self._fail_load_markets:
            raise ConnectionError("simulated network failure")
        return self._markets

    def fetch_tickers(self, symbols):
        if self._fail_tickers:
            raise ConnectionError("simulated ticker failure")
        return {s: self._tickers[s] for s in symbols if s in self._tickers}

    def fetch_order_book(self, symbol):
        if symbol not in self._books:
            raise KeyError(symbol)
        return self._books[symbol]

    def fetch_ohlcv(self, symbol, timeframe="4h", limit=200):
        return self._ohlcv.get(symbol, [])[:limit]


def _market(active=True, spot=True, min_cost=None, min_amount=None):
    limits = {}
    if min_cost is not None:
        limits["cost"] = {"min": min_cost}
    if min_amount is not None:
        limits["amount"] = {"min": min_amount}
    return {"active": active, "spot": spot, "limits": limits}


def _book(mid=100.0, spread_pct=0.001, depth=10_000.0):
    half_spread = mid * spread_pct / 2
    bid, ask = mid - half_spread, mid + half_spread
    # enough size at the top level to satisfy a generous depth requirement
    size = depth / mid
    return {"bids": [[bid, size]], "asks": [[ask, size]]}


def _cfg(**overrides) -> DynamicUniverseConfig:
    defaults = dict(
        enabled=True,
        quote_currencies="CAD",
        min_quote_volume=50_000.0,
        max_spread_pct=0.0015,
        min_depth_quote=500.0,
        depth_band_pct=0.01,
        min_history_candles=200,
        max_candidates=40,
    )
    defaults.update(overrides)
    return DynamicUniverseConfig(**defaults)


def _good_symbol_fixtures(sym="ETH/CAD", vol=1_000_000.0):
    markets = {sym: _market()}
    tickers = {sym: {"quoteVolume": vol, "last": 100.0}}
    books = {sym: _book()}
    ohlcv = {sym: [[i, 1, 1, 1, 1, 1] for i in range(250)]}
    return markets, tickers, books, ohlcv


# ── Happy path ───────────────────────────────────────────────────────────

def test_well_behaved_symbol_is_eligible(tmp_path):
    markets, tickers, books, ohlcv = _good_symbol_fixtures()
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(_cfg(), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)

    assert result.eligible_symbols == ["ETH/CAD"]
    assert result.rejected == []
    assert result.stale is False


# ── Individual filters ──────────────────────────────────────────────────

def test_inactive_market_excluded(tmp_path):
    markets, tickers, books, ohlcv = _good_symbol_fixtures()
    markets["ETH/CAD"]["active"] = False
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(_cfg(), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)

    assert result.eligible_symbols == []
    assert "market inactive" in result.rejected[0].reasons[0]


def test_stablecoin_base_excluded(tmp_path):
    markets, tickers, books, ohlcv = _good_symbol_fixtures(sym="USDT/CAD")
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(_cfg(), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)

    assert result.eligible_symbols == []
    assert any("stablecoin" in r for r in result.rejected[0].reasons)


@pytest.mark.parametrize("base", ["BTCUP", "BTCDOWN", "BTC3L", "BTC3S", "ETHBULL", "ETHBEAR"])
def test_leveraged_token_excluded(tmp_path, base):
    sym = f"{base}/CAD"
    markets, tickers, books, ohlcv = _good_symbol_fixtures(sym=sym)
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(_cfg(), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)

    assert result.eligible_symbols == []
    assert any("leveraged" in r for r in result.rejected[0].reasons)


def test_low_volume_excluded(tmp_path):
    markets, tickers, books, ohlcv = _good_symbol_fixtures(vol=100.0)
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(_cfg(min_quote_volume=50_000.0), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)

    assert result.eligible_symbols == []
    assert "volume" in result.rejected[0].reasons[0]


def test_min_order_size_exceeds_slot_cash_excluded(tmp_path):
    markets, tickers, books, ohlcv = _good_symbol_fixtures()
    markets["ETH/CAD"]["limits"]["cost"] = {"min": 500.0}
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(_cfg(), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)  # slot only has $100, min order is $500

    assert result.eligible_symbols == []
    assert any("minimum order" in r for r in result.rejected[0].reasons)


def test_wide_spread_excluded(tmp_path):
    markets, tickers, books, ohlcv = _good_symbol_fixtures()
    books["ETH/CAD"] = _book(spread_pct=0.05)  # 5% spread, way over the 0.15% cap
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(_cfg(), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)

    assert result.eligible_symbols == []
    assert "spread" in result.rejected[0].reasons[0]


def test_order_book_levels_with_extra_fields_dont_crash(tmp_path):
    """Regression: Kraken's real ccxt order book returns [price, amount,
    timestamp] (3 elements), not [price, amount] — this must not crash the
    depth/spread calculation (caught via a live smoke test 2026-09-13)."""
    markets, tickers, books, ohlcv = _good_symbol_fixtures()
    books["ETH/CAD"] = {
        "bids": [[99.95, 100.0, 1234567890]],
        "asks": [[100.05, 100.0, 1234567891]],
    }
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(_cfg(), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)

    assert result.eligible_symbols == ["ETH/CAD"]


def test_thin_depth_excluded(tmp_path):
    markets, tickers, books, ohlcv = _good_symbol_fixtures()
    books["ETH/CAD"] = _book(depth=10.0)  # far under the $500 minimum
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(_cfg(), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)

    assert result.eligible_symbols == []
    assert any("depth" in r for r in result.rejected[0].reasons)


def test_insufficient_history_excluded(tmp_path):
    markets, tickers, books, ohlcv = _good_symbol_fixtures()
    ohlcv["ETH/CAD"] = [[i, 1, 1, 1, 1, 1] for i in range(50)]  # far under 200
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(_cfg(), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)

    assert result.eligible_symbols == []
    assert any("history" in r for r in result.rejected[0].reasons)


# ── max_candidates cap ───────────────────────────────────────────────────

def test_max_candidates_cap_keeps_top_volume(tmp_path):
    markets, tickers, books, ohlcv = {}, {}, {}, {}
    for i, vol in enumerate([1_000_000, 900_000, 800_000]):
        sym = f"COIN{i}/CAD"
        markets[sym] = _market()
        tickers[sym] = {"quoteVolume": vol, "last": 10.0}
        books[sym] = _book()
        ohlcv[sym] = [[j, 1, 1, 1, 1, 1] for j in range(250)]
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(_cfg(max_candidates=2), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)

    assert set(result.eligible_symbols) == {"COIN0/CAD", "COIN1/CAD"}
    rejected_syms = {c.symbol: c for c in result.rejected}
    assert "top 2" in rejected_syms["COIN2/CAD"].reasons[0]


# ── Duplicate-base dedup across quote currencies ────────────────────────

def test_duplicate_base_across_quotes_keeps_higher_volume(tmp_path):
    markets = {
        "ETH/CAD": _market(),
        "ETH/USD": _market(),
    }
    tickers = {
        "ETH/CAD": {"quoteVolume": 1_000_000.0, "last": 100.0},
        "ETH/USD": {"quoteVolume": 2_000_000.0, "last": 75.0},
    }
    books = {"ETH/CAD": _book(), "ETH/USD": _book()}
    ohlcv = {
        "ETH/CAD": [[i, 1, 1, 1, 1, 1] for i in range(250)],
        "ETH/USD": [[i, 1, 1, 1, 1, 1] for i in range(250)],
    }
    ex = FakeExchange(markets, tickers, books, ohlcv)
    screener = DynamicUniverseScreener(
        _cfg(quote_currencies="CAD,USD"), cache_path=str(tmp_path / "cache.json"),
    )

    result = screener.discover(ex, slot_cash=100.0)

    assert result.eligible_symbols == ["ETH/USD"]  # higher volume wins
    rejected_syms = {c.symbol: c for c in result.rejected}
    assert "duplicate base exposure" in rejected_syms["ETH/CAD"].reasons[0]


# ── Fail-safe: discovery failure, cache, staleness ──────────────────────

def test_discovery_failure_with_no_cache_returns_empty_not_arbitrary(tmp_path):
    ex = FakeExchange({}, {}, {}, {}, fail_load_markets=True)
    screener = DynamicUniverseScreener(_cfg(), cache_path=str(tmp_path / "cache.json"))

    result = screener.discover(ex, slot_cash=100.0)

    assert result.eligible == []
    assert result.eligible_symbols == []
    assert result.stale is True


def test_discovery_failure_falls_back_to_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    good_ex = FakeExchange(*_good_symbol_fixtures())
    screener = DynamicUniverseScreener(_cfg(), cache_path=str(cache_path))
    first = screener.discover(good_ex, slot_cash=100.0)
    assert first.eligible_symbols == ["ETH/CAD"]

    failing_ex = FakeExchange({}, {}, {}, {}, fail_load_markets=True)
    second = screener.discover(failing_ex, slot_cash=100.0)

    assert second.eligible_symbols == ["ETH/CAD"]
    assert second.stale is True


def test_expired_cache_is_not_trusted(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({
        "scanned_at": 0.0,  # epoch — infinitely old
        "eligible": ["ETH/CAD"],
    }))
    screener = DynamicUniverseScreener(_cfg(cache_max_age_hours=1.0), cache_path=str(cache_path))
    failing_ex = FakeExchange({}, {}, {}, {}, fail_load_markets=True)

    result = screener.discover(failing_ex, slot_cash=100.0)

    assert result.eligible_symbols == []
    assert result.stale is True
