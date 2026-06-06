"""
Backtest entry point.

All settings come from config.py / .env — no hardcoded values here.
To change settings, edit .env or config.py.

Run:
    python backtest.py
"""
import logging
logging.basicConfig(level=logging.WARNING)

from config import cfg
from bot.data.historical_feed import fetch_candles_paginated
from bot.backtest import engine, metrics as metrics_mod, report
from bot.backtest.attribution import compute_attribution, print_attribution, save_attribution_csv


def main():
    print(f"\n  Exchange: {cfg.exchange.exchange.capitalize()}  |  Symbol: {cfg.exchange.symbol}  |  Timeframe: {cfg.backtest.timeframe}")

    try:
        candles = fetch_candles_paginated(
            exchange_id = cfg.exchange.exchange,
            symbol      = cfg.exchange.symbol,
            timeframe   = cfg.backtest.timeframe,
            total_limit = cfg.backtest.limit,
        )
    except Exception as exc:
        print(f"\n  ERROR fetching data: {exc}\n")
        return

    print(
        f"  {len(candles)} candles loaded"
        f"  ({candles[0].timestamp.strftime('%Y-%m-%d')}"
        f" → {candles[-1].timestamp.strftime('%Y-%m-%d')})"
    )
    print(f"  Running backtest …\n")

    result = engine.run(
        candles               = candles,
        symbol                = cfg.exchange.symbol,
        timeframe             = cfg.backtest.timeframe,
        strategy_mode         = cfg.strategy.mode,
        starting_cash         = cfg.portfolio.starting_cash,
        risk_per_trade_pct    = cfg.risk.risk_per_trade_pct,
        fee_pct               = cfg.backtest.fee_pct,
        cooldown_ticks        = cfg.risk.cooldown_ticks,
        rsi_period            = cfg.strategy.rsi_period,
        rsi_oversold          = cfg.strategy.rsi_oversold,
        rsi_overbought        = cfg.strategy.rsi_overbought,
        fast_ema_period       = cfg.strategy.fast_ema_period,
        slow_ema_period       = cfg.strategy.slow_ema_period,
        adx_period            = cfg.strategy.adx_period,
        adx_threshold         = cfg.strategy.adx_threshold,
        adx_max               = cfg.strategy.adx_max,
        max_ema_spread_pct    = cfg.strategy.max_ema_spread_pct,
        rsi_filter_enabled    = cfg.strategy.rsi_filter_enabled,
        buy_threshold         = cfg.strategy.buy_threshold,
        sell_threshold        = cfg.strategy.sell_threshold,
        max_position_pct      = cfg.risk.max_position_pct,
        daily_loss_limit_pct  = cfg.risk.daily_loss_limit_pct,
        max_drawdown_pct      = cfg.risk.max_drawdown_pct,
        max_trades_per_day    = cfg.risk.max_trades_per_day,
        stop_loss_pct         = cfg.backtest.stop_loss_pct,
        take_profit_pct       = cfg.backtest.take_profit_pct,
    )

    m = metrics_mod.compute(result)
    report.print_report(m, result)

    rs = result.rejection_stats
    if rs:
        tradeable = rs.get("candles_seen", 0) - rs.get("warmup_rejected", 0)

        def pct(n: int) -> str:
            return f"  ({n / tradeable * 100:.1f}%)" if tradeable > 0 else ""

        print()
        print("  SIGNAL FILTER BREAKDOWN")
        print("  " + "─" * 46)
        print(f"  Candles examined          {rs.get('candles_seen', 0):>6}")
        print(f"  Warmup (skipped)          {rs.get('warmup_rejected', 0):>6}")
        print(f"  Tradeable candles         {tradeable:>6}")
        print(f"  ─────────────────────────────────────────")
        adx_n   = rs.get("adx_rejected", 0)
        trend_n = rs.get("trend_rejected", 0)
        ema_n   = rs.get("ema_rejected", 0)
        rsi_n   = rs.get("rsi_rejected", 0)
        print(f"  ADX rejected              {adx_n:>6}{pct(adx_n)}")
        print(f"  Trend rejected (NEUTRAL)  {trend_n:>6}{pct(trend_n)}")
        print(f"  EMA spread rejected       {ema_n:>6}{pct(ema_n)}")
        print(f"  RSI rejected              {rsi_n:>6}{pct(rsi_n)}")
        print(f"  ─────────────────────────────────────────")
        buy_n  = rs.get("buy_signals", 0)
        sell_n = rs.get("sell_signals", 0)
        print(f"  BUY  signals emitted      {buy_n:>6}{pct(buy_n)}")
        print(f"  SELL signals emitted      {sell_n:>6}{pct(sell_n)}")
        print()

    csv_path = report.save_csv(result)
    print(f"  Saved → {csv_path}\n")

    # ── Trade attribution analysis ────────────────────────────────
    if result.fills:
        attr_report = compute_attribution(result)
        print_attribution(attr_report)
        attr_path = save_attribution_csv(
            attr_report.records,
            symbol    = cfg.exchange.symbol,
            timeframe = cfg.backtest.timeframe,
        )
        print(f"  Attribution CSV → {attr_path}\n")


if __name__ == "__main__":
    main()
