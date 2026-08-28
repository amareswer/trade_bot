#!/usr/bin/env python
"""
stock_mean_reversion_experiment.py — does a Bollinger/RSI mean-reversion
strategy clear the stock bot's own walk-forward bar?

RESEARCH ONLY. Touches no live code: no `bot/strategy/*`, no
`stock_bot/strategy/*`, no `.env`, no `stock_bot/main.py`, no executor. A
PASS here authorises nothing — promotion still requires the full Validation
Discipline workflow (fresh walk-forward on the promoted implementation, per
CLAUDE.md).

Why re-test this on stocks after the crypto version failed
(`mean_reversion_experiment.py`, 2026-08-28, BTC PF 0.30 / SOL PF 0.36): the
crypto failure was driven mostly by the 1.6% round-trip Kraken fee swamping
the small reversions. On IBKR the round-trip cost is
`2 * max($1.00, shares * $0.005)` + 15 bps slippage/fill — for a $1000
trade that's roughly 0.2-0.4%, an order of magnitude less. This experiment
also tests an optional SHORT leg (IBKR allows shorting; Kraken spot does
not), so it can trade the overbought side too.

Data: yfinance daily candles for the current `RULE_WHITELIST` (US-listed,
API-tradeable), same source `stock_backtest.py` uses.

Cost model: IBKR Pro fixed-rate commission (`_round_trip_commission`, the
exact function the paper expectancy report uses) + `slippage_bps` per fill,
matching `StockBacktestConfig` defaults. Whole shares only (`int(notional /
price)`), same as the live paper bot.

Gate (identical to `stock_backtest.py`): full-window completed trades >= 10;
PF >= 1.2 in EVERY window with >= 3 trades; SL-exit rate <= 70% on the full
window.

Methodology — parameters are PRE-REGISTERED: every constant below is fixed
in the source, chosen from round defensible values BEFORE any window's
results were inspected. No parameter is tuned against the walk-forward
output.

Usage:  .venv/bin/python stock_mean_reversion_experiment.py
        STOCK_MR_SYMBOLS=MRNA,AMD  .venv/bin/python stock_mean_reversion_experiment.py
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

from bot.indicators.indicators import adx, bollinger_bands, rsi
from stock_bot.analysis.paper_report import _round_trip_commission
from stock_bot.data.price_feed import Candle, fetch_candles

# ── Pre-registered strategy parameters (do not tune against results) ─────
ADX_RANGING_MAX = 20.0    # only trade when ADX(14) < this (ranging regime)
RSI_LONG_MAX    = 35.0    # long when RSI(14) below this (oversold)
RSI_SHORT_MIN   = 65.0    # short when RSI(14) above this (overbought)
BB_PERIOD       = 20
BB_STD          = 2.0
RSI_PERIOD      = 14
ADX_PERIOD      = 14
STOP_PCT        = 0.05    # protective stop 5% from entry — matches PAPER_STOP_LOSS_PCT,
                           # checked intra-candle (low for longs, high for shorts)
TIME_STOP_BARS  = 15      # exit at close after ~3 trading weeks if neither target nor stop
COOLDOWN_BARS   = 1
WARMUP          = 35      # ADX(14) needs 2*period+1 = 29 candles; 35 is safe headroom

# ── Run configuration ──────────────────────────────────────────────────
NOTIONAL        = 1_000.0
SLIPPAGE_BPS    = 15      # per fill — same as StockBacktestConfig / PAPER_SLIPPAGE_BPS
FETCH_DAYS      = int(os.getenv("STOCK_MR_DAYS", "1500"))
WINDOWS         = [0, 750, 500, 250]   # trading days, 0 = full history (same as stock_backtest.py)
MIN_TRADES_FOR_VERDICT = 3
MIN_TRADES_FULL_WINDOW = 10
MIN_PF          = 1.2
MAX_SL_EXIT     = 0.70

REPORT_PATH = os.path.join(
    "logs", f"stock_mean_reversion_experiment_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
)


def _symbols() -> list[str]:
    """RULE_WHITELIST by default (loaded lazily so importing this module never
    touches `.env`), overridable via STOCK_MR_SYMBOLS."""
    env = os.getenv("STOCK_MR_SYMBOLS", "").strip()
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    from stock_bot.config import load as _load_stock_config
    wl_str = _load_stock_config().rule_whitelist_str or ""
    wl = [s.strip().upper() for s in wl_str.split(",") if s.strip()]
    # API-tradeable only — .TO names are advisory-only (CIRO block), never rule-traded
    return [s for s in wl if not s.endswith(".TO")] or ["MRNA", "AMD", "PLTR"]


# ─────────────────────────────────────────────────────────────────────────
# Trade-stats helper — same PF convention as bot/backtest/metrics.py:
# profit_factor = gross_profit / gross_loss; inf if wins and no losses.
# ─────────────────────────────────────────────────────────────────────────

def _pf_stats(pnls: list[float]) -> dict:
    if not pnls:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "pf": 0.0, "net": 0.0}
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp, gl = sum(wins), abs(sum(losses))
    pf     = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {"trades": len(pnls), "wins": len(wins),
            "win_rate": len(wins) / len(pnls) * 100, "pf": pf, "net": sum(pnls)}


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


# ─────────────────────────────────────────────────────────────────────────
# Strategy — pure decision helpers (hand-testable)
# ─────────────────────────────────────────────────────────────────────────

def _signal(
    c_slice: list[float],
    h_slice: list[float],
    l_slice: list[float],
    allow_short: bool,
) -> tuple[str | None, float | None]:
    """
    Returns (side, adx_value): side is "LONG", "SHORT" or None.
    Growing slice ending at the candle being decided — nothing beyond it is read.

      LONG  : close < lower Bollinger  AND RSI < RSI_LONG_MAX   AND ADX < ADX_RANGING_MAX
      SHORT : close > upper Bollinger  AND RSI > RSI_SHORT_MIN  AND ADX < ADX_RANGING_MAX
              (only when allow_short)
    """
    bb = bollinger_bands(c_slice, BB_PERIOD, BB_STD)
    if bb is None:
        return None, None
    upper, _, lower = bb
    close = c_slice[-1]

    want_long  = close < lower
    want_short = allow_short and close > upper
    if not (want_long or want_short):
        return None, None

    r = rsi(c_slice, RSI_PERIOD)
    if r is None:
        return None, None
    if want_long and r >= RSI_LONG_MAX:
        want_long = False
    if want_short and r <= RSI_SHORT_MIN:
        want_short = False
    if not (want_long or want_short):
        return None, None

    a = adx(h_slice, l_slice, c_slice, ADX_PERIOD)
    if a is None or a >= ADX_RANGING_MAX:
        return None, a

    return ("LONG" if want_long else "SHORT"), a


def _exit_reason(
    side:      str,
    high_i:    float,
    low_i:     float,
    close_i:   float,
    entry_px:  float,
    bars_held: int,
    middle:    float | None,
    stop_pct:  float = STOP_PCT,
) -> str | None:
    """
    Exit precedence on the current candle. "stop" is a bare price level
    checked intra-candle (low for a long, high for a short); "target" and
    "time" fill at the close.

      LONG : stop = low  <= entry * (1 - stop_pct)   target = close >= middle
      SHORT: stop = high >= entry * (1 + stop_pct)   target = close <= middle
    """
    if side == "LONG":
        if low_i <= entry_px * (1.0 - stop_pct):
            return "stop"
        if middle is not None and close_i >= middle:
            return "target"
    else:  # SHORT
        if high_i >= entry_px * (1.0 + stop_pct):
            return "stop"
        if middle is not None and close_i <= middle:
            return "target"
    if bars_held >= TIME_STOP_BARS:
        return "time"
    return None


# ─────────────────────────────────────────────────────────────────────────
# Backtest
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class MRTrade:
    side:      str
    entry_idx: int
    exit_idx:  int
    entry_px:  float
    exit_px:   float
    shares:    int
    pnl:       float
    reason:    str
    entry_adx: float


@dataclass
class MRResult:
    trades: list[MRTrade] = field(default_factory=list)
    skipped_unaffordable: int = 0

    @property
    def pnls(self) -> list[float]:
        return [t.pnl for t in self.trades]

    @property
    def sl_exit_rate(self) -> float:
        return (sum(1 for t in self.trades if t.reason == "stop") / len(self.trades)
                if self.trades else 0.0)

    @property
    def avg_hold_bars(self) -> float:
        return (sum(t.exit_idx - t.entry_idx for t in self.trades) / len(self.trades)
                if self.trades else 0.0)


def run_backtest(
    symbol:      str,
    candles:     list[Candle],
    allow_short: bool,
    notional:    float = NOTIONAL,
    slippage_bps: int = SLIPPAGE_BPS,
) -> MRResult:
    """
    One position at a time. Entry decided at candle i's close, filled at that
    close +/- slippage; whole shares only (`int(notional / entry_close)`).
    Exit per `_exit_reason`. IBKR round-trip commission via
    `_round_trip_commission(symbol, shares)`. Any position open at the last
    candle is marked to market at that close (reason "eod").
    """
    res = MRResult()
    if len(candles) <= WARMUP:
        return res

    closes = [c.close for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]
    slip   = slippage_bps / 10_000.0

    in_pos = False
    side = ""
    entry_idx = -1
    entry_px = 0.0        # slippage-adjusted fill
    entry_ref = 0.0       # raw close (for the stop level)
    entry_adx = 0.0
    shares = 0
    last_exit = -(COOLDOWN_BARS + 1)

    def _close(exit_idx: int, exit_ref: float, reason: str) -> None:
        if side == "LONG":
            exit_fill = exit_ref * (1.0 - slip)
            gross = shares * (exit_fill - entry_px)
        else:
            exit_fill = exit_ref * (1.0 + slip)
            gross = shares * (entry_px - exit_fill)
        commission = _round_trip_commission(symbol, shares)
        res.trades.append(MRTrade(
            side=side, entry_idx=entry_idx, exit_idx=exit_idx,
            entry_px=entry_px, exit_px=exit_fill, shares=shares,
            pnl=gross - commission, reason=reason, entry_adx=entry_adx,
        ))

    for i in range(WARMUP, len(candles)):
        c_slice = closes[: i + 1]

        if in_pos:
            bb = bollinger_bands(c_slice, BB_PERIOD, BB_STD)
            middle = bb[1] if bb else None
            reason = _exit_reason(side, highs[i], lows[i], closes[i],
                                  entry_ref, i - entry_idx, middle)
            if reason == "stop":
                lvl = entry_ref * (1.0 - STOP_PCT) if side == "LONG" else entry_ref * (1.0 + STOP_PCT)
                _close(i, lvl, "stop")
                in_pos, last_exit = False, i
            elif reason is not None:
                _close(i, closes[i], reason)
                in_pos, last_exit = False, i
            continue

        if i - last_exit <= COOLDOWN_BARS:
            continue

        sig, a = _signal(c_slice, highs[: i + 1], lows[: i + 1], allow_short)
        if sig is None:
            continue

        n = int(notional / closes[i])
        if n <= 0:
            res.skipped_unaffordable += 1
            continue

        in_pos = True
        side = sig
        entry_idx = i
        entry_ref = closes[i]
        entry_px = closes[i] * (1.0 + slip) if sig == "LONG" else closes[i] * (1.0 - slip)
        entry_adx = a
        shares = n

    if in_pos:
        _close(len(candles) - 1, closes[-1], "eod")

    return res


# ─────────────────────────────────────────────────────────────────────────
# Walk-forward + verdict
# ─────────────────────────────────────────────────────────────────────────

def _verdict(window_stats: list[dict], full_sl_rate: float) -> str:
    full = next((w for w in window_stats if w["window"] == 0), None)
    if full is None or full["trades"] < MIN_TRADES_FULL_WINDOW:
        return f"FAILED — full window < {MIN_TRADES_FULL_WINDOW} trades"
    if full_sl_rate > MAX_SL_EXIT:
        return f"FAILED — full-window SL-exit rate {full_sl_rate*100:.0f}% > {MAX_SL_EXIT*100:.0f}%"
    counted = [w for w in window_stats if w["trades"] >= MIN_TRADES_FOR_VERDICT]
    failing = [w for w in counted if w["pf"] < MIN_PF]
    if failing:
        return "FAILED — PF < %.1f in %s" % (
            MIN_PF, ", ".join(f"{'full' if w['window']==0 else str(w['window'])+'d'} "
                              f"({_fmt_pf(w['pf'])})" for w in failing))
    return f"PASS — full trades >= {MIN_TRADES_FULL_WINDOW}, PF >= {MIN_PF} every counted window, SL-exit ok"


def _run_symbol(symbol: str, allow_short: bool, report: list[str]) -> tuple[str, dict]:
    candles = fetch_candles(symbol, "1d", FETCH_DAYS)
    if not candles or len(candles) <= WARMUP:
        report.append(f"| {symbol} | — | SKIP — thin history ({len(candles or [])} candles) |")
        return "SKIPPED", {}

    window_stats: list[dict] = []
    full_sl = 0.0
    for w in WINDOWS:
        window = candles if w == 0 else (candles[-w:] if len(candles) >= w else candles)
        r = run_backtest(symbol, window, allow_short)
        s = _pf_stats(r.pnls)
        s["window"] = w
        window_stats.append(s)
        if w == 0:
            full_sl = r.sl_exit_rate
        label = "full" if w == 0 else f"{w}d"
        note = "" if s["trades"] >= MIN_TRADES_FOR_VERDICT else " (low sample)"
        report.append(
            f"| {symbol} | {label} | {s['trades']}{note} | {_fmt_pf(s['pf'])} "
            f"| {s['win_rate']:.0f}% | ${s['net']:+.0f} | {r.sl_exit_rate*100:.0f}% "
            f"| {r.avg_hold_bars:.1f} |"
        )

    verdict = _verdict(window_stats, full_sl)
    report.append(f"| {symbol} | **verdict** | | | | | **{verdict}** | |")
    return verdict, {"full": next(w for w in window_stats if w["window"] == 0)}


def _run_leg(allow_short: bool, symbols: list[str], report: list[str]) -> dict[str, str]:
    leg = "LONG + SHORT" if allow_short else "LONG ONLY"
    print(f"\n{'='*70}\n{leg}\n{'='*70}")
    report += [
        f"## {leg}", "",
        "| Symbol | Window | Trades | PF | Win% | Net $ | SL-exit% | Avg hold (d) |",
        "|--------|--------|--------|-----|------|-------|----------|--------------|",
    ]
    verdicts: dict[str, str] = {}
    for sym in symbols:
        v, _ = _run_symbol(sym, allow_short, report)
        verdicts[sym] = v
        print(f"  {sym:<8} {v}")
    report.append("")
    return verdicts


def main() -> None:
    symbols = _symbols()
    print(f"\nStock mean-reversion experiment — Bollinger({BB_PERIOD},{BB_STD}) / "
          f"RSI(<{RSI_LONG_MAX:.0f} long, >{RSI_SHORT_MIN:.0f} short) / ADX(<{ADX_RANGING_MAX:.0f}), "
          f"1d, {SLIPPAGE_BPS}bps + IBKR commission, ${NOTIONAL:.0f} notional")
    print(f"Symbols ({len(symbols)}): {', '.join(symbols)}")

    report = [
        f"# Stock Mean-Reversion Experiment — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        f"Question: does a Bollinger/RSI mean-reversion strategy pass the stock bot's own "
        f"walk-forward gate (full trades >= {MIN_TRADES_FULL_WINDOW}, PF >= {MIN_PF} in every "
        f"window with >= {MIN_TRADES_FOR_VERDICT} trades, SL-exit <= {MAX_SL_EXIT*100:.0f}%) "
        "on the current `RULE_WHITELIST`? Research only — see the module docstring in "
        "`stock_mean_reversion_experiment.py`. A PASS authorises nothing.",
        "",
        "**Why re-test after the crypto version failed** "
        "(`mean_reversion_experiment.py`, 2026-08-28, BTC PF 0.30 / SOL PF 0.36): that "
        "failure was driven by the 1.6% round-trip Kraken fee. IBKR's cost is "
        "`2 * max($1, shares * $0.005)` + 15 bps/fill — roughly 0.2-0.4% round trip on a "
        "$1000 trade. This run also tests an optional SHORT leg (IBKR allows shorting; "
        "Kraken spot does not).",
        "",
        "**Parameters (pre-registered):** long = close below lower Bollinger("
        f"{BB_PERIOD}, {BB_STD}sigma) AND RSI({RSI_PERIOD}) < {RSI_LONG_MAX:.0f}; short = "
        f"close above upper band AND RSI > {RSI_SHORT_MIN:.0f}; both gated on ADX({ADX_PERIOD}) "
        f"< {ADX_RANGING_MAX:.0f}. Exit = close back to the middle band / {STOP_PCT*100:.0f}% "
        f"stop / {TIME_STOP_BARS}-day time stop; {COOLDOWN_BARS}-day cooldown. Whole shares, "
        "one position at a time, non-compounding.",
        "",
        f"**Data:** yfinance daily, {FETCH_DAYS}-day fetch, {len(symbols)} US-listed "
        "`RULE_WHITELIST` symbols (`.TO` excluded — advisory-only). Windows: full / 750 / "
        "500 / 250 trading days.",
        "",
    ]

    long_only = _run_leg(False, symbols, report)
    long_short = _run_leg(True, symbols, report)

    lo_pass = sorted(s for s, v in long_only.items() if v.startswith("PASS"))
    ls_pass = sorted(s for s, v in long_short.items() if v.startswith("PASS"))

    report += [
        "## Bottom line", "",
        f"- **Long-only:** {len(lo_pass)}/{len(symbols)} pass"
        + (f" — {', '.join(lo_pass)}" if lo_pass else ""),
        f"- **Long + short:** {len(ls_pass)}/{len(symbols)} pass"
        + (f" — {', '.join(ls_pass)}" if ls_pass else ""),
        "",
    ]
    if lo_pass or ls_pass:
        report.append(
            "One or more symbols clear the gate. This does NOT promote anything — next step "
            "is a real implementation validated through the full Validation Discipline "
            "workflow (fresh walk-forward, per-symbol re-check), plus a decision on the "
            "operational cost of running a second strategy (and, for the short leg, borrow "
            "availability / hard-to-borrow fees, which this model does not include)."
        )
    else:
        report.append(
            "No symbol clears the gate on either leg. Even with IBKR's much lower costs, the "
            "mean-reversion regime does not carry enough edge here to justify a second live "
            "strategy. Not worth promoting; re-run only if the parameters change for a "
            "documented reason."
        )

    os.makedirs("logs", exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    print(f"\nReport written to {REPORT_PATH}")
    print(f"Long-only PASS: {len(lo_pass)}/{len(symbols)}   "
          f"Long+short PASS: {len(ls_pass)}/{len(symbols)}")


if __name__ == "__main__":
    main()
