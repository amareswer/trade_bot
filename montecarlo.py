"""
Monte Carlo simulation.

Takes the actual trade outcomes from a backtest and reshuffles
them thousands of times to answer:
  - What is the worst realistic drawdown?
  - What is the probability of ruin (losing >20% of capital)?
  - What is the probability of 10+ consecutive losers?
  - What is the 5th percentile final equity?

This reveals risks that a single backtest sequence never shows.

Run:
    python montecarlo.py
"""
import logging
logging.basicConfig(level=logging.WARNING)

import random
import statistics
from config import cfg
from bot.data.historical_feed import fetch_candles_paginated
from bot.backtest import engine, metrics as metrics_mod

VALIDATED_CONFIG = dict(
    adx_threshold      = 18.0,
    max_ema_spread_pct = 0.005,
    rsi_filter_enabled = True,
)
N_SIMULATIONS  = 10_000
RUIN_THRESHOLD = 0.20


def max_drawdown(equity: list) -> float:
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


def max_consecutive_losses(outcomes: list) -> int:
    best = 0
    current = 0
    for o in outcomes:
        if o < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def simulate(trade_pnls: list, starting_cash: float) -> dict:
    shuffled = trade_pnls[:]
    random.shuffle(shuffled)
    equity = [starting_cash]
    cash   = starting_cash
    for pnl in shuffled:
        cash += pnl
        equity.append(cash)
    return {
        "final":      cash,
        "max_dd":     max_drawdown(equity),
        "max_losses": max_consecutive_losses(shuffled),
        "ruined":     (starting_cash - cash) / starting_cash >= RUIN_THRESHOLD,
    }


def percentile(data: list, p: float) -> float:
    sorted_d = sorted(data)
    idx = p / 100 * (len(sorted_d) - 1)
    lo  = int(idx)
    hi  = min(lo + 1, len(sorted_d) - 1)
    return sorted_d[lo] + (idx - lo) * (sorted_d[hi] - sorted_d[lo])


def main():
    _B  = "\033[1m"
    _R  = "\033[0m"
    _GR = "\033[32m"
    _YL = "\033[33m"
    _RD = "\033[31m"

    print(f"\n  Fetching 5000 × {cfg.backtest.timeframe} candles …")
    try:
        candles = fetch_candles_paginated(
            exchange_id = cfg.exchange.exchange,
            symbol      = cfg.exchange.symbol,
            timeframe   = cfg.backtest.timeframe,
            total_limit = 5000,
        )
    except Exception as exc:
        print(f"  ERROR: {exc}\n")
        return

    print(f"  {len(candles)} candles loaded. Running backtest …\n")

    result = engine.run(
        candles              = candles,
        symbol               = cfg.exchange.symbol,
        timeframe            = cfg.backtest.timeframe,
        strategy_mode        = cfg.strategy.mode,
        starting_cash        = cfg.portfolio.starting_cash,
        risk_per_trade_pct   = cfg.risk.risk_per_trade_pct,
        fee_pct              = cfg.backtest.fee_pct,
        cooldown_ticks       = cfg.risk.cooldown_ticks,
        rsi_period           = cfg.strategy.rsi_period,
        rsi_oversold         = cfg.strategy.rsi_oversold,
        rsi_overbought       = cfg.strategy.rsi_overbought,
        fast_ema_period      = cfg.strategy.fast_ema_period,
        slow_ema_period      = cfg.strategy.slow_ema_period,
        adx_period           = cfg.strategy.adx_period,
        adx_threshold        = VALIDATED_CONFIG["adx_threshold"],
        max_ema_spread_pct   = VALIDATED_CONFIG["max_ema_spread_pct"],
        rsi_filter_enabled   = VALIDATED_CONFIG["rsi_filter_enabled"],
        buy_threshold        = cfg.strategy.buy_threshold,
        sell_threshold       = cfg.strategy.sell_threshold,
        max_position_pct     = cfg.risk.max_position_pct,
        daily_loss_limit_pct = cfg.risk.daily_loss_limit_pct,
        max_drawdown_pct     = cfg.risk.max_drawdown_pct,
        max_trades_per_day   = cfg.risk.max_trades_per_day,
        stop_loss_pct        = cfg.backtest.stop_loss_pct,
        take_profit_pct      = cfg.backtest.take_profit_pct,
    )

    trade_pnls = [
        f.pnl for f in result.fills
        if f.side == "SELL" and f.pnl is not None
    ]

    if len(trade_pnls) < 10:
        print(f"  Too few trades ({len(trade_pnls)}) for Monte Carlo.")
        print(f"  Make sure .env has ADX_THRESHOLD=18 and MAX_EMA_SPREAD_PCT=0.005\n")
        return

    starting_cash = cfg.portfolio.starting_cash
    actual_pf = metrics_mod.compute(result).profit_factor
    print(f"  {len(trade_pnls)} trades extracted  |  "
          f"backtest PF={actual_pf:.2f}  |  "
          f"starting cash ${starting_cash:,.0f}")
    print(f"  Running {N_SIMULATIONS:,} simulations …\n")

    random.seed(42)
    sims = [simulate(trade_pnls, starting_cash) for _ in range(N_SIMULATIONS)]

    finals    = [s["final"]      for s in sims]
    drawdowns = [s["max_dd"]     for s in sims]
    consec    = [s["max_losses"] for s in sims]
    ruin_n    = sum(1 for s in sims if s["ruined"])

    bar = "═" * 52

    print(f"  {_B}{bar}{_R}")
    print(f"  {_B}MONTE CARLO  ({N_SIMULATIONS:,} simulations  ·  "
          f"{len(trade_pnls)} trades){_R}")
    print(f"  {_B}{bar}{_R}\n")

    # ── Final equity ──────────────────────────────────────────────────
    p5   = percentile(finals, 5)
    p50  = percentile(finals, 50)
    p95  = percentile(finals, 95)
    ppos = sum(1 for f in finals if f > starting_cash) / len(finals) * 100

    print(f"  {_B}FINAL EQUITY{_R}")
    print(f"  {'─'*48}")
    rc = _GR if p50 >= starting_cash else _RD
    print(f"  Median outcome        {rc}{p50:>10,.2f}  "
          f"({(p50/starting_cash-1)*100:+.2f}%){_R}")
    print(f"  5th  percentile       {p5:>10,.2f}  "
          f"({(p5/starting_cash-1)*100:+.2f}%)")
    print(f"  95th percentile       {p95:>10,.2f}  "
          f"({(p95/starting_cash-1)*100:+.2f}%)")
    pc = _GR if ppos >= 60 else (_YL if ppos >= 50 else _RD)
    print(f"  Probability positive  {pc}{ppos:>9.1f}%{_R}")

    # ── Drawdown ──────────────────────────────────────────────────────
    dd50 = percentile(drawdowns, 50) * 100
    dd95 = percentile(drawdowns, 95) * 100
    ddwc = max(drawdowns) * 100

    print(f"\n  {_B}DRAWDOWN{_R}")
    print(f"  {'─'*48}")
    print(f"  Median max drawdown   {dd50:>9.2f}%")
    dc = _GR if dd95 < 5 else (_YL if dd95 < 10 else _RD)
    print(f"  95th pct drawdown     {dc}{dd95:>9.2f}%{_R}"
          f"  ← 1-in-20 chance of this or worse")
    print(f"  Worst case            {ddwc:>9.2f}%")

    # ── Losing streaks ────────────────────────────────────────────────
    cs50 = statistics.median(consec)
    cs95 = percentile(consec, 95)
    cswc = max(consec)

    print(f"\n  {_B}LOSING STREAKS{_R}")
    print(f"  {'─'*48}")
    print(f"  Median worst streak   {cs50:>9.0f}  consecutive losses")
    sc = _GR if cs95 <= 8 else (_YL if cs95 <= 12 else _RD)
    print(f"  95th pct streak       {sc}{cs95:>9.0f}  consecutive losses{_R}")
    print(f"  Worst case streak     {cswc:>9}  consecutive losses")

    # ── Ruin ──────────────────────────────────────────────────────────
    ruin_pct = ruin_n / N_SIMULATIONS * 100
    rc2 = _GR if ruin_pct < 1 else (_YL if ruin_pct < 5 else _RD)
    print(f"\n  {_B}RUIN PROBABILITY  (>{RUIN_THRESHOLD*100:.0f}% loss){_R}")
    print(f"  {'─'*48}")
    print(f"  Probability of ruin   {rc2}{ruin_pct:>9.2f}%{_R}")

    # ── Sizing guide ──────────────────────────────────────────────────
    print(f"\n  {_B}POSITION SIZING GUIDE{_R}")
    print(f"  {'─'*48}")
    print(f"  {'Risk/trade':<12}  {'Scaled DD (95th)':>16}  {'Verdict':>10}")
    print(f"  {'─'*12}  {'─'*16}  {'─'*10}")
    for risk in [0.005, 0.01, 0.02, 0.05]:
        scaled = dd95 * (risk / cfg.risk.risk_per_trade_pct)
        ok = "✓ ok" if scaled < 10 else ("~ watch" if scaled < 20 else "✗ risky")
        vc = _GR if scaled < 10 else (_YL if scaled < 20 else _RD)
        print(f"  {risk*100:<11.1f}%  {scaled:>15.1f}%  {vc}{ok}{_R}")

    # ── Verdict ───────────────────────────────────────────────────────
    print(f"\n  {_B}VERDICT{_R}")
    print(f"  {'─'*48}")
    if ppos >= 65 and dd95 < 8 and ruin_pct < 2:
        print(f"  {_GR}✓ Robust: high win probability, "
              f"controlled drawdown, minimal ruin risk.{_R}")
    elif ppos >= 55 and dd95 < 15 and ruin_pct < 5:
        print(f"  {_YL}~ Acceptable: positive expected value "
              f"but tail risk worth monitoring.{_R}")
    else:
        print(f"  {_RD}✗ Concerning: drawdown or ruin risk "
              f"too high for the current position size.{_R}")

    print(f"\n  {_B}{bar}{_R}\n")


if __name__ == "__main__":
    main()
