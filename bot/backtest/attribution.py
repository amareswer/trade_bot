"""
Trade attribution analysis.

For every completed trade (BUY → SELL pair), records the indicator
state at entry and compares winners vs losers.

The goal is not parameter optimisation — it is to answer:
  "What distinguishes the 20 winners from the 49 losers?"

If winners and losers look statistically identical, the entry signal
has no predictive power. If they separate cleanly on one variable
(e.g. ADX > 35 → mostly wins, ADX < 28 → mostly losses), that's
an actionable finding.

Usage (called from backtest.py after engine.run()):
    from bot.backtest.attribution import compute_attribution, \
        print_attribution, save_attribution_csv

    report = compute_attribution(result)
    print_attribution(report)
    save_attribution_csv(report.records)
"""
from __future__ import annotations

import csv
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    """One complete round-trip trade (BUY + SELL)."""
    # Entry
    entry_time:       str
    entry_price:      float
    adx:              Optional[float]
    rsi:              Optional[float]
    ema_fast:         Optional[float]
    ema_slow:         Optional[float]
    ema_spread_pct:   Optional[float]   # abs(fast-slow)/slow × 100
    trend:            str               # BULLISH / BEARISH / NEUTRAL

    # Exit
    exit_time:        str
    exit_price:       float
    exit_reason:      str               # strategy / stop_loss / take_profit
    pnl:              float
    return_pct:       float             # (exit - entry) / entry × 100
    holding_hours:    float
    winner:           bool              # pnl > 0


@dataclass
class GroupStats:
    """Summary statistics for a group of trades (winners or losers)."""
    count:            int
    adx_mean:         Optional[float]
    adx_median:       Optional[float]
    adx_q25:          Optional[float]
    adx_q75:          Optional[float]
    rsi_mean:         Optional[float]
    rsi_median:       Optional[float]
    rsi_q25:          Optional[float]
    rsi_q75:          Optional[float]
    ema_spread_mean:  Optional[float]
    ema_spread_median:Optional[float]
    holding_mean:     float
    holding_median:   float
    exit_sl:          int
    exit_tp:          int
    exit_strategy:    int


@dataclass
class AttributionReport:
    records:  list[TradeRecord]
    winners:  GroupStats
    losers:   GroupStats
    all:      GroupStats


# ── Helpers ───────────────────────────────────────────────────────────────────

def _quartile(data: list[float], q: float) -> Optional[float]:
    if not data:
        return None
    sorted_d = sorted(data)
    n = len(sorted_d)
    idx = q * (n - 1)
    lo, hi = int(idx), min(int(idx) + 1, n - 1)
    frac = idx - lo
    return sorted_d[lo] + frac * (sorted_d[hi] - sorted_d[lo])


def _group_stats(records: list[TradeRecord]) -> GroupStats:
    if not records:
        return GroupStats(
            count=0,
            adx_mean=None, adx_median=None, adx_q25=None, adx_q75=None,
            rsi_mean=None, rsi_median=None, rsi_q25=None, rsi_q75=None,
            ema_spread_mean=None, ema_spread_median=None,
            holding_mean=0.0, holding_median=0.0,
            exit_sl=0, exit_tp=0, exit_strategy=0,
        )

    adx_vals    = [r.adx            for r in records if r.adx            is not None]
    rsi_vals    = [r.rsi            for r in records if r.rsi            is not None]
    spread_vals = [r.ema_spread_pct for r in records if r.ema_spread_pct is not None]
    hold_vals   = [r.holding_hours  for r in records]

    def _safe_mean(lst):   return statistics.mean(lst)   if lst else None
    def _safe_median(lst): return statistics.median(lst) if lst else None

    return GroupStats(
        count             = len(records),
        adx_mean          = _safe_mean(adx_vals),
        adx_median        = _safe_median(adx_vals),
        adx_q25           = _quartile(adx_vals, 0.25),
        adx_q75           = _quartile(adx_vals, 0.75),
        rsi_mean          = _safe_mean(rsi_vals),
        rsi_median        = _safe_median(rsi_vals),
        rsi_q25           = _quartile(rsi_vals, 0.25),
        rsi_q75           = _quartile(rsi_vals, 0.75),
        ema_spread_mean   = _safe_mean(spread_vals),
        ema_spread_median = _safe_median(spread_vals),
        holding_mean      = _safe_mean(hold_vals)   or 0.0,
        holding_median    = _safe_median(hold_vals) or 0.0,
        exit_sl           = sum(1 for r in records if r.exit_reason == "stop_loss"),
        exit_tp           = sum(1 for r in records if r.exit_reason == "take_profit"),
        exit_strategy     = sum(1 for r in records if r.exit_reason == "strategy"),
    )


def _adx_quartile_analysis(records: list[TradeRecord]) -> list[dict]:
    """
    Break trades into ADX buckets and compute win rate + avg return per bucket.
    Buckets: <20, 20-25, 25-30, 30-35, 35-40, 40+
    """
    buckets = [
        ("< 20",   0,   20),
        ("20-25",  20,  25),
        ("25-30",  25,  30),
        ("30-35",  30,  35),
        ("35-40",  35,  40),
        ("40+",    40,  9999),
    ]
    results = []
    for label, lo, hi in buckets:
        group = [r for r in records if r.adx is not None and lo <= r.adx < hi]
        if not group:
            continue
        wins     = sum(1 for r in group if r.winner)
        win_rate = wins / len(group) * 100
        avg_ret  = sum(r.return_pct for r in group) / len(group)
        avg_pnl  = sum(r.pnl for r in group) / len(group)
        results.append({
            "label":    label,
            "count":    len(group),
            "wins":     wins,
            "win_rate": win_rate,
            "avg_ret":  avg_ret,
            "avg_pnl":  avg_pnl,
        })
    return results


# ── Core computation ──────────────────────────────────────────────────────────

def compute_attribution(result) -> AttributionReport:
    """
    Pair BUY fills with their subsequent SELL fill.
    Attach indicator snapshots stored in result.entry_snapshots.
    Returns a full AttributionReport.
    """
    fills = result.fills

    # Separate BUY and SELL fills
    buys  = [f for f in fills if f.side == "BUY"]
    sells = [f for f in fills if f.side == "SELL"]

    # entry_snapshots is a list of dicts keyed by candle_index of the BUY fill
    snapshots: dict[int, dict] = {}
    for snap in getattr(result, "entry_snapshots", []):
        snapshots[snap["candle_index"]] = snap

    records: list[TradeRecord] = []

    for buy, sell in zip(buys, sells):
        snap = snapshots.get(buy.candle_index, {})

        entry_dt = datetime.strptime(buy.timestamp,  "%Y-%m-%d %H:%M")
        exit_dt  = datetime.strptime(sell.timestamp, "%Y-%m-%d %H:%M")
        holding_hours = max(0.0, (exit_dt - entry_dt).total_seconds() / 3600)

        pnl        = sell.pnl if sell.pnl is not None else 0.0
        return_pct = (sell.price - buy.price) / buy.price * 100 if buy.price > 0 else 0.0

        ema_fast   = snap.get("ema_fast")
        ema_slow   = snap.get("ema_slow")
        spread_pct = None
        if ema_fast is not None and ema_slow is not None and ema_slow > 0:
            spread_pct = abs(ema_fast - ema_slow) / ema_slow * 100

        records.append(TradeRecord(
            entry_time      = buy.timestamp,
            entry_price     = buy.price,
            adx             = snap.get("adx"),
            rsi             = snap.get("rsi"),
            ema_fast        = ema_fast,
            ema_slow        = ema_slow,
            ema_spread_pct  = spread_pct,
            trend           = snap.get("trend", "UNKNOWN"),
            exit_time       = sell.timestamp,
            exit_price      = sell.price,
            exit_reason     = sell.reason,
            pnl             = pnl,
            return_pct      = return_pct,
            holding_hours   = holding_hours,
            winner          = pnl > 0,
        ))

    winners = [r for r in records if r.winner]
    losers  = [r for r in records if not r.winner]

    return AttributionReport(
        records = records,
        winners = _group_stats(winners),
        losers  = _group_stats(losers),
        all     = _group_stats(records),
    )


# ── Terminal report ───────────────────────────────────────────────────────────

def print_attribution(report: AttributionReport) -> None:
    """Print winner vs loser comparison to terminal."""
    w = report.winners
    l = report.losers
    a = report.all

    if a.count == 0:
        print("  No completed trades to analyse.\n")
        return

    _B   = "\033[1m"
    _R   = "\033[0m"
    _GR  = "\033[32m"
    _RD  = "\033[31m"
    _YL  = "\033[33m"
    _DIM = "\033[2m"
    _CY  = "\033[36m"

    bar = "═" * 50

    print(f"\n{_B}{bar}{_R}")
    print(f"  {_B}TRADE ATTRIBUTION ANALYSIS{_R}")
    print(f"  {a.count} completed trades  "
          f"({_GR}{w.count} winners{_R} / {_RD}{l.count} losers{_R})")
    print(f"{_B}{bar}{_R}\n")

    # ── Header row ────────────────────────────────────────────────────
    col = 22
    print(f"  {'Metric':<{col}}  {'WINNERS':>12}  {'LOSERS':>12}  {'SEPARATION':>12}")
    print(f"  {'─'*col}  {'─'*12}  {'─'*12}  {'─'*12}")

    def _fmt(v: Optional[float], decimals: int = 1) -> str:
        return f"{v:.{decimals}f}" if v is not None else "n/a"

    def _sep(wv: Optional[float], lv: Optional[float], decimals: int = 1) -> str:
        """Show winner minus loser, coloured green if winners > losers."""
        if wv is None or lv is None:
            return "n/a"
        diff = wv - lv
        col_ = _GR if diff > 0 else (_RD if diff < 0 else "")
        return f"{col_}{diff:+.{decimals}f}{_R}"

    def row(label, wv, lv, decimals=1):
        print(f"  {label:<{col}}  {_fmt(wv, decimals):>12}  {_fmt(lv, decimals):>12}  {_sep(wv, lv, decimals):>21}")

    print(f"\n  {_CY}ADX at entry{_R}")
    row("  Mean",           w.adx_mean,   l.adx_mean)
    row("  Median",         w.adx_median, l.adx_median)
    row("  Q25",            w.adx_q25,    l.adx_q25)
    row("  Q75",            w.adx_q75,    l.adx_q75)

    print(f"\n  {_CY}RSI at entry{_R}")
    row("  Mean",           w.rsi_mean,   l.rsi_mean)
    row("  Median",         w.rsi_median, l.rsi_median)
    row("  Q25",            w.rsi_q25,    l.rsi_q25)
    row("  Q75",            w.rsi_q75,    l.rsi_q75)

    print(f"\n  {_CY}EMA spread % at entry{_R}")
    row("  Mean",           w.ema_spread_mean,   l.ema_spread_mean,   decimals=3)
    row("  Median",         w.ema_spread_median, l.ema_spread_median, decimals=3)

    print(f"\n  {_CY}Holding period (hours){_R}")
    row("  Mean",           w.holding_mean,   l.holding_mean,   decimals=1)
    row("  Median",         w.holding_median, l.holding_median, decimals=1)

    # ── Exit reason breakdown ─────────────────────────────────────────
    print(f"\n  {_CY}Exit reasons{_R}")
    def exit_row(label, wn, ln):
        print(f"  {label:<{col}}  {wn:>12}  {ln:>12}")
    exit_row("  Stop loss",    w.exit_sl,       l.exit_sl)
    exit_row("  Take profit",  w.exit_tp,       l.exit_tp)
    exit_row("  Strategy",     w.exit_strategy, l.exit_strategy)

    # ── ADX quartile breakdown ────────────────────────────────────────
    print(f"\n  {_CY}ADX quartile breakdown{_R}")
    print(f"  {'Bucket':<10}  {'Trades':>6}  {'Wins':>5}  {'Win%':>6}  {'Avg ret%':>9}  {'Avg PnL':>9}")
    print(f"  {'─'*10}  {'─'*6}  {'─'*5}  {'─'*6}  {'─'*9}  {'─'*9}")
    buckets = _adx_quartile_analysis(report.records)
    for b in buckets:
        wr_col = _GR if b["win_rate"] >= 33 else _RD
        print(
            f"  {b['label']:<10}  {b['count']:>6}  {b['wins']:>5}  "
            f"{wr_col}{b['win_rate']:>5.1f}%{_R}  "
            f"{b['avg_ret']:>+8.2f}%  "
            f"{b['avg_pnl']:>+8.2f}"
        )

    # ── Separation summary ────────────────────────────────────────────
    print(f"\n  {_B}KEY SEPARATIONS{_R}")
    print(f"  {'─'*46}")

    separations = []
    pairs = [
        ("ADX mean",        w.adx_mean,          l.adx_mean,          1),
        ("RSI mean",        w.rsi_mean,           l.rsi_mean,          1),
        ("EMA spread mean", w.ema_spread_mean,    l.ema_spread_mean,   3),
        ("Holding hours",   w.holding_mean,       l.holding_mean,      1),
    ]
    for name, wv, lv, dec in pairs:
        if wv is not None and lv is not None and lv != 0:
            diff    = wv - lv
            rel_pct = abs(diff / lv) * 100
            separations.append((rel_pct, name, wv, lv, diff, dec))

    separations.sort(reverse=True)
    if separations:
        for rel_pct, name, wv, lv, diff, dec in separations:
            direction = "higher in winners" if diff > 0 else "lower in winners"
            col_     = _GR if diff > 0 else _RD
            print(f"  {name:<22}  {col_}{rel_pct:5.1f}% {direction}{_R}"
                  f"  (W={wv:.{dec}f} L={lv:.{dec}f})")
    else:
        print(f"  {_YL}Insufficient data to compute separations.{_R}")

    print(f"\n{_DIM}{bar}{_R}\n")


# ── CSV export ────────────────────────────────────────────────────────────────

def save_attribution_csv(
    records: list[TradeRecord],
    directory: str = "logs",
    symbol: str = "BTC_USDT",
    timeframe: str = "4h",
) -> str:
    """Save full per-trade attribution data to CSV. Returns file path."""
    os.makedirs(directory, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename = f"attribution_{symbol.replace('/', '_')}_{timeframe}_{date_str}.csv"
    path     = os.path.join(directory, filename)

    fieldnames = [
        "entry_time", "entry_price",
        "adx", "rsi", "ema_fast", "ema_slow", "ema_spread_pct", "trend",
        "exit_time", "exit_price", "exit_reason",
        "pnl", "return_pct", "holding_hours", "winner",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            writer.writerow({
                "entry_time":     r.entry_time,
                "entry_price":    round(r.entry_price, 2),
                "adx":            round(r.adx, 2)            if r.adx            is not None else "",
                "rsi":            round(r.rsi, 2)            if r.rsi            is not None else "",
                "ema_fast":       round(r.ema_fast, 2)       if r.ema_fast       is not None else "",
                "ema_slow":       round(r.ema_slow, 2)       if r.ema_slow       is not None else "",
                "ema_spread_pct": round(r.ema_spread_pct, 4) if r.ema_spread_pct is not None else "",
                "trend":          r.trend,
                "exit_time":      r.exit_time,
                "exit_price":     round(r.exit_price, 2),
                "exit_reason":    r.exit_reason,
                "pnl":            round(r.pnl, 4),
                "return_pct":     round(r.return_pct, 4),
                "holding_hours":  round(r.holding_hours, 1),
                "winner":         r.winner,
            })

    return path