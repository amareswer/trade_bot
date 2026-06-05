"""
Fee and slippage sensitivity analysis.

Tests whether the validated edge (PF ~1.22) survives realistic
transaction costs. A robust edge should not collapse if fees
move a few basis points.

Run:
    python sensitivity.py
"""
import logging
logging.basicConfig(level=logging.WARNING)

from config import cfg
from bot.data.historical_feed import fetch_candles_paginated
from bot.backtest import engine, metrics as metrics_mod

VALIDATED_CONFIG = dict(
    adx_threshold      = 15.0,
    max_ema_spread_pct = 0.005,
    rsi_filter_enabled = True,
)

# Fee levels to test (taker fee per side)
FEE_LEVELS = [0.0005, 0.001, 0.0015, 0.002, 0.003]
# Slippage levels to test (per side, on top of fee)
SLIPPAGE_LEVELS = [0.0, 0.0002, 0.0005, 0.001]


def run_config(candles, fee_pct, slippage_pct):
    result = engine.run(
        candles              = candles,
        symbol               = cfg.exchange.symbol,
        timeframe            = cfg.backtest.timeframe,
        strategy_mode        = cfg.strategy.mode,
        starting_cash        = cfg.portfolio.starting_cash,
        risk_per_trade_pct   = cfg.risk.risk_per_trade_pct,
        fee_pct              = fee_pct,
        slippage_pct         = slippage_pct,
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
    return m


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

    print(f"  {len(candles)} candles  "
          f"({candles[0].timestamp.strftime('%Y-%m-%d')} → "
          f"{candles[-1].timestamp.strftime('%Y-%m-%d')})\n")

    # ── Fee sensitivity (zero slippage) ──────────────────────────────
    print(f"{_B}  FEE SENSITIVITY  (slippage = 0){_R}")
    print(f"  {'Fee':>8}  {'Trades':>6}  {'Win%':>6}  {'PF':>6}  "
          f"{'Return':>8}  {'Sharpe':>7}  {'Verdict':>10}")
    print(f"  {'─'*8}  {'─'*6}  {'─'*6}  {'─'*6}  "
          f"{'─'*8}  {'─'*7}  {'─'*10}")

    for fee in FEE_LEVELS:
        m = run_config(candles, fee, 0.0)
        trades  = m.total_trades
        wr      = m.win_rate * 100
        pf      = m.profit_factor
        ret     = (m.final_value / m.starting_cash - 1) * 100
        sr      = m.sharpe_ratio
        pfc     = _GR if pf >= 1.1 else (_YL if pf >= 1.0 else _RD)
        verdict = "✓ viable" if pf >= 1.1 else ("~ marginal" if pf >= 1.0 else "✗ dead")
        print(f"  {fee*100:>7.3f}%  {trades:>6}  {wr:>5.1f}%  "
              f"{pfc}{pf:>6.2f}{_R}  {ret:>+7.2f}%  {sr:>7.2f}  {verdict}")

    # ── Slippage sensitivity (baseline fee) ──────────────────────────
    print(f"\n{_B}  SLIPPAGE SENSITIVITY  (fee = 0.1%){_R}")
    print(f"  {'Slip':>8}  {'Trades':>6}  {'Win%':>6}  {'PF':>6}  "
          f"{'Return':>8}  {'Sharpe':>7}  {'Verdict':>10}")
    print(f"  {'─'*8}  {'─'*6}  {'─'*6}  {'─'*6}  "
          f"{'─'*8}  {'─'*7}  {'─'*10}")

    for slip in SLIPPAGE_LEVELS:
        m = run_config(candles, 0.001, slip)
        trades  = m.total_trades
        wr      = m.win_rate * 100
        pf      = m.profit_factor
        ret     = (m.final_value / m.starting_cash - 1) * 100
        sr      = m.sharpe_ratio
        pfc     = _GR if pf >= 1.1 else (_YL if pf >= 1.0 else _RD)
        verdict = "✓ viable" if pf >= 1.1 else ("~ marginal" if pf >= 1.0 else "✗ dead")
        print(f"  {slip*100:>7.3f}%  {trades:>6}  {wr:>5.1f}%  "
              f"{pfc}{pf:>6.2f}{_R}  {ret:>+7.2f}%  {sr:>7.2f}  {verdict}")

    # ── Combined worst case ───────────────────────────────────────────
    print(f"\n{_B}  COMBINED WORST CASE  (fee=0.15% + slippage=0.05%){_R}")
    m = run_config(candles, 0.0015, 0.0005)
    pf  = m.profit_factor
    ret = (m.final_value / m.starting_cash - 1) * 100
    pfc = _GR if pf >= 1.1 else (_YL if pf >= 1.0 else _RD)
    print(f"  PF={pfc}{pf:.2f}{_R}  Return={ret:+.2f}%  "
          f"Sharpe={m.sharpe_ratio:.2f}  Trades={m.total_trades}")

    print()


if __name__ == "__main__":
    main()
