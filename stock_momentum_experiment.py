#!/usr/bin/env python
"""
stock_momentum_experiment.py — does a cross-sectional (relative) momentum
strategy beat buy-and-hold on a risk-adjusted basis, out of sample?

RESEARCH ONLY. Touches no live code: no `bot/strategy/*`, no
`stock_bot/strategy/*`, no `.env`, no `stock_bot/main.py`, no executor. A
PASS here authorises nothing — promotion needs the full Validation
Discipline workflow (CLAUDE.md).

Why this, after mean-reversion and grid/DCA both failed
(`.memory/decisions/mean-reversion-experiment-2026-08-28.md`): those are
single-symbol *timing* strategies, the same shape as the live trend
strategy. Cross-sectional momentum is structurally different — rank a
universe by trailing return, hold the top slice, rebalance monthly. It's
the most-replicated equity-market anomaly (Jegadeesh-Titman 1993 and every
study since), and it's a portfolio/rotation method, not an entry trigger.

Construction (pre-registered — every constant fixed BEFORE any result was
inspected):
  - Universe: a fixed ~50-name liquid US large-cap set spanning sectors
    (below). Chosen for liquidity + sector spread, not past performance.
  - Signal: 6-1 momentum — total return over the 126 trading days ending
    21 days ago (the standard skip-month, which drops the short-term
    reversal that contaminates a raw trailing return).
  - Hold: the top TOP_N by score, equal-weighted.
  - Rebalance: every 21 trading days.
  - Regime filter (tested BOTH ways): only hold when SPY closes above its
    200-day SMA — the classic momentum-crash guard (momentum blows up in
    sharp V-reversals: 2009, Nov 2020).
  - Long only, whole shares, IBKR cost model (commission + 15 bps/fill).

Gate: in the OUT-OF-SAMPLE (validation) window the strategy must beat BOTH
benchmarks (SPY buy-and-hold, equal-weight-hold-all) on annualised Sharpe,
AND keep max drawdown within 1.1x SPY's. Reported for both regime-filter
settings.

Usage:  .venv/bin/python stock_momentum_experiment.py
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

from stock_bot.data.price_feed import Candle, fetch_candles

# ── Pre-registered universe (fixed before any result seen) ───────────────
_UNIVERSE = [
    # mega-cap tech / comm
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AVGO", "ORCL", "CRM",
    "ADBE", "AMD", "CSCO", "INTC", "QCOM", "TXN", "NFLX",
    # financials
    "JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "AXP", "BLK",
    # healthcare
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "MRNA",
    # consumer
    "WMT", "COST", "PG", "KO", "PEP", "MCD", "NKE", "HD", "LOW",
    # energy / industrials
    "XOM", "CVX", "CAT", "BA", "HON", "UPS", "LMT",
    # other
    "DIS", "T", "VZ", "PLTR", "GM",
]
_BENCH = "SPY"

# ── Pre-registered strategy parameters ──────────────────────────────────
LOOKBACK_DAYS = 126    # 6 months
SKIP_DAYS     = 21     # skip the most recent month (short-term reversal)
TOP_N         = 10     # hold the top 10 by momentum, equal-weighted
REBAL_DAYS    = 21     # rebalance monthly
SPY_MA        = 200    # regime filter: SPY > its 200d SMA
# enough history for BOTH the momentum lookback and the SPY 200d SMA, so the
# regime filter applies from the very first rebalance (not silently skipped)
WARMUP        = max(LOOKBACK_DAYS + SKIP_DAYS + 5, SPY_MA + 1)

# ── Run configuration ──────────────────────────────────────────────────
CAPITAL       = 100_000.0
SLIPPAGE_BPS  = 15
COMMISSION_PER_SHARE = 0.005
COMMISSION_MIN       = 1.0
FETCH_DAYS    = int(os.getenv("STOCK_MOM_DAYS", "1800"))
VALIDATION_FRAC = 0.40   # most-recent 40% of the aligned history is out-of-sample
TRADING_DAYS_YR = 252

REPORT_PATH = os.path.join(
    "logs", f"stock_momentum_experiment_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
)


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────

def _momentum_score(closes: list[float], lookback: int = LOOKBACK_DAYS,
                    skip: int = SKIP_DAYS) -> float | None:
    """Total return over the `lookback` days ending `skip` days before the
    last close. None if there isn't enough history."""
    need = lookback + skip + 1
    if len(closes) < need:
        return None
    end = closes[-1 - skip]
    start = closes[-1 - skip - lookback]
    if start <= 0:
        return None
    return end / start - 1.0


def _select_top(scores: dict[str, float], n: int) -> list[str]:
    """The n highest-scoring symbols, ties broken by symbol name for
    determinism."""
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [s for s, _ in ranked[:n]]


def _sharpe(daily_returns: list[float]) -> float:
    """Annualised Sharpe, risk-free = 0. 0.0 if degenerate."""
    if len(daily_returns) < 2:
        return 0.0
    mean = sum(daily_returns) / len(daily_returns)
    var = sum((r - mean) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return (mean / sd) * math.sqrt(TRADING_DAYS_YR)


def _max_drawdown(equity: list[float]) -> float:
    """Largest peak-to-trough drop as a positive fraction."""
    peak = equity[0] if equity else 0.0
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def _cagr(equity: list[float], days: int) -> float:
    if not equity or equity[0] <= 0 or days <= 0:
        return 0.0
    yrs = days / TRADING_DAYS_YR
    return (equity[-1] / equity[0]) ** (1.0 / yrs) - 1.0 if yrs > 0 else 0.0


def _commission(shares: float) -> float:
    return max(COMMISSION_MIN, shares * COMMISSION_PER_SHARE)


# ─────────────────────────────────────────────────────────────────────────
# Backtest
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class MomResult:
    equity:  list[float] = field(default_factory=list)   # daily portfolio value
    dates:   list = field(default_factory=list)
    rebalances: int = 0
    total_cost: float = 0.0

    @property
    def daily_returns(self) -> list[float]:
        return [self.equity[i] / self.equity[i - 1] - 1.0
                for i in range(1, len(self.equity)) if self.equity[i - 1] > 0]


def run_momentum(
    price_matrix: dict[str, list[float]],
    dates:        list,
    spy_closes:   list[float] | None,
    use_regime:   bool,
    start_i:      int,
    end_i:        int,
    capital:      float = CAPITAL,
) -> MomResult:
    """
    Walk `dates[start_i:end_i]`. On each rebalance day, score every symbol
    with enough history, optionally gate on the SPY regime, pick the top
    TOP_N, move to an equal-weight target (whole shares), pay
    commission + slippage on every buy and sell. Mark the book daily.
    """
    res = MomResult()
    slip = SLIPPAGE_BPS / 10_000.0
    cash = capital
    holdings: dict[str, int] = {}     # symbol -> shares

    def _book_value(i: int) -> float:
        v = cash
        for s, sh in holdings.items():
            v += sh * price_matrix[s][i]
        return v

    for i in range(start_i, end_i):
        if (i - start_i) % REBAL_DAYS == 0:
            invested = True
            if use_regime and spy_closes is not None and i >= SPY_MA:
                sma = sum(spy_closes[i - SPY_MA + 1: i + 1]) / SPY_MA
                invested = spy_closes[i] > sma

            scores: dict[str, float] = {}
            if invested:
                for s, closes in price_matrix.items():
                    sc = _momentum_score(closes[: i + 1])
                    if sc is not None and sc > 0:      # only positive-momentum names
                        scores[s] = sc
            target = _select_top(scores, TOP_N) if scores else []

            # sell everything not in target (and everything, if flat)
            for s in list(holdings):
                if s not in target:
                    px = price_matrix[s][i] * (1.0 - slip)
                    sh = holdings.pop(s)
                    cash += sh * px - _commission(sh)
                    res.total_cost += _commission(sh) + sh * price_matrix[s][i] * slip

            # rebalance to equal weight across target
            if target:
                book = _book_value(i)
                per_name = book / len(target)
                for s in target:
                    px_raw = price_matrix[s][i]
                    want = int(per_name / px_raw) if px_raw > 0 else 0
                    have = holdings.get(s, 0)
                    if want > have:                     # buy up
                        add = want - have
                        buy_px = px_raw * (1.0 + slip)
                        cost = add * buy_px + _commission(add)
                        if cost <= cash:
                            cash -= cost
                            holdings[s] = want
                            res.total_cost += _commission(add) + add * px_raw * slip
                    elif want < have:                   # trim
                        cut = have - want
                        sell_px = px_raw * (1.0 - slip)
                        cash += cut * sell_px - _commission(cut)
                        holdings[s] = want
                        res.total_cost += _commission(cut) + cut * px_raw * slip
            res.rebalances += 1

        res.equity.append(_book_value(i))
        res.dates.append(dates[i])

    return res


def _buy_and_hold(closes: list[float], start_i: int, end_i: int,
                  capital: float = CAPITAL) -> list[float]:
    px0 = closes[start_i]
    sh = int(capital / (px0 * (1.0 + SLIPPAGE_BPS / 10_000.0)))
    cash = capital - sh * px0 * (1.0 + SLIPPAGE_BPS / 10_000.0) - _commission(sh)
    return [cash + sh * closes[i] for i in range(start_i, end_i)]


def _equal_weight_hold(price_matrix: dict[str, list[float]], start_i: int,
                       end_i: int, capital: float = CAPITAL) -> list[float]:
    """Buy every name equal-weight at start_i, hold (no rebalancing) — the
    'own the whole universe' benchmark."""
    slip = SLIPPAGE_BPS / 10_000.0
    per = capital / len(price_matrix)
    holds, cash = {}, capital
    for s, closes in price_matrix.items():
        px = closes[start_i] * (1.0 + slip)
        sh = int(per / px) if px > 0 else 0
        cost = sh * px + _commission(sh)
        if cost <= cash and sh > 0:
            cash -= cost
            holds[s] = sh
    return [cash + sum(sh * price_matrix[s][i] for s, sh in holds.items())
            for i in range(start_i, end_i)]


# ─────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────

def _load_aligned() -> tuple[dict[str, list[float]], list, list[float]]:
    """Fetch every universe symbol + SPY, align on the common trading days."""
    raw: dict[str, list[Candle]] = {}
    for s in _UNIVERSE + [_BENCH]:
        c = fetch_candles(s, "1d", FETCH_DAYS)
        if c and len(c) > WARMUP:
            raw[s] = c
        else:
            print(f"  {s}: thin/no data ({len(c or [])}) — excluded")
    if _BENCH not in raw:
        raise SystemExit("SPY data unavailable — cannot benchmark; aborting.")

    common = None
    for s, c in raw.items():
        ds = {cd.timestamp.date() for cd in c}
        common = ds if common is None else (common & ds)
    common = sorted(common)

    matrix: dict[str, list[float]] = {}
    for s, c in raw.items():
        by_date = {cd.timestamp.date(): cd.close for cd in c}
        matrix[s] = [by_date[d] for d in common]
    spy = matrix.pop(_BENCH)
    return matrix, common, spy


def _stats_row(label: str, equity: list[float], days: int) -> tuple[str, dict]:
    s = {"cagr": _cagr(equity, days), "sharpe": _sharpe(_returns(equity)),
         "mdd": _max_drawdown(equity), "final": equity[-1] if equity else 0.0}
    row = (f"| {label} | {s['cagr']*100:+.1f}% | {s['sharpe']:.2f} "
           f"| {s['mdd']*100:.1f}% | ${s['final']:,.0f} |")
    return row, s


def _returns(equity: list[float]) -> list[float]:
    return [equity[i] / equity[i - 1] - 1.0
            for i in range(1, len(equity)) if equity[i - 1] > 0]


def _verdict(strat: dict, spy: dict, ew: dict) -> str:
    beats_spy = strat["sharpe"] > spy["sharpe"]
    beats_ew  = strat["sharpe"] > ew["sharpe"]
    dd_ok     = strat["mdd"] <= spy["mdd"] * 1.1
    if beats_spy and beats_ew and dd_ok:
        return "PASS — higher Sharpe than SPY and equal-weight-hold, drawdown within 1.1x SPY"
    reasons = []
    if not beats_spy: reasons.append(f"Sharpe {strat['sharpe']:.2f} <= SPY {spy['sharpe']:.2f}")
    if not beats_ew:  reasons.append(f"Sharpe {strat['sharpe']:.2f} <= equal-weight {ew['sharpe']:.2f}")
    if not dd_ok:     reasons.append(f"maxDD {strat['mdd']*100:.0f}% > 1.1x SPY {spy['mdd']*100:.0f}%")
    return "FAILED — " + "; ".join(reasons)


def main() -> None:
    print(f"\nCross-sectional momentum experiment — {len(_UNIVERSE)} names, "
          f"6-1 momentum, top {TOP_N} equal-weight, monthly rebalance, "
          f"{SLIPPAGE_BPS}bps + IBKR commission\n")
    matrix, dates, spy = _load_aligned()
    n = len(dates)
    print(f"  {len(matrix)} symbols aligned over {n} days "
          f"({dates[0]} -> {dates[-1]})\n")

    val_start = max(WARMUP + REBAL_DAYS, int(n * (1.0 - VALIDATION_FRAC)))
    full_start = WARMUP

    report = [
        f"# Cross-Sectional Momentum Experiment — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        "Question: does a relative-momentum rotation (rank a fixed liquid large-cap "
        f"universe by 6-1 momentum, hold the top {TOP_N} equal-weight, rebalance "
        "monthly) beat buy-and-hold on a risk-adjusted basis OUT OF SAMPLE? Research "
        "only — see the module docstring. A PASS authorises nothing.",
        "",
        "**Why test this** after mean-reversion and grid/DCA both failed: those are "
        "single-symbol timing strategies, the same shape as the live trend strategy. "
        "This is a portfolio rotation method — the most-replicated equity anomaly "
        "(Jegadeesh-Titman 1993).",
        "",
        f"**Pre-registered:** universe = {len(_UNIVERSE)} fixed liquid large-caps "
        "(sector-spread, not performance-picked); signal = total return over the "
        f"{LOOKBACK_DAYS}d ending {SKIP_DAYS}d ago; hold top {TOP_N} equal-weight; "
        f"rebalance every {REBAL_DAYS}d; optional SPY>{SPY_MA}d-SMA regime gate "
        "(tested both ways); long only, whole shares, IBKR cost model.",
        "",
        f"**Data:** yfinance daily, {FETCH_DAYS}-day fetch, aligned to {n} common "
        f"trading days ({dates[0]} → {dates[-1]}). Validation = most-recent "
        f"{VALIDATION_FRAC*100:.0f}% ({n - val_start} days).",
        "",
        f"**Gate (validation window only):** strategy Sharpe > SPY AND > equal-weight-"
        "hold, AND max drawdown <= 1.1x SPY's.",
        "",
    ]

    for regime_on in (False, True):
        tag = "WITH SPY>200d regime filter" if regime_on else "NO regime filter"
        print("=" * 66); print(tag); print("=" * 66)
        report += [f"## {tag}", "",
                   "| Window | Book | CAGR | Sharpe | MaxDD | Final |",
                   "|--------|------|------|--------|-------|-------|"]

        for wlabel, s_i in (("full", full_start), ("validation", val_start)):
            e_i = n
            mom = run_momentum(matrix, dates, spy, regime_on, s_i, e_i)
            spy_eq = _buy_and_hold(spy, s_i, e_i)
            ew_eq  = _equal_weight_hold(matrix, s_i, e_i)
            days = e_i - s_i

            for lbl, eq in (("momentum", mom.equity), ("SPY hold", spy_eq),
                            ("equal-wt hold", ew_eq)):
                row, st = _stats_row(f"{wlabel} — {lbl}", eq, days)
                report.append(row)
                if wlabel == "validation" and lbl == "momentum":  mom_v = st
                if wlabel == "validation" and lbl == "SPY hold":   spy_v = st
                if wlabel == "validation" and lbl == "equal-wt hold": ew_v = st

            print(f"  {wlabel:<11} mom CAGR {_cagr(mom.equity, days)*100:+.1f}% "
                  f"Sharpe {_sharpe(_returns(mom.equity)):.2f} DD {_max_drawdown(mom.equity)*100:.0f}%  "
                  f"| SPY Sharpe {_sharpe(_returns(spy_eq)):.2f}  "
                  f"| rebals {mom.rebalances} cost ${mom.total_cost:,.0f}")

        v = _verdict(mom_v, spy_v, ew_v)
        report += ["", f"**Verdict ({tag}): {v}**", ""]
        print(f"  -> {v}")

    report += ["## Bottom line", ""]
    os.makedirs("logs", exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    print(f"\nReport written to {REPORT_PATH}")


if __name__ == "__main__":
    main()
