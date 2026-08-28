#!/usr/bin/env python
"""
mean_reversion_experiment.py — does a Bollinger/RSI mean-reversion strategy
clear this project's own PF walk-forward bar on BTC and SOL?

RESEARCH ONLY. Touches no live code: no bot/strategy/* files, no .env, no
bot/main.py, no CapitalPool, no live executor, no fingerprint. A PASS here
authorises nothing by itself — any promotion still requires the full
Validation Discipline workflow in CLAUDE.md (fresh 3-window walk-forward on
the promoted code, hash stamp, per-symbol re-check).

Why this exists: the live 4h strategy is trend-following (ADX gate + pullback
entry) and by design sits flat in ranging/choppy markets — exactly the
regime where a mean-reversion strategy is supposed to work. This script asks
whether the complementary strategy actually has edge, or whether "trade more
in the chop" just pays more fees.

Why a SEPARATE engine, not bot/backtest/engine.py: same reasoning the grid
experiment documents. That engine's fill model and signal wiring are specific
to the trend strategy. Here, entries are indicator-driven (known at candle
close, filled at close — same convention as the live bot's candle-close
signal) but the protective stop is a bare price level that can be pierced
intra-candle, so the stop is checked against each candle's LOW, not its
close. Documented per-branch below.

Data: BTC/USDT and SOL/USDT via Binance — the standard proxy every validation
script in this repo uses (CLAUDE.md "Exchange Setup": Kraken OHLCV history is
capped at ~720 candles, Binance has 5000+; price diff confirmed ~0.05%).
SOL/USDT is included because SOL/CAD is live.

Methodology — parameters are PRE-REGISTERED: every constant below is fixed in
the source, chosen from round defensible values BEFORE any window's results
were inspected. No parameter is tuned against the walk-forward output. If
none of the windows pass, the honest answer is "this doesn't pass," not
"try until one does."

Usage:  .venv/bin/python mean_reversion_experiment.py
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

from bot.data.historical_feed import Candle, fetch_candles_paginated
from bot.indicators.indicators import adx, bollinger_bands, rsi

# ── Pre-registered strategy parameters (do not tune against results) ──────
ADX_RANGING_MAX = 20.0   # only trade when ADX(14) < this — the ranging regime,
                          # the inverse of the live trend strategy's ADX>=18 gate
RSI_ENTRY_MAX   = 35.0   # long only when RSI(14) below this (oversold)
BB_PERIOD       = 20
BB_STD          = 2.0
RSI_PERIOD      = 14
ADX_PERIOD      = 14
STOP_PCT        = 0.04   # protective stop 4% below entry (wider than the trend
                          # strategy's ~1.5-3% — mean reversion needs noise room),
                          # checked intra-candle against the candle low
TIME_STOP_BARS  = 18     # exit at close after this many bars (72h at 4h) if
                          # neither target nor stop hit — a reversion that hasn't
                          # happened in 3 days probably isn't coming
COOLDOWN_BARS   = 1      # bars to wait after an exit before a new entry
WARMUP          = 35     # ADX(14) needs 2*period+1 = 29 candles; 35 is safe headroom

# ── Run configuration ───────────────────────────────────────────────────
SYMBOLS    = ["BTC/USDT", "SOL/USDT"]
TIMEFRAME  = os.getenv("BACKTEST_TIMEFRAME", "4h")
FEE        = float(os.getenv("BACKTEST_FEE_PCT", "0.008"))   # 0.8%/side, live
                                                              # Kraken finding — do not lower
WINDOWS    = [5000, 3000, 1000]   # trailing candles, same shape as the screen tables
MIN_TRADES = 10                   # per-window sample floor
PASS_PF    = 1.2                  # promotion bar (CLAUDE.md capital/symbol gate); the
                                   # report also shows the softer PF>=1.0 line for reference

RESEARCH_CAPITAL = 1000.0   # fixed notional per trade, non-compounding — independent
                             # of the live $77/$376 CAD slot sizing; disclosed, not hidden

REPORT_PATH = os.path.join(
    "logs", f"mean_reversion_experiment_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
)


# ─────────────────────────────────────────────────────────────────────────
# Trade-stats helper — same PF convention as bot/backtest/metrics.py and
# grid_dca_experiment.py: profit_factor = gross_profit / gross_loss;
# inf if there are wins and no losses, 0.0 if no trades at all.
# ─────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────
# Mean-reversion backtester
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class MRTrade:
    entry_idx:  int
    exit_idx:   int
    entry_px:   float
    exit_px:    float
    pnl:        float
    reason:     str      # "target" | "stop" | "time" | "eod"
    entry_adx:  float


@dataclass
class MRResult:
    trades:     list[MRTrade] = field(default_factory=list)

    @property
    def pnls(self) -> list[float]:
        return [t.pnl for t in self.trades]

    @property
    def sl_exit_pct(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.reason == "stop") / len(self.trades) * 100

    @property
    def avg_hold_bars(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.exit_idx - t.entry_idx for t in self.trades) / len(self.trades)


def _entry_check(
    c_slice: list[float],
    h_slice: list[float],
    l_slice: list[float],
) -> tuple[bool, float | None]:
    """
    The three entry gates, evaluated on a growing slice ending at the current
    candle (c_slice[-1] is the candle being decided; nothing beyond it is
    read). Returns (enter?, adx_value) — the adx value is passed back so the
    caller can record it on the trade even when the gate passes.

      close below lower Bollinger(BB_PERIOD, BB_STD)   — pierced the band
      RSI(RSI_PERIOD)  < RSI_ENTRY_MAX                 — oversold
      ADX(ADX_PERIOD)  < ADX_RANGING_MAX               — ranging regime
    """
    bb = bollinger_bands(c_slice, BB_PERIOD, BB_STD)
    if bb is None:
        return False, None
    _, _, lower = bb
    if c_slice[-1] >= lower:
        return False, None

    r = rsi(c_slice, RSI_PERIOD)
    if r is None or r >= RSI_ENTRY_MAX:
        return False, None

    a = adx(h_slice, l_slice, c_slice, ADX_PERIOD)
    if a is None or a >= ADX_RANGING_MAX:
        return False, a
    return True, a


def _exit_reason(
    low_i:     float,
    close_i:   float,
    entry_px:  float,
    bars_held: int,
    middle:    float | None,
) -> str | None:
    """
    Exit precedence on the current candle, holding a position entered
    `bars_held` candles ago:

      "stop"   — low pierced entry * (1 - STOP_PCT)   (bare level, intra-candle)
      "target" — close reached the middle band        (reversion complete)
      "time"   — held TIME_STOP_BARS candles          (reversion never came)
      None     — keep holding
    """
    if low_i <= entry_px * (1.0 - STOP_PCT):
        return "stop"
    if middle is not None and close_i >= middle:
        return "target"
    if bars_held >= TIME_STOP_BARS:
        return "time"
    return None


def run_mean_reversion_backtest(
    candles: list[Candle],
    capital: float = RESEARCH_CAPITAL,
    fee_pct: float = FEE,
) -> MRResult:
    """
    Long-only Bollinger/RSI mean reversion.

    On candle i (decisions use closes[:i+1] — the current close is known, the
    same convention as the live bot's candle-close signal; nothing beyond i
    is ever read):

      FLAT, and i - last_exit > COOLDOWN_BARS:
        enter at close[i] when
          ADX(14) on [:i+1]           <  ADX_RANGING_MAX   (ranging regime)
          close[i]                    <  lower Bollinger(20, 2.0)
          RSI(14) on [:i+1]           <  RSI_ENTRY_MAX      (oversold)

      HOLDING (entered on candle j < i):
        checked in this order on candle i —
          STOP  : candle i low  <= entry * (1 - STOP_PCT)  -> exit at the stop
                  price (bare level, can be pierced intra-candle -> checked
                  against the LOW, not the close)
          TARGET: candle i close >= middle Bollinger(20)   -> exit at close[i]
                  (reversion to the mean completed)
          TIME  : i - j >= TIME_STOP_BARS                  -> exit at close[i]
        no entry and exit on the same candle.

    Any position still open at the last candle is marked to market at that
    close (reason "eod") so an open trade is not silently dropped.

    P&L per trade uses a fixed notional (`capital`), non-compounding:
      cost     = notional * (1 + fee_pct)
      proceeds = notional * (exit/entry) * (1 - fee_pct)
      pnl      = proceeds - cost
    """
    res = MRResult()
    if len(candles) <= WARMUP:
        return res

    closes = [c.close for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]

    in_pos      = False
    entry_idx   = -1
    entry_px    = 0.0
    entry_adx   = 0.0
    last_exit   = -(COOLDOWN_BARS + 1)

    def _record(exit_idx: int, exit_px: float, reason: str) -> None:
        cost     = capital * (1.0 + fee_pct)
        proceeds = capital * (exit_px / entry_px) * (1.0 - fee_pct)
        res.trades.append(MRTrade(
            entry_idx=entry_idx, exit_idx=exit_idx,
            entry_px=entry_px, exit_px=exit_px,
            pnl=proceeds - cost, reason=reason, entry_adx=entry_adx,
        ))

    for i in range(WARMUP, len(candles)):
        c_slice = closes[: i + 1]

        if in_pos:
            bb = bollinger_bands(c_slice, BB_PERIOD, BB_STD)
            middle = bb[1] if bb else None
            reason = _exit_reason(lows[i], closes[i], entry_px, i - entry_idx, middle)
            if reason == "stop":
                _record(i, entry_px * (1.0 - STOP_PCT), "stop")
                in_pos, last_exit = False, i
            elif reason is not None:
                _record(i, closes[i], reason)
                in_pos, last_exit = False, i
            continue

        # FLAT — no entry and exit on the same candle
        if i - last_exit <= COOLDOWN_BARS:
            continue

        ok, a = _entry_check(c_slice, highs[: i + 1], lows[: i + 1])
        if ok:
            in_pos, entry_idx, entry_px, entry_adx = True, i, closes[i], a

    if in_pos:
        _record(len(candles) - 1, closes[-1], "eod")

    return res


# ─────────────────────────────────────────────────────────────────────────
# 3-window walk-forward
# ─────────────────────────────────────────────────────────────────────────

def _verdict(window_stats: list[dict]) -> str:
    """PASS / MARGINAL / FAILED against PF >= PASS_PF in every window that
    reached MIN_TRADES."""
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
    candles = fetch_candles_paginated(
        exchange_id="binance", symbol=symbol, timeframe=TIMEFRAME, total_limit=WINDOWS[0],
    )
    if not candles:
        line = f"### {symbol}\n\nNo candle data returned — skipped."
        report.append(line)
        print(f"  {symbol}: no data")
        return "SKIPPED — no data"
    print(f"  {len(candles)} candles "
          f"({candles[0].timestamp:%Y-%m-%d} -> {candles[-1].timestamp:%Y-%m-%d})")

    report += [
        f"### {symbol}",
        "",
        "| Window | Trades | PF | Win% | Return | SL-exit% | Avg hold (bars) | Entry ADX (min/mean/max) |",
        "|--------|--------|-----|------|--------|----------|-----------------|--------------------------|",
    ]
    window_stats: list[dict] = []
    for w in WINDOWS:
        window = candles[-w:] if len(candles) >= w else candles
        r = run_mean_reversion_backtest(window)
        s = _pf_stats(r.pnls, RESEARCH_CAPITAL)
        s["window"] = w
        window_stats.append(s)
        adxs = [t.entry_adx for t in r.trades]
        adx_str = (f"{min(adxs):.0f}/{sum(adxs)/len(adxs):.0f}/{max(adxs):.0f}"
                   if adxs else "-")
        report.append(
            f"| {w}c | {s['trades']} | {_fmt_pf(s['pf'])} | {s['win_rate']:.0f}% "
            f"| {s['ret_pct']:+.2f}% | {r.sl_exit_pct:.0f}% | {r.avg_hold_bars:.1f} | {adx_str} |"
        )
        print(f"  {w:>5}c  trades={s['trades']:<4} PF={_fmt_pf(s['pf']):<5} "
              f"win={s['win_rate']:.0f}%  ret={s['ret_pct']:+.2f}%  "
              f"SL-exit={r.sl_exit_pct:.0f}%  hold={r.avg_hold_bars:.1f}b")

    verdict = _verdict(window_stats)
    soft = all(w["pf"] >= 1.0 for w in window_stats if w["trades"] >= MIN_TRADES) \
        and any(w["trades"] >= MIN_TRADES for w in window_stats)
    report += [
        "",
        f"**Verdict ({symbol}): {verdict}**",
        f"  (softer PF >= 1.0 line, for reference: {'clears' if soft else 'does not clear'})",
        "",
    ]
    print(f"  -> {verdict}")
    return verdict


def main() -> None:
    print(f"\nMean-reversion experiment — Bollinger({BB_PERIOD},{BB_STD}) / "
          f"RSI(<{RSI_ENTRY_MAX:.0f}) / ADX(<{ADX_RANGING_MAX:.0f}), "
          f"{TIMEFRAME}, fee {FEE*100:.2f}%/side, ${RESEARCH_CAPITAL:.0f} notional\n")

    report = [
        f"# Mean-Reversion Experiment — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        "Question: does a Bollinger/RSI mean-reversion strategy pass this project's "
        f"own PF >= {PASS_PF} / >= {MIN_TRADES}-trades-per-window walk-forward bar on "
        "BTC and SOL? Research only — see the module docstring in "
        "`mean_reversion_experiment.py` for full methodology. A PASS authorises "
        "nothing; promotion still needs the full Validation Discipline workflow.",
        "",
        "**Why this strategy:** the live 4h strategy is trend-following and sits flat "
        "in ranging markets by design (ADX >= 18 gate). This tests whether the "
        "complementary regime — buy oversold dips inside a range, exit on reversion "
        "to the 20-period mean — has genuine edge, net of the real 0.8%/side Kraken "
        "fee, or whether trading the chop just bleeds fees.",
        "",
        "**Parameters (pre-registered, fixed before any result was seen):** "
        f"entry = close below lower Bollinger({BB_PERIOD}, {BB_STD}sigma) AND "
        f"RSI({RSI_PERIOD}) < {RSI_ENTRY_MAX:.0f} AND ADX({ADX_PERIOD}) < "
        f"{ADX_RANGING_MAX:.0f}; exit = close >= middle band (target) / "
        f"{STOP_PCT*100:.0f}% stop below entry (intra-candle vs low) / "
        f"{TIME_STOP_BARS}-bar time stop; {COOLDOWN_BARS}-bar cooldown after exit; "
        "long only (Kraken spot).",
        "",
        f"**Data:** Binance proxy ({', '.join(SYMBOLS)}), {TIMEFRAME}, fee "
        f"{FEE*100:.2f}%/side (live Kraken finding — not lowered). Windows: "
        f"{'/'.join(str(w) for w in WINDOWS)} trailing candles. Fixed "
        f"${RESEARCH_CAPITAL:.0f} notional per trade, non-compounding.",
        "",
        "**Complementarity note:** every entry here is gated on ADX < "
        f"{ADX_RANGING_MAX:.0f}; the live trend strategy's regime gate requires "
        "ADX >= 18 to allow a BUY. The two are near mutually exclusive by regime "
        "— the 'Entry ADX' column shows the actual distribution.",
        "",
        "## Results",
        "",
    ]

    verdicts = {}
    for sym in SYMBOLS:
        verdicts[sym] = _run_symbol(sym, report)

    report += [
        "## Bottom line",
        "",
    ]
    passed = [s for s, v in verdicts.items() if v.startswith("PASS")]
    if passed:
        report.append(
            "One or more symbols cleared the bar: " + ", ".join(passed) + ". This does "
            "NOT promote anything — next step is the full 3-window walk-forward on a "
            "real implementation (`bot/strategy/`), a fresh hash stamp, and a per-symbol "
            "re-check, per CLAUDE.md Validation Discipline. Also weigh the operational "
            "cost of running a second strategy against the marginal edge."
        )
    else:
        report.append(
            "No symbol cleared PF >= %.1f across all windows with >= %d trades. "
            "On this data, with real fees, the mean-reversion regime does not carry "
            "enough edge to justify a second live strategy. Not worth promoting; "
            "re-run only if the parameters or the fee assumption change for a "
            "documented reason." % (PASS_PF, MIN_TRADES)
        )

    os.makedirs("logs", exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    print(f"\nReport written to {REPORT_PATH}")


if __name__ == "__main__":
    main()
