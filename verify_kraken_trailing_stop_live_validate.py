"""
⚠️  MAKES A REAL AUTHENTICATED KRAKEN API CALL. DO NOT ADD TO pytest OR CI.
    DO NOT RE-RUN CASUALLY — re-run only after a deliberate discussion, the
    same way this was built (see claude.md / .memory/execution_layer.md for
    the 2026-08-19 run's recorded result before deciding you need a repeat).

Unlike verify_kraken_trailing_stop_param.py (local-only, no network, safe to
re-run anytime), this script uses the REAL Kraken API keys from .env
(via config.cfg — the same cfg.exchange.api_key/api_secret bot/main.py passes
into the real LiveExecutor, not re-parsed or hardcoded here) and calls
ccxt's kraken.create_order() for real against Kraken's live AddOrder endpoint,
with Kraken's own `validate` param set to prevent execution.

WHY validate='true' (STRING) AND NOT True (Python bool) — READ BEFORE EDITING:
ccxt's kraken.py has zero special handling for `validate` (confirmed via
`grep -n validate kraken.py` — no matches) — it is not a recognized param, so
it flows straight through as a leftover into the raw request dict, which
create_order() merges into the POST body via urlencode_nested(). That helper
has NO bool->'true'/'false' string normalization (unlike ccxt's OTHER
urlencode() helper, which does) — a Python `True` becomes the literal string
"True" (capital T) in the actual bytes sent over the wire. Verified directly:

    >>> ccxt.Exchange.urlencode_nested({'validate': True})
    'validate=True'
    >>> ccxt.Exchange.urlencode_nested({'validate': 'true'})
    'validate=true'

ccxt's own kraken.py hits this exact pitfall for reduce_only/post_only and
works around it by hardcoding the lowercase string (kraken.py:2122, :2187,
comment: "not using hasattr(self, boolean) case, because the urlencodedNested
transforms it into 'True' string"). If Kraken's parser doesn't accept "True"
as truthy and silently defaults validate to false, this call would place a
REAL order instead of validating one. This script ONLY ever passes the
string 'true' for this reason — do not "simplify" it to True.

Expected outcome going in: logs/live_state_BTC_CAD.json shows position=0.0
BTC. A native trailing-stop backstop is always a SELL (it closes a long), so
validating a SELL against a zero position will very plausibly be rejected
for insufficient funds — a DIFFERENT thing from a malformed-shape rejection.
This script prints Kraken's literal response/error text and does NOT try to
auto-classify it — a human reads the actual text.

Run (only when explicitly told to): .venv/bin/python verify_kraken_trailing_stop_live_validate.py --i-understand-this-makes-a-real-kraken-api-call
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
    print(f"cfg.exchange.exchange = {cfg.exchange.exchange}")
    print(f"cfg.exchange.symbol   = {cfg.exchange.symbol}")
    if cfg.exchange.exchange != "kraken":
        print(f"ABORT: cfg.exchange.exchange is {cfg.exchange.exchange!r}, not 'kraken' — "
              f"this script is Kraken-specific.")
        return 1
    if not cfg.exchange.api_key or not cfg.exchange.api_secret:
        print("ABORT: cfg.exchange.api_key / api_secret are empty — check .env.")
        return 1

    ex = ccxt.kraken({
        "apiKey": cfg.exchange.api_key,
        "secret": cfg.exchange.api_secret,
    })

    # Public call — real-time size the same way the live bot's own slot cap
    # would produce a quantity (MAX_SLOT_CASH_CAD is the hard per-slot
    # ceiling; this mirrors the actual position size the bot could hold).
    ticker = ex.fetch_ticker(cfg.exchange.symbol)
    price = float(ticker["last"])
    slot_cash = cfg.portfolio.max_slot_cash_cad
    quantity = round(slot_cash / price, 6)
    print(f"Live {cfg.exchange.symbol} price: {price}")
    print(f"MAX_SLOT_CASH_CAD: {slot_cash}  ->  quantity: {quantity}")

    trailing_params = {"trailingPercent": "2.0000", "validate": "true"}

    # Local preview first — show the EXACT request this will build, before
    # sending it, using the same order_request() the real create_order()
    # call below will invoke internally.
    preview_request = {
        "pair": ex.market(cfg.exchange.symbol)["id"],
        "type": "sell",
        "ordertype": "market",
        "volume": ex.amount_to_precision(cfg.exchange.symbol, quantity),
    }
    built, leftover = ex.order_request(
        "createOrder", cfg.exchange.symbol, "market", dict(preview_request),
        amount=quantity, price=None, params=dict(trailing_params),
    )
    print()
    print("Request that will be sent (local preview via order_request(), before firing):")
    print(f"  base fields (from create_order):        {preview_request}")
    print(f"  ccxt-built trailing fields (merged in):  {built}")
    print(f"  raw params merged straight through:      {leftover}")
    assert built["ordertype"] == "trailing-stop"
    assert built["price"] == "+2.0000%"
    assert leftover.get("validate") == "true", (
        f"validate did not survive as the string 'true' — got {leftover.get('validate')!r}. "
        f"ABORTING before making the real call."
    )

    print()
    print("Firing the real authenticated create_order() call now...")
    try:
        raw = ex.create_order(
            cfg.exchange.symbol, "market", "sell", quantity,
            params=trailing_params,
        )
        print()
        print("Kraken's raw response (SUCCESS path — validate=true means this")
        print("should NOT have executed a real trade):")
        print(f"  {raw}")
    except Exception as exc:
        print()
        print(f"Kraken REJECTED the request. Exception type: {type(exc).__name__}")
        print(f"Literal exception text: {exc}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
