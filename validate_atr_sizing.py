"""ATR-based stop-distance walk-forward — validates PAPER_ATR_SIZING_ENABLED's
paired stop-distance override (stock_bot/config.py's calc_shares_atr_risk) before
it's ever enabled live, per CLAUDE.md's existing "do not enable without a
stock_backtest.py walk-forward PASS first" rule.

Added 2026-08-23, part of the risk-hardening pass that followed RULE_WHITELIST's
removal as a BUY gate. Deliberately a SEPARATE script from stock_backtest.py, not
a flag added to it — stock_backtest.py's output (logs/stock_backtest_latest.json)
is a fixed path LiveTradingGate.check_gate1() depends on for the CURRENT
(flat-stop) live behavior; running an ATR-mode variant there would corrupt that
gate's read. This script writes its own separate dated report and touches no
JSON at all.

Question this answers: does swapping the flat 5% stop for an ATR(14)*mult stop
distance change the PASS/FAIL verdict for the symbols that already passed under
the flat-stop model? Defaults to RULE_WHITELIST (the already-PASSED set) rather
than the full watchlist — the full watchlist includes symbols that already FAIL
under flat stops (HOOD, NCLH, AC.TO, CCL, INTC, NVDA, TSLA, SHOP.TO, META, AMZN
per logs/stock_backtest_20260710.md), and re-testing those against a different
stop distance doesn't answer the actual question ("is it still safe to flip
PAPER_ATR_SIZING_ENABLED for the trusted, already-validated set").

Does NOT touch bot/strategy/* or build_indicator_config() — only the SL/TP
intra-candle check in stock_bot/backtest/engine.py changes (new optional
atr_sl_mult field, default None = pre-existing flat behavior unchanged). Strategy
hash b30f2f9e769c8d41 is unaffected.

Run:    .venv/bin/python validate_atr_sizing.py
Output: console table + logs/stock_backtest_atr_validation_<date>.md
        (NOT logs/stock_backtest_latest.json — that stays stock_backtest.py's)

Env overrides:
  ATR_VALIDATE_SYMBOLS=AAPL,NVDA   (default: RULE_WHITELIST from stock_bot/.env)
  ATR_VALIDATE_DAYS=1500           (fetch depth)
  ATR_VALIDATE_NOTIONAL=1000       ($ per trade)
  ATR_VALIDATE_MULT=2.0            (default: PAPER_ATR_SL_MULT from stock_bot/.env)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date

# Ensure project root importable when run as a script
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from stock_bot.config import load as load_stock_config
from stock_bot.data.price_feed import fetch_candles
from stock_bot.backtest.engine import (
    StockBacktestConfig,
    BacktestResult,
    run_symbol,
)
from stock_bot.strategy.rules import build_indicator_config

logging.basicConfig(level=logging.WARNING)
logging.getLogger("bot.strategy").setLevel(logging.ERROR)

# Same walk-forward windows and gate criteria as stock_backtest.py — an
# apples-to-apples comparison against the flat-stop baseline.
WINDOWS = [0, 750, 500, 250]
MIN_TRADES_FOR_VERDICT = 3
MIN_TRADES_FULL_WINDOW = 10
MAX_SL_EXIT_RATE       = 70.0
MIN_PF                 = 1.2


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def run() -> int:
    cfg_stock = load_stock_config()
    default_symbols = ",".join(sorted({
        s.strip().upper() for s in cfg_stock.rule_whitelist_str.split(",") if s.strip()
    }))
    symbols = [
        s.strip().upper()
        for s in os.getenv("ATR_VALIDATE_SYMBOLS", default_symbols).split(",")
        if s.strip()
    ]
    days     = int(os.getenv("ATR_VALIDATE_DAYS", "1500"))
    notional = float(os.getenv("ATR_VALIDATE_NOTIONAL", "1000"))
    atr_mult = float(os.getenv("ATR_VALIDATE_MULT", str(cfg_stock.paper_atr_sl_mult)))

    bt_cfg = StockBacktestConfig(
        notional=notional,
        slippage_bps=cfg_stock.paper_slippage_bps,
        stop_loss_pct=cfg_stock.paper_stop_loss_pct,   # fallback only when ATR is unavailable
        take_profit_pct=cfg_stock.paper_take_profit_pct,
        indicator=build_indicator_config(),
        atr_sl_mult=atr_mult,
    )

    print(f"\nATR-STOP WALK-FORWARD VALIDATION — {len(symbols)} symbols · {days}d fetch")
    print(f"Stop distance: ATR(14) × {atr_mult} (falls back to flat "
          f"{bt_cfg.stop_loss_pct*100:.0f}% when ATR unavailable) · TP "
          f"{bt_cfg.take_profit_pct*100:.0f}% · {bt_cfg.slippage_bps} bps slippage + "
          f"IBKR commissions · ${notional:,.0f}/trade")
    print("Reference: logs/stock_backtest_20260710.md validated these symbols under a "
          "FLAT 5% stop — this run checks whether the ATR-based stop distance holds up.\n")

    report_lines = [
        f"# ATR-stop walk-forward validation — {date.today().isoformat()}",
        "",
        f"Validates PAPER_ATR_SIZING_ENABLED's paired stop-distance override "
        f"(ATR(14) × {atr_mult}) against the RULE_WHITELIST symbols, which were "
        f"originally validated under a flat {bt_cfg.stop_loss_pct*100:.0f}% stop. "
        f"NOT the live gate JSON — see validate_atr_sizing.py docstring.",
        "",
        "| Symbol | Window | Trades | Win rate | PF | Net P&L | SL rate | Verdict |",
        "|--------|--------|--------|----------|-----|---------|---------|---------|",
    ]

    passes: list[str] = []
    fails:  list[str] = []
    regressions: list[str] = []   # PASS under flat stop, FAIL here

    # PASS under the flat-stop model per logs/stock_backtest_20260710.md — used
    # only to flag a regression clearly; does not affect this run's own verdicts.
    # That report tested "RY.TO" (TSX); RULE_WHITELIST holds "RY" (the NYSE
    # cross-listing — TSX symbols are permanently API-blocked, see CLAUDE.md) —
    # a different ticker, close but not identical price history. Both are
    # checked here since either could appear depending on ATR_VALIDATE_SYMBOLS.
    _FLAT_STOP_PASS = {"MRNA", "AMD", "RY", "RY.TO", "PLTR"}

    for sym in symbols:
        candles = fetch_candles(sym, "1d", days)
        if not candles or len(candles) < 400:
            print(f"{sym:<10} SKIP — insufficient history ({len(candles) if candles else 0} candles)")
            report_lines.append(f"| {sym} | — | — | — | — | — | — | SKIP (thin history) |")
            continue

        sym_ok = True
        for w in WINDOWS:
            start_idx = 0 if w == 0 else max(0, len(candles) - w)
            res: BacktestResult = run_symbol(sym, candles, bt_cfg, trade_start_idx=start_idx)
            label = "full" if w == 0 else f"{w}d"
            n, pf, wr, sl = res.n_trades, res.profit_factor, res.win_rate, res.sl_exit_rate

            if w == 0:
                if n < MIN_TRADES_FULL_WINDOW:
                    sym_ok = False
                if sl > MAX_SL_EXIT_RATE:
                    sym_ok = False
            if n >= MIN_TRADES_FOR_VERDICT and pf < MIN_PF:
                sym_ok = False

            note = "" if n >= MIN_TRADES_FOR_VERDICT else " (low sample)"
            print(f"{sym:<10} {label:<6} trades={n:<3} WR={wr:5.1f}%  PF={_fmt_pf(pf):<5} "
                  f"net=${res.total_net_pnl:+8.2f}  SL={sl:4.1f}%{note}")
            report_lines.append(
                f"| {sym} | {label} | {n} | {wr:.1f}% | {_fmt_pf(pf)} | "
                f"${res.total_net_pnl:+.2f} | {sl:.1f}% |{note or ' '}|"
            )

        verdict = "PASS" if sym_ok else "FAIL"
        (passes if sym_ok else fails).append(sym)
        if sym.upper() in _FLAT_STOP_PASS and not sym_ok:
            regressions.append(sym)
        print(f"{sym:<10} → {verdict}")
        print()
        report_lines.append(f"| **{sym}** | | | | | | | **{verdict}** |")

    report_lines += [
        "",
        "## Summary",
        f"- PASS ({len(passes)}): {', '.join(passes) if passes else '—'}",
        f"- FAIL ({len(fails)}): {', '.join(fails) if fails else '—'}",
        f"- Regressions vs. flat-stop baseline (PASS there, FAIL here): "
        f"{', '.join(regressions) if regressions else 'none'}",
        "",
        f"Gate: full-window trades ≥ {MIN_TRADES_FULL_WINDOW}, PF ≥ {MIN_PF} in every "
        f"window with ≥ {MIN_TRADES_FOR_VERDICT} trades, SL-exit rate ≤ {MAX_SL_EXIT_RATE:.0f}% "
        f"(identical criteria to stock_backtest.py).",
    ]

    os.makedirs("logs", exist_ok=True)
    out = f"logs/stock_backtest_atr_validation_{date.today().strftime('%Y%m%d')}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines) + "\n")

    print("=" * 70)
    print(f"PASS ({len(passes)}): {', '.join(passes) if passes else '—'}")
    print(f"FAIL ({len(fails)}): {', '.join(fails) if fails else '—'}")
    if regressions:
        print(f"⚠️  REGRESSIONS vs. flat-stop baseline: {', '.join(regressions)}")
    print(f"Report: {out}")
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(run())
