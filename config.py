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
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _str(key: str, default: str) -> str:
    return os.getenv(key, default).strip()


def _required_str(key: str) -> str:
    """Fail fast if a critical env var is absent — no silent wrong defaults."""
    val = os.getenv(key)
    if not val or not val.strip():
        raise ValueError(
            f"Config error: {key} is required but not set in .env. "
            f"Add it to your .env file before running the bot."
        )
    return val.strip()


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
    exchange:       str           # required — set EXCHANGE in .env
    symbol:         str           # required — set SYMBOL in .env
    feed_mode:      str  = "live" # "live" | "simulated"
    loop_interval:  int  = 30     # seconds between ticks
    candle_minutes: int  = 240    # aggregation window for live indicator mode
    live_trading:   bool = False
    dry_run:        bool = False
    api_key:        str  = ""
    api_secret:     str  = ""
    order_type:              str   = "market"  # "market" | "limit"
    limit_order_enabled:     bool  = False    # reads LIMIT_ORDER_ENABLED
    limit_chase_timeout_s:   int   = 120      # reads LIMIT_CHASE_TIMEOUT_S
    limit_chase_max_retries: int   = 3        # reads LIMIT_CHASE_MAX_RETRIES
    limit_chase_tick_pct:    float = 0.0001   # reads LIMIT_CHASE_TICK_PCT — offset as % of price (0.0001 = 0.01%)

    def __post_init__(self):
        if self.feed_mode not in ("live", "simulated"):
            raise ValueError(f"FEED_MODE must be 'live' or 'simulated', got '{self.feed_mode}'")
        if "/" not in self.symbol:
            raise ValueError(f"SYMBOL must be 'BASE/QUOTE' format, got '{self.symbol}'")
        if self.loop_interval < 1:
            raise ValueError("LOOP_INTERVAL must be >= 1")
        if self.candle_minutes < 1:
            raise ValueError("CANDLE_MINUTES must be >= 1")
        if self.live_trading and not self.api_key:
            raise ValueError(
                "LIVE_TRADING=true requires KRAKEN_API_KEY to be set in .env. "
                "The bot refuses to start live without credentials."
            )
        if self.live_trading and not self.api_secret:
            raise ValueError(
                "LIVE_TRADING=true requires KRAKEN_API_SECRET to be set in .env. "
                "The bot refuses to start live without credentials."
            )
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"ORDER_TYPE must be 'market' or 'limit', got '{self.order_type}'")


@dataclass
class StrategyConfig:
    mode:                    str   = "indicator"  # "indicator" | "threshold"
    rsi_period:              int   = 14
    rsi_oversold:            float = 30.0
    rsi_overbought:          float = 70.0
    fast_ema_period:         int   = 9
    slow_ema_period:         int   = 21
    buy_threshold:           float = 0.0
    sell_threshold:          float = 0.0
    adx_period:              int   = 14
    adx_threshold:           float = 25.0  # < this = ranging market → HOLD
    adx_max:                 float = 0.0   # 0 = disabled; 30.0 = reject ADX > 30
    max_ema_spread_pct:      float = 0.0   # 0 = disabled; 0.005 = 0.5% ceiling
    rsi_filter_enabled:      bool  = True  # False = bypass RSI level/direction checks
    regime_ema_period:       int   = 200   # BUY only when price > this EMA (0 = disabled)
    regime_ema_slope_filter: bool  = False # BUY only when EMA200 slope > 0 (rising)
    volume_k:                float = 0.0   # volume filter multiplier (0 = disabled)
    macd_enabled:            bool  = True  # BUY only when MACD histogram is rising
    # ── Dual-regime additions ─────────────────────────────────────────────────
    regime_enabled:          bool  = True  # True = dual-regime; False = trend-only (original)
    bb_period:               int   = 20    # Bollinger Band period for ranging detection
    bb_std_dev:              float = 2.0   # Bollinger Band std-dev multiplier
    mr_rsi_oversold:         float = 35.0  # mean-reversion BUY threshold (ranging mode)
    mr_rsi_overbought:       float = 65.0  # mean-reversion SELL threshold (ranging mode)
    atr_volatile_multiplier: float = 1.5   # ATR > mult × avg ATR → sit flat (VOLATILE)
    atr_sl_mult:             float = 2.0   # reads ATR_SL_MULT — SL = entry - atr × mult
    atr_tp_mult:             float = 4.0   # reads ATR_TP_MULT — TP = entry + atr × mult
    atr_period:              int   = 14    # reads ATR_PERIOD

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
        if self.regime_ema_period < 0:
            raise ValueError("REGIME_EMA_PERIOD must be >= 0 (0 = disabled)")


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
        # Raised from 10%/50% — small account ($100 CAD) needs 50% position
        # sizing to overcome Kraken's fixed 0.80% fee per round trip.
        # At $100 capital, 10% trade size ($10) is consumed by fees before
        # any profit is possible. 50% trade size ($50) makes fee = 1.6% of
        # trade, which TP=10% can overcome.
        if not 0 < self.risk_per_trade_pct <= 0.75:
            errors.append("RISK_PER_TRADE_PCT must be between 0% and 75%")
        if not 0 < self.max_position_pct <= 0.80:
            errors.append("RISK_MAX_POSITION_PCT must be between 0% and 80%")
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
    timeframe:           str   = "4h"
    limit:               int   = 5000    # paginated — up to 5000 candles
    fee_pct:             float = 0.008   # 0.8% Kraken taker fee (validated 2026-06-19)
    stop_loss_pct:       float = 0.015   # exit if price drops 1.5% from entry (0 = disabled)
    take_profit_pct:     float = 0.10    # exit if price rises 10% from entry (0 = disabled)
    trail_stop_pct:      float = 0.0     # trailing stop distance from peak (0 = disabled)
    partial_tp_pct:      float = 0.0     # sell partial_tp_size at this gain (0 = disabled)
    partial_tp_size:     float = 0.5     # fraction of position to sell at partial TP
    atr_sl_enabled:      bool  = False   # True = ATR-based SL; False = fixed % SL
    atr_sl_multiplier:   float = 2.0     # SL = entry_price - ATR × multiplier

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


@dataclass
class ExternalSignalsConfig:
    fng_enabled:           bool  = True
    fng_bear_max:          float = 75.0    # block BUY when FNG > this (extreme greed)
    fng_bull_min:          float = 0.0     # require FNG >= this (0 = disabled)
    fng_cache_seconds:     int   = 3600    # TTL for Fear & Greed cache
    funding_enabled:       bool  = True
    funding_symbol:        str   = "BTCUSDT"
    funding_max:           float = 0.0005  # block BUY when funding > 0.05%
    funding_cache_seconds: int   = 3600    # TTL for funding rate cache


@dataclass
class AlertConfig:
    telegram_enabled:  bool = False
    telegram_bot_token: str = ""
    telegram_chat_id:  str  = ""


@dataclass
class UniverseConfig:
    enabled:         bool  = False                        # UNIVERSE_ENABLED
    size:            int   = 5                            # UNIVERSE_SIZE
    min_vol:         float = 1000.0                       # UNIVERSE_MIN_VOL_CAD
    universe_quote:  str   = "CAD"                        # UNIVERSE_QUOTE
    universe_exclude: str  = "EUR,USD,USDC,USDT,DAI,BUSD"  # UNIVERSE_EXCLUDE

    def __post_init__(self):
        if self.size < 1:
            raise ValueError("UNIVERSE_SIZE must be >= 1")
        if self.min_vol < 0:
            raise ValueError("UNIVERSE_MIN_VOL_CAD must be >= 0")


@dataclass
class PaperConfig:
    paper_mode:          bool  = False
    paper_starting_cash: float = 1000.0


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
    signals:   ExternalSignalsConfig
    alerts:    AlertConfig
    universe:  UniverseConfig
    paper:     PaperConfig = field(default_factory=PaperConfig)

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

    def calc_trade_qty_atr(
        self,
        cash:          float,
        price:         float,
        atr_value:     float,
        atr_multiplier: float | None = None,
    ) -> float:
        """
        ATR-based sizing: SL distance = ATR × multiplier.
        Risks exactly risk_per_trade_pct of cash if the ATR stop is hit.
        Falls back to calc_trade_qty() when ATR is 0 or unavailable.
        """
        mult = atr_multiplier if atr_multiplier is not None else self.backtest.atr_sl_multiplier
        if price <= 0 or cash <= 0 or atr_value <= 0 or mult <= 0:
            return self.calc_trade_qty(cash, price)
        sl_distance = atr_value * mult
        dollar_risk = cash * self.risk.risk_per_trade_pct
        return round(dollar_risk / sl_distance, 6)

    def calc_trade_qty_sl(
        self,
        cash:            float,
        entry_price:     float,
        stop_loss_price: float,
    ) -> float:
        """
        SL-based sizing: if stop is hit, lose exactly risk_per_trade_pct of cash.
        Falls back to calc_trade_qty() when stop_loss_price is 0 or sl_distance ~ 0.
        """
        if stop_loss_price <= 0 or entry_price <= 0 or cash <= 0:
            return self.calc_trade_qty(cash, entry_price)
        sl_distance = abs(entry_price - stop_loss_price)
        if sl_distance < 1e-9:
            return self.calc_trade_qty(cash, entry_price)
        dollar_risk = cash * self.risk.risk_per_trade_pct
        return round(dollar_risk / sl_distance, 6)

    def log_startup(self) -> None:
        """Log all config on startup (no secrets logged)."""
        logger.info("─" * 60)
        logger.info("CONFIG  exchange=%s  symbol=%s  feed=%s  candle=%dmin  order_type=%s",
            self.exchange.exchange, self.exchange.symbol, self.exchange.feed_mode,
            self.exchange.candle_minutes, self.exchange.order_type)
        logger.info("CONFIG  limit_order=%s  timeout=%ds  retries=%d  tick_pct=%.4f%%",
            self.exchange.limit_order_enabled, self.exchange.limit_chase_timeout_s,
            self.exchange.limit_chase_max_retries, self.exchange.limit_chase_tick_pct * 100)
        adx_src = "from .env" if "ADX_THRESHOLD" in os.environ else "CODE DEFAULT — set ADX_THRESHOLD in .env"
        logger.info("CONFIG  strategy=%s  RSI(%d) %g/%g  EMA(%d/%d)  ADX=%.1f (%s)  regime_ema=%d  slope=%s",
            self.strategy.mode, self.strategy.rsi_period,
            self.strategy.rsi_oversold, self.strategy.rsi_overbought,
            self.strategy.fast_ema_period, self.strategy.slow_ema_period,
            self.strategy.adx_threshold, adx_src,
            self.strategy.regime_ema_period,
            self.strategy.regime_ema_slope_filter)
        logger.info("CONFIG  atr  sl_mult=%.1f  tp_mult=%.1f  period=%d",
            self.strategy.atr_sl_mult, self.strategy.atr_tp_mult, self.strategy.atr_period)
        logger.info("CONFIG  risk  per_trade=%.0f%%  max_pos=%.0f%%  daily_loss=%.0f%%  max_dd=%.0f%%  max_trades=%d/day  cooldown=%d",
            self.risk.risk_per_trade_pct * 100,
            self.risk.max_position_pct * 100,
            self.risk.daily_loss_limit_pct * 100,
            self.risk.max_drawdown_pct * 100,
            self.risk.max_trades_per_day,
            self.risk.cooldown_ticks)
        logger.info("CONFIG  portfolio  starting_cash=$%.2f  AI=%s",
            self.portfolio.starting_cash, self.ai.enabled)
        if self.paper.paper_mode:
            logger.info("CONFIG  paper_mode=True  starting_cash=$%.2f",
                self.paper.paper_starting_cash)

        if self.risk.max_position_pct <= self.risk.risk_per_trade_pct * 1.05:
            logger.warning(
                "CONFIG WARNING: RISK_MAX_POSITION_PCT (%.0f%%) is within 5%% of "
                "RISK_PER_TRADE_PCT (%.0f%%) — every BUY will likely be blocked. "
                "qty rounding (6dp) can push new_position_pct up to %.2f%% above the "
                "intended trade value, exceeding an equal limit on every order. "
                "Raise RISK_MAX_POSITION_PCT above %.1f%% to fix.",
                self.risk.max_position_pct * 100,
                self.risk.risk_per_trade_pct * 100,
                0.05,
                self.risk.risk_per_trade_pct * 1.05 * 100,
            )

        logger.info("─" * 60)


# ---------------------------------------------------------------------------
# Helpers — called at runtime with live values, not cached
# ---------------------------------------------------------------------------

def per_symbol_max_pct(universe_size: int, max_position_pct: float) -> float:
    """Max position fraction per symbol slot.
    e.g. universe_size=5, max_position_pct=0.55 → 0.11 per symbol"""
    if universe_size <= 0:
        return 0.0
    return max_position_pct / universe_size


# ---------------------------------------------------------------------------
# Load — reads env vars, validates, returns singleton
# ---------------------------------------------------------------------------

def _load() -> AppConfig:
    cfg = AppConfig(
        exchange=ExchangeConfig(
            exchange       = _required_str("EXCHANGE"),
            symbol         = _required_str("SYMBOL"),
            feed_mode      = _str ("FEED_MODE",       "live"),
            loop_interval  = _int ("LOOP_INTERVAL",   30),
            candle_minutes = _int ("CANDLE_MINUTES",  240),
            live_trading   = _bool("LIVE_TRADING",    False),
            dry_run        = _bool("DRY_RUN",         False),
            api_key        = _str ("KRAKEN_API_KEY",  ""),
            api_secret     = _str ("KRAKEN_API_SECRET", ""),
            order_type              = _str  ("ORDER_TYPE",             "market"),
            limit_order_enabled     = _bool ("LIMIT_ORDER_ENABLED",      False),
            limit_chase_timeout_s   = _int  ("LIMIT_CHASE_TIMEOUT_S",    120),
            limit_chase_max_retries = _int  ("LIMIT_CHASE_MAX_RETRIES",  3),
            limit_chase_tick_pct    = _float("LIMIT_CHASE_TICK_PCT",     0.0001),
        ),
        strategy=StrategyConfig(
            mode                    = _str  ("STRATEGY_MODE",           "indicator"),
            rsi_period              = _int  ("RSI_PERIOD",              14),
            rsi_oversold            = _float("RSI_OVERSOLD",            30.0),
            rsi_overbought          = _float("RSI_OVERBOUGHT",          70.0),
            fast_ema_period         = _int  ("FAST_EMA_PERIOD",         9),
            slow_ema_period         = _int  ("SLOW_EMA_PERIOD",         21),
            buy_threshold           = _float("BUY_THRESHOLD",           0.0),
            sell_threshold          = _float("SELL_THRESHOLD",          0.0),
            adx_period              = _int  ("ADX_PERIOD",              14),
            adx_threshold           = _float("ADX_THRESHOLD",           25.0),  # live .env must set 18
            adx_max                 = _float("ADX_MAX",                 0.0),
            max_ema_spread_pct      = _float("MAX_EMA_SPREAD_PCT",      0.0),
            rsi_filter_enabled      = _bool ("RSI_FILTER_ENABLED",      True),
            regime_ema_period       = _int  ("REGIME_EMA_PERIOD",       200),
            regime_ema_slope_filter = _bool ("REGIME_EMA_SLOPE_FILTER", False),
            volume_k                = _float("VOLUME_K",                0.0),
            macd_enabled            = _bool ("MACD_ENABLED",            True),
            regime_enabled          = _bool ("REGIME_ENABLED",          True),
            bb_period               = _int  ("BB_PERIOD",               20),
            bb_std_dev              = _float("BB_STD_DEV",              2.0),
            mr_rsi_oversold         = _float("MR_RSI_OVERSOLD",         35.0),
            mr_rsi_overbought       = _float("MR_RSI_OVERBOUGHT",       65.0),
            atr_volatile_multiplier = _float("ATR_VOLATILE_MULTIPLIER", 1.5),
            atr_sl_mult             = _float("ATR_SL_MULT",              2.0),
            atr_tp_mult             = _float("ATR_TP_MULT",              4.0),
            atr_period              = _int  ("ATR_PERIOD",               14),
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
            timeframe        = _str  ("BACKTEST_TIMEFRAME",   "4h"),
            limit            = _int  ("BACKTEST_LIMIT",       5000),
            fee_pct          = _float("BACKTEST_FEE_PCT",     0.008),
            stop_loss_pct    = _float("STOP_LOSS_PCT",        0.015),
            take_profit_pct  = _float("TAKE_PROFIT_PCT",      0.10),
            trail_stop_pct   = _float("TRAIL_STOP_PCT",       0.0),
            partial_tp_pct   = _float("PARTIAL_TP_PCT",       0.0),
            partial_tp_size  = _float("PARTIAL_TP_SIZE",      0.5),
            atr_sl_enabled   = _bool ("ATR_SL_ENABLED",       False),
            atr_sl_multiplier= _float("ATR_SL_MULTIPLIER",    2.0),
        ),
        signals=ExternalSignalsConfig(
            fng_enabled            = _bool ("EXT_FNG_ENABLED",       True),
            fng_bear_max           = _float("EXT_FNG_BEAR_MAX",      75.0),
            fng_bull_min           = _float("EXT_FNG_BULL_MIN",      0.0),
            fng_cache_seconds      = _int  ("EXT_FNG_CACHE_S",       3600),
            funding_enabled        = _bool ("EXT_FUNDING_ENABLED",   True),
            funding_symbol         = _str  ("EXT_FUNDING_SYMBOL",    "BTCUSDT"),
            funding_max            = _float("EXT_FUNDING_MAX",       0.0005),
            funding_cache_seconds  = _int  ("EXT_FUNDING_CACHE_S",   3600),
        ),
        alerts=AlertConfig(
            telegram_enabled    = _bool("TELEGRAM_ENABLED",    False),
            telegram_bot_token  = _str ("TELEGRAM_BOT_TOKEN",  ""),
            telegram_chat_id    = _str ("TELEGRAM_CHAT_ID",    ""),
        ),
        universe=UniverseConfig(
            enabled           = _bool ("UNIVERSE_ENABLED",       False),
            size              = _int  ("UNIVERSE_SIZE",          5),
            min_vol           = _float("UNIVERSE_MIN_VOL_CAD",   1000.0),
            universe_quote    = _str  ("UNIVERSE_QUOTE",         "CAD"),
            universe_exclude  = _str  ("UNIVERSE_EXCLUDE",       "EUR,USD,USDC,USDT,DAI,BUSD"),
        ),
        paper=PaperConfig(
            paper_mode          = _bool ("PAPER_MODE",          False),
            paper_starting_cash = _float("PAPER_STARTING_CASH", 1000.0),
        ),
    )

    # Warn loudly when critical strategy values are absent from the environment.
    # These fire at import time so they appear in every run, not buried mid-log.
    if "ADX_THRESHOLD" not in os.environ:
        logger.warning(
            "ADX_THRESHOLD not set in .env — falling back to code default %.1f. "
            "Live-validated strategy uses 18.0. Backtest and live bot WILL diverge.",
            cfg.strategy.adx_threshold,
        )
    if cfg.strategy.volume_k > 0:
        logger.warning(
            "VOLUME_K=%.1f — volume filter is ACTIVE. "
            "Live-validated strategy uses VOLUME_K=0 (disabled). "
            "Set VOLUME_K=0 in .env unless you have re-validated with volume filter on.",
            cfg.strategy.volume_k,
        )

    return cfg


# Single instance — import this everywhere
cfg: AppConfig = _load()