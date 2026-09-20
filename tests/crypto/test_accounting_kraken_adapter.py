"""
Offline contract test for bot/accounting/kraken_adapter.py against the
REAL, installed ccxt.kraken class — same mocking discipline as
test_kraken_ccxt_adapter_contract.py (mocks only the lowest-level transport,
`exchange.fetch`; real HMAC signing, request construction, and response
parsing all run for real above it). No network access, no production
bot/execution or bot/main import.

Exercises exactly the two things kraken_adapter.py hand-writes on top of
ccxt (everything else — fetch_balance/fetch_deposits/fetch_withdrawals — is
a thin pass-through to ccxt's own already-tested unified methods, not
re-tested here):
  1. True `ofs`-based pagination against Kraken's real `count` field
     (confirmed via kraken_adapter.py's own module docstring: ccxt's
     unified fetch_my_trades never does this).
  2. Account-wide retrieval + correct account-wide `count`, with the
     `symbol` argument filtering the RETURNED page only, never corrupting
     the completeness arithmetic (module docstring's central claim).
"""
from __future__ import annotations

import base64

import pytest

ccxt = pytest.importorskip("ccxt")

from bot.accounting.kraken_adapter import KrakenAccountingAdapter  # noqa: E402


def _kraken_with_mocked_transport(raw_response_by_endpoint: dict, *, markets: list):
    exchange = ccxt.kraken({
        "apiKey": "test-key-not-real",
        "secret": base64.b64encode(b"0" * 32).decode(),
        "enableRateLimit": False,
    })
    exchange.set_markets(markets)
    captured_requests: "list[dict]" = []

    def fake_fetch(url, method="GET", headers=None, body=None):
        captured_requests.append({"url": url, "method": method, "headers": headers, "body": body})
        for endpoint, response_or_fn in raw_response_by_endpoint.items():
            if endpoint in url:
                return response_or_fn(body) if callable(response_or_fn) else response_or_fn
        raise AssertionError(f"test stub received an unexpected request: {url}")

    exchange.fetch = fake_fetch
    return exchange, captured_requests


_BTC_CAD_MARKET = {
    "id": "XXBTZCAD", "symbol": "BTC/CAD", "base": "BTC", "quote": "CAD",
    "baseId": "XXBT", "quoteId": "ZCAD", "active": True, "type": "spot",
    "spot": True, "precision": {"amount": 1e-08, "price": 0.1},
    "limits": {"amount": {"min": 0.0001}},
}
_SOL_CAD_MARKET = {
    "id": "SOLCAD", "symbol": "SOL/CAD", "base": "SOL", "quote": "CAD",
    "baseId": "SOL", "quoteId": "ZCAD", "active": True, "type": "spot",
    "spot": True, "precision": {"amount": 1e-08, "price": 0.01},
    "limits": {"amount": {"min": 0.001}},
}


def _trade(tid, ordertxid, pair, side, price, cost, fee, vol, time_):
    return {
        "ordertxid": ordertxid, "pair": pair, "time": time_, "type": side,
        "ordertype": "market", "price": str(price), "cost": str(cost),
        "fee": str(fee), "vol": str(vol), "margin": "0.00000000", "misc": "",
    }


def test_fetch_my_trades_page_is_account_wide_and_reports_real_count():
    """Two DIFFERENT symbols in one account-wide page — reported_total must
    reflect Kraken's own account-wide `count`, and requesting one specific
    symbol must filter the OUTPUT without changing that total (module
    docstring's central claim: filtering before counting would make
    coverage-proof arithmetic permanently report "incomplete" for any
    multi-symbol account)."""
    raw = {
        "error": [], "result": {
            "trades": {
                "T1": _trade("T1", "O1", "XXBTZCAD", "buy", 90000.0, 90.0, 0.09, 0.001, 1781222405.0),
                "T2": _trade("T2", "O2", "SOLCAD", "buy", 200.0, 100.0, 0.1, 0.5, 1781222406.0),
            },
            "count": 2,
        },
    }
    exchange, captured = _kraken_with_mocked_transport(
        {"TradesHistory": raw}, markets=[_BTC_CAD_MARKET, _SOL_CAD_MARKET],
    )
    adapter = KrakenAccountingAdapter(exchange)

    all_page = adapter.fetch_my_trades_page(None, since=None, offset=0, limit=50)
    assert all_page.reported_total == 2
    assert {t.symbol for t in all_page.trades} == {"BTC/CAD", "SOL/CAD"}

    btc_only = adapter.fetch_my_trades_page("BTC/CAD", since=None, offset=0, limit=50)
    assert btc_only.reported_total == 2  # UNCHANGED — account-wide, not symbol-scoped
    assert [t.symbol for t in btc_only.trades] == ["BTC/CAD"]
    assert btc_only.trades[0].trade_id == "T1"
    assert btc_only.trades[0].order_id == "O1"
    assert btc_only.trades[0].fee_currency == "CAD"


def test_fetch_my_trades_page_sends_real_ofs_and_advances_next_offset():
    """A page smaller than the real account-wide count must report a
    next_offset so the caller's pagination loop actually continues, and the
    request sent to Kraken must carry the real `ofs` — the whole reason
    this adapter exists instead of ccxt's unified (non-paginating)
    fetch_my_trades (kraken_adapter.py's own module docstring)."""
    def _response(body):
        assert "ofs=" in (body or "")
        return {
            "error": [], "result": {
                "trades": {"T1": _trade("T1", "O1", "XXBTZCAD", "buy", 90000.0, 90.0, 0.09, 0.001, 1781222405.0)},
                "count": 5,
            },
        }

    exchange, captured = _kraken_with_mocked_transport({"TradesHistory": _response}, markets=[_BTC_CAD_MARKET])
    adapter = KrakenAccountingAdapter(exchange)
    page = adapter.fetch_my_trades_page(None, since=None, offset=0, limit=1)
    assert page.reported_total == 5
    assert page.next_offset == 1  # offset(0) + len(page.trades)(1) < count(5)
    assert "ofs=0" in captured[0]["body"]


def test_fetch_my_trades_page_since_reaches_request_as_kraken_start():
    from datetime import datetime, timezone
    since_iso = datetime.fromtimestamp(1781222000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _response(body):
        assert "start=1781222000" in (body or "")
        return {"error": [], "result": {"trades": {}, "count": 0}}

    exchange, captured = _kraken_with_mocked_transport({"TradesHistory": _response}, markets=[_BTC_CAD_MARKET])
    adapter = KrakenAccountingAdapter(exchange)
    page = adapter.fetch_my_trades_page(None, since=since_iso, offset=0, limit=50)
    assert page.reported_total == 0
    assert page.next_offset is None


def test_amount_tolerance_uses_real_market_tick_size():
    exchange, _ = _kraken_with_mocked_transport({}, markets=[_BTC_CAD_MARKET])
    adapter = KrakenAccountingAdapter(exchange)
    assert adapter.amount_tolerance("BTC/CAD") == pytest.approx(5e-09)


def test_cash_tolerance_prefers_display_decimals_over_raw_precision():
    exchange, _ = _kraken_with_mocked_transport({}, markets=[_BTC_CAD_MARKET])
    exchange.currencies = {
        "CAD": {"info": {"display_decimals": "2"}, "precision": 0.0001},
    }
    adapter = KrakenAccountingAdapter(exchange)
    assert adapter.cash_tolerance("CAD") == pytest.approx(0.005)


def test_cash_tolerance_falls_back_when_currency_metadata_missing():
    exchange, _ = _kraken_with_mocked_transport({}, markets=[_BTC_CAD_MARKET])
    exchange.currencies = {}
    adapter = KrakenAccountingAdapter(exchange)
    assert adapter.cash_tolerance("CAD") == pytest.approx(0.005)
