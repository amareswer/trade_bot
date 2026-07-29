"""
Swing book ATR-stop research — PRE-REGISTERED, RESEARCH ONLY.

===============================================================================
PRE-REGISTRATION (committed before running anything — do not edit after seeing
results; if the split/criteria need to change, that's a new experiment)
===============================================================================

Background: the swing book (stock_bot/fast_validator.py, 1h candles, 48h max
hold) was retired 2026-07-22 after backtesting the real Mode A/B rule strategy
against its own 7 traded symbols with its REAL 1.5%/3.0% SL/TP gave combined
PF 0.76 across 394 trades, 64.0% SL-exit rate, only 1/7 symbols passing
(logs/CLAUDE_HISTORY.md, "Swing book retired (2026-07-22)"). Diagnosed as
"1.5% fixed stop too tight for hourly noise" — the same failure shape as
crypto's 1h day-trading experiment (63% SL-exit rate, ruled out 2026-07-10),
and the same shape BTC/CAD had at 1h before ATR×2.0 fixed it on 2026-07-17.

HYPOTHESIS: replacing the swing book's fixed 1.5% stop-loss with an ATR-based
stop (ATR × 2.0, matching the BTC fix exactly — take-profit stays the fixed
3.0%, only the stop changes) raises combined PF above 1.0 on the same 7
symbols (HOOD, MRNA, NCLH, AC.TO, RY, AMZN, BNS) without materially raising
the SL-exit rate.

PASS CRITERIA (all four required — set now, before running anything):
  1. Combined PF >= 1.2 in-sample across all 7 symbols on 1h candles
  2. At least 4 of 7 individual symbols pass PF >= 1.0 (in-sample)
  3. SL-exit rate drops meaningfully from the 64% baseline — defined here,
     before seeing results, as: below 50%
  4. Result must hold on a second, genuinely separate out-of-sample window
     (same four checks re-applied to OOS: combined PF >= 1.2, >=4/7 symbols
     PF >= 1.0, SL-exit rate < 50%)

Anything short of all four = NOT SUPPORTED. This script runs ATR x 2.0 ONCE.
If it fails, the result is reported as FAILED — no grid search over other
multipliers to find one that passes (that would be exactly the p-hacking
pattern pre-registration exists to prevent). A different multiplier is a new,
separately pre-registered experiment.

DATA-AVAILABILITY CHECK (done before choosing the split — this is a fact
about what data exists, not a result, so checking it first is not
p-hacking): yfinance 1h data caps at ~730 days. Checked empirically
2026-07-28 — all 7 symbols return consistent history from ~2023-08-30/09-01
through 2026-07-28. Common range across all 7: 2023-09-01 -> 2026-07-28.
SPLIT (fixed before running the backtest):
  IN-SAMPLE      2023-09-01 -> 2026-01-28  (~2y5m)
  OUT-OF-SAMPLE  2026-01-28 -> 2026-07-28  (6 months, genuinely later data,
                                             not a shuffled subset)

WHAT THIS DOES NOT TOUCH: stock_bot/fast_validator.py, stock_bot/.env,
FAST_ENABLED, or any other live-facing code. This is a standalone research
script. A passing result here would only justify a follow-up conversation
about re-enabling the swing book -- it does not re-enable anything itself.

===============================================================================

Reuses the same backtest engine used for the original 0.76 PF finding
(stock_bot/backtest/engine.py: BacktestTrade, BacktestResult,
StockBacktestConfig, IBKR commission model) and the same unmodified crypto
IndicatorStrategy (Mode A/B) the swing book's own validation used. Only the
stop-loss calculation is swapped from fixed-pct to ATR-based, using the exact
ATR function (bot.indicators.indicators.atr) and formula
(sl = entry_price - atr * mult) the live BTC ATR SL fix uses
(bot/main.py, ~1855-1870), computed from the strategy's own _highs/_lows/_closes
at the moment of fill -- the same mechanism, not a reimplementation.

Run:    .venv/bin/python swing_atr_walkforward.py
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logging.basicConfig(level=logging.WARNING)
logging.getLogger("bot.strategy").setLevel(logging.ERROR)

from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig
from bot.strategy.threshold_strategy import Signal
from bot.data.historical_feed import Candle as StrategyCandle
from bot.indicators.indicators import atr as calc_atr
from stock_bot.data.price_feed import fetch_candles
from stock_bot.strategy.rules import build_indicator_config
from stock_bot.backtest.engine import BacktestTrade, BacktestResult, StockBacktestConfig
from stock_bot.analysis.paper_report import _round_trip_commission

# ── Pre-registered constants ────────────────────────────────────────────────
SYMBOLS      = ["HOOD", "MRNA", "NCLH", "AC.TO", "RY", "AMZN", "BNS"]
LOOKBACK_DAYS = 729          # yfinance 1h cap is ~730 days
ATR_MULT     = 2.0           # matching the BTC fix, run once, no grid search
ATR_PERIOD   = 14            # matching ATR_PERIOD used elsewhere in this repo
FIXED_TP_PCT = 0.03          # swing book's real TP (FAST_TP_PCT) — unchanged
NOTIONAL     = 250.0         # FAST_RISK_PCT (0.25) x FAST_STARTING_CASH (1000)
SLIPPAGE_BPS = 15            # matches PAPER_SLIPPAGE_BPS default

IS_START  = datetime(2023, 9, 1,  tzinfo=timezone.utc)
IS_END    = datetime(2026, 1, 28, tzinfo=timezone.utc)
OOS_START = IS_END
OOS_END   = datetime(2026, 7, 28, tzinfo=timezone.utc)

PASS_PF          = 1.2
PASS_SYMBOL_PF   = 1.0
PASS_MIN_SYMBOLS = 4
PASS_SL_RATE_MAX = 50.0


# ── ATR-stop backtest loop (copy of engine.run_symbol with only the SL
#    calculation swapped — kept separate from stock_bot/backtest/engine.py
#    on purpose, per "research only, don't touch live/shared code") ──────────
def run_symbol_atr(
    symbol: str,
    candles: list,
    cfg: StockBacktestConfig,
    atr_mult: float,
    atr_period: int = ATR_PERIOD,
    strategy: Optional[IndicatorStrategy] = None,
) -> BacktestResult:
    if strategy is None:
        strategy = IndicatorStrategy(cfg.indicator)
    slip = cfg.slippage_bps / 10_000.0

    trades: list[BacktestTrade] = []
    in_pos        = False
    entry_price   = 0.0
    entry_ts      = None
    shares        = 0
    pending_entry = False
    pending_exit  = False
    sl_price      = 0.0
    tp_price      = 0.0
    atr_fallback_count = 0

    def close_trade(exit_px: float, ts, reason: str) -> None:
        nonlocal in_pos, pending_exit
        commission = _round_trip_commission(symbol, shares)
        trades.append(BacktestTrade(
            symbol=symbol, entry_ts=entry_ts, exit_ts=ts,
            entry_price=entry_price, exit_price=exit_px * (1 - slip),
            shares=shares, commission=commission, exit_reason=reason,
        ))
        in_pos = False
        pending_exit = False

    for i, c in enumerate(candles):
        if pending_entry:
            pending_entry = False
            fill = c.open * (1 + slip)
            n = int(cfg.notional / fill) if fill > 0 else 0
            if n > 0:
                in_pos, entry_price, entry_ts, shares = True, fill, c.timestamp, n
                # ATR at the moment of fill, from the strategy's own running
                # highs/lows/closes (already includes the signal candle —
                # evaluate() appends before computing indicators). Same
                # function + same formula as the live BTC ATR SL fix.
                atr_val = calc_atr(
                    list(strategy._highs), list(strategy._lows), list(strategy._closes),
                    atr_period,
                )
                if atr_val is not None and atr_val > 0:
                    sl_price = entry_price - atr_val * atr_mult
                else:
                    # Matches the live bot's fallback (bot/main.py): if ATR
                    # is unavailable, fall back to the swing book's real
                    # fixed 1.5% SL rather than leaving the position unprotected.
                    atr_fallback_count += 1
                    sl_price = entry_price * (1 - 0.015)
                tp_price = entry_price * (1 + cfg.take_profit_pct)
        elif pending_exit and in_pos:
            close_trade(c.open, c.timestamp, "strategy")

        if in_pos:
            if c.low <= sl_price:
                close_trade(min(c.open, sl_price), c.timestamp, "sl")
            elif c.high >= tp_price:
                close_trade(max(c.open, tp_price), c.timestamp, "tp")

        sig = strategy.evaluate(StrategyCandle(
            timestamp=c.timestamp, open=c.open, high=c.high,
            low=c.low, close=c.close, volume=c.volume,
        ))

        if not in_pos and not pending_entry and sig == Signal.BUY:
            pending_entry = True
        elif in_pos and sig == Signal.SELL:
            pending_exit = True

    if in_pos:
        last = candles[-1]
        close_trade(last.close, last.timestamp, "end_of_data")

    result = BacktestResult(
        symbol=symbol, trades=trades, candles_total=len(candles), trade_start_idx=0,
    )
    result.atr_fallback_count = atr_fallback_count  # type: ignore[attr-defined]
    return result


def _window_result(result: BacktestResult, start: datetime, end: datetime) -> BacktestResult:
    """Bucket a full-history result's trades by entry_ts into [start, end)."""
    windowed = [t for t in result.trades if start <= t.entry_ts < end]
    return BacktestResult(
        symbol=result.symbol, trades=windowed,
        candles_total=result.candles_total, trade_start_idx=0,
    )


@dataclass
class Row:
    symbol: str
    trades: int
    pf: float
    win_rate: float
    sl_rate: float
    net_pnl: float


def _rows_for(results: dict[str, BacktestResult]) -> list[Row]:
    return [
        Row(sym, r.n_trades, r.profit_factor, r.win_rate, r.sl_exit_rate, r.total_net_pnl)
        for sym, r in results.items()
    ]


def _combined_pf(results: dict[str, BacktestResult]) -> float:
    wins   = sum(t.net_pnl for r in results.values() for t in r.completed if t.net_pnl > 0)
    losses = -sum(t.net_pnl for r in results.values() for t in r.completed if t.net_pnl <= 0)
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def _combined_sl_rate(results: dict[str, BacktestResult]) -> float:
    all_completed = [t for r in results.values() for t in r.completed]
    if not all_completed:
        return 0.0
    sl = sum(1 for t in all_completed if t.exit_reason == "sl")
    return sl / len(all_completed) * 100.0


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def _print_table(title: str, results: dict[str, BacktestResult]) -> None:
    rows = _rows_for(results)
    print(f"\n  {title}")
    print(f"  {'Symbol':<8} | {'Trades':>6} | {'PF':>6} | {'WinRate':>7} | {'SL%':>6} | {'NetPnL':>9}")
    print(f"  {'-'*8}-+-{'-'*6}-+-{'-'*6}-+-{'-'*7}-+-{'-'*6}-+-{'-'*9}")
    for row in rows:
        print(
            f"  {row.symbol:<8} | {row.trades:>6} | {_fmt_pf(row.pf):>6} | "
            f"{row.win_rate:>6.1f}% | {row.sl_rate:>5.1f}% | ${row.net_pnl:>+8.2f}"
        )
    combined_pf = _combined_pf(results)
    combined_sl = _combined_sl_rate(results)
    total_trades = sum(r.n_trades for r in results.values())
    print(f"  {'-'*8}-+-{'-'*6}-+-{'-'*6}-+-{'-'*7}-+-{'-'*6}-+-{'-'*9}")
    print(
        f"  {'COMBINED':<8} | {total_trades:>6} | {_fmt_pf(combined_pf):>6} | "
        f"{'':>7} | {combined_sl:>5.1f}% |"
    )


def _evaluate(results: dict[str, BacktestResult]) -> tuple[float, int, float]:
    """Return (combined_pf, symbols_passing, sl_rate) for a window."""
    combined_pf = _combined_pf(results)
    sl_rate = _combined_sl_rate(results)
    symbols_passing = sum(
        1 for r in results.values()
        if r.n_trades > 0 and r.profit_factor >= PASS_SYMBOL_PF
    )
    return combined_pf, symbols_passing, sl_rate


def main() -> None:
    print("\n  SWING BOOK ATR-STOP RESEARCH — PRE-REGISTERED, ATR x 2.0, RUN ONCE")
    print(f"  Symbols: {', '.join(SYMBOLS)}")
    print(f"  ATR period={ATR_PERIOD}  mult={ATR_MULT}  TP={FIXED_TP_PCT*100:.1f}% (fixed, unchanged)")
    print(f"  Notional=${NOTIONAL:.0f}/trade  slippage={SLIPPAGE_BPS}bps  1h candles")

    # ── Fetch + report real availability (fact-check, not a result) ────────
    print("\n  Fetching 1h candles (lookback=%dd, yfinance 1h cap ~730d)…" % LOOKBACK_DAYS)
    all_candles: dict[str, list] = {}
    for sym in SYMBOLS:
        candles = fetch_candles(sym, interval="1h", lookback_days=LOOKBACK_DAYS)
        if not candles:
            print(f"  {sym:8} NO DATA — excluded")
            continue
        all_candles[sym] = candles
        print(
            f"  {sym:8} n={len(candles):5}  "
            f"{candles[0].timestamp.date()} -> {candles[-1].timestamp.date()}"
        )

    missing = set(SYMBOLS) - set(all_candles)
    if missing:
        print(f"\n  WARNING: {len(missing)}/7 symbols missing data: {sorted(missing)}")
        print("  Proceeding with the remaining symbols — denominators below reflect this.")

    print(f"\n  Pre-registered split (decided before this run, from data-availability only):")
    print(f"    IN-SAMPLE      {IS_START.date()} -> {IS_END.date()}")
    print(f"    OUT-OF-SAMPLE  {OOS_START.date()} -> {OOS_END.date()}")

    # ── Run once per symbol over full history, bucket by entry_ts ──────────
    bt_cfg = StockBacktestConfig(
        notional=NOTIONAL,
        slippage_bps=SLIPPAGE_BPS,
        stop_loss_pct=0.015,          # unused (ATR replaces it) — kept for record
        take_profit_pct=FIXED_TP_PCT,
        indicator=build_indicator_config(),
    )

    full_results: dict[str, BacktestResult] = {}
    fallback_counts: dict[str, int] = {}
    for sym, candles in all_candles.items():
        res = run_symbol_atr(sym, candles, bt_cfg, ATR_MULT, ATR_PERIOD)
        full_results[sym] = res
        fallback_counts[sym] = getattr(res, "atr_fallback_count", 0)

    is_results  = {sym: _window_result(r, IS_START, IS_END)   for sym, r in full_results.items()}
    oos_results = {sym: _window_result(r, OOS_START, OOS_END) for sym, r in full_results.items()}

    total_fallback = sum(fallback_counts.values())
    if total_fallback:
        print(
            f"\n  NOTE: ATR unavailable at entry for {total_fallback} fills "
            f"(fell back to fixed 1.5% SL, matching the live bot's fallback path)."
        )

    _print_table("IN-SAMPLE (2023-09-01 -> 2026-01-28)", is_results)
    _print_table("OUT-OF-SAMPLE (2026-01-28 -> 2026-07-28)", oos_results)

    # ── Evaluate against the 4 pre-registered criteria ──────────────────────
    is_pf, is_syms, is_sl   = _evaluate(is_results)
    oos_pf, oos_syms, oos_sl = _evaluate(oos_results)

    n = len(all_candles)
    c1 = is_pf >= PASS_PF
    c2 = is_syms >= PASS_MIN_SYMBOLS
    c3 = is_sl < PASS_SL_RATE_MAX
    c4 = (oos_pf >= PASS_PF) and (oos_syms >= PASS_MIN_SYMBOLS) and (oos_sl < PASS_SL_RATE_MAX)

    def _pf(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    print("\n" + "=" * 78)
    print("  PRE-REGISTERED CRITERIA — EVALUATED AGAINST RESULTS ABOVE, NO CHANGES MADE")
    print("=" * 78)
    print(f"  1. In-sample combined PF >= {PASS_PF}         : {is_pf:.2f}  -> {_pf(c1)}")
    print(f"  2. >= {PASS_MIN_SYMBOLS}/{n} symbols PF >= {PASS_SYMBOL_PF} (in-sample) : {is_syms}/{n}  -> {_pf(c2)}")
    print(f"  3. SL-exit rate < {PASS_SL_RATE_MAX:.0f}% (baseline 64.0%)  : {is_sl:.1f}%  -> {_pf(c3)}")
    print(f"  4. Holds out-of-sample (same 3 checks)  : PF={oos_pf:.2f} syms={oos_syms}/{n} SL={oos_sl:.1f}%  -> {_pf(c4)}")
    print("=" * 78)

    overall = c1 and c2 and c3 and c4
    if overall:
        print(
            "\n  RESULT: SUPPORTED — all 4 pre-registered criteria pass.\n"
            "  This justifies a follow-up conversation about re-enabling the swing book.\n"
            "  It does NOT re-enable anything — FAST_ENABLED is untouched by this script.\n"
        )
    else:
        failed = [i + 1 for i, ok in enumerate([c1, c2, c3, c4]) if not ok]
        print(
            f"\n  RESULT: NOT SUPPORTED — criteria {failed} failed.\n"
            "  Per pre-registration, this is reported as FAILED. No other ATR multiplier\n"
            "  was tried. The ATR x 2.0 fix that worked for BTC does not carry over as-is.\n"
        )

    print("  No files touched other than this script and its console output.")
    print("  stock_bot/.env, stock_bot/fast_validator.py: NOT modified. FAST_ENABLED: NOT changed.\n")


if __name__ == "__main__":
    main()
