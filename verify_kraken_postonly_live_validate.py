"""
⚠️  MAKES A REAL AUTHENTICATED KRAKEN API CALL. DO NOT ADD TO pytest OR CI.
    DO NOT RE-RUN CASUALLY — re-run only after a deliberate discussion.

Real-server round-trip proof for the postOnly fix in _place_limit_order()
(bot/execution/live_executor.py), companion to the local, no-network
verify_kraken_postonly_param.py. Same reasoning as
verify_kraken_trailing_stop_live_validate.py: a local order_request() check
proves what ccxt WOULD build, but only Kraken's own server can confirm it
actually accepts the request — this fires the real AddOrder endpoint with
Kraken's `validate` param set so nothing executes.

Background: _place_limit_order() sent {"timeInForce": "PO"} for two months
(since commit 08644b1f, 2026-06-22) — Kraken rejected every attempt with
EGeneral:Invalid arguments:timeinforce, silently falling back to market
every time (confirmed live 2026-08-26 on SOL/CAD's first fill). Fixed to
{"postOnly": True} — verified locally in verify_kraken_postonly_param.py.
This script confirms the FIXED param against the real server.

WHY validate='true' (STRING) AND NOT True (Python bool) — same pitfall as
the trailing-stop script: ccxt's urlencode_nested() has no bool->string
normalization, so a Python True becomes the literal string "True" on the
wire, not Kraken's documented lowercase "true". Only ever pass the string.

Run (only when explicitly told to): .venv/bin/python verify_kraken_postonly_live_validate.py --i-understand-this-makes-a-real-kraken-api-call
"""
from __future__ import annotations

import sys

import ccxt

from config import cfg

CONFIRM_FLAG = "--i-understand-this-makes-a-real-kraken-api-call"


def main() -> int:
    if CONFIRM_FLAG not in sys.argv:
        print(
            "Refusing to run without the explicit confirmation flag — this "
            "makes a real authenticated call to Kraken's live AddOrder "
            f"endpoint. Re-run with:\n  {sys.argv[0]} {CONFIRM_FLAG}"
        )
        return 1

    print(f"ccxt.__version__ = {ccxt.__version__}")
    if cfg.exchange.exchange != "kraken":
        print(f"ABORT: cfg.exchange.exchange is {cfg.exchange.exchange!r}, not 'kraken'.")
        return 1
    if not cfg.exchange.api_key or not cfg.exchange.api_secret:
        print("ABORT: cfg.exchange.api_key / api_secret are empty — check .env.")
        return 1

    ex = ccxt.kraken({
        "apiKey": cfg.exchange.api_key,
        "secret": cfg.exchange.api_secret,
    })

    # SOL/CAD, not the .env default BTC/CAD — this is the symbol the real
    # incident happened on (2026-08-26), sized to its actual live slot cap.
    symbol    = "SOL/CAD"
    slot_cash = cfg.portfolio.max_slot_cash_cad_by_base.get("SOL", 376.0)
    ticker    = ex.fetch_ticker(symbol)
    price     = float(ticker["bid"])  # _place_limit_order uses the bid for a BUY
    quantity  = round((slot_cash * 0.98) / price, 6)
    print(f"Live {symbol} bid: {price}")
    print(f"SOL slot cash: {slot_cash}  ->  quantity: {quantity}")

    limit_price = ex.price_to_precision(symbol, price * 1.0001)  # matches the tiny tick offset _place_limit_order uses
    params      = {"postOnly": True, "validate": "true"}

    # Local preview first — same order_request() the real create_order()
    # call below invokes internally.
    preview_request = {
        "pair":      ex.market(symbol)["id"],
        "type":      "buy",
        "ordertype": "limit",
        "volume":    ex.amount_to_precision(symbol, quantity),
        "price":     limit_price,
    }
    built, leftover = ex.order_request(
        "createOrder", symbol, "limit", dict(preview_request),
        amount=quantity, price=float(limit_price), params=dict(params),
    )
    print()
    print("Request that will be sent (local preview via order_request(), before firing):")
    print(f"  base fields:              {preview_request}")
    print(f"  ccxt-built fields merged: {built}")
    print(f"  raw params merged through: {leftover}")
    assert "timeinforce" not in built, f"timeinforce leaked in: {built.get('timeinforce')!r}"
    assert built.get("oflags") == "post", f"expected oflags='post', got {built.get('oflags')!r}"
    assert leftover.get("validate") == "true", (
        f"validate did not survive as the string 'true' — got {leftover.get('validate')!r}. "
        f"ABORTING before making the real call."
    )

    print()
    print("Firing the real authenticated create_order() call now (validate=true — no execution)...")
    try:
        result = ex.create_order(symbol, "limit", "buy", quantity, float(limit_price), params)
    except Exception as exc:
        print()
        print(f"Kraken/ccxt raised: {type(exc).__name__}: {exc}")
        print()
        print("Read this text yourself — a validate=true rejection for an "
              "unrelated reason (e.g. balance) is a DIFFERENT thing from a "
              "malformed-shape rejection. Do not auto-classify.")
        return 1

    print()
    print("Kraken's literal response:")
    print(f"  {result}")
    print()
    print("Check: result['id'] should be None/absent (validate-only, nothing "
          "executed) and result['info'] should echo back a well-formed "
          "'buy ... @ limit ...' description with no error.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
