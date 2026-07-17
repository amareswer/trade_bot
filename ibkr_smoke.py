"""
IBKR executor smoke test — requires a running, logged-in TWS/Gateway (paper).

Read-only by default: connects through the REAL IBKRExecutor, prints account,
cash, net liquidation, positions, and exercises every read path main.py uses.

With --trade SYMBOL it places a 1-share market BUY then immediately SELLs it
back (paper account only — the executor's own guards refuse live accounts).
Run only while the symbol's market is open, e.g.:

    .venv/bin/python ibkr_smoke.py                # read-only checks
    .venv/bin/python ibkr_smoke.py --trade KO     # 1-share round trip
"""
from __future__ import annotations

import argparse
import sys

from stock_bot.execution.ibkr import IBKRExecutor
from stock_bot.execution.base import OrderStatus


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=42)
    ap.add_argument("--trade", metavar="SYMBOL",
                    help="place a 1-share BUY then SELL round trip (paper only)")
    args = ap.parse_args()

    print(f"Connecting to TWS at {args.host}:{args.port} …")
    ex = IBKRExecutor(host=args.host, port=args.port, client_id=args.client_id)

    try:
        print("\n── Read paths ──")
        print(f"  cash            : ${ex.cash:,.2f}")
        print(f"  starting_cash   : ${ex.starting_cash:,.2f}")
        print(f"  realized_pnl    : ${ex.realized_pnl():,.2f}")
        snap = ex.positions_snapshot()
        print(f"  positions       : {snap or '(none)'}")
        print(f"  sector exposure : {ex.get_sector_exposure() or '(none)'}")
        print(f"  check_exposure  : {ex.check_exposure({})}")
        ex.log_state({})

        if args.trade:
            sym = args.trade.upper()
            print(f"\n── 1-share round trip on {sym} (paper) ──")
            # price arg feeds the sanity gates only — the fill price comes
            # from the broker; $1.00 clears every gate for a 1-share test
            buy = ex.buy(sym, 1, 1.0, reason="ibkr_smoke round trip")
            if buy.status != OrderStatus.FILLED:
                print(f"  BUY not filled: {buy.reject_reason}")
                return 1
            print(f"  BUY  filled @ ${buy.price:,.2f}")
            sell = ex.sell(sym, 1, buy.price, reason="ibkr_smoke round trip")
            if sell.status != OrderStatus.FILLED:
                print(f"  SELL not filled: {sell.reject_reason} — "
                      f"POSITION LEFT OPEN, close manually in TWS")
                return 1
            print(f"  SELL filled @ ${sell.price:,.2f}")
            print(f"  round-trip P&L: ${(sell.price - buy.price) * 1:,.2f}")

        print("\nSmoke test OK")
        return 0
    finally:
        ex.disconnect()


if __name__ == "__main__":
    sys.exit(main())
