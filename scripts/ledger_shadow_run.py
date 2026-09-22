"""
Shadow-only ledger reconciliation runner.

Off by default: does nothing unless LEDGER_SHADOW_ENABLED=true is set in
the environment. NOT wired into bot/main.py, does not read or write
logs/HALT, logs/risk_state.json, or logs/trades.db, and has no effect on
any trading decision — this script cannot even express one; it has no
import relationship with bot.main, reconciliation, four_way, or any
execution/risk module.

Two fetch modes:
  --fixture   Runs one shadow cycle against a small built-in offline
              fixture (no live calls, no network) — proves the wiring
              (fetch -> persist -> reconcile -> publish) end to end.
  --live      Runs one shadow cycle against a REAL, read-only Kraken
              Ledgers fetch + a REAL, read-only balance read, via
              bot/accounting/kraken_ledger_fetch.py (pagination-with-
              coverage-proof against ccxt's privatePostLedgers, id taken
              from the response envelope's own dictionary key, asset
              scoped + normalized via resolve_asset_alias_group(); balance
              read strictly AFTER the ledger is fetched and persisted).
              Building this adapter and exercising it against real,
              committed fixtures was done offline (tests/crypto/
              test_kraken_ledger_fetch.py) — actually invoking --live
              against a real account is a separate, later, explicit
              decision by whoever runs this command; nothing in this
              repository or its test suite ever does so.

              Without --zero-opening-confirmed, a real account's first
              ever shadow cycle correctly reports overall_pass=False
              (opening_balance_verified=False) — there is no assumed
              anchor point for a real account's pre-existing history.
              Pass --zero-opening-confirmed ONLY for a genuinely
              brand-new account with zero pre-existing balance in this
              asset; a nonzero verified opening checkpoint is not yet
              exposed as a CLI option (a documented gap, not a silent one).

Storage: --fixture and --live default to SEPARATE directories
(logs/shadow/ledger_reconciliation/fixture/ and .../live/) so the two
documented example commands below never collide by default. This is a
convenience, not the actual safety guarantee — run_shadow_cycle() itself
stamps every database with the evidence_mode it was first used with
(EvidenceModeConflict, in bot/accounting/ledger_shadow_run.py) and refuses
to run a cycle of the OTHER mode against it, even under an explicit --db
override that names the same file for both. Neither default ever points
at production trades.db.

Usage:
    LEDGER_SHADOW_ENABLED=true .venv/bin/python scripts/ledger_shadow_run.py --fixture
    LEDGER_SHADOW_ENABLED=true .venv/bin/python scripts/ledger_shadow_run.py --live --asset BTC
"""
import argparse
import os
import sys
from decimal import Decimal

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from bot.accounting.ledger_quantity_reconciliation import LedgerEntry  # noqa: E402
from bot.accounting.ledger_shadow_run import is_shadow_enabled, run_shadow_cycle  # noqa: E402

_SHADOW_ROOT = os.path.join(_PROJECT_ROOT, "logs", "shadow", "ledger_reconciliation")
_MODE_DEFAULTS = {
    "fixture": {
        "db": os.path.join(_SHADOW_ROOT, "fixture", "observations.db"),
        "status": os.path.join(_SHADOW_ROOT, "fixture", "status.json"),
        "account_id": "shadow-fixture",
    },
    "live": {
        "db": os.path.join(_SHADOW_ROOT, "live", "observations.db"),
        "status": os.path.join(_SHADOW_ROOT, "live", "status.json"),
        "account_id": "kraken:trade_bot_local",
    },
}

# Matched exactly so the fixture cycle reconciles trusted=True — proves the
# plumbing, not any claim about a real account's actual balance.
_FIXTURE_WALLET_BALANCE = Decimal("0.0001")
_FIXTURE_WALLET_READ_AT = "2026-01-01T00:00:01.000000Z"


def _fixture_fetch(*, account_id: str, batch_id: str, observed_at: str):
    """A tiny, self-contained offline fixture — no network access. Only
    demonstrates that the pipeline itself runs correctly end to end; not
    a substitute for real data and never asserted to be."""
    return [
        LedgerEntry(
            ledger_id="FIXTURE-1", reference_id="FIXTURE-REF-1", account_id=account_id,
            type="deposit", asset="XXBT", amount_raw="0.0001", fee_raw="0",
            balance_raw="0.0001", exchange_timestamp="2026-01-01T00:00:00.000000Z",
            observed_at=observed_at, batch_id=batch_id,
        ),
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixture", action="store_true", help="Run one cycle against the offline fixture (no live calls).")
    parser.add_argument("--live", action="store_true", help="Run one cycle against real, read-only Kraken data.")
    parser.add_argument("--db", default=None, help="Isolated shadow SQLite path. Defaults to a mode-specific "
                                                     "path under logs/shadow/ledger_reconciliation/ — never "
                                                     "production trades.db, and never shared between modes "
                                                     "even if you override this (see EvidenceModeConflict).")
    parser.add_argument("--status", default=None, help="Isolated shadow status JSON path. Same mode-specific "
                                                         "default behavior as --db.")
    parser.add_argument("--account-id", default=None, help="Defaults to a mode-specific identity.")
    parser.add_argument("--asset", default="XXBT",
                         help="Any recognized alias or raw Kraken code (BTC, XBT, and XXBT are all "
                              "equivalent and resolve identically) — for --fixture, the raw code the "
                              "built-in fixture already uses.")
    parser.add_argument("--zero-opening-confirmed", action="store_true",
                         help="Assert this account genuinely started at zero for --asset. "
                              "Only correct for a brand-new account — see module docstring.")
    args = parser.parse_args(argv)

    if not is_shadow_enabled():
        print("LEDGER_SHADOW_ENABLED is not set to a truthy value — shadow run is off by default. Nothing was done.")
        return 0

    if args.live:
        mode = "live"
        from bot.accounting.kraken_ledger_fetch import (
            build_exchange, build_live_fetch_fn, build_read_wallet_balance_fn, resolve_asset_alias_group,
        )
        ex = build_exchange()   # the one point in this script that can make a real, read-only API call
        fetch_fn = build_live_fetch_fn(ex, asset=args.asset)
        read_wallet_balance_fn = build_read_wallet_balance_fn(ex, asset=args.asset)
        wallet_balance, wallet_read_at = None, None   # supplied instead via read_wallet_balance_fn, after persist
        persistence_asset = resolve_asset_alias_group(args.asset)[0]
    elif args.fixture:
        mode = "fixture"
        fetch_fn = _fixture_fetch
        read_wallet_balance_fn = None
        wallet_balance, wallet_read_at = _FIXTURE_WALLET_BALANCE, _FIXTURE_WALLET_READ_AT
        persistence_asset = args.asset
    else:
        print("Specify --fixture (offline) or --live (real, read-only Kraken data).")
        return 1

    defaults = _MODE_DEFAULTS[mode]
    db_path = args.db if args.db is not None else defaults["db"]
    status_path = args.status if args.status is not None else defaults["status"]
    account_id = args.account_id if args.account_id is not None else defaults["account_id"]

    result = run_shadow_cycle(
        fetch_fn=fetch_fn, account_id=account_id, asset=persistence_asset,
        db_path=db_path, status_path=status_path, evidence_mode=mode,
        wallet_balance_at_read=wallet_balance, wallet_balance_read_at=wallet_read_at,
        read_wallet_balance_fn=read_wallet_balance_fn,
        zero_opening_confirmed=args.zero_opening_confirmed if args.live else True,
    )
    print(f"mode={mode} observation_id={result.observation_id} fetch_succeeded={result.fetch_succeeded} "
          f"trusted={result.trusted} reason={result.reason}")
    print(f"status published to {status_path}")
    return 0 if result.trusted else 1


if __name__ == "__main__":
    raise SystemExit(main())
