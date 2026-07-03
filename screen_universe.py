"""
screen_universe.py — walk-forward screening harness for Kraken spot pairs.

Supports both CAD and USD quote currencies via SCREEN_QUOTE env var (default CAD).
For USD mode, fetches all tickers in a single API call, ranks by volume, caps at
SCREEN_MAX_CANDIDATES before running the expensive walk-forward step.

Proxy data: Binance USDT pairs — same convention as all prior BTC/XRP validation.
(e.g. LTC/CAD or LTC/USD on Kraken → LTC/USDT on Binance for 5000-candle history)

Read-only — no changes to live config, whitelist, or strategy code.

Usage:
    # CAD universe (default):
    python screen_universe.py

    # USD universe:
    SCREEN_QUOTE=USD python screen_universe.py

Env vars:
    SCREEN_QUOTE=CAD|USD          quote currency to screen (default CAD)
    SCREEN_SYMBOLS=LTC/USD,...    explicit list; skips auto-discovery
    SCREEN_EXCLUDE=ADA,ADA/CAD   additional base exclusions (accepts base or full sym)
    SCREEN_MAX_CANDIDATES=15     max walk-forward runs (ranked by volume; default 15)
    SCREEN_MIN_VOL_CAD=50000     liquidity gate in quote-currency units (default 50000)
    SCREEN_PF_MIN=1.2            pass PF threshold, all windows (default 1.2)
    SCREEN_MIN_TRADES=10         min trades, full window (default 10)
    SCREEN_MAX_SL_RATE=0.70      max SL-exit fraction (default 0.70 — the XRP failure mode)
    BACKTEST_FEE_PCT=0.008       fee used in walk-forward (default 0.008)
    BACKTEST_TIMEFRAME=4h        candle timeframe (default 4h)
"""
from __future__ import annotations

import logging
logging.basicConfig(level=logging.WARNING)

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import ccxt

from config import cfg
from bot.data.historical_feed import fetch_candles_paginated
from bot.backtest import engine, metrics as metrics_mod
from bot.strategy.fingerprint import compute_strategy_hash

# ── Screening config (all env-driven) ─────────────────────────────────────────
_SCREEN_SYMBOLS_ENV = os.getenv("SCREEN_SYMBOLS", "").strip()
_SCREEN_EXCLUDE_ENV = os.getenv("SCREEN_EXCLUDE", "").strip()

SCREEN_QUOTE       = os.getenv("SCREEN_QUOTE",        "CAD").upper()
SCREEN_MAX_CANDS   = int(os.getenv("SCREEN_MAX_CANDIDATES", "15"))
SCREEN_MIN_VOL     = float(os.getenv("SCREEN_MIN_VOL_CAD",  "50000"))
SCREEN_PF_MIN      = float(os.getenv("SCREEN_PF_MIN",       "1.2"))
SCREEN_MIN_TRADES  = int(os.getenv("SCREEN_MIN_TRADES",     "10"))
SCREEN_MAX_SL_PCT  = float(os.getenv("SCREEN_MAX_SL_RATE",  "0.70"))
SCREEN_TIMEFRAME   = os.getenv("BACKTEST_TIMEFRAME",        "4h")
SCREEN_FEE         = float(os.getenv("BACKTEST_FEE_PCT",    "0.008"))
SCREEN_EXCHANGE    = os.getenv("SCREEN_KRAKEN_ID",          "kraken")
PROXY_EXCHANGE     = "binance"   # 5000+ candle history; Kraken limited to ~720
PROXY_QUOTE        = "USDT"      # LTC/CAD → LTC/USDT on Binance

WINDOWS = [5000, 3000, 1000]

# ── Exclusion lists (base-asset based — works for any quote currency) ──────────
_STABLECOIN_BASES: frozenset[str] = frozenset({
    # USD stablecoins
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "PAX", "GUSD", "UST", "FRAX",
    "USDP", "LUSD", "CRVUSD", "PYUSD", "RLUSD", "USDD", "USDE", "USDG",
    "USDQ", "USDS", "USDUC", "USD1", "AUSD", "MIM", "STABLE", "STBL",
    # EUR/GBP stablecoins
    "EURC", "EUROP", "EURQ", "TGBP", "EURS",
    # Other fiat-pegged / commodity-backed
    "QCAD", "MXNB", "PAXG", "XAUT", "TBTC", "WBTC", "WETH",
    # Stablecoin-adjacent
    "XDAI", "AMPL", "FEI", "TRIBE",
})

_FIAT_BASES: frozenset[str] = frozenset({
    "EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CHF", "SGD", "CAD",
    "HKD", "MXN", "KRW", "BRL",
})

# Base assets already decided (ACTIVE / WATCHLIST / BLOCKED for any quote)
_ALWAYS_EXCLUDE_BASES: frozenset[str] = frozenset({
    "BTC",   # ACTIVE as BTC/CAD
    "XRP",   # WATCHLIST — walk-forward failed on current Mode A/B strategy
    "DOGE",  # BLOCKED — walk-forward failed at 0.8% fee
    "ETH",   # BLOCKED — walk-forward failed all windows
    "SOL",   # BLOCKED — walk-forward failed all windows
})


def _user_exclude_bases() -> set[str]:
    """Parse SCREEN_EXCLUDE: accepts base symbols (BTC) or full pairs (BTC/CAD)."""
    if not _SCREEN_EXCLUDE_ENV:
        return set()
    bases: set[str] = set()
    for tok in _SCREEN_EXCLUDE_ENV.split(","):
        tok = tok.strip().upper()
        if not tok:
            continue
        bases.add(tok.split("/")[0] if "/" in tok else tok)
    return bases


# ── Universe discovery ─────────────────────────────────────────────────────────

def discover_candidates(kraken_ex) -> list[str]:
    """Return all candidate symbols for SCREEN_QUOTE after exclusion filters."""
    if _SCREEN_SYMBOLS_ENV:
        return sorted(s.strip() for s in _SCREEN_SYMBOLS_ENV.split(",") if s.strip())

    print(f"  Discovering Kraken {SCREEN_QUOTE} spot pairs …", flush=True)
    try:
        markets = kraken_ex.load_markets()
    except Exception as exc:
        print(f"  ERROR: could not load Kraken markets: {exc}")
        sys.exit(1)

    all_exclude = _ALWAYS_EXCLUDE_BASES | _user_exclude_bases()
    candidates: list[str] = []
    for sym, m in markets.items():
        if not sym.endswith(f"/{SCREEN_QUOTE}"):
            continue
        if not (m.get("active") and m.get("spot")):
            continue
        base = sym.split("/")[0]
        if base in _STABLECOIN_BASES or base in _FIAT_BASES:
            continue
        if base in all_exclude:
            continue
        candidates.append(sym)

    return sorted(candidates)


# ── Volume fetch + ranking ─────────────────────────────────────────────────────

def fetch_and_rank(
    kraken_ex,
    candidates: list[str],
) -> tuple[list[tuple[str, float]], dict[str, float | None]]:
    """
    Fetch all tickers in one API call, compute quote-currency volumes for candidates,
    filter by liquidity gate, sort desc by volume, return (top_n, all_vols).

    Falls back to individual fetches if the bulk call fails.
    """
    print(f"  Fetching 24h volumes for {len(candidates)} candidates (single call) …",
          flush=True)
    all_tickers: dict = {}
    try:
        all_tickers = kraken_ex.fetch_tickers()   # returns all ~1500 Kraken tickers
    except Exception as exc:
        print(f"  WARNING: bulk fetch_tickers() failed ({exc})")
        print("  Falling back to individual ticker fetches (slow) …")
        for sym in candidates:
            try:
                all_tickers[sym] = kraken_ex.fetch_ticker(sym)
            except Exception:
                pass

    all_vols: dict[str, float | None] = {}
    for sym in candidates:
        t       = all_tickers.get(sym) or {}
        last    = t.get("last") or 0.0
        vol_base = t.get("baseVolume")
        all_vols[sym] = (vol_base * last) if (vol_base and last) else None

    passing = [
        (sym, vol)
        for sym, vol in all_vols.items()
        if vol is not None and vol >= SCREEN_MIN_VOL
    ]
    passing.sort(key=lambda x: x[1], reverse=True)
    return passing[:SCREEN_MAX_CANDS], all_vols


# ── Binance proxy ──────────────────────────────────────────────────────────────

def to_proxy_symbol(sym: str) -> str:
    """Convert ANY/QUOTE → ANY/USDT for Binance lookup."""
    return f"{sym.split('/')[0]}/{PROXY_QUOTE}"


# ── Walk-forward ───────────────────────────────────────────────────────────────

def _run_window(candles: list, n: int) -> dict:
    """Run engine on the last *n* candles; return metrics dict."""
    window = candles[-n:] if len(candles) >= n else candles
    if len(window) < 100:
        return {"trades": 0, "pf": 0.0, "sl_rate": 0.0, "usable": False}

    result = engine.run(
        candles                  = window,
        symbol                   = "SCREEN",
        timeframe                = SCREEN_TIMEFRAME,
        strategy_mode            = cfg.strategy.mode,
        starting_cash            = cfg.portfolio.starting_cash,
        risk_per_trade_pct       = cfg.risk.risk_per_trade_pct,
        fee_pct                  = SCREEN_FEE,
        cooldown_ticks           = cfg.risk.cooldown_ticks,
        rsi_period               = cfg.strategy.rsi_period,
        rsi_oversold             = cfg.strategy.rsi_oversold,
        rsi_overbought           = cfg.strategy.rsi_overbought,
        fast_ema_period          = cfg.strategy.fast_ema_period,
        slow_ema_period          = cfg.strategy.slow_ema_period,
        adx_period               = cfg.strategy.adx_period,
        adx_threshold            = cfg.strategy.adx_threshold,
        adx_max                  = cfg.strategy.adx_max,
        min_ema_spread_pct       = cfg.strategy.min_ema_spread_pct,
        max_ema_spread_pct       = cfg.strategy.max_ema_spread_pct,
        rsi_filter_enabled       = cfg.strategy.rsi_filter_enabled,
        buy_threshold            = cfg.strategy.buy_threshold,
        sell_threshold           = cfg.strategy.sell_threshold,
        max_position_pct         = cfg.risk.max_position_pct,
        daily_loss_limit_pct     = cfg.risk.daily_loss_limit_pct,
        max_drawdown_pct         = 0.25,
        max_trades_per_day       = cfg.risk.max_trades_per_day,
        stop_loss_pct            = cfg.backtest.stop_loss_pct,
        take_profit_pct          = cfg.backtest.take_profit_pct,
        trail_stop_pct           = cfg.backtest.trail_stop_pct,
        trail_stop_activation_pct= cfg.backtest.trail_stop_activation_pct,
        partial_tp_pct           = cfg.backtest.partial_tp_pct,
        partial_tp_size          = cfg.backtest.partial_tp_size,
        regime_ema_period        = cfg.strategy.regime_ema_period,
        regime_ema_slope_filter  = cfg.strategy.regime_ema_slope_filter,
        volume_k                 = cfg.strategy.volume_k,
        atr_volatile_multiplier  = cfg.strategy.atr_volatile_multiplier,
        atr_sl_mult              = cfg.backtest.atr_sl_mult,
    )

    m        = metrics_mod.compute(result)
    sells    = [f for f in result.fills if f.side == "SELL"]
    sl_exits = [f for f in sells if f.reason == "stop_loss"]
    sl_rate  = (len(sl_exits) / len(sells)) if sells else 0.0

    return {
        "trades":  m.total_trades,
        "pf":      m.profit_factor if m.profit_factor != float("inf") else 99.0,
        "sl_rate": sl_rate,
        "usable":  True,
    }


def run_walkforward(proxy_sym: str) -> tuple[list[dict], str | None]:
    """Fetch 5000 candles from Binance, run 3 windows. Returns (results, err)."""
    try:
        candles = fetch_candles_paginated(
            exchange_id = PROXY_EXCHANGE,
            symbol      = proxy_sym,
            timeframe   = SCREEN_TIMEFRAME,
            total_limit = WINDOWS[0],
        )
    except Exception as exc:
        return [], f"fetch failed: {exc}"

    if not candles:
        return [], "no candles returned"

    return [_run_window(candles, w) for w in WINDOWS], None


# ── Pass/Fail verdict ──────────────────────────────────────────────────────────

def verdict(wrs: list[dict]) -> tuple[str, str]:
    """Returns ("PASS", "") or ("FAIL", "reason"). Checks full window first."""
    full = wrs[0]
    if not full["usable"]:
        return "FAIL", "insufficient data"
    if full["trades"] < SCREEN_MIN_TRADES:
        return "FAIL", f"full-window trades {full['trades']} < {SCREEN_MIN_TRADES}"
    if full["sl_rate"] > SCREEN_MAX_SL_PCT:
        return "FAIL", f"SL-exit rate {full['sl_rate']*100:.0f}% > {SCREEN_MAX_SL_PCT*100:.0f}%"
    for w, wr in zip(WINDOWS, wrs):
        if not wr["usable"]:
            return "FAIL", f"{w}c window: insufficient data"
        if wr["pf"] < SCREEN_PF_MIN:
            return "FAIL", f"{w}c PF {wr['pf']:.2f} < {SCREEN_PF_MIN}"
    return "PASS", ""


# ── Output helpers ─────────────────────────────────────────────────────────────

def _vol_str(vol: float | None) -> str:
    return f"${vol:>12,.0f}" if vol is not None else "         N/A"


def _md_row(sym: str, vol: float | None, wrs: list[dict] | None, verd: str, why: str) -> str:
    vol_s  = f"${vol:,.0f}" if vol is not None else "N/A"
    if wrs is None:
        row = f"| {sym} | {vol_s} | — | — | — | — | — | — | N/A | {verd}: {why} |"
    else:
        pf  = [f"{wrs[i]['pf']:.2f}" if wrs[i]["usable"] else "N/A" for i in range(3)]
        slr = f"{wrs[0]['sl_rate']*100:.0f}%" if wrs[0]["usable"] else "N/A"
        verd_s = verd if not why else f"{verd} ({why})"
        row = (f"| {sym} | {vol_s} "
               f"| {wrs[0]['trades']} | {pf[0]} "
               f"| {wrs[1]['trades']} | {pf[1]} "
               f"| {wrs[2]['trades']} | {pf[2]} "
               f"| {slr} | {verd_s} |")
    return row


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    strategy_hash = compute_strategy_hash()
    now_str       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    date_tag      = datetime.now(timezone.utc).strftime("%Y%m%d")

    print()
    print(f"  ── Symbol Universe Screener  [{now_str}] ──")
    print(f"  Strategy hash : {strategy_hash}")
    print(f"  Quote         : {SCREEN_QUOTE}  |  Max candidates : {SCREEN_MAX_CANDS}")
    print(f"  Proxy data    : {PROXY_EXCHANGE.capitalize()}  {PROXY_QUOTE}-pairs  {SCREEN_TIMEFRAME}")
    print(f"  Windows       : {WINDOWS[0]}c / {WINDOWS[1]}c / {WINDOWS[2]}c")
    print(f"  Pass criteria : PF ≥ {SCREEN_PF_MIN} (all windows)  |  "
          f"trades ≥ {SCREEN_MIN_TRADES} (full)  |  "
          f"SL rate ≤ {SCREEN_MAX_SL_PCT*100:.0f}%")
    print(f"  Fee           : {SCREEN_FEE*100:.2f}%  |  "
          f"Min vol ({SCREEN_QUOTE}/day): ${SCREEN_MIN_VOL:,.0f}")
    print()

    kraken_ex = ccxt.kraken({"timeout": 30_000})

    # ── Discover ───────────────────────────────────────────────────────────────
    candidates = discover_candidates(kraken_ex)
    print(f"  {len(candidates)} candidates after exclusion filters.")

    # ── Volume fetch + rank ────────────────────────────────────────────────────
    top_n, all_vols = fetch_and_rank(kraken_ex, candidates)

    n_above_gate = sum(
        1 for v in all_vols.values() if v is not None and v >= SCREEN_MIN_VOL
    )
    print(f"  {n_above_gate} cleared ${SCREEN_MIN_VOL:,.0f} liquidity gate; "
          f"running walk-forward on top {len(top_n)} by volume.")
    print()

    # ── Walk-forward ───────────────────────────────────────────────────────────
    md_rows:  list[str]  = []
    passes:   list[dict] = []
    wf_results: list[dict] = []

    for sym, vol in top_n:
        proxy_sym = to_proxy_symbol(sym)
        print(f"  [{sym}]  vol=${vol:,.0f}  proxy={proxy_sym} …", flush=True)

        wrs, err = run_walkforward(proxy_sym)
        if err:
            print(f"    SKIP — {err}")
            md_rows.append(_md_row(sym, vol, None, "SKIP", err))
            wf_results.append({"sym": sym, "vol": vol, "wrs": None,
                               "verd": "SKIP", "why": err})
            print()
            continue

        verd, why = verdict(wrs)
        wsum = "  ".join(
            f"{WINDOWS[i]}c: {wrs[i]['trades']}t PF{wrs[i]['pf']:.2f}"
            for i in range(len(wrs))
        )
        tag = "✓ PASS" if verd == "PASS" else f"✗ FAIL"
        detail = f"({why})" if why else ""
        print(f"    {tag} {detail}  |  {wsum}  |  SL {wrs[0]['sl_rate']*100:.0f}%")

        md_rows.append(_md_row(sym, vol, wrs, verd, why))
        wf_results.append({"sym": sym, "vol": vol, "wrs": wrs, "verd": verd, "why": why})

        if verd == "PASS":
            passes.append({
                "sym": sym, "vol": vol,
                "pf5k": wrs[0]["pf"], "tr5k": wrs[0]["trades"],
                "pf3k": wrs[1]["pf"], "tr3k": wrs[1]["trades"],
                "pf1k": wrs[2]["pf"], "tr1k": wrs[2]["trades"],
                "sl_rate": wrs[0]["sl_rate"],
            })
        print()

    # ── Terminal summary ───────────────────────────────────────────────────────
    print()
    print("  " + "─" * 140)
    hdr = ("  {:28}  {:>14}  {:>5} {:>5}  {:>5} {:>5}  {:>5} {:>5}  {:>7}  {}"
           .format("Symbol", f"Vol ({SCREEN_QUOTE}/day)",
                   "5k tr", "5k PF", "3k tr", "3k PF", "1k tr", "1k PF",
                   "SL rate", "Verdict"))
    print(hdr)
    print("  " + "─" * 140)

    for r in wf_results:
        if r["wrs"] is None:
            print(f"  {r['sym']:<28}  {_vol_str(r['vol'])}"
                  f"  {'—':>5} {'—':>5}  {'—':>5} {'—':>5}  {'—':>5} {'—':>5}"
                  f"  {'N/A':>7}  SKIP ({r['why']})")
        else:
            wrs = r["wrs"]
            sl_s  = f"{wrs[0]['sl_rate']*100:.0f}%"
            pf    = [f"{wrs[i]['pf']:.2f}" if wrs[i]['usable'] else "N/A" for i in range(3)]
            verd_s = r["verd"] if not r["why"] else f"{r['verd']} ({r['why']})"
            print(f"  {r['sym']:<28}  {_vol_str(r['vol'])}"
                  f"  {wrs[0]['trades']:>5} {pf[0]:>5}"
                  f"  {wrs[1]['trades']:>5} {pf[1]:>5}"
                  f"  {wrs[2]['trades']:>5} {pf[2]:>5}"
                  f"  {sl_s:>7}  {verd_s}")

    print("  " + "─" * 140)
    print()
    if passes:
        print(f"  PASS ({len(passes)}): {', '.join(p['sym'] for p in passes)}")
    else:
        print("  No symbols passed all walk-forward windows.")
    print()

    # ── Markdown report ────────────────────────────────────────────────────────
    log_dir  = Path("logs")
    log_dir.mkdir(exist_ok=True)
    quote_lc = SCREEN_QUOTE.lower()
    out_name = (f"screen_results_{date_tag}.md" if SCREEN_QUOTE == "CAD"
                else f"screen_results_{quote_lc}_{date_tag}.md")
    out_path = log_dir / out_name

    lines: list[str] = [
        f"# Symbol Screening Results ({SCREEN_QUOTE}) — {now_str}",
        "",
        f"**Strategy hash:** `{strategy_hash}`  ",
        f"**Quote currency:** {SCREEN_QUOTE}  "
        f"**Proxy:** {PROXY_EXCHANGE.capitalize()} {PROXY_QUOTE}-pairs  "
        f"timeframe={SCREEN_TIMEFRAME}  fee={SCREEN_FEE*100:.2f}%  ",
        f"**Windows:** {' / '.join(str(w)+'c' for w in WINDOWS)}  ",
        f"**Pass criteria:** PF ≥ {SCREEN_PF_MIN} (all windows) | "
        f"trades ≥ {SCREEN_MIN_TRADES} (full window) | "
        f"SL-exit rate ≤ {SCREEN_MAX_SL_PCT*100:.0f}%  ",
        f"**Liquidity gate:** ${SCREEN_MIN_VOL:,.0f} {SCREEN_QUOTE}/day | "
        f"Candidates: {len(candidates)} → {n_above_gate} passed gate → "
        f"top {len(top_n)} walk-forwarded",
        "",
        "## Walk-forward results",
        "",
        "| Symbol | Vol/day | 5000c trades | 5000c PF | 3000c trades | 3000c PF "
        "| 1000c trades | 1000c PF | SL rate | Verdict |",
        "|--------|---------|------------|--------|------------|--------|"
        "------------|--------|---------|---------|",
    ]
    lines.extend(md_rows)

    lines += [
        "",
        "## Pass list",
        "",
    ]
    if passes:
        for p in passes:
            lines.append(
                f"- **{p['sym']}** — vol ${p['vol']:,.0f}/day | "
                f"5000c: {p['tr5k']} trades PF {p['pf5k']:.2f} | "
                f"3000c: {p['tr3k']} trades PF {p['pf3k']:.2f} | "
                f"1000c: {p['tr1k']} trades PF {p['pf1k']:.2f} | "
                f"SL rate {p['sl_rate']*100:.0f}%"
            )
    else:
        lines.append("No symbols passed all walk-forward windows.")

    lines += [
        "",
        "## Notes",
        "",
        f"- Screened {len(candidates)} Kraken {SCREEN_QUOTE} spot pairs after "
        "exclusions (stablecoins, fiat FX, already-decided base assets: BTC/XRP/ETH/SOL/DOGE).",
        f"- {n_above_gate} cleared ${SCREEN_MIN_VOL:,.0f} {SCREEN_QUOTE}/day liquidity gate; "
        f"top {len(top_n)} by volume were walk-forwarded.",
        f"- Proxy: Kraken {SCREEN_QUOTE} pairs → Binance {PROXY_QUOTE} pairs "
        "(same convention as all prior BTC/XRP validation).",
        "- A PASS here is a bench candidate only.  Promotion to UNIVERSE_WHITELIST requires:",
        "  1. BTC/CAD live gates met (≥15 fills, live PF ≥ 1.2).",
        "  2. Capital ≥ $500 AND documented decision on CAD→USD conversion cost/FX exposure "
        "(for USD pairs).",
        "  3. Full 3-window walk-forward pass on the CURRENT strategy code at time of promotion.",
        "",
    ]

    out_path.write_text("\n".join(lines) + "\n")
    print(f"  Report → {out_path}")
    print()


if __name__ == "__main__":
    main()
