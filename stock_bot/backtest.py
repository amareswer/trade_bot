"""
Stock bot historical backtester — LEGACY / UNUSED (confirmed 2026-08-20).

This file has zero importers anywhere in the codebase (verified via grep,
confirmed with the user during the 2026-08-20 offline-code audit) — it is a
standalone CLI tool only, not called from any live or gating code path.

The docstring below used to claim this "runs the same indicator pipeline...
as the live stock bot." That stopped being true once stock_bot/strategy/
rules.py switched to importing IndicatorStrategy directly from
bot/strategy/indicator_strategy.py (the crypto strategy module) instead of
this file's own indicator implementations
(stock_bot/indicators/indicators.py). This file's indicators are NOT what
gates any live trade decision.

The real, load-bearing walk-forward gate for whitelist additions is the
root-level stock_backtest.py script, which imports stock_bot/backtest/engine.py
(a DIFFERENT file, in the stock_bot/backtest/ *package* — not this
stock_bot/backtest.py *module*) — that engine already imports
bot/strategy/indicator_strategy.py directly, so it stays in sync with the
live strategy automatically. See "Adding a symbol requires a fresh
stock_backtest.py walk-forward PASS" in CLAUDE.md for the actual gating
workflow.

stock_bot/indicators/indicators.py (used by this file) was still audited on
2026-08-20 despite this file being unused, since that indicators module is
independently live-relevant elsewhere — see CLAUDE.md's "Stock bot regime()
live-gating + offline-audit note" for the audit result (clean: no
lookahead, no self-referential-baseline bug, no incremental-state drift).

Kept in the repo only as the historical implementation this file's own
--walkforward output (stock_bot/backtest_results.json) still feeds into —
LiveTradingGate.check_gate1() in stock_bot/analysis/accuracy_tracker.py
reads that file for a DISPLAY-ONLY (dashboard + weekly email) IBKR-paper-
to-live readiness indicator, never wired into automated trade execution.

Default run — AAPL, MSFT, SPY across full / 2y / 6m windows:
    python -m stock_bot.backtest

Single run with detailed output:
    python -m stock_bot.backtest --symbol AAPL --window 2y
    python -m stock_bot.backtest --symbol MSFT --window 6m --fee-mode ibkr
    python -m stock_bot.backtest --symbol SPY --sl 3 --tp 10
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import yfinance as yf
from dotenv import load_dotenv

from stock_bot.indicators.indicators import (
    adx    as _calc_adx,
    atr    as _calc_atr,
    ema    as _calc_ema,
    macd   as _calc_macd,
    regime as _calc_regime,
    rsi    as _calc_rsi,
    trend  as _calc_trend,
)

_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=_ENV_PATH, override=False)

# ─── Constants ────────────────────────────────────────────────────────────────

_FEE_PAPER            = 0.00005   # 0.005% per side
_FEE_IBKR             = 0.0001    # 0.010% per side
_WARMUP_BARS          = 100       # bars before window_start reserved for indicator warmup
_SYMBOLS              = ["AAPL", "MSFT", "SPY"]
_WINDOWS              = ["full", "2y", "6m"]
_WF_PASS_PF           = 1.3       # minimum profit factor required in every walk-forward window
_WF_WARMUP_DAYS       = 150       # calendar days of warmup fetched before each walk-forward window
_RESULTS_PATH         = os.path.join(os.path.dirname(__file__), "backtest_results.json")
_SPY_FETCH_BUFFER_DAYS = 30       # extra calendar days prepended when fetching SPY for regime warmup
_REGIME_GAP_DAYS      = 5         # max days to walk back when SPY date is missing (weekend/holiday)


# ─── Config ───────────────────────────────────────────────────────────────────

def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


@dataclass
class BacktestConfig:
    sl_pct:          float   # stop-loss fraction   (e.g. 0.05 = 5%)
    tp_pct:          float   # take-profit fraction  (e.g. 0.12 = 12%)
    adx_min:         float   # minimum ADX to enter  (ranging filter)
    rsi_max:         float   # maximum RSI to enter  (overbought gate)
    ema_fast:        int     # fast EMA period for crossover signal
    ema_slow:        int     # slow EMA period for crossover signal
    fee_per_side:    float   # per-side fee fraction
    starting_cash:   float
    regime_filter:   bool    # True = only enter on BULL macro regime
    regime_ma:       int     # slow MA period for regime (e.g. 200)
    regime_fast_ma:  int     # fast MA period for regime (e.g. 50)

    @classmethod
    def from_env(cls) -> BacktestConfig:
        _regime_enabled = os.getenv("REGIME_FILTER_ENABLED", "true").strip().lower()
        return cls(
            sl_pct         = _env_float("PAPER_STOP_LOSS_PCT",   0.05),
            tp_pct         = _env_float("PAPER_TAKE_PROFIT_PCT", 0.12),
            adx_min        = _env_float("AI_GATE_ADX_MIN",       15.0),
            rsi_max        = _env_float("AI_GATE_RSI_MAX",       75.0),
            ema_fast       = 9,
            ema_slow       = 21,
            fee_per_side   = _FEE_PAPER,
            starting_cash  = _env_float("PAPER_STARTING_CASH",   10_000.0),
            regime_filter  = _regime_enabled in ("1", "true", "yes"),
            regime_ma      = int(_env_float("REGIME_MA_PERIOD",  200)),
            regime_fast_ma = int(_env_float("REGIME_FAST_MA",    50)),
        )


# ─── Data ─────────────────────────────────────────────────────────────────────

@dataclass
class Bar:
    date:   date
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float


def fetch_history(symbol: str, window: str) -> tuple[list[Bar], date]:
    """
    Download OHLCV and return (bars_sorted_oldest_first, window_start).
    Fetches window + warmup days so indicators are ready at window_start.
    """
    today = date.today()

    if window == "full":
        start        = today - timedelta(days=365 * 10)
        window_start = start + timedelta(days=_WARMUP_BARS)   # refined below from bar index
    elif window == "2y":
        window_start = today - timedelta(days=365 * 2)
        start        = window_start - timedelta(days=_WARMUP_BARS * 2)
    elif window == "6m":
        window_start = today - timedelta(days=183)
        start        = window_start - timedelta(days=_WARMUP_BARS * 2)
    else:
        raise ValueError(f"window must be full|2y|6m, got {window!r}")

    df = yf.download(
        symbol,
        start       = start.isoformat(),
        end         = (today + timedelta(days=1)).isoformat(),
        interval    = "1d",
        auto_adjust = True,
        progress    = False,
    )

    if df is None or df.empty:
        return [], window_start

    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

    bars: list[Bar] = []
    for ts, row in df.iterrows():
        try:
            close = float(row["Close"])
            if math.isnan(close) or close <= 0:
                continue
            ts_date = ts.date() if hasattr(ts, "date") else ts.to_pydatetime().date()
            bars.append(Bar(
                date   = ts_date,
                open   = float(row["Open"]),
                high   = float(row["High"]),
                low    = float(row["Low"]),
                close  = close,
                volume = float(row.get("Volume", 0)),
            ))
        except (KeyError, ValueError, TypeError):
            continue

    if window == "full" and len(bars) >= _WARMUP_BARS:
        window_start = bars[_WARMUP_BARS].date

    return bars, window_start


def fetch_history_range(
    symbol:       str,
    window_start: date,
    window_end:   date,
) -> tuple[list[Bar], date]:
    """
    Download OHLCV for [window_start, window_end] with a calendar-day warmup buffer.
    Returns (bars_sorted_oldest_first, window_start). Bars include the warmup period.
    """
    fetch_start = window_start - timedelta(days=_WF_WARMUP_DAYS)

    df = yf.download(
        symbol,
        start       = fetch_start.isoformat(),
        end         = (window_end + timedelta(days=1)).isoformat(),
        interval    = "1d",
        auto_adjust = True,
        progress    = False,
    )

    if df is None or df.empty:
        return [], window_start

    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

    bars: list[Bar] = []
    for ts, row in df.iterrows():
        try:
            close = float(row["Close"])
            if math.isnan(close) or close <= 0:
                continue
            ts_date = ts.date() if hasattr(ts, "date") else ts.to_pydatetime().date()
            bars.append(Bar(
                date   = ts_date,
                open   = float(row["Open"]),
                high   = float(row["High"]),
                low    = float(row["Low"]),
                close  = close,
                volume = float(row.get("Volume", 0)),
            ))
        except (KeyError, ValueError, TypeError):
            continue

    return bars, window_start


# ─── Indicators ───────────────────────────────────────────────────────────────

@dataclass
class BarIndicators:
    rsi:   Optional[float]
    adx:   Optional[float]
    trend: str                                   # BULLISH / BEARISH / NEUTRAL
    macd:  Optional[tuple[float, float, float]]
    atr:   Optional[float]
    ema20: Optional[float]
    ema50: Optional[float]


def compute_indicators(
    bars: list[Bar], cfg: BacktestConfig,
) -> list[Optional[BarIndicators]]:
    """
    Compute indicators for every bar using growing slices (no look-ahead).
    Returns None for bars where data is insufficient.
    """
    closes = [b.close for b in bars]
    highs  = [b.high  for b in bars]
    lows   = [b.low   for b in bars]

    result: list[Optional[BarIndicators]] = []
    for i in range(len(bars)):
        c  = closes[:i + 1]
        h  = highs[:i + 1]
        lo = lows[:i + 1]

        if len(c) < cfg.ema_slow + 2:
            result.append(None)
            continue

        result.append(BarIndicators(
            rsi   = _calc_rsi(c),
            adx   = _calc_adx(h, lo, c),
            trend = _calc_trend(
                c,
                fast_period         = cfg.ema_fast,
                slow_period         = cfg.ema_slow,
                confirmation_candles = 2,
            ),
            macd  = _calc_macd(c),
            atr   = _calc_atr(h, lo, c),
            ema20 = _calc_ema(c, 20),
            ema50 = _calc_ema(c, 50),
        ))

    return result


# ─── Regime helpers ───────────────────────────────────────────────────────────

def _spy_regime_at(spy_regimes: dict[date, str], d: date) -> str:
    """
    Return the precomputed SPY regime on or before date d.
    Walks back up to _REGIME_GAP_DAYS days to bridge weekend/holiday gaps.
    Returns 'NEUTRAL' when no matching date is found.
    """
    if d in spy_regimes:
        return spy_regimes[d]
    for delta in range(1, _REGIME_GAP_DAYS + 1):
        candidate = d - timedelta(days=delta)
        if candidate in spy_regimes:
            return spy_regimes[candidate]
    return "NEUTRAL"


def _fetch_spy_regimes(
    symbol: str,
    bars:   list[Bar],
    cfg:    BacktestConfig,
) -> Optional[dict[date, str]]:
    """
    Build a {date: regime_str} dict covering the date range of `bars`.

    SPY closes are fetched fresh unless symbol == 'SPY', in which case the
    passed bars are used directly (avoids a redundant download).
    Returns None when regime_filter is disabled or SPY data is unavailable.
    """
    if not cfg.regime_filter or not bars:
        return None

    if symbol.upper() == "SPY":
        spy_dates  = [b.date  for b in bars]
        spy_closes = [b.close for b in bars]
    else:
        fetch_start = bars[0].date - timedelta(days=_SPY_FETCH_BUFFER_DAYS)
        fetch_end   = bars[-1].date + timedelta(days=1)
        df = yf.download(
            "SPY",
            start       = fetch_start.isoformat(),
            end         = fetch_end.isoformat(),
            interval    = "1d",
            auto_adjust = True,
            progress    = False,
        )
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        spy_dates:  list[date]  = []
        spy_closes: list[float] = []
        for ts, row in df.iterrows():
            try:
                close = float(row["Close"])
                if math.isnan(close) or close <= 0:
                    continue
                ts_date = ts.date() if hasattr(ts, "date") else ts.to_pydatetime().date()
                spy_dates.append(ts_date)
                spy_closes.append(close)
            except (KeyError, ValueError, TypeError):
                continue

        if not spy_dates:
            return None

    regimes: dict[date, str] = {}
    for i, d in enumerate(spy_dates):
        regimes[d] = _calc_regime(
            spy_closes[:i + 1],
            cfg.regime_ma,
            cfg.regime_fast_ma,
        )
    return regimes


# ─── Signal ───────────────────────────────────────────────────────────────────

def _is_entry(ind: Optional[BarIndicators], cfg: BacktestConfig) -> bool:
    if ind is None:
        return False
    if ind.trend != "BULLISH":
        return False
    if ind.adx is None or ind.adx < cfg.adx_min:
        return False
    if ind.rsi is None or ind.rsi > cfg.rsi_max:
        return False
    return True


def _is_exit(ind: Optional[BarIndicators]) -> bool:
    return ind is not None and ind.trend == "BEARISH"


# ─── Simulation ───────────────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_date:   date
    entry_price:  float
    exit_date:    date
    exit_price:   float
    exit_reason:  str    # "SL" | "TP" | "SIGNAL"
    gross_return: float
    net_return:   float  # after round-trip fee


def run_backtest(
    bars:         list[Bar],
    inds:         list[Optional[BarIndicators]],
    cfg:          BacktestConfig,
    window_start: date,
    spy_regimes:  Optional[dict[date, str]] = None,
) -> tuple[list[Trade], int]:
    trades:       list[Trade]    = []
    regime_skips: int            = 0
    in_position:  bool           = False
    entry_price:  float          = 0.0
    entry_date:   Optional[date] = None
    entry_idx:    int            = -1

    for i, (bar, ind) in enumerate(zip(bars, inds)):
        if bar.date < window_start:
            continue

        if in_position:
            if i == entry_idx:
                continue   # no exit check on the bar we just entered

            sl_price = entry_price * (1.0 - cfg.sl_pct)
            tp_price = entry_price * (1.0 + cfg.tp_pct)

            # SL takes priority over TP (conservative; we don't know intraday order)
            if bar.low <= sl_price:
                exit_price, reason = sl_price, "SL"
            elif bar.high >= tp_price:
                exit_price, reason = tp_price, "TP"
            elif _is_exit(ind):
                exit_price, reason = bar.close, "SIGNAL"
            else:
                continue

            gross = (exit_price - entry_price) / entry_price
            trades.append(Trade(
                entry_date   = entry_date,     # type: ignore[arg-type]
                entry_price  = entry_price,
                exit_date    = bar.date,
                exit_price   = exit_price,
                exit_reason  = reason,
                gross_return = gross,
                net_return   = gross - 2.0 * cfg.fee_per_side,
            ))
            in_position = False
            entry_price = 0.0
            entry_date  = None
            entry_idx   = -1

        else:
            if _is_entry(ind, cfg):
                if spy_regimes is not None:
                    reg = _spy_regime_at(spy_regimes, bar.date)
                    if reg != "BULL":
                        regime_skips += 1
                        continue
                entry_price = bar.close
                entry_date  = bar.date
                entry_idx   = i
                in_position = True

    return trades, regime_skips


# ─── Metrics ──────────────────────────────────────────────────────────────────

@dataclass
class BacktestMetrics:
    total_trades:  int
    wins:          int
    win_rate:      float   # 0.0–1.0
    profit_factor: float
    max_drawdown:  float   # fraction (0.12 = 12%)
    sharpe:        float
    total_return:  float   # compounded net return as a fraction
    sl_exits:      int
    tp_exits:      int
    signal_exits:  int
    avg_hold_days: float
    regime_skips:  int = 0  # BUY signals blocked by macro regime filter


def compute_metrics(
    trades:        list[Trade],
    starting_cash: float,
    regime_skips:  int = 0,
) -> BacktestMetrics:
    if not trades:
        return BacktestMetrics(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0.0, regime_skips)

    wins       = sum(1 for t in trades if t.net_return > 0)
    gross_wins = sum(t.net_return for t in trades if t.net_return > 0)
    gross_loss = sum(abs(t.net_return) for t in trades if t.net_return < 0)
    pf         = gross_wins / gross_loss if gross_loss > 0 else float("inf")

    sl_exits     = sum(1 for t in trades if t.exit_reason == "SL")
    tp_exits     = sum(1 for t in trades if t.exit_reason == "TP")
    signal_exits = sum(1 for t in trades if t.exit_reason == "SIGNAL")

    # Compounded equity curve → total return + max drawdown
    equity = starting_cash
    peak   = starting_cash
    max_dd = 0.0
    for t in trades:
        equity *= (1.0 + t.net_return)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak
        if dd > max_dd:
            max_dd = dd

    total_return = (equity - starting_cash) / starting_cash

    # Sharpe: annualized from per-trade returns
    rets   = [t.net_return for t in trades]
    mean_r = sum(rets) / len(rets)
    var    = sum((r - mean_r) ** 2 for r in rets) / max(1, len(rets) - 1)
    std_r  = math.sqrt(var) if var > 0 else 0.0

    span_days       = (trades[-1].exit_date - trades[0].entry_date).days or 1
    trades_per_year = len(trades) * 365.0 / span_days
    ann_factor      = math.sqrt(max(trades_per_year, 1.0))
    sharpe          = (mean_r / std_r) * ann_factor if std_r > 0 else 0.0

    hold_days = [(t.exit_date - t.entry_date).days for t in trades]
    avg_hold  = sum(hold_days) / len(hold_days)

    return BacktestMetrics(
        total_trades  = len(trades),
        wins          = wins,
        win_rate      = wins / len(trades),
        profit_factor = pf,
        max_drawdown  = max_dd,
        sharpe        = sharpe,
        total_return  = total_return,
        sl_exits      = sl_exits,
        tp_exits      = tp_exits,
        signal_exits  = signal_exits,
        avg_hold_days = avg_hold,
        regime_skips  = regime_skips,
    )


# ─── Reporting ────────────────────────────────────────────────────────────────

def _pf_str(pf: float) -> str:
    return " inf" if math.isinf(pf) else f"{pf:.2f}"


def print_single(
    symbol: str, window: str, m: BacktestMetrics, cfg: BacktestConfig,
) -> None:
    divider = "─" * 66
    print(f"\n{divider}")
    print(
        f"  {symbol}  │  window={window}  │  fee={cfg.fee_per_side * 100:.4f}%/side"
        f"  │  SL={cfg.sl_pct * 100:.1f}%  TP={cfg.tp_pct * 100:.1f}%"
    )
    print(
        f"  Signal: EMA{cfg.ema_fast}/{cfg.ema_slow} crossover"
        f"  ADX≥{cfg.adx_min:.0f}  RSI≤{cfg.rsi_max:.0f}"
    )
    print(divider)
    if m.total_trades == 0:
        print("  No completed trades in this window.")
        print(divider)
        return
    n = m.total_trades
    print(f"  Total trades   : {n}")
    print(f"  Win rate       : {m.win_rate * 100:.1f}%  ({m.wins}W / {n - m.wins}L)")
    print(f"  Profit factor  : {_pf_str(m.profit_factor)}")
    print(f"  Total return   : {m.total_return * 100:+.2f}%")
    print(f"  Max drawdown   : {m.max_drawdown * 100:.2f}%")
    print(f"  Sharpe ratio   : {m.sharpe:.2f}")
    print(f"  Avg hold       : {m.avg_hold_days:.1f} days")
    print(
        f"  Exit breakdown : SL={m.sl_exits} ({m.sl_exits/n*100:.0f}%)"
        f"  TP={m.tp_exits} ({m.tp_exits/n*100:.0f}%)"
        f"  Signal={m.signal_exits} ({m.signal_exits/n*100:.0f}%)"
    )
    if m.regime_skips:
        print(f"  Regime skips   : {m.regime_skips} BUY signals blocked (market not BULL)")
    print(divider)


def print_table(results: list[tuple[str, str, BacktestMetrics]]) -> None:
    hdr = (
        f"  {'Symbol':<6} {'Window':<6} {'Trades':>6} {'Win%':>5}"
        f" {'PF':>5} {'Return%':>8} {'MaxDD%':>7}"
        f" {'Sharpe':>7}  {'SL/TP/Sig%':<12} {'Rskip':>6}"
    )
    ruler = "  " + "─" * (len(hdr) - 2)
    print(f"\n{ruler}")
    print(hdr)
    print(ruler)
    for sym, win, m in results:
        if m.total_trades == 0:
            print(
                f"  {sym:<6} {win:<6} {'—':>6} {'—':>5}"
                f" {'—':>5} {'—':>8} {'—':>7} {'—':>7}  {'—':<12} {'—':>6}"
            )
            continue
        n = m.total_trades
        breakdown = f"{m.sl_exits/n*100:.0f}/{m.tp_exits/n*100:.0f}/{m.signal_exits/n*100:.0f}"
        print(
            f"  {sym:<6} {win:<6} {n:>6} {m.win_rate * 100:>5.1f}"
            f" {_pf_str(m.profit_factor):>5} {m.total_return * 100:>+8.2f}"
            f" {m.max_drawdown * 100:>7.2f}"
            f" {m.sharpe:>7.2f}  {breakdown:<12} {m.regime_skips:>6}"
        )
    print(ruler)


# ─── Orchestration ────────────────────────────────────────────────────────────

def backtest(symbol: str, window: str, cfg: BacktestConfig) -> BacktestMetrics:
    bars, window_start = fetch_history(symbol, window)
    if not bars:
        print(f"  Warning: no data returned for {symbol}", file=sys.stderr)
        return BacktestMetrics(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0.0)
    spy_regimes        = _fetch_spy_regimes(symbol, bars, cfg)
    inds               = compute_indicators(bars, cfg)
    trades, rskips     = run_backtest(bars, inds, cfg, window_start, spy_regimes)
    return compute_metrics(trades, cfg.starting_cash, rskips)


# ─── Walk-Forward Validation ─────────────────────────────────────────────────

def _wf_windows() -> list[tuple[str, date, date]]:
    """Three fixed walk-forward windows. End date of the last window is today."""
    return [
        ("2019–2021", date(2019, 1, 1), date(2021, 12, 31)),
        ("2022–2023", date(2022, 1, 1), date(2023, 12, 31)),
        ("2024–now",  date(2024, 1, 1), date.today()),
    ]


@dataclass
class WalkForwardWindowResult:
    label:   str
    start:   date
    end:     date
    metrics: BacktestMetrics


@dataclass
class WalkForwardResult:
    symbol:  str
    windows: list[WalkForwardWindowResult]
    verdict: str   # "PASS" | "FAIL"


def run_walkforward(symbol: str, cfg: BacktestConfig) -> WalkForwardResult:
    """Run the three fixed walk-forward windows for one symbol."""
    window_results: list[WalkForwardWindowResult] = []

    for label, wf_start, wf_end in _wf_windows():
        print(f"    {label}...", end="", flush=True)
        bars, ws = fetch_history_range(symbol, wf_start, wf_end)
        if not bars:
            print(" no data")
            window_results.append(WalkForwardWindowResult(
                label   = label,
                start   = wf_start,
                end     = wf_end,
                metrics = BacktestMetrics(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0.0),
            ))
            continue

        spy_regimes       = _fetch_spy_regimes(symbol, bars, cfg)
        inds              = compute_indicators(bars, cfg)
        trades, rskips    = run_backtest(bars, inds, cfg, ws, spy_regimes)
        metrics           = compute_metrics(trades, cfg.starting_cash, rskips)
        pf_tag  = _pf_str(metrics.profit_factor)
        status  = "✓" if metrics.total_trades > 0 and metrics.profit_factor >= _WF_PASS_PF else "✗"
        print(f" {metrics.total_trades:>3} trades  PF={pf_tag}  {status}")
        window_results.append(WalkForwardWindowResult(
            label   = label,
            start   = wf_start,
            end     = wf_end,
            metrics = metrics,
        ))

    verdict = "PASS" if all(
        w.metrics.total_trades > 0 and w.metrics.profit_factor >= _WF_PASS_PF
        for w in window_results
    ) else "FAIL"

    return WalkForwardResult(symbol=symbol, windows=window_results, verdict=verdict)


def print_walkforward_table(wf_results: list[WalkForwardResult], cfg: BacktestConfig) -> None:
    """Print a combined summary table for all symbols."""
    sep   = "  " + "─" * 72
    thick = "  " + "═" * 72

    regime_label = (
        f"SPY SMA({cfg.regime_ma})/SMA({cfg.regime_fast_ma}) ON"
        if cfg.regime_filter else "OFF"
    )
    print(f"\n{thick}")
    print(f"  Walk-Forward Validation  (PASS = PF ≥ {_WF_PASS_PF:.2f} in all 3 windows)")
    print(
        f"  Config  EMA{cfg.ema_fast}/{cfg.ema_slow}"
        f"  ADX≥{cfg.adx_min:.0f}  RSI≤{cfg.rsi_max:.0f}"
        f"  SL={cfg.sl_pct*100:.1f}%  TP={cfg.tp_pct*100:.1f}%"
        f"  fee={cfg.fee_per_side*100:.4f}%/side"
        f"  Regime={regime_label}"
    )
    print(thick)
    print(
        f"  {'Symbol':<7} {'Window':<12} {'Trades':>6}"
        f" {'Win%':>5} {'PF':>5} {'MaxDD%':>7} {'Sharpe':>7}  Status"
    )

    for wfr in wf_results:
        print(sep)
        for w in wfr.windows:
            m = w.metrics
            if m.total_trades == 0:
                print(
                    f"  {wfr.symbol:<7} {w.label:<12} {'—':>6}"
                    f" {'—':>5} {'—':>5} {'—':>7} {'—':>7}  no data"
                )
                continue
            pf_pass    = m.total_trades > 0 and m.profit_factor >= _WF_PASS_PF
            status     = "PF ✓" if pf_pass else "PF ✗"
            skip_note  = f"  +{m.regime_skips}sk" if m.regime_skips else ""
            print(
                f"  {wfr.symbol:<7} {w.label:<12} {m.total_trades:>6}"
                f" {m.win_rate*100:>5.1f} {_pf_str(m.profit_factor):>5}"
                f" {m.max_drawdown*100:>7.2f} {m.sharpe:>7.2f}  {status}{skip_note}"
            )

        # Per-symbol verdict row
        passing = [
            w for w in wfr.windows
            if w.metrics.total_trades > 0 and w.metrics.profit_factor >= _WF_PASS_PF
        ]
        n_pass   = len(passing)
        min_pf   = min((w.metrics.profit_factor for w in passing), default=0.0)
        v_icon   = "✓" if wfr.verdict == "PASS" else "✗"
        min_note = f"  min PF {_pf_str(min_pf)}  " if passing else "  "
        print(
            f"  {'':<7} {wfr.symbol} ►  {wfr.verdict} {v_icon}"
            f"  ({n_pass}/{len(wfr.windows)} windows pass{min_note})"
        )

    print(thick)

    # Overall summary line
    n_sym_pass = sum(1 for r in wf_results if r.verdict == "PASS")
    summary = "  │  ".join(
        f"{r.symbol} {'PASS ✓' if r.verdict == 'PASS' else 'FAIL ✗'}"
        for r in wf_results
    )
    print(f"  Summary: {summary}   ({n_sym_pass}/{len(wf_results)} PASS)")
    print(thick)


def save_wf_results(
    wf_results: list[WalkForwardResult],
    cfg:        BacktestConfig,
    path:       str = _RESULTS_PATH,
) -> None:
    """Persist walk-forward results to JSON for later comparison."""
    def _metrics_dict(m: BacktestMetrics) -> dict:
        return {
            "total_trades":  m.total_trades,
            "wins":          m.wins,
            "win_rate":      round(m.win_rate, 4),
            "profit_factor": None if math.isinf(m.profit_factor) else round(m.profit_factor, 4),
            "max_drawdown":  round(m.max_drawdown, 4),
            "sharpe":        round(m.sharpe, 4),
            "total_return":  round(m.total_return, 4),
            "sl_exits":      m.sl_exits,
            "tp_exits":      m.tp_exits,
            "signal_exits":  m.signal_exits,
            "avg_hold_days": round(m.avg_hold_days, 1),
        }

    payload: dict = {
        "run_at":           datetime.now().isoformat(timespec="seconds"),
        "pass_threshold_pf": _WF_PASS_PF,
        "config": {
            "sl_pct":          round(cfg.sl_pct, 4),
            "tp_pct":          round(cfg.tp_pct, 4),
            "adx_min":         cfg.adx_min,
            "rsi_max":         cfg.rsi_max,
            "ema_fast":        cfg.ema_fast,
            "ema_slow":        cfg.ema_slow,
            "fee_per_side":    round(cfg.fee_per_side, 6),
            "regime_filter":   cfg.regime_filter,
            "regime_ma":       cfg.regime_ma,
            "regime_fast_ma":  cfg.regime_fast_ma,
        },
        "windows": [
            {"label": label, "start": start.isoformat(), "end": end.isoformat()}
            for label, start, end in _wf_windows()
        ],
        "results": [
            {
                "symbol":  r.symbol,
                "verdict": r.verdict,
                "windows": [
                    {
                        "label": w.label,
                        "start": w.start.isoformat(),
                        "end":   w.end.isoformat(),
                        **_metrics_dict(w.metrics),
                    }
                    for w in r.windows
                ],
            }
            for r in wf_results
        ],
    }

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\n  Results saved → {path}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog            = "python -m stock_bot.backtest",
        description     = "Rule-based backtester: EMA crossover + RSI + ADX on daily OHLCV",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--symbol", "-s", default=None,
        help="Ticker symbol (e.g. AAPL). Omit to run AAPL/MSFT/SPY table.",
    )
    p.add_argument(
        "--window", "-w", default=None, choices=["full", "2y", "6m"],
        help="Backtest window. Omit to run all three windows.",
    )
    p.add_argument(
        "--fee-mode", "-f", default="paper", choices=["paper", "ibkr"],
        help="paper=0.005%%/side  ibkr=0.01%%/side",
    )
    p.add_argument(
        "--sl", type=float, default=None, metavar="PCT",
        help="Stop-loss %% (e.g. 5 for 5%%). Overrides PAPER_STOP_LOSS_PCT in .env.",
    )
    p.add_argument(
        "--tp", type=float, default=None, metavar="PCT",
        help="Take-profit %% (e.g. 12 for 12%%). Overrides PAPER_TAKE_PROFIT_PCT in .env.",
    )
    p.add_argument(
        "--adx-min", type=float, default=None, metavar="VAL",
        help="Min ADX to enter. Overrides AI_GATE_ADX_MIN in .env.",
    )
    p.add_argument(
        "--rsi-max", type=float, default=None, metavar="VAL",
        help="Max RSI to enter. Overrides AI_GATE_RSI_MAX in .env.",
    )
    p.add_argument(
        "--ema-fast", type=int, default=None, metavar="N",
        help="Fast EMA period for crossover signal (default 9).",
    )
    p.add_argument(
        "--ema-slow", type=int, default=None, metavar="N",
        help="Slow EMA period for crossover signal (default 21).",
    )
    p.add_argument(
        "--walkforward", "-W", action="store_true",
        help=(
            "Run walk-forward validation across 2019–2021 / 2022–2023 / 2024–now. "
            "PASS requires PF ≥ 1.30 in all three windows. "
            "Saves results to stock_bot/backtest_results.json."
        ),
    )
    p.add_argument(
        "--regime-filter", dest="regime_filter", action="store_true", default=None,
        help="Enable macro regime filter — only enter BUY when SPY is in BULL regime (default ON).",
    )
    p.add_argument(
        "--no-regime-filter", dest="regime_filter", action="store_false",
        help="Disable macro regime filter (run without SPY trend gate).",
    )
    p.add_argument(
        "--regime-ma", type=int, default=None, metavar="N",
        help="Slow MA period for regime detection. Overrides REGIME_MA_PERIOD env (default 200).",
    )
    p.add_argument(
        "--regime-fast-ma", type=int, default=None, metavar="N",
        help="Fast MA period for regime detection. Overrides REGIME_FAST_MA env (default 50).",
    )
    return p


def main() -> None:
    logging.getLogger("yfinance").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.ERROR)

    args = _build_parser().parse_args()

    cfg = BacktestConfig.from_env()
    cfg.fee_per_side = _FEE_IBKR if args.fee_mode == "ibkr" else _FEE_PAPER
    if args.sl              is not None: cfg.sl_pct         = args.sl  / 100.0
    if args.tp              is not None: cfg.tp_pct         = args.tp  / 100.0
    if args.adx_min         is not None: cfg.adx_min        = args.adx_min
    if args.rsi_max         is not None: cfg.rsi_max        = args.rsi_max
    if args.ema_fast        is not None: cfg.ema_fast       = args.ema_fast
    if args.ema_slow        is not None: cfg.ema_slow       = args.ema_slow
    if args.regime_filter   is not None: cfg.regime_filter  = args.regime_filter
    if args.regime_ma       is not None: cfg.regime_ma      = args.regime_ma
    if args.regime_fast_ma  is not None: cfg.regime_fast_ma = args.regime_fast_ma

    if args.walkforward:
        symbols = [args.symbol.upper()] if args.symbol else _SYMBOLS
        wf_results: list[WalkForwardResult] = []
        for sym in symbols:
            print(f"\n  {sym}:")
            wf_results.append(run_walkforward(sym, cfg))
        print_walkforward_table(wf_results, cfg)
        save_wf_results(wf_results, cfg)
        return

    symbols = [args.symbol.upper()] if args.symbol else _SYMBOLS
    windows = [args.window]         if args.window else _WINDOWS

    print(
        f"\n  Config  SL={cfg.sl_pct*100:.1f}%  TP={cfg.tp_pct*100:.1f}%"
        f"  ADX≥{cfg.adx_min:.0f}  RSI≤{cfg.rsi_max:.0f}"
        f"  EMA{cfg.ema_fast}/{cfg.ema_slow}"
        f"  fee={cfg.fee_per_side*100:.4f}%/side"
    )

    if len(symbols) == 1 and len(windows) == 1:
        m = backtest(symbols[0], windows[0], cfg)
        print_single(symbols[0], windows[0], m, cfg)
    else:
        results: list[tuple[str, str, BacktestMetrics]] = []
        for sym in symbols:
            for win in windows:
                print(f"  Fetching {sym} ({win})...", end="", flush=True)
                m = backtest(sym, win, cfg)
                results.append((sym, win, m))
                print(f" {m.total_trades} trades")
        print_table(results)


if __name__ == "__main__":
    main()
