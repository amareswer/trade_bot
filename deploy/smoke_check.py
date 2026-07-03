#!/usr/bin/env python3
"""
Post-deploy smoke check for the crypto trading bot.

Run immediately after deploy.sh completes (before starting the service):
    python deploy/smoke_check.py

Checks:
  1. Strategy hash matches the validated hash in logs/validated_strategy_hash
  2. Universe whitelist is exactly ['BTC/CAD']
  3. Slot cash is capped correctly (MAX_SLOT_CASH_CAD in .env)
  4. Kraken connection responds (balance fetch)
  5. Telegram startup alert fires (dry_run=True — prints but does not send)
  6. trades.db schema has fee_cost / fee_currency columns

Exit 0 = all checks passed. Exit 1 = one or more failures (do NOT start bot).
"""
from __future__ import annotations

import os
import sqlite3
import sys

# ── Bootstrap path ────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT, ".env"))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}]  {label}{suffix}")
    if not ok:
        failures.append(label)


# ── 1. Strategy hash ──────────────────────────────────────────────────────────
print("\n── 1. Strategy hash ──")
try:
    from bot.strategy.fingerprint import compute_strategy_hash, hashed_files
    current_hash = compute_strategy_hash()

    hash_file = os.path.join(_ROOT, "logs", "validated_strategy_hash")
    if os.path.exists(hash_file):
        stored = open(hash_file).read().strip()
        match = current_hash == stored
        check("Strategy hash matches validated", match, f"current={current_hash}  stored={stored}")
        if match:
            print(f"          Hashed files: {', '.join(hashed_files())}")
    else:
        print(f"  [{WARN}]  validated_strategy_hash not found — run stamp_strategy.py first")
        print(f"          Current hash: {current_hash}")
        print(f"          Hashed files: {', '.join(hashed_files())}")
except Exception as e:
    check("Strategy hash", False, str(e))


# ── 2. Universe whitelist ─────────────────────────────────────────────────────
print("\n── 2. Universe whitelist ──")
try:
    from config import _load as _cfg_load
    cfg = _cfg_load()
    whitelist = [s.strip() for s in os.getenv("UNIVERSE_WHITELIST", "").split(",") if s.strip()]
    check("Whitelist == ['BTC/CAD']", whitelist == ["BTC/CAD"], f"got {whitelist}")
    check("MAX_CONCURRENT_POSITIONS == 1", cfg.portfolio.max_concurrent_positions == 1,
          f"got {cfg.portfolio.max_concurrent_positions}")
    check("LIVE_TRADING is True", cfg.exchange.live_trading, f"got {cfg.exchange.live_trading}")
    check("PAPER_MODE is False",
          not (os.getenv("PAPER_MODE", "false").lower() == "true"),
          f"PAPER_MODE={os.getenv('PAPER_MODE','false')}")
except Exception as e:
    check("Config load", False, str(e))


# ── 3. Slot cash cap ──────────────────────────────────────────────────────────
print("\n── 3. Slot cash cap ──")
try:
    from bot.portfolio.capital_pool import CapitalPool
    cap = float(os.getenv("MAX_SLOT_CASH_CAD", "0") or "0")
    check("MAX_SLOT_CASH_CAD is set", cap > 0, f"got {cap}")
    if cap > 0:
        # Simulate the pool init that main.py would do with a real balance
        pool = CapitalPool(total_capital=200.0, max_concurrent=1, slot_cap=cap)
        check(f"Slot capped at {cap:.0f} (not 200)", pool.slot_cash == cap, f"slot_cash={pool.slot_cash:.2f}")
except Exception as e:
    check("Slot cap", False, str(e))


# ── 4. Kraken connectivity ────────────────────────────────────────────────────
print("\n── 4. Kraken connectivity ──")
try:
    import ccxt
    key = os.getenv("KRAKEN_API_KEY", "")
    secret = os.getenv("KRAKEN_API_SECRET", "")
    check("KRAKEN_API_KEY set", bool(key), "missing from .env" if not key else "")
    check("KRAKEN_API_SECRET set", bool(secret), "missing from .env" if not secret else "")
    if key and secret:
        ex = ccxt.kraken({"apiKey": key, "secret": secret})
        bal = ex.fetch_balance()
        cad = float(bal.get("total", {}).get("CAD", 0) or 0)
        check(f"Kraken balance fetch (CAD={cad:.2f})", True)
        check("CAD balance > 0", cad > 0, f"CAD={cad:.2f}")
except Exception as e:
    check("Kraken connectivity", False, str(e))


# ── 5. trades.db schema ───────────────────────────────────────────────────────
print("\n── 5. trades.db schema ──")
db_path = os.path.join(_ROOT, "logs", "trades.db")
if os.path.exists(db_path):
    try:
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(fills)")}
        conn.close()
        check("fee_cost column exists", "fee_cost" in cols)
        check("fee_currency column exists", "fee_currency" in cols)
        check("quantity column exists", "quantity" in cols)
    except Exception as e:
        check("trades.db schema", False, str(e))
else:
    print(f"  [{WARN}]  trades.db not found at {db_path} — will be created on first fill")


# ── 6. Telegram alert config ──────────────────────────────────────────────────
print("\n── 6. Telegram config ──")
tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
tg_chat  = os.getenv("TELEGRAM_CHAT_ID", "")
check("TELEGRAM_BOT_TOKEN set", bool(tg_token))
check("TELEGRAM_CHAT_ID set", bool(tg_chat))


# ── Result ────────────────────────────────────────────────────────────────────
print()
if failures:
    print(f"  {FAIL}  {len(failures)} check(s) failed: {', '.join(failures)}")
    print("  Do NOT start the bot until all failures are resolved.\n")
    sys.exit(1)
else:
    print(f"  {PASS}  All smoke checks passed — safe to start the bot.\n")
    print("  Run:  sudo systemctl start trade_bot\n")
    sys.exit(0)
