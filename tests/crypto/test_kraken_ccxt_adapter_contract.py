"""
Offline contract test: does KrakenFixtureSource's custom raw-payload
parser (test_execution_accounting_fixture_integration.py) actually match
the INSTALLED ccxt library's real parsing behavior?

Fixture-integration review finding 2: KrakenFixtureSource and its fixture
were authored by the same process and can only validate each other's
self-consistency — neither exercises real ccxt market normalization,
request construction, or response parsing. This file closes that gap: it
instantiates the real `ccxt.kraken` exchange class, mocks ONLY the lowest-
level network call (`exchange.fetch`, the actual HTTP transport — sign(),
path/param construction, and HMAC signing all run for real above it) and
market loading (`exchange.set_markets`, so `load_markets()` never hits the
network either), feeds an INDEPENDENTLY-DEFINED raw Kraken-shaped payload
(not reused from the main fixture) through the real `fetch_my_trades()`,
and asserts its real normalized output on exactly the dimensions
KrakenFixtureSource's parser assumes: raw pair-id -> unified symbol
normalization, trade-id-from-dict-key extraction, ordertxid -> order
mapping, price/amount/cost/fee field correctness, millisecond timestamp
conversion, and that `since` correctly reaches the request as Kraken's
`start` (unix seconds) — i.e. account-scoped page filtering.

No live network call is made anywhere in this file (dummy API
credentials; a stub `fetch()`). No production code
(bot/execution/live_executor.py) is imported. HALT is untouched.
"""
from __future__ import annotations

import base64

import pytest

ccxt = pytest.importorskip("ccxt")


def _kraken_with_mocked_transport(raw_response_by_endpoint: dict, *, market: dict):
    """Builds a real ccxt.kraken instance with dummy credentials (so its
    real HMAC signing code doesn't raise), pre-populated markets (so
    load_markets() never calls the network — see Exchange.load_markets's
    own `if not reload: if self.markets: return self.markets` short
    circuit, confirmed against the installed ccxt source this session),
    and a stubbed `.fetch()` (the one real network I/O point) dispatching
    on which endpoint path appears in the request URL. Returns (exchange,
    captured_requests) where captured_requests records every call's real
    url/body for the account-scoped-filtering assertions."""
    exchange = ccxt.kraken({
        "apiKey": "test-key-not-real",
        "secret": base64.b64encode(b"0" * 32).decode(),
        "enableRateLimit": False,
    })
    exchange.set_markets([market])
    captured_requests: "list[dict]" = []

    def fake_fetch(url, method="GET", headers=None, body=None):
        captured_requests.append({"url": url, "method": method, "headers": headers, "body": body})
        for endpoint, response in raw_response_by_endpoint.items():
            if endpoint in url:
                return response
        raise AssertionError(f"test stub received an unexpected request: {url}")

    exchange.fetch = fake_fetch
    return exchange, captured_requests


# An INDEPENDENTLY-DEFINED raw payload — deliberately NOT copied from
# kraken_reconciliation_2026_07_03.json, so this test cannot simply be
# validating the same fixture against itself. Uses Kraken's real,
# publicly-documented market-id convention (X-prefixed crypto, Z-prefixed
# major fiat — "XXBTZCAD" for BTC/CAD) — not independently re-verified via
# a live call this session, but the standard documented Kraken REST
# convention, used here specifically to exercise ccxt's OWN pair
# resolution, not to make a "captured from this account" claim.
_RAW_TRADES_HISTORY_RESPONSE = {
    "error": [],
    "result": {
        "trades": {
            "TESTID1-AAAAA-BBBBB": {
                "ordertxid": "OTEST1-AAAAA-BBBBB",
                "postxid": "TKH2SE-M7IF5-CFI7LT",
                "pair": "XXBTZCAD",
                "time": 1781222405.1234,
                "type": "buy",
                "ordertype": "market",
                "price": "88870.30000",
                "cost": "10.04234390",
                "fee": "0.08034000",
                "vol": "0.00011300",
                "margin": "0.00000000",
                "misc": "",
            },
            "TESTID2-CCCCC-DDDDD": {
                "ordertxid": "OTEST2-CCCCC-DDDDD",
                "postxid": "TKH2SE-M7IF5-CFI7LT",
                "pair": "XXBTZCAD",
                "time": 1781409380.5,
                "type": "sell",
                "ordertype": "limit",
                "price": "90280.10000",
                "cost": "9.93081100",
                "fee": "0.03972000",
                "vol": "0.00011000",
                "margin": "0.00000000",
                "misc": "",
            },
        },
        "count": 2,
    },
}

_BTC_CAD_MARKET = {
    "id": "XXBTZCAD", "symbol": "BTC/CAD", "base": "BTC", "quote": "CAD",
    "baseId": "XXBT", "quoteId": "ZCAD", "active": True, "type": "spot",
    "spot": True, "precision": {"amount": 8, "price": 1},
    "limits": {"amount": {"min": 0.0001}},
}


def test_real_ccxt_fetch_my_trades_matches_what_the_fixture_parser_assumes():
    exchange, captured = _kraken_with_mocked_transport(
        {"TradesHistory": _RAW_TRADES_HISTORY_RESPONSE}, market=_BTC_CAD_MARKET,
    )

    trades = exchange.fetch_my_trades("BTC/CAD", since=1781222000000)

    # --- returned ids: the dict KEY becomes the trade's id, exactly what
    #     KrakenFixtureSource assumes when it iterates raw['result']['trades'] ---
    assert [t["id"] for t in trades] == ["TESTID1-AAAAA-BBBBB", "TESTID2-CCCCC-DDDDD"]
    assert trades[0]["order"] == "OTEST1-AAAAA-BBBBB"  # ordertxid -> order

    # --- raw pair identifier -> unified symbol normalization ---
    # "XXBTZCAD" (Kraken's own internal id, NOT ccxt-normalized "BTC/CAD")
    # was the RAW value in the payload above; ccxt resolved it via the
    # pre-populated market to the unified symbol — this is exactly the
    # normalization step KrakenFixtureSource's parser skips (it assumes
    # 'pair' is already "BTC/CAD"), now proven correct against real ccxt
    # behavior rather than merely assumed.
    assert all(t["symbol"] == "BTC/CAD" for t in trades)

    # --- side/type ---
    assert trades[0]["side"] == "buy"
    assert trades[1]["side"] == "sell"

    # --- price/amount/cost/fee: the exact fields KrakenFixtureSource
    #     reads directly off the raw dict (price/vol/cost/fee) must match
    #     what ccxt's OWN parser produces from the SAME raw input ---
    assert trades[0]["price"] == 88870.3
    assert trades[0]["amount"] == 0.000113
    assert trades[0]["cost"] == pytest.approx(10.0423439)
    assert trades[0]["fee"]["cost"] == pytest.approx(0.08034)
    assert trades[0]["fee"]["currency"] == "CAD"

    # --- precision/timestamps: fractional-second raw `time` converts to
    #     millisecond `timestamp`, matching KrakenFixtureSource's own
    #     `int(round(float(t["time"]) * 1000))` conversion ---
    assert trades[0]["timestamp"] == 1781222405123
    assert trades[1]["timestamp"] == 1781409380500

    # --- account-scoped page filtering: the unified `since` (ms) must
    #     reach the real signed request as Kraken's own `start` (unix
    #     SECONDS, per ccxt's fetch_my_trades source read this session) ---
    assert len(captured) == 1
    assert "start=1781222000" in captured[0]["body"]
    assert "nonce=" in captured[0]["body"]  # a real signed private request, not a bare GET


def test_real_ccxt_leaves_an_unresolvable_raw_pair_unnormalized_not_silently_wrong():
    """A raw pair id with NO matching pre-loaded market must not silently
    produce a plausible-looking but wrong symbol — proving
    KrakenFixtureSource's blanket "pair is already normalized" assumption
    would genuinely break (not just theoretically) against an account
    trading a market this fixture-based test harness hasn't pre-populated."""
    unresolvable_response = {
        "error": [],
        "result": {
            "trades": {
                "TESTID3-EEEEE-FFFFF": {
                    "ordertxid": "OTEST3-EEEEE-FFFFF",
                    "pair": "XETHZCAD",  # ETH/CAD — deliberately NOT in the pre-loaded markets
                    "time": 1781222405.0,
                    "type": "buy",
                    "ordertype": "market",
                    "price": "3000.0",
                    "cost": "3.0",
                    "fee": "0.01",
                    "vol": "0.001",
                    "misc": "",
                }
            },
            "count": 1,
        },
    }
    exchange, _ = _kraken_with_mocked_transport(
        {"TradesHistory": unresolvable_response}, market=_BTC_CAD_MARKET,
    )
    trades = exchange.fetch_my_trades(since=1781222000000)
    # ccxt still returns the trade, but its symbol falls back to the raw,
    # UN-normalized pair id rather than a real unified symbol — exactly
    # the failure mode a parser that "assumes pair is already normalized"
    # (KrakenFixtureSource's own documented simplification) would mis-handle.
    assert trades[0]["symbol"] != "ETH/CAD"
    assert trades[0]["symbol"] == "XETHZCAD"
