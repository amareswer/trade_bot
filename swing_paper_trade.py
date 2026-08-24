"""
1d swing strategy — live paper-trade observation.

Built 2026-08-24 as the scaffold for the "4-week paper observation" next
step documented in .memory/decisions/swing-1d-validated.md — but NOT
started: the strategy's original 2026-06-23 validation (SL=4%/TP=25%,
PF 2.67/2.30/1.54) no longer reproduced on current bot/strategy/
indicator_strategy.py (Mode A/B wiring 2026-07-20, ATR-regime-baseline fix
2026-08-20 both changed signal generation). A same-day re-derivation
(swing_backtest.py sweep, current code) found a new best SL/TP — 3%/20% —
and a re-run walk-forward under it is still PARTIAL, not PASS: Train and
Val_1 pass, but Val_2 has only 3 completed trades against the 5-trade
minimum, regardless of which swept SL/TP is used (entry frequency is
governed by ADX/RSI-filter/Mode-A/B logic, not the exit parameters this
script's config sweeps) — see the decision doc's 2026-08-24 entries for
the full trail. This script reads its SL/TP live from swing_walkforward.py's
FIXED dict below, so it already reflects the current 3%/20% best candidate
with no code change needed here — but per the user's own standing
condition ("start only if the re-validation still PASSes"), it remains
UNSTARTED. Do NOT add to any live .env — research/observation only, and
only once/if a future re-validation actually clears all 3 windows.

── Design: reuse, not reimplement ──────────────────────────────────────────
Re-runs bot.backtest.engine.run() — the EXACT code path swing_walkforward.py
already used to validate the strategy — against the latest live candle
history on a daily cycle, using the FIXED config imported directly from
swing_walkforward.py (never re-typed here, so there is no way for this
script's params to silently drift from what was actually validated). Any
fill in the replayed result newer than the last one already logged is a new
"trade" for this observation, appended to logs/swing_trades.csv. This
sidesteps an entire class of live-vs-backtest drift bug this codebase has
been bitten by before (e.g. the Mode A/B and self-referential-ATR-regime
incidents) — there is no separate "live" strategy implementation to drift
FROM; it's the same deterministic engine.run() call, re-fed fresh data.

Fill de-duplication is timestamp-based (not index/count-based) — robust to
fetch_candles_paginated's rolling window occasionally dropping its oldest
candle as the window advances, which would desync a naive fill-count offset
over a period longer than TOTAL_LIMIT days (not a real risk within a
4-week observation at TOTAL_LIMIT=5000, but timestamp comparison costs
nothing extra and removes the failure mode entirely).

── Isolation guarantee (same shape as stock_bot/fast_validator.py) ─────────
  - Paper only, via bot.backtest.engine's own PaperExecutor — never
    LiveExecutor, never places a real order, never touches an exchange
    account beyond public OHLCV reads.
  - Own state (logs/swing_state.json) and own trade log
    (logs/swing_trades.csv) — never reads or writes
    logs/live_state_BTC_CAD.json, logs/risk_state.json, or the live bot's
    trades.db.
  - Does not import bot/main.py — a fully separate process, started and
    stopped independently of the live 4h bot (same relationship the crypto
    and stock bots already have to each other).
  - Reads bot/strategy/indicator_strategy.py (via bot.backtest.engine) but
    writes nothing there — bot/strategy/* and build_indicator_config() are
    untouched by this file's existence or operation.

── Data source ──────────────────────────────────────────────────────────────
Binance BTC/USDT (bot.backtest.engine's EXCHANGE/SYMBOL/TIMEFRAME, imported
from swing_walkforward.py) — matches the validated data source exactly, NOT
Kraken BTC/CAD (the live 4h bot's market). This observation is checking
signal quality against what was actually validated, not simulating the
eventual live venue; confirmed with the user before building (2026-08-24).

Run:
    .venv/bin/python swing_paper_trade.py            # loop forever, once/day
    .venv/bin/python swing_paper_trade.py --once      # single cycle, then exit
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("swing_paper_trade")

from bot.data.historical_feed import fetch_candles_paginated
from bot.backtest import engine, metrics as metrics_mod
from swing_walkforward import FIXED, EXCHANGE, SYMBOL, TIMEFRAME, TOTAL_LIMIT

_STATE_PATH = os.path.join("logs", "swing_state.json")
_TRADES_CSV = os.path.join("logs", "swing_trades.csv")
_CSV_HEADER = ["timestamp", "side", "price", "quantity", "total_value", "pnl", "fee", "reason"]

_RUN_BUFFER_MINUTES = 10   # run this many minutes after UTC midnight, giving Binance's
                           # own daily-candle aggregation a moment to settle


# ---------------------------------------------------------------------------
# State / trade log persistence
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if os.path.exists(_STATE_PATH):
        try:
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("swing_state.json unreadable (%s) — starting fresh", exc)
    return {
        "started_at":          None,
        "last_run_at":         None,
        "last_fill_timestamp": None,
        "latest_candle_ts":    None,
        "total_trades":        0,
        "win_rate_pct":        0.0,
        "profit_factor":       None,
    }


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, _STATE_PATH)


def _ensure_csv() -> None:
    os.makedirs(os.path.dirname(_TRADES_CSV), exist_ok=True)
    if not os.path.exists(_TRADES_CSV):
        with open(_TRADES_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(_CSV_HEADER)


def _append_fills(fills: list) -> None:
    _ensure_csv()
    with open(_TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for fill in fills:
            w.writerow([
                fill.timestamp, fill.side, fill.price, fill.quantity,
                fill.total_value, fill.pnl if fill.pnl is not None else "",
                fill.fee, fill.reason,
            ])


# ---------------------------------------------------------------------------
# One evaluation cycle
# ---------------------------------------------------------------------------

def run_cycle(state: dict) -> dict:
    """Fetch latest candles, replay the validated engine end-to-end, log any
    fills newer than the last one already recorded. Never raises — a fetch
    or engine failure is logged and the state is returned unchanged so the
    next scheduled cycle just tries again."""
    try:
        candles = fetch_candles_paginated(EXCHANGE, SYMBOL, TIMEFRAME, total_limit=TOTAL_LIMIT)
    except Exception as exc:
        logger.warning("Candle fetch failed (%s) — skipping this cycle", exc)
        return state
    if not candles:
        logger.warning("No candles returned — skipping this cycle")
        return state

    result = engine.run(candles=candles, symbol=SYMBOL, timeframe=TIMEFRAME, **FIXED)
    m = metrics_mod.compute(result)

    last_ts   = state.get("last_fill_timestamp")
    new_fills = [f for f in result.fills if last_ts is None or f.timestamp > last_ts]
    if new_fills:
        _append_fills(new_fills)
        state["last_fill_timestamp"] = new_fills[-1].timestamp
        logger.info(
            "Recorded %d new fill(s): %s",
            len(new_fills),
            ", ".join(f"{f.side}@{f.price:.2f}({f.reason})" for f in new_fills),
        )
    else:
        logger.info("No new fills this cycle")

    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_run_at"]      = now_iso
    state.setdefault("started_at", now_iso)
    state["latest_candle_ts"] = candles[-1].timestamp.isoformat()
    state["total_trades"]     = m.total_trades
    state["win_rate_pct"]     = round(m.win_rate * 100, 2)
    state["profit_factor"]    = None if m.profit_factor == float("inf") else round(m.profit_factor, 3)
    _save_state(state)
    return state


# ---------------------------------------------------------------------------
# Scheduling — once/day, not every tick
# ---------------------------------------------------------------------------

def _seconds_until_next_run() -> float:
    now = datetime.now(timezone.utc)
    next_run = now.replace(hour=0, minute=_RUN_BUFFER_MINUTES, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


def main() -> int:
    parser = argparse.ArgumentParser(description="1d swing strategy paper-trade observation")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args()

    logger.info(
        "1d swing paper-trade observation — %s %s %s (FIXED config from swing_walkforward.py, "
        "SL=%.0f%% TP=%.0f%% ADX>=%.0f cooldown=%d)",
        EXCHANGE, SYMBOL, TIMEFRAME,
        FIXED["stop_loss_pct"] * 100, FIXED["take_profit_pct"] * 100,
        FIXED["adx_threshold"], FIXED["cooldown_ticks"],
    )
    state = _load_state()

    # Run once immediately so the observation starts recording right away,
    # rather than waiting up to 24h for the first data point.
    state = run_cycle(state)

    if args.once:
        return 0

    while True:
        sleep_s = _seconds_until_next_run()
        logger.info("Next cycle in %.1fh", sleep_s / 3600)
        time.sleep(sleep_s)
        try:
            state = run_cycle(state)
        except Exception as exc:
            logger.warning("Cycle failed unexpectedly (%s) — will retry next scheduled run", exc)


if __name__ == "__main__":
    sys.exit(main())
