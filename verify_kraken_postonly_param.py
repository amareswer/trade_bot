"""
Standalone, re-runnable proof of the post-only limit-order bug found 2026-08-26
(SOL/CAD's first live BUY fell back to a market order) and of the fix.

_place_limit_order() (bot/execution/live_executor.py, introduced commit
08644b1f, 2026-06-22) has been calling:
    self._exchange.create_order(symbol, "limit", side, qty, price,
                                 {"timeInForce": "PO"})
intending to request a Kraken post-only limit order. ccxt's Kraken adapter
treats timeInForce and postOnly as two SEPARATE unified params — timeInForce
is passed through nearly verbatim to Kraken's own `timeinforce` field (which
only accepts GTC/IOC/GTD), while post-only is a distinct concept requested via
the `postOnly` param, translated to `oflags=post`. Passing "PO" as a
timeInForce value is not a valid GTC/IOC/GTD, so Kraken's real API rejects
it — confirmed live 2026-08-26: `EGeneral:Invalid arguments:timeinforce`,
caught by the generic exception handler, silently falling back to a market
order every time. The only network call this script makes is the public,
unauthenticated load_markets() (needed to resolve SOL/CAD's price precision)
— order_request() itself is a pure request-dict builder, so this checks the
REAL installed library's actual behavior, not a mock.

Run: .venv/bin/python verify_kraken_postonly_param.py
Exits non-zero if a future ccxt upgrade ever changes this mapping.
"""
from __future__ import annotations

import sys

import ccxt

EXPECTED_CCXT_VERSION = "4.5.56"  # requirements.lock.txt pin as of this writing


def main() -> int:
    print(f"ccxt.__version__ = {ccxt.__version__}")
    if ccxt.__version__ != EXPECTED_CCXT_VERSION:
        print(
            f"NOTE: installed ccxt ({ccxt.__version__}) differs from the "
            f"version this script was last verified against "
            f"({EXPECTED_CCXT_VERSION}). Re-verify below, don't just trust "
            f"a green run.",
        )

    ex = ccxt.kraken({"apiKey": "dummy", "secret": "ZHVtbXk="})
    # load_markets() is the one network call this script makes — a public,
    # unauthenticated endpoint (no API key actually used, market data only).
    # order_request() needs it loaded to resolve SOL/CAD's price precision.
    ex.load_markets()

    base_request = {
        "pair": "SOLCAD",
        "type": "buy",
        "ordertype": "limit",
        "volume": "0.08080800",
        "price": "134.01",
    }

    # ── The BUGGY call, reproduced exactly as _place_limit_order() sends it ──
    buggy_request, buggy_leftover = ex.order_request(
        "createOrder", "SOL/CAD", "limit", dict(base_request),
        amount=0.080808, price=134.01,
        params={"timeInForce": "PO"},
    )
    print()
    print("BUGGY  params={'timeInForce': 'PO'}  ->")
    print(f"  {buggy_request}")
    print(f"  leftover params: {buggy_leftover}")
    assert buggy_request.get("timeinforce") == "PO", (
        "Expected the buggy call to literally place the invalid 'PO' string "
        "into Kraken's timeinforce field — if this no longer holds, the "
        "live 'Invalid arguments:timeinforce' error may have a different "
        "cause than documented here."
    )
    # Surprise (found while writing this script): ccxt's Kraken adapter ALSO
    # recognizes "PO" as its own unified post-only shorthand internally
    # (self.handle_post_only()), so oflags='post' gets set correctly too —
    # the buggy call sends BOTH fields. Kraken's server rejects the whole
    # request because of the spurious raw timeinforce='PO' (not a valid
    # GTC/IOC/GTD), even though oflags was also right. This doesn't change
    # the diagnosis or the fix — postOnly=True alone is still the clean,
    # correct, minimal request — it just means the intent was being
    # half-translated correctly by ccxt, not that oflags was ever missing.
    assert buggy_request.get("oflags") == "post", (
        f"Expected ccxt to ALSO derive oflags='post' from timeInForce='PO' "
        f"via its own unified shorthand (this is what makes the bug subtle "
        f"— the intent partially worked), got {buggy_request.get('oflags')!r}"
    )

    # ── The FIXED call — postOnly is its own param, not a timeInForce value ──
    fixed_request, fixed_leftover = ex.order_request(
        "createOrder", "SOL/CAD", "limit", dict(base_request),
        amount=0.080808, price=134.01,
        params={"postOnly": True},
    )
    print()
    print("FIXED  params={'postOnly': True}  ->")
    print(f"  {fixed_request}")
    print(f"  leftover params: {fixed_leftover}")
    assert "timeinforce" not in fixed_request, (
        f"Expected no timeinforce field at all, got "
        f"{fixed_request.get('timeinforce')!r}"
    )
    assert "post" in fixed_request.get("oflags", ""), (
        f"Expected oflags to contain 'post' (Kraken's real post-only flag), "
        f"got {fixed_request.get('oflags')!r}"
    )
    assert "postOnly" not in fixed_leftover, (
        "postOnly should have been consumed into oflags, not leaked through "
        "as a raw URL param"
    )

    print()
    print("CONFIRMED: {'timeInForce': 'PO'} builds an invalid Kraken request "
          "(timeinforce='PO', not a real GTC/IOC/GTD value — matches the "
          "live 'Invalid arguments:timeinforce' error from Kraken 2026-08-26). "
          "{'postOnly': True} builds the correct request (oflags contains "
          "'post', no timeinforce field at all) — matching Kraken's own "
          "AddOrder REST docs (docs.kraken.com/api/docs/rest-api/add-order/): "
          "post-only is requested via the oflags='post' order flag, not the "
          "timeinforce field.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
