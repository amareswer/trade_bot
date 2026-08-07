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
from pathlib import Path

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
    adopt_external_holdings: bool  = False    # ADOPT_EXTERNAL_HOLDINGS — if True, bot manages all exchange balance incl. deposits
    drift_alert_threshold:   int   = 3       # DRIFT_ALERT_THRESHOLD — escalate to alerter.error() after this many consecutive drift detections
    native_stop_loss_enabled: bool = False    # NATIVE_STOP_LOSS_ENABLED — rest a real stop-loss order on the
                                               # exchange after every BUY (backstop if the bot process/VPS goes
                                               # down while a position is open). Static: set once per fill at
                                               # whatever SL price the bot already computed, never repriced to
                                               # follow a trailing stop. Default OFF — validate on live before enabling.

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
                "LIVE_TRADING=true requires EXCHANGE_API_KEY (or legacy KRAKEN_API_KEY) "
                "to be set in .env. The bot refuses to start live without credentials."
            )
        if self.live_trading and not self.api_secret:
            raise ValueError(
                "LIVE_TRADING=true requires EXCHANGE_API_SECRET (or legacy KRAKEN_API_SECRET) "
                "to be set in .env. The bot refuses to start live without credentials."
            )
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"ORDER_TYPE must be 'market' or 'limit', got '{self.order_type}'")
        if self.limit_chase_timeout_s < 1:
            raise ValueError("LIMIT_CHASE_TIMEOUT_S must be >= 1")
        if self.limit_chase_max_retries < 0:
            raise ValueError("LIMIT_CHASE_MAX_RETRIES must be >= 0")
        if not 0 <= self.limit_chase_tick_pct <= 0.01:
            raise ValueError("LIMIT_CHASE_TICK_PCT must be between 0 and 0.01 (1% of price)")
        if self.drift_alert_threshold < 1:
            raise ValueError("DRIFT_ALERT_THRESHOLD must be >= 1")


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
    adx_threshold:           float = 18.0  # < this = ranging market → HOLD (validated live value)
    adx_max:                 float = 0.0   # 0 = disabled; 30.0 = reject ADX > 30
    min_ema_spread_pct:      float = 0.002 # require fast EMA ≥ this % above slow EMA to enter
    max_ema_spread_pct:      float = 0.0   # 0 = disabled; 0.005 = 0.5% ceiling
    rsi_filter_enabled:      bool  = True  # False = bypass RSI level/direction checks
    regime_ema_period:       int   = 200   # BUY only when price > this EMA (0 = disabled)
    regime_ema_slope_filter: bool  = False # BUY only when EMA200 slope > 0 (rising)
    volume_k:                float = 0.0   # volume filter multiplier (0 = disabled)
    macd_enabled:            bool  = True  # BUY only when MACD histogram is rising
    atr_volatile_multiplier: float = 1.5   # ATR > mult × avg ATR → sit flat (VOLATILE)
    atr_sl_mult:             float = 0.0   # reads ATR_SL_MULT — SL = entry - atr × mult; 0 = disabled (use fixed SL)
    atr_sizing_enabled:      bool  = False # reads ATR_SIZING_ENABLED — cap qty so an ATR stop-out never risks more $ than the fixed-SL baseline
    atr_period:              int   = 14    # reads ATR_PERIOD
    # ── Entry mode parameters (Mode A = pullback, Mode B = breakout) ──
    pullback_rsi_min:        float = 38.0  # Mode A: RSI lower bound
    pullback_rsi_max:        float = 58.0  # Mode A: RSI upper bound
    breakout_rsi_min:        float = 50.0  # Mode B: RSI lower bound
    breakout_rsi_max:        float = 72.0  # Mode B: RSI upper bound
    breakout_lookback:       int   = 20    # Mode B: N-candle high for breakout check
    max_price_extension_pct: float = 0.03  # Mode B: max % above N-candle high (anti-chase)
    breakout_adx_threshold:  float = 22.0  # Mode B: stricter ADX requirement

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
        if self.pullback_rsi_min >= self.pullback_rsi_max:
            raise ValueError("PULLBACK_RSI_MIN must be < PULLBACK_RSI_MAX")
        if self.breakout_rsi_min >= self.breakout_rsi_max:
            raise ValueError("BREAKOUT_RSI_MIN must be < BREAKOUT_RSI_MAX")
        if self.max_price_extension_pct <= 0:
            raise ValueError("MAX_PRICE_EXTENSION_PCT must be > 0")
        if self.breakout_lookback < 5:
            raise ValueError("BREAKOUT_LOOKBACK must be >= 5")
        if self.atr_sl_mult < 0:
            raise ValueError("ATR_SL_MULT must be >= 0 (0 = disabled)")
        if self.atr_period < 2:
            raise ValueError("ATR_PERIOD must be >= 2")
        if self.atr_volatile_multiplier < 0:
            raise ValueError("ATR_VOLATILE_MULTIPLIER must be >= 0")


@dataclass
class RiskConfig:
    risk_per_trade_pct:     float = 0.005  # 0.5% of cash per trade — conservative dynamic sizing
    max_position_pct:       float = 0.03   # never more than 3% of portfolio in one position
    daily_loss_limit_pct:   float = 0.01   # halt new BUYs if down 1% today
    max_drawdown_pct:       float = 0.05   # drawdown-HALT tier — halt new BUYs if down 5% from
                                            # all-time peak. Not sticky — auto-lifts on recovery.
    max_trades_per_day:     int   = 3      # hard cap per calendar day
    cooldown_ticks:         int   = 10     # candles to wait after each trade
    risk_halt_blocks_stops: bool  = False  # when True, manual HALT also blocks SL/TP exits
    # ── Weekly loss / drawdown-from-peak tiers (added 2026-08-07, mirrors the
    # stock bot's breaker upgrade 2026-08-05 — crypto had fallen behind with
    # only a single non-sticky drawdown check even though it trades real money) ──
    weekly_loss_limit_pct:  float = 0.05   # halt new BUYs if down X% from this ISO-week's
                                            # UTC-Monday-open value. Not sticky — resets fresh
                                            # every week regardless of prior trip.
    drawdown_warning_pct:   float = 0.03   # non-blocking — Telegram alert only, trading
                                            # continues. Must be < max_drawdown_pct. Tighter
                                            # than the stock bot's 10% — crypto's halt tier
                                            # itself is already a tighter 5%, not 15%.
    kill_switch_pct:        float = 0.10   # halt new BUYs if down X% from all-time peak.
                                            # STICKY — persists across restart, does not
                                            # auto-clear on recovery. Requires manually editing
                                            # kill_switch_tripped to false in logs/risk_state.json.
                                            # Must be > max_drawdown_pct.

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
        if not 0 <= self.weekly_loss_limit_pct <= 0.60:
            errors.append("RISK_WEEKLY_LOSS_LIMIT must be between 0% and 60%")
        if self.daily_loss_limit_pct > self.weekly_loss_limit_pct:
            errors.append("RISK_DAILY_LOSS_LIMIT should not exceed RISK_WEEKLY_LOSS_LIMIT")
        if not 0 <= self.drawdown_warning_pct <= 0.60:
            errors.append("RISK_DRAWDOWN_WARNING must be between 0% and 60%")
        if not 0 < self.kill_switch_pct <= 0.90:
            errors.append("RISK_KILL_SWITCH must be between 0% and 90%")
        if not (self.drawdown_warning_pct < self.max_drawdown_pct < self.kill_switch_pct):
            errors.append(
                "Must satisfy: RISK_DRAWDOWN_WARNING < RISK_MAX_DRAWDOWN < RISK_KILL_SWITCH "
                "(strictly increasing severity tiers)"
            )
        if errors:
            raise ValueError("Config errors:\n" + "\n".join(f"  - {e}" for e in errors))


@dataclass
class PortfolioConfig:
    starting_cash:            float = 10_000.0
    sim_start_price:          float = 68_500.0  # simulated feed start price
    sim_volatility:           float = 200.0     # simulated feed volatility per tick
    max_concurrent_positions: int   = 2         # MAX_CONCURRENT_POSITIONS — capital pool slots
    max_slot_cash_cad:        float = 0.0       # MAX_SLOT_CASH_CAD — per-slot hard cap (0 = uncapped)
    live_dust_value_cad:      float = 10.0      # positions worth < this are dust — skip recovery

    def __post_init__(self):
        if self.starting_cash <= 0:
            raise ValueError("STARTING_CASH must be > 0")
        if self.max_concurrent_positions < 1:
            raise ValueError("MAX_CONCURRENT_POSITIONS must be >= 1")
        if self.max_slot_cash_cad < 0:
            raise ValueError("MAX_SLOT_CASH_CAD must be >= 0")
        if self.live_dust_value_cad < 0:
            raise ValueError("LIVE_DUST_VALUE_CAD must be >= 0")


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
    trail_stop_pct:             float = 0.0  # trailing stop distance from peak (0 = disabled)
    trail_stop_activation_pct:  float = 0.03 # min profit before trail activates (0 = immediate)
    partial_tp_pct:      float = 0.0     # sell partial_tp_size at this gain (0 = disabled)
    partial_tp_size:     float = 0.5     # fraction of position to sell at partial TP
    atr_sl_mult:         float = 0.0     # ATR SL multiplier; 0 = disabled (uses fixed stop_loss_pct)
    atr_sizing_enabled:  bool  = False   # same ATR_SIZING_ENABLED key as StrategyConfig (drift-incident rule: one key, two readers)

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
        if not 0 <= self.trail_stop_pct <= 0.50:
            raise ValueError("TRAILING_STOP_PCT must be between 0% and 50% (0 = disabled)")
        if not 0 <= self.trail_stop_activation_pct <= 1.0:
            raise ValueError("TRAILING_STOP_ACTIVATION_PCT must be between 0% and 100%")
        if not 0 <= self.partial_tp_pct <= 1.0:
            raise ValueError("PARTIAL_TP_PCT must be between 0% and 100% (0 = disabled)")
        if not 0 < self.partial_tp_size <= 1.0:
            raise ValueError("PARTIAL_TP_SIZE must be between 0 (exclusive) and 1")
        if self.atr_sl_mult < 0:
            raise ValueError("ATR_SL_MULT must be >= 0 (0 = disabled)")


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
    enabled:          bool  = False                        # UNIVERSE_ENABLED
    size:             int   = 5                            # UNIVERSE_SIZE
    min_vol:          float = 1000.0                       # UNIVERSE_MIN_VOL_CAD
    universe_quote:   str   = "CAD"                        # UNIVERSE_QUOTE
    universe_exclude: str   = "EUR,USD,USDC,USDT,DAI,BUSD"  # UNIVERSE_EXCLUDE
    universe_whitelist: str = ""                           # UNIVERSE_WHITELIST — comma-separated fixed list; skips dynamic scan when set

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
        Notional position sizing: allocate risk_per_trade_pct of current cash
        to this trade's position value (NOT dollar-risk-at-stop — this does
        not look at any stop-loss distance). Quantity shrinks when cash
        shrinks, grows when cash grows.

        2026-07-20 audit note: this docstring previously (incorrectly)
        described this as the "industry standard fixed-fractional method"
        that "risks exactly risk_per_trade_pct of cash" — that description
        belongs to true risk-based sizing (position size derived from a stop
        distance so a stop-out costs exactly risk_per_trade_pct of cash),
        which this function does NOT do. With the live config
        (RISK_PER_TRADE_PCT=0.10, fixed SL fallback 1.5%), the REAL dollar
        risk if a stop is hit is risk_per_trade_pct × stop_loss_pct ≈ 0.15%
        of cash — not 10%. The actual risk-based cap on live BUYs comes from
        calc_trade_qty_atr_risk() below, which IS a true dollar-risk
        calculation and is the one that runs whenever ATR_SIZING_ENABLED=true
        (the live setting since 2026-07-17).
        """
        if price <= 0 or cash <= 0:
            return 0.0
        trade_value = cash * self.risk.risk_per_trade_pct
        return round(trade_value / price, 6)

    def calc_trade_qty_atr_risk(
        self,
        cash:            float,
        price:           float,
        atr_value:       float,
        atr_mult:        float,
        baseline_sl_pct: float,
    ) -> float:
        """
        ATR-aware sizing with a fixed-SL dollar-risk baseline (2026-07-17).

        The validated fixed-SL config risked cash × risk_pct × baseline_sl_pct
        dollars per stop-out (10% notional × 1.5% SL = 0.15% of cash). With
        ATR stops (ATR_SL_MULT) the stop distance varies per entry, so plain
        notional sizing lets a wide-ATR entry risk MORE dollars than that
        baseline. This caps qty so a stop-out never exceeds the baseline
        dollar risk; a tight ATR stop does NOT size up past the standard
        notional (min(), not equality — sizing up would be a leverage change,
        not a risk cap). Falls back to calc_trade_qty() whenever any input
        is unusable.
        """
        base_qty = self.calc_trade_qty(cash, price)
        if (
            price <= 0 or cash <= 0 or atr_value <= 0
            or atr_mult <= 0 or baseline_sl_pct <= 0
        ):
            return base_qty
        sl_distance = atr_value * atr_mult
        risk_budget = cash * self.risk.risk_per_trade_pct * baseline_sl_pct
        return round(min(base_qty, risk_budget / sl_distance), 6)

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
        logger.info("CONFIG  atr  sl_mult=%.1f  period=%d",
            self.strategy.atr_sl_mult, self.strategy.atr_period)
        logger.info("CONFIG  risk  per_trade=%.0f%%  max_pos=%.0f%%  daily_loss=%.0f%%  max_dd=%.0f%%  max_trades=%d/day  cooldown=%d",
            self.risk.risk_per_trade_pct * 100,
            self.risk.max_position_pct * 100,
            self.risk.daily_loss_limit_pct * 100,
            self.risk.max_drawdown_pct * 100,
            self.risk.max_trades_per_day,
            self.risk.cooldown_ticks)
        logger.info("CONFIG  portfolio  starting_cash=$%.2f  max_concurrent=%d  AI=%s",
            self.portfolio.starting_cash, self.portfolio.max_concurrent_positions, self.ai.enabled)
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

        # ── Strategy hash + drift guard ───────────────────────────────────────
        from bot.strategy.fingerprint import compute_strategy_hash  # local import — avoids circular at module init
        _shash = compute_strategy_hash()
        logger.info("CONFIG  strategy_hash=%s", _shash)
        _hash_file = Path(os.getenv("STRATEGY_HASH_FILE", "logs/validated_strategy_hash"))
        if _hash_file.exists():
            _saved = _hash_file.read_text().strip()
            if _saved != _shash:
                logger.warning(
                    "STRATEGY CODE DIFFERS FROM LAST VALIDATED VERSION  "
                    "saved=%s  current=%s  "
                    "Walk-forward results are STALE — re-run walkforward.py then stamp with: "
                    "python stamp_strategy.py",
                    _saved, _shash,
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
# Drift guard — env keys that are KNOWN to config.py for strategy-critical
# prefixes.  Any env key matching a prefix that is NOT in this set triggers
# a startup WARNING (typo, renamed key, or stale legacy setting).
# Convention: mult=0.0 means disabled — no separate _ENABLED keys.
# ---------------------------------------------------------------------------
_STRATEGY_CRITICAL_PREFIXES: tuple[str, ...] = (
    "ATR_", "RSI_", "ADX_", "STOP_", "TAKE_", "RISK_", "EMA_",
)
_KNOWN_STRATEGY_ENV_KEYS: frozenset[str] = frozenset({
    # ATR
    "ATR_SL_MULT", "ATR_PERIOD", "ATR_VOLATILE_MULTIPLIER",
    "ATR_SIZING_ENABLED",
    # RSI
    "RSI_PERIOD", "RSI_OVERSOLD", "RSI_OVERBOUGHT", "RSI_FILTER_ENABLED",
    # ADX
    "ADX_PERIOD", "ADX_THRESHOLD", "ADX_MAX",
    # STOP / TAKE
    "STOP_LOSS_PCT", "TAKE_PROFIT_PCT",
    # RISK
    "RISK_PER_TRADE_PCT", "RISK_MAX_POSITION_PCT", "RISK_DAILY_LOSS_LIMIT",
    "RISK_MAX_DRAWDOWN", "RISK_MAX_TRADES_PER_DAY", "RISK_HALT_BLOCKS_STOPS",
    "RISK_WEEKLY_LOSS_LIMIT", "RISK_DRAWDOWN_WARNING", "RISK_KILL_SWITCH",
    # EMA — none of the recognised keys start with EMA_ (they use MIN_/MAX_/FAST_/SLOW_/REGIME_)
    # so any EMA_* key in .env is unrecognised and will be flagged correctly.
})

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
            # Generic credentials preferred; KRAKEN_* kept as legacy fallback so
            # existing live deployments keep working without an .env edit.
            api_key        = _str ("EXCHANGE_API_KEY",    "") or _str("KRAKEN_API_KEY",    ""),
            api_secret     = _str ("EXCHANGE_API_SECRET", "") or _str("KRAKEN_API_SECRET", ""),
            order_type              = _str  ("ORDER_TYPE",             "market"),
            limit_order_enabled     = _bool ("LIMIT_ORDER_ENABLED",      False),
            limit_chase_timeout_s   = _int  ("LIMIT_CHASE_TIMEOUT_S",    120),
            limit_chase_max_retries  = _int  ("LIMIT_CHASE_MAX_RETRIES",  3),
            limit_chase_tick_pct     = _float("LIMIT_CHASE_TICK_PCT",     0.0001),
            adopt_external_holdings  = _bool ("ADOPT_EXTERNAL_HOLDINGS",  False),
            drift_alert_threshold    = _int  ("DRIFT_ALERT_THRESHOLD",    3),
            native_stop_loss_enabled = _bool ("NATIVE_STOP_LOSS_ENABLED", False),
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
            adx_threshold           = _float("ADX_THRESHOLD",           18.0),
            adx_max                 = _float("ADX_MAX",                 0.0),
            min_ema_spread_pct      = _float("MIN_EMA_SPREAD_PCT",      0.002),
            max_ema_spread_pct      = _float("MAX_EMA_SPREAD_PCT",      0.0),
            rsi_filter_enabled      = _bool ("RSI_FILTER_ENABLED",      True),
            regime_ema_period       = _int  ("REGIME_EMA_PERIOD",       200),
            regime_ema_slope_filter = _bool ("REGIME_EMA_SLOPE_FILTER", False),
            volume_k                = _float("VOLUME_K",                0.0),
            macd_enabled            = _bool ("MACD_ENABLED",            True),
            atr_volatile_multiplier = _float("ATR_VOLATILE_MULTIPLIER", 1.5),
            atr_sl_mult             = _float("ATR_SL_MULT",              0.0),
            atr_sizing_enabled      = _bool ("ATR_SIZING_ENABLED",      False),
            atr_period              = _int  ("ATR_PERIOD",               14),
            pullback_rsi_min        = _float("PULLBACK_RSI_MIN",        38.0),
            pullback_rsi_max        = _float("PULLBACK_RSI_MAX",        58.0),
            breakout_rsi_min        = _float("BREAKOUT_RSI_MIN",        50.0),
            breakout_rsi_max        = _float("BREAKOUT_RSI_MAX",        72.0),
            breakout_lookback       = _int  ("BREAKOUT_LOOKBACK",       20),
            max_price_extension_pct = _float("MAX_PRICE_EXTENSION_PCT", 0.03),
            breakout_adx_threshold  = _float("BREAKOUT_ADX_THRESHOLD",  22.0),
        ),
        risk=RiskConfig(
            risk_per_trade_pct     = _float("RISK_PER_TRADE_PCT",       0.01),
            max_position_pct       = _float("RISK_MAX_POSITION_PCT",    0.05),
            daily_loss_limit_pct   = _float("RISK_DAILY_LOSS_LIMIT",    0.02),
            max_drawdown_pct       = _float("RISK_MAX_DRAWDOWN",        0.10),
            max_trades_per_day     = _int  ("RISK_MAX_TRADES_PER_DAY",  5),
            cooldown_ticks         = _int  ("COOLDOWN_TICKS",           10),
            risk_halt_blocks_stops = _bool ("RISK_HALT_BLOCKS_STOPS",   False),
            weekly_loss_limit_pct  = _float("RISK_WEEKLY_LOSS_LIMIT",   0.05),
            drawdown_warning_pct   = _float("RISK_DRAWDOWN_WARNING",    0.03),
            kill_switch_pct        = _float("RISK_KILL_SWITCH",         0.15),
        ),
        portfolio=PortfolioConfig(
            starting_cash            = _float("STARTING_CASH",            10_000.0),
            sim_start_price          = _float("SIM_START_PRICE",           68_500.0),
            sim_volatility           = _float("SIM_VOLATILITY",            200.0),
            max_concurrent_positions = _int  ("MAX_CONCURRENT_POSITIONS",  2),
            max_slot_cash_cad        = _float("MAX_SLOT_CASH_CAD",         0.0),
            live_dust_value_cad      = _float("LIVE_DUST_VALUE_CAD",       10.0),
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
            trail_stop_pct              = _float("TRAILING_STOP_PCT",            0.0),
            trail_stop_activation_pct   = _float("TRAILING_STOP_ACTIVATION_PCT", 0.03),
            partial_tp_pct   = _float("PARTIAL_TP_PCT",       0.0),
            partial_tp_size  = _float("PARTIAL_TP_SIZE",      0.5),
            atr_sl_mult      = _float("ATR_SL_MULT",           0.0),
            atr_sizing_enabled = _bool("ATR_SIZING_ENABLED",  False),
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
            enabled             = _bool ("UNIVERSE_ENABLED",       False),
            size                = _int  ("UNIVERSE_SIZE",          5),
            min_vol             = _float("UNIVERSE_MIN_VOL_CAD",   1000.0),
            universe_quote      = _str  ("UNIVERSE_QUOTE",         "CAD"),
            universe_exclude    = _str  ("UNIVERSE_EXCLUDE",       "EUR,USD,USDC,USDT,DAI,BUSD"),
            universe_whitelist  = _str  ("UNIVERSE_WHITELIST",     ""),
        ),
        paper=PaperConfig(
            paper_mode          = _bool ("PAPER_MODE",          False),
            paper_starting_cash = _float("PAPER_STARTING_CASH", 1000.0),
        ),
    )

    # Warn loudly when critical strategy values are absent from the environment.
    # These fire at import time so they appear in every run, not buried mid-log.
    if "ADX_THRESHOLD" not in os.environ:
        logger.info(
            "ADX_THRESHOLD not set in .env — using code default %.1f (validated value).",
            cfg.strategy.adx_threshold,
        )
    if cfg.strategy.volume_k > 0:
        logger.warning(
            "VOLUME_K=%.1f — volume filter is ACTIVE. "
            "Live-validated strategy uses VOLUME_K=0 (disabled). "
            "Set VOLUME_K=0 in .env unless you have re-validated with volume filter on.",
            cfg.strategy.volume_k,
        )

    # ── Startup strategy fingerprint ──────────────────────────────────────
    sl_type = "ATR" if cfg.strategy.atr_sl_mult > 0 else "fixed"
    logger.info(
        "STRATEGY FINGERPRINT  SL=%s(%.3f%%)  ATR_SL_MULT=%.2f  TP=%.2f%%  "
        "ADX=%.1f  RSI=%g/%g  MIN_EMA_SPREAD=%.4f  whitelist=%s",
        sl_type, cfg.backtest.stop_loss_pct * 100, cfg.strategy.atr_sl_mult,
        cfg.backtest.take_profit_pct * 100,
        cfg.strategy.adx_threshold,
        cfg.strategy.rsi_oversold, cfg.strategy.rsi_overbought,
        cfg.strategy.min_ema_spread_pct,
        cfg.universe.universe_whitelist or "(dynamic)",
    )

    # ── Drift guard: warn on unrecognised strategy-critical env keys ──────
    _unrecognised = [
        k for k in os.environ
        if any(k.startswith(p) for p in _STRATEGY_CRITICAL_PREFIXES)
        and k not in _KNOWN_STRATEGY_ENV_KEYS
    ]
    if _unrecognised:
        logger.warning(
            "CONFIG DRIFT: .env contains unrecognised strategy keys: %s — "
            "these are ignored by config.py. Check for typos or stale legacy settings.",
            ", ".join(sorted(_unrecognised)),
        )

    return cfg


# Single instance — import this everywhere
cfg: AppConfig = _load()