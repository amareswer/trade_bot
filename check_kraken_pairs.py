"""
check_kraken_pairs.py — one-off script.
Connects to Kraken public API (no key needed) via ccxt and reports:
  - whether the pair exists
  - minimum order size (in base currency)
  - 24h volume in CAD
  - current bid-ask spread as a percentage
"""
import ccxt

EXCHANGE_ID = "kraken"
PAIRS = ["XRP/CAD", "DOGE/CAD"]


def pct(v):
    return f"{v*100:.4f}%"


def main():
    ex = ccxt.kraken({"enableRateLimit": True})
    print(f"\nLoading Kraken markets …")
    markets = ex.load_markets()

    for symbol in PAIRS:
        print(f"\n{'─'*52}")
        print(f"  {symbol}")
        print(f"{'─'*52}")

        if symbol not in markets:
            # Try normalised alternatives (e.g. Kraken uses XBT for BTC)
            print(f"  Pair NOT found on Kraken.")
            print(f"  Available CAD pairs:")
            cad = sorted(s for s in markets if s.endswith("/CAD"))
            for s in cad:
                print(f"    {s}")
            continue

        mkt = markets[symbol]

        # ── Minimum order size ────────────────────────────────────────────
        limits = mkt.get("limits", {})
        min_amount = limits.get("amount", {}).get("min")
        min_cost   = limits.get("cost",   {}).get("min")

        # ── Live ticker ───────────────────────────────────────────────────
        try:
            ticker = ex.fetch_ticker(symbol)
        except Exception as e:
            print(f"  ERROR fetching ticker: {e}")
            continue

        bid   = ticker.get("bid")
        ask   = ticker.get("ask")
        last  = ticker.get("last")
        vol_base = ticker.get("baseVolume")   # volume in base currency (XRP / DOGE)

        # Convert base volume to CAD using last price
        vol_cad = (vol_base * last) if (vol_base is not None and last) else None

        # Spread
        if bid and ask and bid > 0:
            spread_pct = (ask - bid) / bid
        else:
            spread_pct = None

        print(f"  Exists on Kraken:     YES")
        print(f"  Last price:           {last:.6f} CAD" if last else "  Last price:           n/a")
        print(f"  Bid:                  {bid:.6f} CAD" if bid  else "  Bid:                  n/a")
        print(f"  Ask:                  {ask:.6f} CAD" if ask  else "  Ask:                  n/a")
        print(f"  Bid-ask spread:       {pct(spread_pct)}" if spread_pct is not None else "  Bid-ask spread:       n/a")
        print(f"  Min order (base):     {min_amount} {mkt['base']}" if min_amount is not None else "  Min order (base):     n/a")
        print(f"  Min order (cost):     {min_cost} CAD" if min_cost is not None else "  Min order (cost):     n/a")
        print(f"  24h volume (base):    {vol_base:,.0f} {mkt['base']}" if vol_base else "  24h volume (base):    n/a")
        print(f"  24h volume (CAD):     {vol_cad:,.0f} CAD" if vol_cad else "  24h volume (CAD):     n/a")

    print(f"\n{'─'*52}\n")


if __name__ == "__main__":
    main()
