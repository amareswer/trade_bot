#!/usr/bin/env python
"""
breakout_experiment.py — does a Donchian-channel breakout / trend-continuation
strategy (Turtle-style) clear this project's own PF walk-forward bar on BTC,
SOL, and a few alts?

RESEARCH ONLY. Touches no live code: no bot/strategy/* files, no .env, no
bot/main.py, no CapitalPool, no live executor, no fingerprint. A PASS here
authorises nothing by itself — any promotion still requires the full
Validation Discipline workflow in CLAUDE.md (fresh 3-window walk-forward on
the promoted code, hash stamp, per-symbol re-check).

Why this exists: the live 4h strategy is a *pullback* trend-follower — it buys
dips inside an uptrend (RSI filter + EMA structure) and by design sits out
long stretches. A *breakout* trend-follower is the other classic trend entry:
buy the fresh N-bar high, ride it with a trailing channel stop, no fixed
take-profit. It trades on different candles than the pullback strategy and is
the one remaining untried trend-following shape after the 2026-08-28 strategy
search (mean-reversion / grid-DCA / cross-sectional momentum — all failed).

Prior on this: LOW. Four strategies tested so far, one works. Breakout systems
have low win rates (~30-40%) and whipsaw hardest in ranging regimes — exactly
the regime BTC has been stuck in. This script asks whether it nevertheless
carries a real, fee-net edge, or whether "buy every breakout" just pays for a
lot of failed breakouts.

Why a SEPARATE engine, not bot/backtest/engine.py: same reasoning the grid /
mean-reversion experiments document. That engine's fill model and signal
wiring are specific to the pullback strategy. Here, entries are known at
candle close (close pierced the Donchian upper) and filled at that close —
the same convention as the live bot's candle-close signal — but both stops
are bare price levels that can be pierced intra-candle, so they are checked
against each candle's LOW, not its close.

Data: BTC/USDT, SOL/USDT (live pairs) + ETH/USDT (currently BLOCKED — does
breakout rescue it?) + NEAR/USDT, ENA/USDT (the two least-bad USD screen
candidates from 2026-09-09). Binance proxy — the standard for every
validation script in this repo (Kraken history caps at ~720 candles).

Methodology — parameters are PRE-REGISTERED: every constant below is fixed in
the source, chosen from round, defensible values BEFORE any window's results
were inspected. Nothing is tuned against the walk-forward output. If none of
the windows pass, the honest answer is "this doesn't pass," not "try until
one does."

Usage:  .venv/bin/python breakout_experiment.py
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

from bot.data.historical_feed import Candle, fetch_candles_paginated, slice_candles
from bot.indicators.indicators import adx, atr, ema

# ── Pre-registered strategy parameters (do not tune against results) ─────────
DONCHIAN_ENTRY   = 20      # enter when close > highest high of the prior N candles
DONCHIAN_EXIT    = 10      # exit when close < lowest low of the prior M candles
                            # (shorter than entry — the classic Turtle asymmetry)
EMA_TREND        = 200     # macro regime filter: only enter when close > EMA(200)
                            # — the same 200-EMA gate the live strategy uses; its
                            # absence is a known breakout failure mode in bear markets
ADX_MIN          = 20.0    # only enter when ADX(14) >= this — a breakout with no
                            # trend behind it is noise
ADX_PERIOD       = 14
ATR_PERIOD       = 14
ATR_STOP_MULT    = 2.0     # hard stop ATR(14) * this below entry — matches the live
                            # ATR_SL_MULT=2.0; checked intra-candle against the low
COOLDOWN_BARS    = 1       # bars to wait after an exit before a new entry
WARMUP           = 220     # EMA(200) + headroom

# NO fixed take-profit — the entire point of a breakout system is to let the
# trailing channel stop decide when the move is over.

# ── Run configuration ──────────────────────────────────────────────────────
SYMBOLS    = ["BTC/USDT", "SOL/USDT", "ETH/USDT", "NEAR/USDT", "ENA/USDT"]
PRIMARY    = {"BTC/USDT", "SOL/USDT"}   # the bar that actually matters
TIMEFRAME  = os.getenv("BACKTEST_TIMEFRAME", "4h")
FEE        = float(os.getenv("BACKTEST_FEE_PCT", "0.008"))   # 0.8%/side, live
                                                              # Kraken finding — do not lower
WINDOWS    = [5000, 3000, 1000]   # trailing candles, same shape as the screen tables
MIN_TRADES = 10                   # per-window sample floor
PASS_PF    = 1.2                  # promotion bar (CLAUDE.md capital/symbol gate)

# Out-of-sample train/validation split — the same dates walkforward.py uses.
TRAIN_START = "2024-02-22"
TRAIN_END   = "2025-02-22"
VAL_START   = "2025-02-22"
VAL_END     = None

RESEARCH_CAPITAL = 1000.0   # fixed notional per trade, non-compounding — independent
                             # of the live CAD slot sizing; disclosed, not hidden

REPORT_PATH = os.path.join(
    "logs", f"breakout_experiment_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
)


# ───────────────────────────────────────────────────────────────────────────
# Trade-stats helper — same PF convention as bot/backtest/metrics.py and the
# other experiment scripts: profit_factor = gross_profit / gross_loss;
# inf if there are wins and no losses, 0.0 if no trades at all.
# ───────────────────────────────────────────────────────────────────────────

def _pf_stats(pnls: list[float], starting_cash: float) -> dict:
    if not pnls:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "pf": 0.0, "ret_pct": 0.0}
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp     = sum(wins)
    gl     = abs(sum(losses))
    pf     = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {
        "trades":   len(pnls),
        "wins":     len(wins),
        "win_rate": len(wins) / len(pnls) * 100,
        "pf":       pf,
        "ret_pct":  sum(pnls) / starting_cash * 100 if starting_cash else 0.0,
    }


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


# ───────────────────────────────────────────────────────────────────────────
# Breakout backtester
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class BOTrade:
    entry_idx:  int
    exit_idx:   int
    entry_px:   float
    exit_px:    float
    pnl:        float
    reason:     str      # "channel" | "atr_stop" | "eod"
    entry_adx:  float
    mae_pct:    float     # worst intra-trade drawdown from entry (max adverse excursion)


@dataclass
class BOResult:
    trades: list[BOTrade] = field(default_factory=list)

    @property
    def pnls(self) -> list[float]:
        return [t.pnl for t in self.trades]

    @property
    def stop_exit_pct(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.reason == "atr_stop") / len(self.trades) * 100

    @property
    def avg_hold_bars(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.exit_idx - t.entry_idx for t in self.trades) / len(self.trades)


def run_breakout_backtest(
    candles: list[Candle],
    capital: float = RESEARCH_CAPITAL,
    fee_pct: float = FEE,
) -> BOResult:
    """
    Long-only Donchian breakout with a trailing channel stop + ATR hard stop.

    On candle i (decisions use data[:i+1] — the current close is known, the
    same convention as the live bot's candle-close signal; nothing beyond i
    is ever read):

      FLAT, and i - last_exit > COOLDOWN_BARS:
        enter at close[i] when ALL of —
          close[i]  > max(high[i-DONCHIAN_ENTRY : i])   (fresh N-bar breakout)
          close[i]  > EMA(EMA_TREND) on closes[:i+1]    (macro uptrend)
          ADX(14) on [:i+1]  >= ADX_MIN                 (a real trend behind it)
        the ATR hard-stop level is fixed at entry:
          stop_px = close[i] - ATR_STOP_MULT * ATR(14)

      HOLDING (entered on candle j < i), checked in this order on candle i —
        ATR_STOP : low[i] <= stop_px                    -> exit at stop_px
                   (bare level, pierced intra-candle -> checked vs the LOW)
        CHANNEL  : close[i] < min(low[i-DONCHIAN_EXIT : i])
                   -> exit at close[i]  (trailing Turtle exit)
        no entry and exit on the same candle.

    Any position still open at the last candle is marked to market at that
    close (reason "eod") so an open trade is not silently dropped.

    P&L per trade uses a fixed notional (`capital`), non-compounding:
      cost     = notional * (1 + fee_pct)
      proceeds = notional * (exit/entry) * (1 - fee_pct)
      pnl      = proceeds - cost
    """
    res = BOResult()
    n = len(candles)
    if n < WARMUP + 2:
        return res

    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]
    closes = [c.close for c in candles]

    in_pos     = False
    entry_idx  = 0
    entry_px   = 0.0
    stop_px    = 0.0
    entry_adx  = 0.0
    worst_px   = 0.0
    last_exit  = -10_000

    for i in range(WARMUP, n):
        if in_pos:
            worst_px = min(worst_px, lows[i])
            exit_px: float | None = None
            reason  = ""

            if lows[i] <= stop_px:
                exit_px, reason = stop_px, "atr_stop"
            else:
                chan_low = min(lows[i - DONCHIAN_EXIT:i])
                if closes[i] < chan_low:
                    exit_px, reason = closes[i], "channel"

            if exit_px is not None:
                cost     = capital * (1 + fee_pct)
                proceeds = capital * (exit_px / entry_px) * (1 - fee_pct)
                res.trades.append(BOTrade(
                    entry_idx=entry_idx, exit_idx=i, entry_px=entry_px, exit_px=exit_px,
                    pnl=proceeds - cost, reason=reason, entry_adx=entry_adx,
                    mae_pct=(worst_px / entry_px - 1.0) * 100,
                ))
                in_pos = False
                last_exit = i
            continue

        # FLAT
        if i - last_exit <= COOLDOWN_BARS:
            continue

        donchian_hi = max(highs[i - DONCHIAN_ENTRY:i])
        if closes[i] <= donchian_hi:
            continue

        e = ema(closes[:i + 1], EMA_TREND)
        if e is None or closes[i] <= e:
            continue

        a = adx(highs[:i + 1], lows[:i + 1], closes[:i + 1], ADX_PERIOD)
        if a is None or a < ADX_MIN:
            continue

        atr_val = atr(highs[:i + 1], lows[:i + 1], closes[:i + 1], ATR_PERIOD)
        if atr_val is None or atr_val <= 0:
            continue

        in_pos    = True
        entry_idx = i
        entry_px  = closes[i]
        stop_px   = closes[i] - ATR_STOP_MULT * atr_val
        entry_adx = a
        worst_px  = closes[i]

    if in_pos:
        exit_px  = closes[-1]
        cost     = capital * (1 + fee_pct)
        proceeds = capital * (exit_px / entry_px) * (1 - fee_pct)
        res.trades.append(BOTrade(
            entry_idx=entry_idx, exit_idx=n - 1, entry_px=entry_px, exit_px=exit_px,
            pnl=proceeds - cost, reason="eod", entry_adx=entry_adx,
            mae_pct=(worst_px / entry_px - 1.0) * 100,
        ))

    return res


# ───────────────────────────────────────────────────────────────────────────
# Verdict + per-symbol runner
# ───────────────────────────────────────────────────────────────────────────

def _verdict(window_stats: list[dict]) -> str:
    counted = [w for w in window_stats if w["trades"] >= MIN_TRADES]
    if not counted:
        return f"FAILED — no window reached the {MIN_TRADES}-trade sample floor"
    failing = [w for w in counted if w["pf"] < PASS_PF]
    if failing:
        return "FAILED — PF < %.1f in %s" % (
            PASS_PF, ", ".join(f"{w['window']}c ({_fmt_pf(w['pf'])})" for w in failing)
        )
    if len(counted) < len(window_stats):
        under = [w for w in window_stats if w["trades"] < MIN_TRADES]
        return "MARGINAL — PF >= %.1f in every window with >=%d trades, but %s" % (
            PASS_PF, MIN_TRADES,
            ", ".join(f"{w['window']}c only had {w['trades']}" for w in under),
        )
    return f"PASS — all windows PF >= {PASS_PF}, >= {MIN_TRADES} trades each"


def _run_symbol(symbol: str, report: list[str]) -> str:
    print(f"\nFetching {WINDOWS[0]} x {TIMEFRAME} {symbol} candles from Binance ...",
          flush=True)
    try:
        candles = fetch_candles_paginated(
            exchange_id="binance", symbol=symbol, timeframe=TIMEFRAME, total_limit=WINDOWS[0],
        )
    except Exception as exc:
        report.append(f"### {symbol}\n\nFetch failed: {exc} — skipped.")
        print(f"  {symbol}: fetch failed — {exc}")
        return "SKIPPED — no data"
    if not candles:
        report.append(f"### {symbol}\n\nNo candle data returned — skipped.")
        return "SKIPPED — no data"
    print(f"  {len(candles)} candles "
          f"({candles[0].timestamp:%Y-%m-%d} -> {candles[-1].timestamp:%Y-%m-%d})")

    tag = "PRIMARY" if symbol in PRIMARY else "secondary"
    report += [
        f"### {symbol}  ({tag})",
        "",
        "| Window | Trades | PF | Win% | Return | ATR-stop% | Avg hold (bars) | Entry ADX (min/mean/max) |",
        "|--------|--------|-----|------|--------|-----------|-----------------|--------------------------|",
    ]
    window_stats: list[dict] = []
    for w in WINDOWS:
        window = candles[-w:] if len(candles) >= w else candles
        r = run_breakout_backtest(window)
        s = _pf_stats(r.pnls, RESEARCH_CAPITAL)
        s["window"] = w
        window_stats.append(s)
        adxs = [t.entry_adx for t in r.trades]
        adx_str = (f"{min(adxs):.0f}/{sum(adxs)/len(adxs):.0f}/{max(adxs):.0f}"
                   if adxs else "-")
        report.append(
            f"| {w}c | {s['trades']} | {_fmt_pf(s['pf'])} | {s['win_rate']:.0f}% "
            f"| {s['ret_pct']:+.2f}% | {r.stop_exit_pct:.0f}% | {r.avg_hold_bars:.1f} | {adx_str} |"
        )
        print(f"  {w:>5}c  trades={s['trades']:<4} PF={_fmt_pf(s['pf']):<5} "
              f"win={s['win_rate']:.0f}%  ret={s['ret_pct']:+.2f}%  "
              f"ATR-stop={r.stop_exit_pct:.0f}%  hold={r.avg_hold_bars:.1f}b")

    # Out-of-sample train/validation split
    train = slice_candles(candles, TRAIN_START, TRAIN_END)
    val   = slice_candles(candles, VAL_START, VAL_END)
    tr_r  = run_breakout_backtest(train) if len(train) > WARMUP + 2 else BOResult()
    va_r  = run_breakout_backtest(val)   if len(val)   > WARMUP + 2 else BOResult()
    tr_s  = _pf_stats(tr_r.pnls, RESEARCH_CAPITAL)
    va_s  = _pf_stats(va_r.pnls, RESEARCH_CAPITAL)
    oos_line = (
        f"OOS split — TRAIN {tr_s['trades']}t PF {_fmt_pf(tr_s['pf'])} "
        f"ret {tr_s['ret_pct']:+.2f}%  |  VALIDATION {va_s['trades']}t "
        f"PF {_fmt_pf(va_s['pf'])} ret {va_s['ret_pct']:+.2f}%"
    )
    print(f"  {oos_line}")

    verdict = _verdict(window_stats)
    soft = all(w["pf"] >= 1.0 for w in window_stats if w["trades"] >= MIN_TRADES) \
        and any(w["trades"] >= MIN_TRADES for w in window_stats)
    report += [
        "",
        f"{oos_line}",
        "",
        f"**Verdict ({symbol}): {verdict}**",
        f"  (softer PF >= 1.0 line, for reference: {'clears' if soft else 'does not clear'})",
        "",
    ]
    print(f"  -> {verdict}")
    return verdict


def main() -> None:
    print(f"\nBreakout experiment — Donchian({DONCHIAN_ENTRY}/{DONCHIAN_EXIT}) + "
          f"EMA({EMA_TREND}) + ADX(>={ADX_MIN:.0f}), ATRx{ATR_STOP_MULT} stop, "
          f"{TIMEFRAME}, fee {FEE*100:.2f}%/side, ${RESEARCH_CAPITAL:.0f} notional\n")

    report = [
        f"# Breakout / Trend-Continuation Experiment — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        "Question: does a Donchian-channel breakout strategy (Turtle-style: buy the "
        f"fresh {DONCHIAN_ENTRY}-bar high, trail with a {DONCHIAN_EXIT}-bar channel "
        "stop, no fixed take-profit) pass this project's own "
        f"PF >= {PASS_PF} / >= {MIN_TRADES}-trades-per-window walk-forward bar? "
        "Research only — see the module docstring for full methodology. A PASS "
        "authorises nothing; promotion still needs the full Validation Discipline "
        "workflow.",
        "",
        "**Why this strategy:** the live 4h strategy is a *pullback* trend-follower "
        "(buy dips inside an uptrend). *Breakout* is the other classic trend entry "
        "and the one untried trend-following shape after the 2026-08-28 strategy "
        "search (mean-reversion / grid-DCA / cross-sectional momentum all failed). "
        "It trades on different candles and lets winners run via the trailing stop.",
        "",
        "**Prior: LOW.** Four strategies tested, one works. Breakout systems have low "
        "win rates and whipsaw hardest in ranging regimes — exactly where BTC has "
        "been. This tests whether it carries real fee-net edge anyway.",
        "",
        "**Parameters (pre-registered, fixed before any result was seen):** "
        f"entry = close > {DONCHIAN_ENTRY}-bar high AND close > EMA({EMA_TREND}) AND "
        f"ADX({ADX_PERIOD}) >= {ADX_MIN:.0f}; exit = close < {DONCHIAN_EXIT}-bar low "
        f"(trailing) OR low <= entry - {ATR_STOP_MULT}xATR({ATR_PERIOD}) (hard stop, "
        f"intra-candle); {COOLDOWN_BARS}-bar cooldown; long only (Kraken spot); "
        "no fixed take-profit.",
        "",
        f"**Data:** Binance proxy, {TIMEFRAME}, fee {FEE*100:.2f}%/side (live Kraken "
        "finding — not lowered). Windows: "
        f"{'/'.join(str(w) for w in WINDOWS)} trailing candles + an OOS train/"
        f"validation split ({TRAIN_START}->{TRAIN_END} / {VAL_START}->present). "
        f"Fixed ${RESEARCH_CAPITAL:.0f} notional per trade, non-compounding. "
        "BTC/USDT + SOL/USDT are the bar that matters; ETH (currently BLOCKED) + "
        "NEAR + ENA are a 'does breakout unlock anything' secondary check.",
        "",
        "## Results",
        "",
    ]

    verdicts = {}
    for sym in SYMBOLS:
        verdicts[sym] = _run_symbol(sym, report)

    report += ["## Bottom line", ""]
    primary_pass = [s for s in PRIMARY if verdicts.get(s, "").startswith("PASS")]
    any_pass     = [s for s, v in verdicts.items() if v.startswith("PASS")]
    if primary_pass:
        report.append(
            "BTC and/or SOL cleared the bar: " + ", ".join(primary_pass) + ". This does "
            "NOT promote anything — next step is a real implementation in `bot/strategy/`, "
            "the full 3-window walk-forward, a fresh hash stamp, and a per-symbol "
            "re-check per CLAUDE.md Validation Discipline. Also weigh the operational "
            "cost of a second live strategy against the marginal edge, and the "
            "multiple-testing bias (this is the 5th strategy tested)."
        )
    elif any_pass:
        report.append(
            "No primary (BTC/SOL) pass, but a secondary symbol cleared: "
            + ", ".join(any_pass) + ". A single-symbol pass on the 5th strategy tested "
            "is most likely multiple-testing noise (see `expert-practices-benchmark`). "
            "Not actionable without a primary pass."
        )
    else:
        report.append(
            "No symbol cleared PF >= %.1f across all windows with >= %d trades. "
            "Breakout / trend-continuation does not carry enough fee-net edge on this "
            "data to justify a second live strategy. This is the 5th strategy tested "
            "and the 4th to fail — the consistent finding (beating a focused position "
            "in the few things that work, net of costs, is hard) stands. "
            "Strategy search remains concluded." % (PASS_PF, MIN_TRADES)
        )

    os.makedirs("logs", exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    print(f"\nReport written to {REPORT_PATH}")


if __name__ == "__main__":
    main()
