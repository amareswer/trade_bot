"""
Market regime analysis.

Tests whether the validated edge holds across different market
conditions: bull trends, bear trends, and sideways markets.

A strategy that only works in one regime is fragile.
A strategy that works across regimes has structural edge.

Run:
    python regimes.py
"""
import logging
logging.basicConfig(level=logging.WARNING)

from config import cfg
from bot.data.historical_feed import fetch_candles_paginated, slice_candles
from bot.backtest import engine, metrics as metrics_mod

VALIDATED_CONFIG = dict(
    adx_threshold      = 15.0,
    max_ema_spread_pct = 0.005,
    rsi_filter_enabled = True,
)

# Regimes defined by approximate BTC market periods
# Adjust dates if your data range differs
REGIMES = [
    {
        "label":       "Bull 2024",
        "description": "BTC $50k → $100k",
        "start":       "2024-02-22",
        "end":         "2024-12-01",
    },
    {
        "label":       "Correction 2025-Q1",
        "description": "BTC $100k → $75k",
        "start":       "2024-12-01",
        "end":         "2025-04-01",
    },
    {
        "label":       "Recovery 2025",
        "description": "BTC $75k → $95k+",
        "start":       "2025-04-01",
        "end":         None,
    },
]


def run_period(candles, start, end):
    period = slice_candles(candles, start, end)
    if len(period) < 50:
        return None, 0
    result = engine.run(
        candles              = period,
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
    m = metrics_mod.compute(result)
    return m, len(period)


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

    print(f"  {len(candles)} candles loaded\n")

    print(f"{_B}  REGIME ANALYSIS{_R}")
    print(f"  Config: ADX≥{VALIDATED_CONFIG['adx_threshold']}  "
          f"EMA≤{VALIDATED_CONFIG['max_ema_spread_pct']*100:.1f}%  RSI=on\n")

    print(f"  {'Regime':<22}  {'Candles':>7}  {'Trades':>6}  "
          f"{'Win%':>6}  {'PF':>6}  {'Return':>8}  {'Sharpe':>7}")
    print(f"  {'─'*22}  {'─'*7}  {'─'*6}  "
          f"{'─'*6}  {'─'*6}  {'─'*8}  {'─'*7}")

    regime_pfs = []
    for r in REGIMES:
        m, n_candles = run_period(candles, r["start"], r["end"])
        if m is None:
            print(f"  {r['label']:<22}  insufficient data")
            continue
        trades = m.total_trades
        wr     = m.win_rate * 100
        pf     = m.profit_factor
        ret    = (m.final_value / m.starting_cash - 1) * 100
        sr     = m.sharpe_ratio
        pfc    = _GR if pf >= 1.1 else (_YL if pf >= 1.0 else _RD)
        regime_pfs.append(pf)
        print(f"  {r['label']:<22}  {n_candles:>7}  {trades:>6}  "
              f"{wr:>5.1f}%  {pfc}{pf:>6.2f}{_R}  {ret:>+7.2f}%  {sr:>7.2f}")
        print(f"  {'':22}  {r['description']}")

    # ── Consistency verdict ───────────────────────────────────────────
    if regime_pfs:
        print(f"\n  {'─'*56}")
        all_positive = all(pf >= 1.0 for pf in regime_pfs)
        all_viable   = all(pf >= 1.1 for pf in regime_pfs)
        spread       = max(regime_pfs) - min(regime_pfs)
        if all_viable:
            print(f"  {_GR}✓ Edge holds across all regimes (all PF ≥ 1.1){_R}")
        elif all_positive:
            print(f"  {_YL}~ Edge positive in all regimes but weak in some{_R}")
        else:
            print(f"  {_RD}✗ Edge fails in at least one regime{_R}")
        print(f"  PF range: {min(regime_pfs):.2f} – {max(regime_pfs):.2f}  "
              f"(spread = {spread:.2f})")
        if spread > 0.5:
            print(f"  {_YL}Warning: large PF spread suggests regime dependence{_R}")

    print()


if __name__ == "__main__":
    main()
