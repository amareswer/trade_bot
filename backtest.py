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
from bot.data.historical_feed import fetch_candles
from bot.backtest import engine, metrics as metrics_mod, report


def main():
    print(
        f"\n  Fetching {cfg.backtest.limit} × {cfg.backtest.timeframe} candles"
        f" for {cfg.exchange.symbol} from {cfg.exchange.exchange} …"
    )

    try:
        candles = fetch_candles(
            exchange_id = cfg.exchange.exchange,
            symbol      = cfg.exchange.symbol,
            timeframe   = cfg.backtest.timeframe,
            limit       = cfg.backtest.limit,
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
        buy_threshold         = cfg.strategy.buy_threshold,
        sell_threshold        = cfg.strategy.sell_threshold,
        max_position_pct      = cfg.risk.max_position_pct,
        daily_loss_limit_pct  = cfg.risk.daily_loss_limit_pct,
        max_drawdown_pct      = cfg.risk.max_drawdown_pct,
        max_trades_per_day    = cfg.risk.max_trades_per_day,
    )

    m = metrics_mod.compute(result)
    report.print_report(m, result)

    csv_path = report.save_csv(result)
    print(f"  Saved → {csv_path}\n")


if __name__ == "__main__":
    main()
