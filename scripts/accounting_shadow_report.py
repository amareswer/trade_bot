"""
Read-only accounting shadow-mode inspection.

Opens the existing trades.db (bot/accounting/store.py's tables, layered on
bot/data/trade_log.py's existing DB — no separate database) and prints the
operator-facing numbers the paper/shadow readiness gate cares about:
unlinked fills (residuals), fee corrections applied, and the watermark
checkpoint per currency scope. Makes NO network call, places NO order,
mutates NOTHING — safe to run at any time, live or shadow.

This does NOT reproduce the four-way verification's `ready`/`explain()`
verdict (bot/accounting/four_way.py) — that needs live executor position
state that only bot.main.run() has at tick time. For the four-way verdict
and the BlockState reason string, check the bot's own log/Telegram alerts
for "ACCOUNTING RECONCILIATION" (bot/main.py's per-cycle alert on a
non-ready state) — this script is a standalone SQLite-only supplement,
useful for a quick manual check without tailing logs.

Usage:
    .venv/bin/python scripts/accounting_shadow_report.py [SYMBOL ...]

With no SYMBOL args, uses UNIVERSE_WHITELIST from .env (comma-separated),
falling back to the single configured SYMBOL.
"""
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.accounting import store as accounting_store  # noqa: E402

load_dotenv()


def _symbols_from_args_or_env() -> "list[str]":
    if len(sys.argv) > 1:
        return sys.argv[1:]
    whitelist = os.getenv("UNIVERSE_WHITELIST", "")
    if whitelist.strip():
        return [s.strip() for s in whitelist.split(",") if s.strip()]
    sym = os.getenv("SYMBOL", "")
    return [sym] if sym else []


def main() -> int:
    symbols = _symbols_from_args_or_env()
    if not symbols:
        print("ERROR: no symbols found — pass them as args or set UNIVERSE_WHITELIST/SYMBOL in .env")
        return 1

    accounting_store.init_db()
    conn = accounting_store.connect()

    print("Accounting shadow report (read-only, local SQLite only — no network call)")
    print("=" * 70)

    total_unlinked = 0
    for sym in symbols:
        unlinked = accounting_store.unlinked_fills(conn, sym)
        total_unlinked += len(unlinked)
        print(f"\n[{sym}]")
        print(f"  Unlinked fills (residuals): {len(unlinked)}")
        for row in unlinked[:10]:
            print(f"    id={row.get('id')}  ts={row.get('timestamp')}  "
                  f"side={row.get('side')}  qty={row.get('quantity')}  "
                  f"notes={row.get('notes')!r}")
        if len(unlinked) > 10:
            print(f"    ... and {len(unlinked) - 10} more")

        quote = os.getenv("UNIVERSE_QUOTE", "") or (sym.split("/")[1] if "/" in sym else "")
        if quote:
            ckpt = accounting_store.latest_checkpoint(conn, quote)
            if ckpt:
                print(f"  Last checkpoint ({quote}): {ckpt.get('computed_at', ckpt)}")
            else:
                print(f"  Last checkpoint ({quote}): NONE — no reconciliation cycle has "
                      f"completed for this scope yet")

    fee_corrections = accounting_store.all_fee_correction_adjustment_ids(conn)
    print(f"\nFee corrections applied (all-time, all symbols): {len(fee_corrections)}")

    print("\n" + "=" * 70)
    if total_unlinked == 0:
        print("PASS on this local check: zero unlinked fills across the symbols checked.")
    else:
        print(f"ATTENTION: {total_unlinked} unlinked fill(s) — investigate before treating "
              f"the shadow run as clean. A fill can be unlinked transiently between "
              f"observation and the next reconciliation cycle; persistent unlinked rows "
              f"across multiple runs of this script are the 'unexplained residual' the "
              f"paper/shadow gate must show zero of.")
    print("\nThis script does not check the four-way `ready` verdict or BlockState — "
          "confirm those from the bot's own log/Telegram 'ACCOUNTING RECONCILIATION' "
          "alerts (silence = last cycle was ready; an alert names what's blocking).")
    return 0 if total_unlinked == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
