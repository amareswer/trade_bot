#!/usr/bin/env python
"""
validate_symbol.py — one-command pipeline to assess a new crypto symbol.

Stages:
  1. Kraken liquidity check — 24h volume ≥ $50k CAD, spread ≤ 0.15%
  2. Binance walk-forward   — 3 windows (5000 / 3000 / 1000 × 4h candles)
     using the validated live-trading config. All windows must have PF > 1.0.

Verdict:
  APPROVED   walk-forward passed all windows + Kraken liquidity OK
  WATCHLIST  walk-forward passed but Kraken volume too thin, OR pair
             doesn't exist on Kraken, OR any window has < 10 trades
  BLOCKED    walk-forward failed (any window PF < 1.0 with enough trades)

Usage:
  python validate_symbol.py ADA
  python validate_symbol.py SOL --timeframe 1h
"""
import argparse
import logging
import sys
import time

import ccxt

logging.basicConfig(level=logging.WARNING)

from config import cfg
from bot.backtest.params import engine_kwargs_from_cfg

# ── Strategy config — sourced from engine_kwargs_from_cfg(cfg), NOT hand-listed ──
# 2026-07-30: this script used to hardcode its own "validated" config block,
# the exact failure class bot/backtest/params.py's docstring documents for
# backtest.py/walkforward.py (macd_enabled drift 2026-07-20, Mode A/B
# entry-param drift same day) — it had silently missed macd_enabled=True
# (live since 2026-07-20) and the live ATR×2.0 stop-loss (live since
# 2026-07-17) ever since. Every prior screen run by this script validated a
# stricter/older strategy shape than what actually trades. Fixed by routing
# through the same shared builder every other validation script uses — see
# run_backtest_window() below. Only `symbol`/`timeframe` (per-candidate, not
# from .env) and `strategy_mode` (pinned to "indicator" regardless of
# whatever cfg.strategy.mode happens to be) are overridden after the fact.

# Constants specific to THIS script — window/gate shape, not strategy params,
# so they stay local rather than moving into the shared builder.
_CANDLES_FULL = 5000
_MIN_VOL_CAD  = 50_000.0    # CAD
_MAX_SPREAD   = 0.0015      # 0.15%
_MIN_TRADES   = 10          # per window — below this, PF is unreliable

# Walk-forward window sizes (candle counts)
_WINDOWS = [5000, 3000, 1000]


# ── ANSI colours ──────────────────────────────────────────────────────────────
_GR = "\033[32m"
_RD = "\033[31m"
_YL = "\033[33m"
_B  = "\033[1m"
_R  = "\033[0m"


def _ok(s):  return f"{_GR}{s}{_R}"
def _fail(s): return f"{_RD}{s}{_R}"
def _warn(s): return f"{_YL}{s}{_R}"
def _bold(s): return f"{_B}{s}{_R}"


# ── Stage 1: Kraken liquidity ─────────────────────────────────────────────────

def check_liquidity(base: str) -> dict:
    """
    Returns dict with keys:
      pair_exists: bool
      vol_cad: float | None
      spread_pct: float | None
      min_order_cad: float | None
      vol_ok: bool
      spread_ok: bool
      pass: bool
    """
    symbol = f"{base}/CAD"
    result = dict(
        symbol=symbol, pair_exists=False,
        vol_cad=None, spread_pct=None, min_order_cad=None,
        vol_ok=False, spread_ok=False,
    )

    print(f"\n  {_bold('Stage 1 — Kraken liquidity')}  ({symbol})")
    print(f"  {'─'*54}")

    try:
        ex = ccxt.kraken({"enableRateLimit": True, "timeout": 15_000})
        markets = ex.load_markets()
    except Exception as exc:
        print(f"  {_fail('ERROR')}: could not connect to Kraken — {exc}")
        result["pass"] = False
        return result

    if symbol not in markets:
        cad_pairs = sorted(s for s in markets if s.endswith("/CAD"))
        print(f"  {_fail(symbol + ' not found on Kraken.')}")
        print(f"  Available CAD pairs: {', '.join(cad_pairs)}")
        result["pass"] = False
        return result

    result["pair_exists"] = True
    mkt = markets[symbol]

    try:
        ticker = ex.fetch_ticker(symbol)
    except Exception as exc:
        print(f"  {_fail('ERROR')}: ticker fetch failed — {exc}")
        result["pass"] = False
        return result

    bid      = ticker.get("bid")
    ask      = ticker.get("ask")
    last     = ticker.get("last")
    vol_base = ticker.get("baseVolume")

    vol_cad    = (vol_base * last) if (vol_base and last) else None
    spread_pct = ((ask - bid) / bid) if (bid and ask and bid > 0) else None

    limits        = mkt.get("limits", {})
    min_amount    = limits.get("amount", {}).get("min")
    min_order_cad = (min_amount * last) if (min_amount and last) else None

    result["vol_cad"]      = vol_cad
    result["spread_pct"]   = spread_pct
    result["min_order_cad"] = min_order_cad
    result["vol_ok"]       = vol_cad is not None and vol_cad >= _MIN_VOL_CAD
    result["spread_ok"]    = spread_pct is not None and spread_pct <= _MAX_SPREAD

    vol_str    = f"{vol_cad:,.0f} CAD"    if vol_cad    is not None else "n/a"
    spread_str = f"{spread_pct*100:.4f}%" if spread_pct is not None else "n/a"
    min_str    = f"{min_order_cad:.2f} CAD" if min_order_cad is not None else "n/a"
    last_str   = f"{last:.6g} CAD"        if last       is not None else "n/a"

    vol_label    = _ok("PASS") if result["vol_ok"]    else _fail("FAIL")
    spread_label = _ok("PASS") if result["spread_ok"] else _fail("FAIL")

    print(f"  Last price:       {last_str}")
    print(f"  24h volume:       {vol_str:>20}  {vol_label}  (≥ {_MIN_VOL_CAD:,.0f} CAD)")
    print(f"  Bid-ask spread:   {spread_str:>20}  {spread_label}  (≤ {_MAX_SPREAD*100:.2f}%)")
    print(f"  Min order:        {min_str}")

    result["pass"] = result["vol_ok"] and result["spread_ok"]
    status = _ok("LIQUIDITY PASS") if result["pass"] else _fail("LIQUIDITY FAIL")
    print(f"\n  → {status}")
    return result


# ── Stage 2: Walk-forward backtest ────────────────────────────────────────────

def run_backtest_window(candles, window_size: int, symbol: str, timeframe: str) -> dict:
    """Slice the most recent *window_size* candles and run a backtest."""
    from bot.backtest import engine, metrics as metrics_mod

    sliced = candles[-window_size:] if len(candles) >= window_size else candles
    if not sliced:
        return dict(candles=0, trades=0, pf=0.0, win_rate=0.0,
                    ret_pct=0.0, max_dd=0.0, error="no candles")

    # Full live config (MACD, ATR SL, Mode A/B entry params, everything) —
    # only the per-candidate symbol/timeframe and the pinned strategy_mode
    # are overridden. See module docstring / 2026-07-30 note above.
    kwargs = engine_kwargs_from_cfg(cfg)
    kwargs.update(symbol=symbol, timeframe=timeframe, strategy_mode="indicator")
    result = engine.run(candles=sliced, **kwargs)

    m = metrics_mod.compute(result)
    trades = len([f for f in result.fills if f.side == "SELL"])

    return dict(
        candles   = len(sliced),
        trades    = trades,
        pf        = m.profit_factor,
        win_rate  = m.win_rate * 100,
        ret_pct   = m.total_return_pct * 100,
        max_dd    = m.max_drawdown_pct * 100,
        period    = f"{sliced[0].timestamp.strftime('%b %Y')} → {sliced[-1].timestamp.strftime('%b %Y')}",
    )


def run_walkforward(base: str, timeframe: str) -> list[dict]:
    """Fetch candles from Binance and run all three window sizes."""
    from bot.data.historical_feed import fetch_candles_paginated

    usdt_symbol = f"{base}/USDT"
    print(f"\n  {_bold('Stage 2 — Walk-forward')}  (Binance {usdt_symbol}, {timeframe})")
    print(f"  {'─'*54}")
    print(f"  Fetching {_CANDLES_FULL} × {timeframe} candles …", end="", flush=True)

    try:
        candles = fetch_candles_paginated(
            exchange_id = "binance",
            symbol      = usdt_symbol,
            timeframe   = timeframe,
            total_limit = _CANDLES_FULL,
        )
    except Exception as exc:
        print(f"\n  {_fail('ERROR')}: could not fetch Binance data — {exc}")
        return []

    print(f"  {len(candles)} candles ({candles[0].timestamp.strftime('%Y-%m-%d')} → "
          f"{candles[-1].timestamp.strftime('%Y-%m-%d')})")

    rows = []
    for w in _WINDOWS:
        label = f"{w:,}c"
        print(f"  Running {label} window …", end="  ", flush=True)
        t0  = time.time()
        row = run_backtest_window(candles, w, usdt_symbol, timeframe)
        row["window"] = label
        elapsed = time.time() - t0

        pf_s = f"{row['pf']:.2f}" if row.get("pf") else "—"
        print(f"PF={pf_s}  trades={row['trades']}  ({elapsed:.1f}s)")
        rows.append(row)

    return rows


# ── Verdict ───────────────────────────────────────────────────────────────────

def decide_verdict(liq: dict, wf_rows: list[dict]) -> tuple[str, list[str]]:
    """
    Returns (verdict, [reason, ...]).

    APPROVED   all 3 windows PF > 1.0 with ≥ MIN_TRADES + liquidity pass
    WATCHLIST  all 3 windows PF > 1.0 but liquidity thin/missing,
               OR 2/3 windows PF > 1.0 (marginal),
               OR any window has < MIN_TRADES (data too thin to judge)
    BLOCKED    any window PF < 1.0 with enough trades
    """
    if not wf_rows:
        return "BLOCKED", ["Walk-forward data could not be fetched from Binance."]

    reasons = []

    pfs        = [r["pf"]     for r in wf_rows]
    trades     = [r["trades"] for r in wf_rows]
    thin_data  = any(t < _MIN_TRADES for t in trades)
    all_pass   = all(pf > 1.0 for pf in pfs)
    any_fail   = any(pf < 1.0 and t >= _MIN_TRADES for pf, t in zip(pfs, trades))
    n_pass     = sum(1 for pf in pfs if pf > 1.0)

    liq_ok     = liq.get("pass", False)

    if any_fail:
        reasons.append(
            f"Walk-forward failed: "
            + ", ".join(
                f"{r['window']} PF={r['pf']:.2f}"
                for r in wf_rows if r["pf"] < 1.0 and r["trades"] >= _MIN_TRADES
            )
        )
        return "BLOCKED", reasons

    if thin_data:
        thin_wins = [r["window"] for r in wf_rows if r["trades"] < _MIN_TRADES]
        reasons.append(
            f"Too few trades in window(s) {', '.join(thin_wins)} "
            f"— PF is unreliable with < {_MIN_TRADES} closed trades."
        )
        if all_pass:
            reasons.append("Walk-forward nominally passed but needs more trading history.")
        return "WATCHLIST", reasons

    if not all_pass:
        failing = [r for r in wf_rows if r["pf"] <= 1.0]
        reasons.append(
            "Marginal: "
            + ", ".join(f"{r['window']} PF={r['pf']:.2f}" for r in failing)
            + f" — {n_pass}/3 windows above 1.0."
        )
        return "WATCHLIST", reasons

    # Walk-forward passed — check liquidity
    if not liq_ok:
        if not liq.get("pair_exists"):
            reasons.append(f"{liq['symbol']} does not exist on Kraken — cannot trade live.")
        else:
            if not liq.get("vol_ok"):
                vol = liq.get("vol_cad")
                vol_str = f"{vol:,.0f}" if vol else "n/a"
                reasons.append(
                    f"Kraken 24h volume {vol_str} CAD is below ${_MIN_VOL_CAD:,.0f} CAD "
                    f"threshold — too thin for limit orders."
                )
            if not liq.get("spread_ok"):
                sp = liq.get("spread_pct")
                sp_str = f"{sp*100:.4f}%" if sp else "n/a"
                reasons.append(
                    f"Bid-ask spread {sp_str} exceeds {_MAX_SPREAD*100:.2f}% — "
                    f"slippage cost is too high."
                )
        return "WATCHLIST", reasons

    return "APPROVED", ["All 3 walk-forward windows PF > 1.0 + Kraken liquidity OK."]


# ── Output ────────────────────────────────────────────────────────────────────

def print_wf_table(wf_rows: list[dict]) -> None:
    if not wf_rows:
        return
    bar = "─" * 72
    print(f"\n  {_bold('Walk-forward results')}")
    print(f"  {bar}")
    header = f"  {'Window':>8}  {'Candles':>8}  {'Trades':>7}  {'PF':>6}  {'Win%':>6}  {'Return':>8}  {'MaxDD':>8}  Period"
    print(header)
    print(f"  {bar}")
    for r in wf_rows:
        pf = r["pf"]
        if pf >= 1.2:
            pf_col = _GR
        elif pf >= 1.0:
            pf_col = _YL
        else:
            pf_col = _RD
        verdict = "✓" if pf > 1.0 else "✗"
        if r["trades"] < _MIN_TRADES:
            pf_col = _YL
            verdict = "?"
        print(
            f"  {r['window']:>8}  {r['candles']:>8,}  {r['trades']:>7}  "
            f"{pf_col}{pf:>5.2f}{_R} {verdict}  "
            f"{r['win_rate']:>5.1f}%  "
            f"{r['ret_pct']:>+7.2f}%  "
            f"{r['max_dd']:>7.2f}%  "
            f"{r.get('period', '')}"
        )
    print(f"  {bar}")


def print_verdict(verdict: str, base: str, reasons: list[str]) -> None:
    bar = "═" * 54
    if verdict == "APPROVED":
        col = _GR
    elif verdict == "WATCHLIST":
        col = _YL
    else:
        col = _RD

    print(f"\n  {_bold(bar)}")
    print(f"  {_bold('VERDICT')}  {col}{_bold(verdict)}{_R}  —  {base}/CAD")
    print(f"  {_bold(bar)}")
    for r in reasons:
        print(f"  • {r}")
    if verdict == "APPROVED":
        print(f"\n  Next step: add {base}/CAD to UNIVERSE_WHITELIST in .env")
    elif verdict == "WATCHLIST":
        print(f"\n  Next step: monitor until conditions are met, then re-run.")
    else:
        print(f"\n  Do not add to live trading with current config.")
    print(f"  {_bold(bar)}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Validate a crypto symbol for live trading."
    )
    parser.add_argument("symbol", help="Base currency (e.g. ADA, SOL, MATIC)")
    parser.add_argument(
        "--timeframe", default=cfg.backtest.timeframe,
        help=f"Backtest timeframe (default: {cfg.backtest.timeframe})",
    )
    parser.add_argument(
        "--skip-liquidity", action="store_true",
        help="Skip Kraken liquidity check (backtest only)",
    )
    args = parser.parse_args()

    base      = args.symbol.upper().strip()
    timeframe = args.timeframe

    bar = "═" * 54
    print(f"\n  {_bold(bar)}")
    print(f"  {_bold('Symbol Validation Pipeline')}")
    sl_str = (f"ATRx{cfg.backtest.atr_sl_mult:g}" if cfg.backtest.atr_sl_mult > 0
              else f"{cfg.backtest.stop_loss_pct*100:.1f}%")
    print(f"  Base: {base}  |  Timeframe: {timeframe}")
    print(f"  Config: ADX≥{cfg.strategy.adx_threshold:g}  "
          f"EMA≥{cfg.strategy.min_ema_spread_pct*100:.1f}%"
          f"  RSI={'on' if cfg.strategy.rsi_filter_enabled else 'off'}"
          f"  MACD={'on' if cfg.strategy.macd_enabled else 'off'}"
          f"  SL={sl_str}  TP={cfg.backtest.take_profit_pct*100:.0f}%"
          f"  fee={cfg.backtest.fee_pct*100:.1f}%")
    print(f"  {_bold(bar)}")

    # Stage 1 — Kraken liquidity
    if args.skip_liquidity:
        liq = dict(
            symbol=f"{base}/CAD", pair_exists=False,
            vol_cad=None, spread_pct=None,
            vol_ok=False, spread_ok=False, pass_=False,
        )
        liq["pass"] = False
        print(f"\n  Stage 1 skipped (--skip-liquidity)")
    else:
        liq = check_liquidity(base)

    # Stage 2 — Walk-forward (always runs — even if liquidity fails, we want the backtest)
    wf_rows = run_walkforward(base, timeframe)

    # Results table
    print_wf_table(wf_rows)

    # Verdict
    verdict, reasons = decide_verdict(liq, wf_rows)
    print_verdict(verdict, base, reasons)

    # Exit code useful for CI
    sys.exit(0 if verdict == "APPROVED" else 1)


if __name__ == "__main__":
    main()
