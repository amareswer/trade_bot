"""
Standalone, re-runnable proof that ccxt's installed Kraken adapter translates
params={"trailingPercent": ...} into Kraken's native trailing-stop AddOrder
request shape — the param used by LiveExecutor._place_native_trailing_stop()
(bot/execution/live_executor.py).

Written 2026-08-19 after a review found the prior "verification" of this was
just restated prose (line-number citations, no literal evidence). This script
makes no network calls — ccxt.kraken.order_request() is a pure request-dict
builder, so this checks the REAL installed library's actual behavior, not a
mock and not a manual trace of the source that could contain a reading error.

Run: .venv/bin/python verify_kraken_trailing_stop_param.py
Exits non-zero (with a clear message) if a future ccxt upgrade ever changes
this mapping — re-run after any ccxt version bump in requirements.lock.txt.
"""
from __future__ import annotations

import sys

import ccxt

EXPECTED_CCXT_VERSION = "4.5.56"  # requirements.lock.txt pin as of this writing


def main() -> int:
    print(f"ccxt.__version__ = {ccxt.__version__}")
    print(f"ccxt.__file__    = {ccxt.__file__}")
    if ccxt.__version__ != EXPECTED_CCXT_VERSION:
        print(
            f"NOTE: installed ccxt ({ccxt.__version__}) differs from the "
            f"version this script was last verified against "
            f"({EXPECTED_CCXT_VERSION}). Re-verify the assertions below "
            f"still hold, don't just trust a green run.",
        )

    ex = ccxt.kraken({"apiKey": "dummy", "secret": "ZHVtbXk="})  # never touches the network below

    # Mirrors exactly what _place_native_trailing_stop() passes:
    #   self._exchange.create_order(self.symbol, "market", "sell", quantity,
    #                                params={"trailingPercent": f"{trailing_pct*100:.4f}"})
    # create_order() builds this base request dict itself, then hands off to
    # order_request() for param-specific handling — reproduced here so we can
    # inspect order_request()'s output directly.
    base_request = {
        "pair": "XBTCAD",
        "type": "sell",
        "ordertype": "market",
        "volume": "0.00100000",
    }
    built_request, leftover_params = ex.order_request(
        "createOrder", "BTC/CAD", "market", base_request,
        amount=0.001, price=None,
        params={"trailingPercent": "2.0000"},
    )

    print()
    print("order_request() output (the actual dict ccxt would POST as Kraken's AddOrder body):")
    print(f"  {built_request}")
    print(f"  leftover params: {leftover_params}")

    # Assertions — this is the pasteable proof: if ccxt's Kraken adapter ever
    # stops translating trailingPercent this way, this script fails loudly
    # instead of silently trusting stale documentation.
    assert built_request["ordertype"] == "trailing-stop", (
        f"Expected ordertype='trailing-stop', got {built_request.get('ordertype')!r}"
    )
    assert built_request["price"] == "+2.0000%", (
        f"Expected price='+2.0000%% (Kraken's documented relative-price format "
        f"for trailing orders), got {built_request.get('price')!r}"
    )
    assert built_request.get("trigger") == "last", (
        f"Expected trigger='last' (ccxt's default), got {built_request.get('trigger')!r}"
    )
    assert "trailingPercent" not in leftover_params, (
        "trailingPercent should have been consumed into ordertype/price, not "
        "leaked through as a raw URL param"
    )

    print()
    print("CONFIRMED: params={'trailingPercent': 'X.XXXX'} on a market-type "
          "create_order() call builds ordertype='trailing-stop' + a relative "
          "'+X.XXXX%' price field — matching Kraken's own AddOrder REST docs "
          "(docs.kraken.com/api/docs/rest-api/add-order/): ordertype enum "
          "includes 'trailing-stop' with no spot/margin restriction, and the "
          "price field is documented as exactly this '+X%' relative format "
          "for trailing order types.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
