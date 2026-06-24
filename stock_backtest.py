"""
Stock bot backtester — indicator-only strategy (no AI).

Runs the same RSI + EMA + ADX signal pipeline that the live paper bot uses,
over 5 years of daily candles, with shared cash pool and 4-position cap.

Usage:
    python stock_backtest.py
"""
from __future__ import annotations

import csv
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import yfinance as yf

# Ensure project root importable when run as a script
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from stock_bot.indicators.indicators import rsi, trend, adx

# ---------------------------------------------------------------------------
# Config — hardcoded (do not read from .env)
# ---------------------------------------------------------------------------
STOP_LOSS_PCT    = 0.05
TAKE_PROFIT_PCT  = 0.12
SLIPPAGE_BPS     = 15
STARTING_CASH    = 10_000.0
RISK_PCT         = 0.25
MAX_POSITIONS    = 4
COMMISSION_PCT   = 0.005

SYMBOLS = [
    "HOOD", "MRNA", "NCLH", "CCL", "INTC", "AAPL", "NVDA", "AMD",
    "AC.TO", "BMO.TO", "CM.TO",
]

_LOG_DIR = os.path.join(os.path.dirname(__file__), "stock_bot", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Fill:
    symbol:    str
    date:      date
    side:      str   # "BUY" | "SELL"
    shares:    int
    price:     float
    pnl:       float
    reason:    str
    cash_after: float


@dataclass
class Position:
    symbol:   str
    shares:   int
    avg_cost: float
    entry_date: date


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def _fetch(symbol: str) -> tuple[list, list, list, list, list[date]] | None:
    """Returns (opens, highs, lows, closes, volumes, dates) or None on failure."""
    try:
        df = yf.download(symbol, period="5y", interval="1d", auto_adjust=True, progress=False)
    except Exception as exc:
        print(f"  [SKIP] {symbol} — download error: {exc}")
        return None

    if df is None or df.empty:
        print(f"  [SKIP] {symbol} — empty dataframe")
        return None

    # Flatten MultiIndex columns (yfinance ≥ 0.2.38)
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

    df = df.dropna(subset=["Close"])
    if len(df) < 30:
        print(f"  [SKIP] {symbol} — only {len(df)} candles after NaN drop")
        return None

    opens   = [float(v) for v in df["Open"]]
    highs   = [float(v) for v in df["High"]]
    lows    = [float(v) for v in df["Low"]]
    closes  = [float(v) for v in df["Close"]]
    volumes = [float(v) for v in df["Volume"]]
    dates   = [ts.date() for ts in df.index]

    return opens, highs, lows, closes, volumes, dates


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------

def _fill_px(price: float, side: str) -> float:
    factor = SLIPPAGE_BPS / 10_000
    return round(price * (1 + factor), 4) if side == "BUY" else round(price * (1 - factor), 4)


# ---------------------------------------------------------------------------
# Per-symbol backtest
# ---------------------------------------------------------------------------

@dataclass
class SymbolResult:
    symbol:      str
    trades:      int  = 0
    wins:        int  = 0
    gross_profit: float = 0.0
    gross_loss:  float = 0.0
    max_dd:      float = 0.0
    equity_peak: float = STARTING_CASH
    fills:       list[Fill] = field(default_factory=list)


def _run_single(
    symbol: str,
    data: tuple,
    shared_state: dict,   # mutated in-place: cash, positions, equity_curve, fills, commission_total
) -> SymbolResult:
    """
    Simulate the indicator strategy for one symbol against the shared cash pool.
    Candles are fed oldest→newest. No lookahead.
    """
    opens, highs, lows, closes, volumes, dates = data
    result = SymbolResult(symbol=symbol)
    prev_trend: str | None = None

    for i in range(len(closes)):
        if i < 28:   # need at least 29 bars for ADX(14) and RSI(14)
            # Track prev_trend even in warmup so we have 2 candles of confirmation
            closes_so_far = closes[:i + 1]
            tr = trend(closes_so_far, prev_trend=prev_trend)
            prev_trend = tr
            continue

        closes_so_far  = closes[:i + 1]
        highs_so_far   = highs[:i + 1]
        lows_so_far    = lows[:i + 1]
        price          = closes[i]
        today          = dates[i]

        # Guard: skip obviously bad prices
        if price < 5.0 or price > 500.0:
            tr = trend(closes_so_far, prev_trend=prev_trend)
            prev_trend = tr
            continue

        rsi_val  = rsi(closes_so_far, period=14)
        tr       = trend(closes_so_far, prev_trend=prev_trend)
        adx_val  = adx(highs_so_far, lows_so_far, closes_so_far, period=14)
        prev_trend = tr

        pos: Position | None = shared_state["positions"].get(symbol)

        # ── SL / TP check if holding ──────────────────────────────────────
        if pos is not None:
            sl_price = pos.avg_cost * (1 - STOP_LOSS_PCT)
            tp_price = pos.avg_cost * (1 + TAKE_PROFIT_PCT)

            exit_reason: str | None = None
            if price <= sl_price:
                exit_reason = "STOP_LOSS"
            elif price >= tp_price:
                exit_reason = "TAKE_PROFIT"
            elif rsi_val is not None and rsi_val > 70 and tr == "BEARISH":
                exit_reason = "STRATEGY_SELL"

            if exit_reason:
                fp = _fill_px(price, "SELL")
                proceeds = pos.shares * fp
                comm     = proceeds * COMMISSION_PCT
                pnl      = round((fp - pos.avg_cost) * pos.shares - comm, 2)
                shared_state["cash"] += proceeds - comm
                shared_state["commission_total"] += comm

                del shared_state["positions"][symbol]

                result.trades += 1
                if pnl > 0:
                    result.wins += 1
                    result.gross_profit += pnl
                else:
                    result.gross_loss += abs(pnl)

                fill = Fill(
                    symbol=symbol, date=today, side="SELL",
                    shares=pos.shares, price=fp, pnl=pnl,
                    reason=exit_reason, cash_after=shared_state["cash"],
                )
                result.fills.append(fill)
                shared_state["all_fills"].append(fill)

                # Update equity curve
                shared_state["equity_curve"].append(shared_state["cash"])
                peak = result.equity_peak
                curr = shared_state["cash"]
                result.equity_peak = max(peak, curr)
                dd = (result.equity_peak - curr) / result.equity_peak if result.equity_peak > 0 else 0
                result.max_dd = max(result.max_dd, dd)
                continue

        # ── BUY signal check ──────────────────────────────────────────────
        if (
            pos is None
            and rsi_val is not None and rsi_val < 35
            and tr == "BULLISH"
            and adx_val is not None and adx_val >= 20
            and len(shared_state["positions"]) < MAX_POSITIONS
            and 5.0 <= price <= 500.0
        ):
            cash      = shared_state["cash"]
            risk_amt  = cash * RISK_PCT
            fp        = _fill_px(price, "BUY")
            shares    = int(risk_amt / fp)
            if shares < 1:
                continue
            cost  = shares * fp
            comm  = cost * COMMISSION_PCT
            total = cost + comm
            if total > cash:
                continue

            shared_state["cash"] -= total
            shared_state["commission_total"] += comm
            shared_state["positions"][symbol] = Position(
                symbol=symbol, shares=shares, avg_cost=fp, entry_date=today,
            )

            fill = Fill(
                symbol=symbol, date=today, side="BUY",
                shares=shares, price=fp, pnl=0.0,
                reason="STRATEGY_BUY", cash_after=shared_state["cash"],
            )
            result.fills.append(fill)
            shared_state["all_fills"].append(fill)

    # Force-close any open position at end of data
    pos = shared_state["positions"].get(symbol)
    if pos is not None:
        price = closes[-1]
        today = dates[-1]
        fp    = _fill_px(price, "SELL")
        proceeds = pos.shares * fp
        comm     = proceeds * COMMISSION_PCT
        pnl      = round((fp - pos.avg_cost) * pos.shares - comm, 2)
        shared_state["cash"] += proceeds - comm
        shared_state["commission_total"] += comm
        del shared_state["positions"][symbol]

        result.trades += 1
        if pnl > 0:
            result.wins += 1
            result.gross_profit += pnl
        else:
            result.gross_loss += abs(pnl)

        fill = Fill(
            symbol=symbol, date=today, side="SELL",
            shares=pos.shares, price=fp, pnl=pnl,
            reason="END_OF_DATA", cash_after=shared_state["cash"],
        )
        result.fills.append(fill)
        shared_state["all_fills"].append(fill)
        shared_state["equity_curve"].append(shared_state["cash"])

    return result


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

def _profit_factor(gross_profit: float, gross_loss: float) -> float:
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return round(gross_profit / gross_loss, 2)


def _sharpe(equity_curve: list[float]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    rets = [(equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            for i in range(1, len(equity_curve))
            if equity_curve[i - 1] > 0]
    if not rets:
        return 0.0
    n   = len(rets)
    avg = sum(rets) / n
    if n < 2:
        return 0.0
    var = sum((r - avg) ** 2 for r in rets) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return 0.0
    return round((avg / std) * math.sqrt(252), 2)


def _max_drawdown(equity_curve: list[float]) -> float:
    peak = -math.inf
    mdd  = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > mdd:
            mdd = dd
    return mdd


# ---------------------------------------------------------------------------
# Save CSV
# ---------------------------------------------------------------------------

def _save_csv(fills: list[Fill]) -> str:
    today_str = datetime.now().strftime("%Y%m%d")
    path = os.path.join(_LOG_DIR, f"stock_backtest_{today_str}.csv")
    header = ["symbol", "date", "side", "shares", "price", "pnl", "reason", "cash_after"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for fill in fills:
            w.writerow([
                fill.symbol, fill.date.isoformat(), fill.side, fill.shares,
                f"{fill.price:.4f}", f"{fill.pnl:.2f}", fill.reason,
                f"{fill.cash_after:.2f}",
            ])
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Fetching data …")
    symbol_data: dict[str, tuple] = {}
    for sym in SYMBOLS:
        result = _fetch(sym)
        if result is not None:
            symbol_data[sym] = result
            closes = result[3]
            dates  = result[5]
            print(f"  {sym:12s}  {len(closes)} candles  "
                  f"{dates[0].isoformat()} → {dates[-1].isoformat()}")
        time.sleep(0.5)

    if not symbol_data:
        print("No data fetched — exiting.")
        return

    # Shared state — all symbols draw from one cash pool
    shared: dict = {
        "cash": STARTING_CASH,
        "positions": {},         # symbol → Position
        "commission_total": 0.0,
        "all_fills": [],
        "equity_curve": [STARTING_CASH],
    }

    # Determine combined date range
    all_dates: list[date] = []
    for _, _, _, _, _, dates in symbol_data.values():
        all_dates.extend(dates)
    date_min = min(all_dates)
    date_max = max(all_dates)
    n_days   = len(set(all_dates))

    # Run each symbol sequentially (shared state updated in-place)
    sym_results: list[SymbolResult] = []
    for sym in SYMBOLS:
        if sym not in symbol_data:
            continue
        res = _run_single(sym, symbol_data[sym], shared)
        sym_results.append(res)
        # Append cash snapshots to equity curve
        if shared["all_fills"]:
            shared["equity_curve"].append(shared["cash"])

    # ── Print report ──────────────────────────────────────────────────────
    total_trades  = sum(r.trades for r in sym_results)
    total_wins    = sum(r.wins   for r in sym_results)
    total_losses  = total_trades - total_wins
    gross_profit  = sum(r.gross_profit for r in sym_results)
    gross_loss    = sum(r.gross_loss   for r in sym_results)
    pf            = _profit_factor(gross_profit, gross_loss)
    final_cash    = shared["cash"]
    total_return  = (final_cash - STARTING_CASH) / STARTING_CASH * 100
    mdd           = _max_drawdown(shared["equity_curve"]) * 100
    sharpe        = _sharpe(shared["equity_curve"])
    win_rate      = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
    commission    = shared["commission_total"]
    us_count      = sum(1 for s in SYMBOLS if not s.endswith(".TO"))
    ca_count      = sum(1 for s in SYMBOLS if s.endswith(".TO"))

    print()
    print("══════════════════════════════════════════════════════")
    print("STOCK BOT BACKTEST — indicator strategy")
    print(f"Period: {date_min.isoformat()} → {date_max.isoformat()}  (~5 years, {n_days} trading days)")
    print(f"Symbols: US={us_count}  CA={ca_count}  Total={len(SYMBOLS)}")
    print(f"Config: SL={STOP_LOSS_PCT*100:.0f}%  TP={TAKE_PROFIT_PCT*100:.0f}%  "
          f"Risk={RISK_PCT*100:.0f}%  MaxPos={MAX_POSITIONS}  Commission={COMMISSION_PCT*100:.1f}%")
    print("══════════════════════════════════════════════════════")
    print()
    print("PER-SYMBOL RESULTS")
    print("──────────────────────────────────────────────────────")
    print(f"{'Symbol':<12} {'Trades':>6}  {'Win%':>5}  {'PF':>5}  {'Return%':>8}  {'MaxDD%':>7}")
    for res in sym_results:
        sym_pf      = _profit_factor(res.gross_profit, res.gross_loss)
        buys        = [f for f in res.fills if f.side == "BUY"]
        sells       = [f for f in res.fills if f.side == "SELL"]
        invested    = sum(f.shares * f.price for f in buys)
        sym_return  = (sum(f.pnl for f in sells) / invested * 100) if invested > 0 else 0.0
        sym_wr      = (res.wins / res.trades * 100) if res.trades > 0 else 0.0
        sym_dd      = res.max_dd * 100
        print(f"{res.symbol:<12} {res.trades:>6}  {sym_wr:>4.0f}%  {sym_pf:>5.2f}  "
              f"{sym_return:>+8.2f}%  {-sym_dd:>7.2f}%")
    print("──────────────────────────────────────────────────────")
    print()
    print("AGGREGATE (all symbols, shared cash pool)")
    print("──────────────────────────────────────────────────────")
    print(f"Total trades:     {total_trades}")
    print(f"Win rate:         {win_rate:.1f}%  ({total_wins} wins / {total_losses} losses)")
    print(f"Profit factor:    {pf:.2f}")
    print(f"Total return:     {total_return:+.2f}%")
    print(f"Max drawdown:     -{mdd:.2f}%")
    print(f"Sharpe ratio:     {sharpe:.2f}")
    print(f"Total commission: ${commission:.2f}")
    print("──────────────────────────────────────────────────────")

    if pf >= 1.5 and total_trades >= 20:
        verdict = "STRONG"
    elif pf >= 1.2 and total_trades >= 15:
        verdict = "PASS"
    elif pf >= 1.0 and total_trades >= 10:
        verdict = "WEAK"
    else:
        verdict = "FAIL"

    print(f"Baseline verdict: {verdict}")
    print("──────────────────────────────────────────────────────")

    # Save CSV
    csv_path = _save_csv(shared["all_fills"])
    print(f"\nResults saved to {csv_path}")


if __name__ == "__main__":
    main()
