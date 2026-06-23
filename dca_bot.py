"""
DCA (Dollar-Cost Averaging) bot — standalone, separate from the live trading bot.

Runs independently: separate process, separate state file, never touches
bot/main.py, the live executor, or any live trading state.

Usage:
    DCA_DRY_RUN=true python dca_bot.py         # dry-run buy (default)
    python dca_bot.py                          # live buy if DCA_DRY_RUN=false in .env
    python dca_bot.py --report                 # show buy history (no network calls)

State file:  logs/dca_state.json
Config:      read from .env (see CONFIG section below)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

def _float(key: str, default: float) -> float:
    raw = os.getenv(key)
    return float(raw.strip()) if raw and raw.strip() else default

def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    return int(raw.strip()) if raw and raw.strip() else default

def _str(key: str, default: str) -> str:
    return (os.getenv(key) or default).strip()

def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes")


DCA_AMOUNT_CAD         = _float("DCA_AMOUNT_CAD",          50.0)
DCA_INTERVAL_DAYS      = _int  ("DCA_INTERVAL_DAYS",        7)
DCA_SYMBOL             = _str  ("DCA_SYMBOL",               "BTC/CAD")
DCA_EXCHANGE           = _str  ("DCA_EXCHANGE",             "kraken")
DCA_SKIP_IF_RSI_ABOVE  = _float("DCA_SKIP_IF_RSI_ABOVE",   75.0)
DCA_SKIP_IF_BEARISH    = _bool ("DCA_SKIP_IF_DAILY_BEARISH", True)
DCA_DRY_RUN            = _bool ("DCA_DRY_RUN",              True)

KRAKEN_API_KEY    = _str("KRAKEN_API_KEY",    "")
KRAKEN_API_SECRET = _str("KRAKEN_API_SECRET", "")

_STATE_PATH = os.path.join(os.path.dirname(__file__), "logs", "dca_state.json")

# ── State I/O ─────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not os.path.exists(_STATE_PATH):
        return {"total_invested": 0.0, "total_units": 0.0, "last_buy_date": None, "buys": []}
    with open(_STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    with open(_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── Indicators ────────────────────────────────────────────────────────────────

def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(closes: list[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


# ── Exchange ──────────────────────────────────────────────────────────────────

def _build_exchange(live: bool):
    import ccxt
    cls = getattr(ccxt, DCA_EXCHANGE.lower())
    if live:
        return cls({"apiKey": KRAKEN_API_KEY, "secret": KRAKEN_API_SECRET, "timeout": 20_000})
    return cls({"timeout": 20_000})


def _fetch_price(exchange) -> float:
    ticker = exchange.fetch_ticker(DCA_SYMBOL)
    return float(ticker["last"])


def _fetch_daily_closes(exchange, limit: int = 30) -> list[float]:
    raw = exchange.fetch_ohlcv(DCA_SYMBOL, timeframe="1d", limit=limit + 1)
    # drop the still-forming candle
    return [float(row[4]) for row in raw[:-1]]


# ── Report ────────────────────────────────────────────────────────────────────

def report() -> None:
    state = _load_state()
    buys  = state.get("buys", [])

    print("\n  DCA REPORT — logs/dca_state.json")
    print("  " + "─" * 60)

    if not buys:
        print("  No buys recorded yet.\n")
        return

    print(f"  {'Date':<12}  {'Price':>12}  {'Units':>12}  {'Amount CAD':>12}  {'Fee':>8}")
    print("  " + "─" * 60)
    for b in buys:
        print(
            f"  {b['date']:<12}  "
            f"${b['price']:>11,.2f}  "
            f"{b['units']:>12.6f}  "
            f"${b['amount_cad']:>11.2f}  "
            f"${b.get('fee', 0.0):>7.4f}"
        )

    total_invested = state.get("total_invested", 0.0)
    total_units    = state.get("total_units",    0.0)
    avg_cost       = total_invested / total_units if total_units > 0 else 0.0
    last_price     = buys[-1]["price"] if buys else 0.0
    current_value  = total_units * last_price
    unrealized     = current_value - total_invested
    unrealized_pct = unrealized / total_invested * 100 if total_invested > 0 else 0.0

    print("  " + "─" * 60)
    print(f"  Total invested:  ${total_invested:,.2f} CAD  ({len(buys)} buys)")
    print(f"  Total units:      {total_units:.6f} {DCA_SYMBOL.split('/')[0]}")
    print(f"  Avg cost basis:  ${avg_cost:,.2f}")
    print(f"  Last fill price: ${last_price:,.2f}  (use --report after market hours for accuracy)")
    pnl_sym = "+" if unrealized >= 0 else ""
    print(f"  Unrealized P&L:  {pnl_sym}${unrealized:,.2f}  ({pnl_sym}{unrealized_pct:.2f}%)")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> None:
    state    = _load_state()
    today    = date.today().isoformat()
    last_buy = state.get("last_buy_date")

    # 1. Check if due
    if last_buy is not None:
        last_date  = date.fromisoformat(last_buy)
        next_date  = last_date.fromordinal(last_date.toordinal() + DCA_INTERVAL_DAYS)
        today_date = date.today()
        if today_date < next_date:
            print(f"\n  DCA not due yet.  Next buy: {next_date.isoformat()}\n")
            return

    # 2. Fetch price
    exchange = _build_exchange(live=not DCA_DRY_RUN)
    try:
        price = _fetch_price(exchange)
    except Exception as exc:
        print(f"\n  ERROR: could not fetch price — {exc}\n")
        return

    print(f"\n  Current {DCA_SYMBOL} price: ${price:,.2f}")

    # 3. RSI filter
    if DCA_SKIP_IF_RSI_ABOVE > 0:
        try:
            closes = _fetch_daily_closes(exchange, limit=20)
            rsi_val = _rsi(closes)
            if rsi_val is not None:
                print(f"  RSI (14d): {rsi_val:.1f}", end="")
                if rsi_val > DCA_SKIP_IF_RSI_ABOVE:
                    print(f"  — SKIP: RSI {rsi_val:.1f} > {DCA_SKIP_IF_RSI_ABOVE:.0f} (overbought)\n")
                    return
                print()
        except Exception as exc:
            print(f"  RSI check failed ({exc}) — skipping RSI filter")

    # 4. Trend filter
    if DCA_SKIP_IF_BEARISH:
        try:
            closes = _fetch_daily_closes(exchange, limit=30)
            ema9  = _ema(closes, 9)
            ema21 = _ema(closes, 21)
            if ema9 is not None and ema21 is not None:
                trend = "BULLISH" if ema9 >= ema21 else "BEARISH"
                print(f"  Daily trend:  EMA9={ema9:,.2f}  EMA21={ema21:,.2f}  → {trend}", end="")
                if trend == "BEARISH":
                    print(f"  — SKIP: daily trend BEARISH\n")
                    return
                print()
        except Exception as exc:
            print(f"  Trend check failed ({exc}) — skipping trend filter")

    # 5. Calculate units
    units = round(DCA_AMOUNT_CAD / price, 6)

    # 6 & 7. Dry run or live order
    fee         = 0.0
    fill_price  = price
    fill_units  = units

    if DCA_DRY_RUN:
        print(f"\n  *** DRY RUN — no real order placed ***")
        print(f"  Would BUY {units:.6f} {DCA_SYMBOL.split('/')[0]} @ ${price:,.2f}  (${DCA_AMOUNT_CAD:.2f} CAD)")
    else:
        if not KRAKEN_API_KEY or not KRAKEN_API_SECRET:
            print("\n  ERROR: KRAKEN_API_KEY / KRAKEN_API_SECRET not set in .env\n")
            return
        try:
            order = exchange.create_market_buy_order(
                DCA_SYMBOL,
                units,
                params={"oflags": "fciq"},  # quote currency (CAD)
            )
            # Extract actual fill details from order response
            fill_price = float(order.get("average") or order.get("price") or price)
            fill_units = float(order.get("filled") or order.get("amount") or units)
            _fee_info  = order.get("fee") or {}
            fee        = float(_fee_info.get("cost", 0.0))
            print(f"\n  LIVE BUY FILLED: {fill_units:.6f} {DCA_SYMBOL.split('/')[0]} @ ${fill_price:,.2f}")
        except Exception as exc:
            print(f"\n  ERROR placing order: {exc}\n")
            return

    # 8. Update state
    entry = {
        "date":       today,
        "price":      fill_price,
        "units":      fill_units,
        "amount_cad": DCA_AMOUNT_CAD,
        "fee":        fee,
    }
    state["buys"].append(entry)
    state["total_invested"] = round(state.get("total_invested", 0.0) + DCA_AMOUNT_CAD, 6)
    state["total_units"]    = round(state.get("total_units", 0.0) + fill_units, 6)
    state["last_buy_date"]  = today
    _save_state(state)

    # Print summary
    total_invested = state["total_invested"]
    total_units    = state["total_units"]
    avg_cost       = total_invested / total_units if total_units > 0 else 0.0
    current_value  = total_units * fill_price
    unrealized     = current_value - total_invested
    unrealized_pct = unrealized / total_invested * 100 if total_invested > 0 else 0.0
    pnl_sym        = "+" if unrealized >= 0 else ""

    print()
    print(f"  DCA {'DRY RUN' if DCA_DRY_RUN else 'BUY'}: "
          f"{fill_units:.6f} {DCA_SYMBOL} @ ${fill_price:,.2f}  "
          f"| Cost: ${DCA_AMOUNT_CAD:.2f} CAD  "
          f"| Fee: ${fee:.4f}")
    print(f"  Total invested:   ${total_invested:,.2f}  | Total units: {total_units:.6f} {DCA_SYMBOL.split('/')[0]}")
    print(f"  Avg cost basis:   ${avg_cost:,.2f}  | Current price: ${fill_price:,.2f}")
    print(f"  Unrealized P&L:   {pnl_sym}${unrealized:,.2f}  ({pnl_sym}{unrealized_pct:.2f}%)")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DCA bot for crypto")
    parser.add_argument("--report", action="store_true", help="Print buy history (no network calls)")
    args = parser.parse_args()

    if args.report:
        report()
    else:
        run()
