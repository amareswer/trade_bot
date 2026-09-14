"""
Historical evaluation: BTC/SOL baseline vs. an expanded crypto universe,
same unmodified strategy, same fee/slippage model, on both a deterministic
pinned window and the current rolling window.

IMPORTANT — read before trusting these numbers as "the answer":

1. This is DEVELOPMENT/DIRECTIONAL evidence, not fresh out-of-sample
   validation. All the market data used here predates 2026-09-12 and has
   been examined before in this repo's research history (BTC/SOL windows
   are the same ones already documented in CLAUDE.md; ETH/XRP are new
   RUNS but on old, previously-existing market data). Per this repo's own
   Validation Discipline and the 2026-09-12 review-deadline decision,
   resuming live BUYs requires a walk-forward on data strictly AFTER
   2026-09-12 — this script does not and cannot produce that; only time
   and the paper-mode forward run (dynamic_universe_bot.py) can.
2. Kraken's own CAD OHLCV history is short (~720 candles). Like the
   existing backtest.py / walkforward.py, this uses each base's Binance
   USDT pair as a liquid proxy for its Kraken CAD price action — the same
   proxy-market assumption already documented in CLAUDE.md ("price diff
   0.048% — negligible" for BTC; not independently re-verified per-symbol
   here for ETH/XRP, a disclosed limitation).
3. No true point-in-time historical universe reconstruction is attempted.
   ccxt/exchange APIs don't expose "which pairs were listed and liquid on
   date X" historically — only today's snapshot. This script does NOT
   pretend otherwise: it evaluates the SAME fixed candidate list (today's
   real eligible set) across the whole historical window, which cannot
   suffer from "picking today's winners in hindsight" in the way a
   survivorship-biased backtest usually does (today's screen doesn't know
   which of these 4 will do best over 2024-2026), but it also cannot prove
   what an actually-time-varying dynamic universe would have looked like
   further in the past (a coin that would have failed liquidity in 2024 is
   not excluded from this test). This is the disclosed survivorship-style
   limitation the task asked to name explicitly.
4. The "expanded universe" aggregate below is a SIMPLIFICATION: it reports
   each symbol's OWN independent single-symbol backtest (same engine.py,
   unmodified, each with the full account's starting cash) side by side,
   plus a naive equal-weight blended return. It does NOT model shared-slot
   contention (two symbols signalling on the same day competing for a
   capped number of concurrent positions) — building a true multi-symbol,
   shared-capital, timestamp-interleaved backtest engine was out of scope
   for this pass. The real dynamic_universe_bot.py DOES enforce shared
   capital + slot caps; this script only isolates "is there edge to be
   found in these extra symbols at all" before worrying about how it would
   compete for capital.

Usage:
    .venv/bin/python dynamic_universe_backtest.py
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from config import cfg
from bot.data.historical_feed import fetch_candles_paginated
from bot.backtest import engine, metrics as metrics_mod
from bot.backtest.params import engine_kwargs_from_cfg
from bot.strategy.fingerprint import compute_strategy_hash

BASELINE_SYMBOLS = ["BTC/USDT", "SOL/USDT"]           # current live whitelist, USDT proxy
EXPANDED_SYMBOLS = ["BTC/USDT", "SOL/USDT", "ETH/USDT", "XRP/USDT"]  # + the only other
                                                        # CAD pairs that clear Kraken's
                                                        # liquidity/spread/depth bar today
                                                        # (verified live 2026-09-13 via
                                                        # dynamic_universe_bot.py --once —
                                                        # PEPE/DOGE/XDC all fail volume)

PINNED_SINCE = "2024-03-07"
PINNED_UNTIL = "2026-06-20"   # same deterministic window CLAUDE.md's fingerprint check uses


def _date_to_ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def evaluate_symbol(symbol: str, since_ms: int | None, until_ms: int | None, limit: int) -> dict:
    # Hardcoded to Binance, NOT cfg.exchange.exchange — the live/paused
    # BTC/CAD .env has EXCHANGE=kraken, and Kraken's own USDT-pair history
    # is short/inconsistent. This mirrors backtest.py's own documented
    # methodology (EXCHANGE=binance SYMBOL=BTC/USDT), which is independent
    # of whichever exchange the live bot is currently configured against.
    # (Caught live during this evaluation: an earlier version of this
    # script silently used the ambient kraken/BTC-CAD config and produced
    # a truncated, meaningless ~1-month "pinned window" with 0 trades for
    # every symbol — verify-against-real-inputs lesson applied.)
    candles = fetch_candles_paginated(
        exchange_id="binance",
        symbol=symbol,
        timeframe=cfg.backtest.timeframe,
        total_limit=limit,
        since_ms=since_ms,
        until_ms=until_ms,
    )
    run_kwargs = engine_kwargs_from_cfg(cfg, symbol=symbol)
    result = engine.run(candles=candles, **run_kwargs)
    m = metrics_mod.compute(result)
    bh_return = (candles[-1].close - candles[0].close) / candles[0].close if candles[0].close else 0.0
    return {
        "symbol": symbol,
        "period": f"{candles[0].timestamp:%Y-%m-%d} → {candles[-1].timestamp:%Y-%m-%d}",
        "candles": len(candles),
        "trades": m.total_trades,
        "win_rate": m.win_rate,
        "profit_factor": m.profit_factor,
        "gross_profit_factor": m.gross_profit_factor,
        "total_return_pct": m.total_return_pct * 100,  # stored as a fraction (e.g. -0.0072) — normalize to a true percent here so every downstream use (formatting, averaging) is consistent with buy_and_hold_return_pct below
        "max_drawdown_pct": m.max_drawdown_pct,
        "total_fees": m.total_fees,
        "buy_and_hold_return_pct": bh_return * 100,
    }


def _fmt_row(r: dict) -> str:
    return (
        f"| {r['symbol']} | {r['period']} | {r['trades']} | {r['win_rate']*100:.1f}% "
        f"| {r['profit_factor']:.2f} | {r['gross_profit_factor']:.2f} "
        f"| {r['total_return_pct']:+.2f}% | {r['max_drawdown_pct']*100:.2f}% "
        f"| {r['buy_and_hold_return_pct']:+.2f}% | ${r['total_fees']:.2f} |"
    )


def main() -> None:
    strategy_hash = compute_strategy_hash()
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = os.path.join("logs", f"dynamic_universe_backtest_{date_str}.md")

    lines: list[str] = []
    lines.append(f"# Dynamic universe evaluation — {date_str}\n")
    lines.append(f"Strategy hash: `{strategy_hash}` (unchanged from the live/paused BTC+SOL fingerprint — this evaluation changes the universe, not the strategy).\n")
    lines.append(
        "**Read the limitations at the top of `dynamic_universe_backtest.py` before "
        "treating any number below as validation.** In short: this is historical, "
        "previously-existing market data, not the genuinely-fresh post-2026-09-12 "
        "evaluation this repo's own Validation Discipline requires before a live "
        "decision — that only comes from the forward paper run "
        "(`dynamic_universe_bot.py`), which has ~0 days of history as of this writing.\n"
    )

    header = (
        "| Symbol | Period | Trades | Win% | Net PF | Gross PF | Net return | Max DD | B&H return | Fees |\n"
        "|---|---|---|---|---|---|---|---|---|---|"
    )

    for window_name, since, until, limit in [
        ("Pinned window (deterministic)", PINNED_SINCE, PINNED_UNTIL, cfg.backtest.limit),
        ("Rolling window (current)", None, None, cfg.backtest.limit),
    ]:
        since_ms = _date_to_ms(since) if since else None
        until_ms = _date_to_ms(until) if until else None
        print(f"\n=== {window_name} ===")
        lines.append(f"\n## {window_name}\n")
        lines.append(header)

        results: dict[str, dict] = {}
        for sym in EXPANDED_SYMBOLS:
            print(f"  running {sym} …")
            r = evaluate_symbol(sym, since_ms, until_ms, limit)
            results[sym] = r
            lines.append(_fmt_row(r))
            print(f"    trades={r['trades']} net_pf={r['profit_factor']:.2f} return={r['total_return_pct']:+.2f}% b&h={r['buy_and_hold_return_pct']:+.2f}%")

        baseline_return = sum(results[s]["total_return_pct"] for s in BASELINE_SYMBOLS) / len(BASELINE_SYMBOLS)
        expanded_return = sum(results[s]["total_return_pct"] for s in EXPANDED_SYMBOLS) / len(EXPANDED_SYMBOLS)
        baseline_pf_min = min(results[s]["profit_factor"] for s in BASELINE_SYMBOLS)
        expanded_pf_min = min(results[s]["profit_factor"] for s in EXPANDED_SYMBOLS)

        lines.append("")
        lines.append(
            f"**Naive equal-weight blend** (no shared-slot contention modeled — see "
            f"limitation #4 above): baseline (BTC+SOL) avg return "
            f"**{baseline_return:+.2f}%**, expanded (BTC+SOL+ETH+XRP) avg return "
            f"**{expanded_return:+.2f}%**. Worst-symbol net PF: baseline "
            f"{baseline_pf_min:.2f}, expanded {expanded_pf_min:.2f}."
        )
        verdict = "WORSE" if expanded_return < baseline_return else "BETTER"
        lines.append(
            f"\n→ On this window, expanding the universe to include ETH and XRP would "
            f"have made the blended result **{verdict}** than staying with BTC/SOL alone."
        )

    report = "\n".join(lines)
    print("\n" + report)
    os.makedirs("logs", exist_ok=True)
    with open(out_path, "w") as f:
        f.write(report + "\n")
    print(f"\nReport written to {out_path}")


if __name__ == "__main__":
    main()
