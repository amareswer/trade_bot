"""
Read-only Kraken balance check.

Calls fetch_balance() ONLY — no orders, no state mutation, no writes to
logs/live_state_*.json. Safe to run anytime to verify a deposit landed
before deciding on capital-gate math (CLAUDE.md "Capital Sizing Rules").

Usage:
    .venv/bin/python check_kraken_balance.py
"""
import os
import sys

import ccxt
from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    api_key = os.getenv("EXCHANGE_API_KEY") or os.getenv("KRAKEN_API_KEY") or ""
    api_secret = os.getenv("EXCHANGE_API_SECRET") or os.getenv("KRAKEN_API_SECRET") or ""
    if not api_key or not api_secret:
        print("ERROR: no API key/secret found in .env (EXCHANGE_API_KEY/EXCHANGE_API_SECRET "
              "or legacy KRAKEN_API_KEY/KRAKEN_API_SECRET).")
        return 1

    exchange = ccxt.kraken({
        "apiKey": api_key,
        "secret": api_secret,
        "timeout": 15_000,
        "enableRateLimit": True,
    })

    try:
        balance = exchange.fetch_balance()
    except Exception as exc:
        print(f"ERROR: fetch_balance() failed: {exc}")
        return 1

    free = balance.get("free", {})
    total = balance.get("total", {})
    nonzero = sorted(k for k, v in total.items() if v and float(v or 0) > 0)

    print("Kraken account balance (live, read-only):")
    for cur in nonzero:
        print(f"  {cur:6s} free={float(free.get(cur, 0) or 0):.8f}  total={float(total.get(cur, 0) or 0):.8f}")

    cad_free = float(free.get("CAD", 0) or 0)
    print(f"\nCAD free balance: ${cad_free:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
