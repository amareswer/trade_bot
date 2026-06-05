"""
Central configuration for the trading bot.

All settings have safe defaults. Override any value via environment variable
(see .env.example). The bot validates every value on startup and refuses to
run with invalid config — fail fast, fail clearly.

Usage:
    from config import cfg

    cfg.exchange.symbol
    cfg.risk.risk_per_trade_pct
    qty = cfg.calc_trade_qty(cash, price)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _str(key: str, default: str) -> str:
    return os.getenv(key, default).strip()


def _float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        raise ValueError(f"Config error: {key} must be a number, got '{raw}'")


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"Config error: {key} must be an integer, got '{raw}'")


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Config groups
# ---------------------------------------------------------------------------

@dataclass
class ExchangeConfig:
    exchange:       str = "kraken"
    symbol:         str = "BTC/USDT"
    feed_mode:      str = "live"    # "live" | "simulated"
    loop_interval:  int = 30        # seconds between ticks
    candle_minutes: int = 240       # aggregation window for live indicator mode

    def __post_init__(self):
        if self.feed_mode not in ("live", "simulated"):
            raise ValueError(f"FEED_MODE must be 'live' or 'simulated', got '{self.feed_mode}'")
        if "/" not in self.symbol:
            raise ValueError(f"SYMBOL must be 'BASE/QUOTE' format, got '{self.symbol}'")
        if self.loop_interval < 1:
            raise ValueError("LOOP_INTERVAL must be >= 1")
        if self.candle_minutes < 1:
            raise ValueError("CANDLE_MINUTES must be >= 1")


@dataclass
class StrategyConfig:
    mode:            str   = "indicator"  # "indicator" | "threshold"
    rsi_period:      int   = 14
    rsi_oversold:    float = 30.0
    rsi_overbought:  float = 70.0
    fast_ema_period: int   = 9
    slow_ema_period: int   = 21
    buy_threshold:   float = 0.0
    sell_threshold:  float = 0.0
    adx_period:         int   = 14
    adx_threshold:      float = 25.0  # < this = ranging market → HOLD
    max_ema_spread_pct: float = 0.0   # 0 = disabled; 0.005 = 0.5% ceiling
    rsi_filter_enabled: bool  = True  # False = bypass RSI level/direction checks

    def __post_init__(self):
        if self.mode not in ("indicator", "threshold"):
            raise ValueError(f"STRATEGY_MODE must be 'indicator' or 'threshold', got '{self.mode}'")
        if not (0 < self.rsi_oversold < self.rsi_overbought < 100):
            raise ValueError("Must satisfy: 0 < RSI_OVERSOLD < RSI_OVERBOUGHT < 100")
        if self.fast_ema_period >= self.slow_ema_period:
            raise ValueError("FAST_EMA_PERIOD must be less than SLOW_EMA_PERIOD")
        if self.mode == "threshold" and self.buy_threshold >= self.sell_threshold:
            raise ValueError("BUY_THRESHOLD must be less than SELL_THRESHOLD")
        if self.adx_period < 2:
            raise ValueError("ADX_PERIOD must be >= 2")
        if not 0 <= self.adx_threshold <= 100:
            raise ValueError("ADX_THRESHOLD must be between 0 and 100")
        if self.max_ema_spread_pct < 0:
            raise ValueError("MAX_EMA_SPREAD_PCT must be >= 0")


@dataclass
class RiskConfig:
    risk_per_trade_pct:   float = 0.005  # 0.5% of cash per trade — conservative dynamic sizing
    max_position_pct:     float = 0.03   # never more than 3% of portfolio in one position
    daily_loss_limit_pct: float = 0.01   # halt new BUYs if down 1% today
    max_drawdown_pct:     float = 0.05   # halt new BUYs if down 5% from all-time peak
    max_trades_per_day:   int   = 3      # hard cap per calendar day
    cooldown_ticks:       int   = 10     # candles to wait after each trade

    def __post_init__(self):
        errors = []
        if not 0 < self.risk_per_trade_pct <= 0.10:
            errors.append("RISK_PER_TRADE_PCT must be between 0% and 10%")
        if not 0 < self.max_position_pct <= 0.50:
            errors.append("RISK_MAX_POSITION_PCT must be between 0% and 50%")
        if not 0 < self.daily_loss_limit_pct <= 0.20:
            errors.append("RISK_DAILY_LOSS_LIMIT must be between 0% and 20%")
        if not 0 < self.max_drawdown_pct <= 0.50:
            errors.append("RISK_MAX_DRAWDOWN must be between 0% and 50%")
        if self.daily_loss_limit_pct > self.max_drawdown_pct:
            errors.append("RISK_DAILY_LOSS_LIMIT should not exceed RISK_MAX_DRAWDOWN")
        if self.max_trades_per_day < 1:
            errors.append("RISK_MAX_TRADES_PER_DAY must be >= 1")
        if self.cooldown_ticks < 0:
            errors.append("COOLDOWN_TICKS must be >= 0")
        if errors:
            raise ValueError("Config errors:\n" + "\n".join(f"  - {e}" for e in errors))


@dataclass
class PortfolioConfig:
    starting_cash:   float = 10_000.0
    sim_start_price: float = 68_500.0  # simulated feed start price
    sim_volatility:  float = 200.0     # simulated feed volatility per tick

    def __post_init__(self):
        if self.starting_cash <= 0:
            raise ValueError("STARTING_CASH must be > 0")


@dataclass
class AIConfig:
    enabled:        bool  = True
    model:          str   = "deepseek/deepseek-v4-flash:free"
    min_confidence: float = 0.65
    timeout_s:      float = 8.0

    def __post_init__(self):
        if not 0 <= self.min_confidence <= 1:
            raise ValueError("AI_MIN_CONFIDENCE must be between 0 and 1")
        if self.timeout_s < 1:
            raise ValueError("AI_TIMEOUT_S must be >= 1 second")


@dataclass
class DashboardConfig:
    enabled:   bool = True
    refresh_s: int  = 30

    def __post_init__(self):
        if self.refresh_s < 5:
            raise ValueError("DASHBOARD_REFRESH must be >= 5 seconds")


@dataclass
class BacktestConfig:
    timeframe:       str   = "4h"
    limit:           int   = 5000    # paginated — up to 5000 candles
    fee_pct:         float = 0.001   # 0.1% per trade
    stop_loss_pct:   float = 0.02    # exit if price drops 2% from entry (0 = disabled)
    take_profit_pct: float = 0.04    # exit if price rises 4% from entry (0 = disabled)

    _VALID_TIMEFRAMES = {"1m","5m","15m","30m","1h","2h","4h","6h","12h","1d","1w"}

    def __post_init__(self):
        if self.timeframe not in self._VALID_TIMEFRAMES:
            raise ValueError(
                f"BACKTEST_TIMEFRAME must be one of {sorted(self._VALID_TIMEFRAMES)}, "
                f"got '{self.timeframe}'"
            )
        if self.limit < 50:
            raise ValueError("BACKTEST_LIMIT must be >= 50 candles")
        if not 0 <= self.fee_pct <= 0.01:
            raise ValueError("BACKTEST_FEE_PCT must be between 0% and 1%")
        if not 0 <= self.stop_loss_pct <= 0.50:
            raise ValueError("STOP_LOSS_PCT must be between 0% and 50%")
        if not 0 <= self.take_profit_pct <= 1.0:
            raise ValueError("TAKE_PROFIT_PCT must be between 0% and 100%")


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

@dataclass
class AppConfig:
    exchange:  ExchangeConfig
    strategy:  StrategyConfig
    risk:      RiskConfig
    portfolio: PortfolioConfig
    ai:        AIConfig
    dashboard: DashboardConfig
    backtest:  BacktestConfig

    def calc_trade_qty(self, cash: float, price: float) -> float:
        """
        Dynamic position sizing — industry standard fixed-fractional method.
        Risk exactly risk_per_trade_pct of current cash per trade.
        Quantity shrinks when you lose, grows when you profit.
        """
        if price <= 0 or cash <= 0:
            return 0.0
        trade_value = cash * self.risk.risk_per_trade_pct
        return round(trade_value / price, 6)

    def log_startup(self) -> None:
        """Log all config on startup (no secrets logged)."""
        logger.info("─" * 60)
        logger.info("CONFIG  exchange=%s  symbol=%s  feed=%s  candle=%dmin",
            self.exchange.exchange, self.exchange.symbol, self.exchange.feed_mode,
            self.exchange.candle_minutes)
        logger.info("CONFIG  strategy=%s  RSI(%d) %g/%g  EMA(%d/%d)",
            self.strategy.mode, self.strategy.rsi_period,
            self.strategy.rsi_oversold, self.strategy.rsi_overbought,
            self.strategy.fast_ema_period, self.strategy.slow_ema_period)
        logger.info("CONFIG  risk  per_trade=%.0f%%  max_pos=%.0f%%  daily_loss=%.0f%%  max_dd=%.0f%%  max_trades=%d/day  cooldown=%d",
            self.risk.risk_per_trade_pct * 100,
            self.risk.max_position_pct * 100,
            self.risk.daily_loss_limit_pct * 100,
            self.risk.max_drawdown_pct * 100,
            self.risk.max_trades_per_day,
            self.risk.cooldown_ticks)
        logger.info("CONFIG  portfolio  starting_cash=$%.2f  AI=%s",
            self.portfolio.starting_cash, self.ai.enabled)
        logger.info("─" * 60)


# ---------------------------------------------------------------------------
# Load — reads env vars, validates, returns singleton
# ---------------------------------------------------------------------------

def _load() -> AppConfig:
    return AppConfig(
        exchange=ExchangeConfig(
            exchange       = _str("EXCHANGE",       "kraken"),
            symbol         = _str("SYMBOL",         "BTC/USDT"),
            feed_mode      = _str("FEED_MODE",       "live"),
            loop_interval  = _int("LOOP_INTERVAL",   30),
            candle_minutes = _int("CANDLE_MINUTES",  240),
        ),
        strategy=StrategyConfig(
            mode            = _str  ("STRATEGY_MODE",    "indicator"),
            rsi_period      = _int  ("RSI_PERIOD",       14),
            rsi_oversold    = _float("RSI_OVERSOLD",     30.0),
            rsi_overbought  = _float("RSI_OVERBOUGHT",   70.0),
            fast_ema_period = _int  ("FAST_EMA_PERIOD",  9),
            slow_ema_period = _int  ("SLOW_EMA_PERIOD",  21),
            buy_threshold   = _float("BUY_THRESHOLD",    0.0),
            sell_threshold  = _float("SELL_THRESHOLD",   0.0),
            adx_period          = _int  ("ADX_PERIOD",          14),
            adx_threshold       = _float("ADX_THRESHOLD",       25.0),
            max_ema_spread_pct  = _float("MAX_EMA_SPREAD_PCT",  0.0),
            rsi_filter_enabled  = _bool ("RSI_FILTER_ENABLED",  True),
        ),
        risk=RiskConfig(
            risk_per_trade_pct   = _float("RISK_PER_TRADE_PCT",    0.01),
            max_position_pct     = _float("RISK_MAX_POSITION_PCT",  0.05),
            daily_loss_limit_pct = _float("RISK_DAILY_LOSS_LIMIT",  0.02),
            max_drawdown_pct     = _float("RISK_MAX_DRAWDOWN",      0.10),
            max_trades_per_day   = _int  ("RISK_MAX_TRADES_PER_DAY", 5),
            cooldown_ticks       = _int  ("COOLDOWN_TICKS",          10),
        ),
        portfolio=PortfolioConfig(
            starting_cash   = _float("STARTING_CASH",    10_000.0),
            sim_start_price = _float("SIM_START_PRICE",  68_500.0),
            sim_volatility  = _float("SIM_VOLATILITY",   200.0),
        ),
        ai=AIConfig(
            enabled        = _bool ("AI_ENABLED",        True),
            model          = _str  ("AI_MODEL",          "deepseek/deepseek-v4-flash:free"),
            min_confidence = _float("AI_MIN_CONFIDENCE", 0.65),
            timeout_s      = _float("AI_TIMEOUT_S",      8.0),
        ),
        dashboard=DashboardConfig(
            enabled   = _bool("DASHBOARD_ENABLED", True),
            refresh_s = _int ("DASHBOARD_REFRESH", 30),
        ),
        backtest=BacktestConfig(
            timeframe       = _str  ("BACKTEST_TIMEFRAME", "4h"),
            limit           = _int  ("BACKTEST_LIMIT",     5000),
            fee_pct         = _float("BACKTEST_FEE_PCT",   0.001),
            stop_loss_pct   = _float("STOP_LOSS_PCT",      0.02),
            take_profit_pct = _float("TAKE_PROFIT_PCT",    0.04),
        ),
    )


# Single instance — import this everywhere
cfg: AppConfig = _load()
