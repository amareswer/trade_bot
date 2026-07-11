"""
Stock bot configuration.

Reads from stock_bot/.env (not the root .env — these are separate bots).
All settings have safe defaults so the bot runs out of the box with
just a watchlist defined.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load stock_bot/.env explicitly — never the root .env
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=_ENV_PATH, override=False)

logger = logging.getLogger(__name__)


def _str(key: str, default: str) -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"Stock bot config error: {key} must be an integer, got '{raw}'")


def _float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        raise ValueError(f"Stock bot config error: {key} must be a number, got '{raw}'")


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class StockConfig:
    watchlist_str:  str          # raw comma-separated string from env
    interval:       str          # yfinance interval: "1d", "1h", "5m", …
    lookback_days:  int          # how many days of history to fetch
    loop_interval:  int          # seconds between full watchlist scans
    ai_enabled:     bool         # set False to skip AI and save API calls
    portfolio:             str   # raw PORTFOLIO env var — "SYMBOL:SHARES:COST,..."
    base_currency:         str   # display currency for mixed portfolios
    alert_email_enabled:   bool  # send email alerts (HIGH only)
    alert_email_from:      str   # Gmail sender address
    alert_email_to:        str   # alert destination address
    alert_email_password:  str   # Gmail app password (not login password)
    alert_desktop_enabled: bool  # plyer desktop notifications (HIGH only)
    paper_trading_enabled: bool  # enable paper trading mode (virtual cash)
    paper_starting_cash:   float # virtual cash balance at startup
    paper_risk_pct:        float # fraction of cash to allocate per trade
    rule_trading_enabled:  bool  # rule-based signals trigger trades; AI is advisory only
    rule_whitelist_str:    str   # comma-separated symbols that passed stock_backtest.py walk-forward
    paper_min_confidence:  int   # min AI confidence to trigger a paper BUY (legacy mode: RULE_TRADING_ENABLED=false)
    paper_min_confidence_sell: int  # min AI confidence to exit a HELD position (lower bar — exits reduce risk)
    paper_sell_streak_min_conf: int # SELL verdicts >= this count toward the consecutive-SELL streak
    paper_sell_streak_cycles:   int # consecutive SELL cycles (each >= streak_min_conf) that force an exit
    paper_daily_loss_pct:    float   # daily loss circuit breaker (fraction)
    paper_slippage_bps:      int     # simulated slippage in basis points
    paper_stop_loss_pct:   float # stop-loss threshold as a fraction (e.g. 0.05 = -5%)
    paper_take_profit_pct: float # take-profit threshold as a fraction (e.g. 0.12 = +12%)
    paper_max_exposure_pct: float # max fraction of portfolio value in open positions
    paper_max_positions:   int   # max number of open positions at once
    universe_enabled:      bool  # scan S&P500+TSX60 instead of fixed watchlist
    universe_size:         int   # top N symbols to scan per cycle
    universe_refresh_hours: int  # how often to refresh the symbol lists
    universe_sources:      str   # comma-separated index sources (sp500,nasdaq100,…)
    universe_etfs:         str   # comma-separated ETF list (user-controlled via .env)
    universe_min_avg_volume: int   # minimum 20-day avg daily volume filter
    universe_min_price:    float   # minimum stock price (penny stock filter)
    universe_min_score:    float   # minimum composite momentum score
    universe_weight_volume: float  # scoring weight: volume surge
    universe_weight_mom5d:  float  # scoring weight: 5-day momentum
    universe_weight_mom1d:  float  # scoring weight: 1-day momentum
    universe_weight_relstr: float  # scoring weight: relative strength vs SPY
    screener_enabled:      bool  # skip AI on stocks with no momentum signal
    ai_gate_rsi_max:       float # skip AI call when RSI > this (overbought, e.g. 75)
    ai_gate_adx_min:       float # skip AI call when ADX < this (ranging, e.g. 15)
    earnings_blackout_days: int  # block BUY within N days of next earnings date
    price_sanity_pct:      float # reject live price if it deviates >this from candle close (e.g. 0.05)
    price_outlier_factor:  float # reject latest close if >Nx the median of the same fetch (default 10)
    nvidia_api_key:        str   # NVIDIA NIM API key (nvidia_nim provider)
    nvidia_model:          str   # NVIDIA NIM model name
    regime_filter_enabled: bool  # block BUY when SPY is not in BULL regime
    regime_ma_period:      int   # slow SMA period for golden/death cross (default 200)
    regime_fast_ma:        int   # fast SMA period for golden/death cross (default 50)
    watchlist:             list[str] = field(init=False)

    def __post_init__(self) -> None:
        from stock_bot.data.watchlist import get_watchlist
        self.watchlist = get_watchlist(self.watchlist_str or None)

        valid_intervals = {
            "1m", "2m", "5m", "15m", "30m", "60m", "90m",
            "1h", "1d", "5d", "1wk", "1mo", "3mo",
        }
        if self.interval not in valid_intervals:
            raise ValueError(
                f"INTERVAL must be one of {sorted(valid_intervals)}, got '{self.interval}'"
            )
        if self.lookback_days < 5:
            raise ValueError("LOOKBACK_DAYS must be >= 5")
        if self.loop_interval < 10:
            raise ValueError("LOOP_INTERVAL must be >= 10 seconds")

        total_weight = (
            self.universe_weight_volume + self.universe_weight_mom5d
            + self.universe_weight_mom1d + self.universe_weight_relstr
        )
        if abs(total_weight - 1.0) > 0.01:
            raise ValueError(
                f"Universe scoring weights must sum to 1.0, got {total_weight:.3f}. "
                f"Check UNIVERSE_WEIGHT_* in stock_bot/.env"
            )

    def log_startup(self) -> None:
        logger.info("─" * 50)
        logger.info("STOCK BOT  interval=%s  lookback=%dd  loop=%ds",
                    self.interval, self.lookback_days, self.loop_interval)
        logger.info("WATCHLIST  %s", "  ".join(self.watchlist))
        logger.info("─" * 50)


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load() -> StockConfig:
    return StockConfig(
        watchlist_str = _str ("WATCHLIST",      "SHOP.TO,RY.TO,AAPL,NVDA,AC.TO"),
        interval      = _str ("INTERVAL",       "1d"),
        lookback_days = _int ("LOOKBACK_DAYS",  200),
        loop_interval = _int ("LOOP_INTERVAL",  60),
        ai_enabled    = _bool("AI_ENABLED",     True),
        portfolio             = _str ("PORTFOLIO",             ""),
        base_currency         = _str ("BASE_CURRENCY",         "CAD"),
        alert_email_enabled   = _bool("ALERT_EMAIL_ENABLED",   False),
        alert_email_from      = _str ("ALERT_EMAIL_FROM",      ""),
        alert_email_to        = _str ("ALERT_EMAIL_TO",        ""),
        alert_email_password  = _str ("ALERT_EMAIL_PASSWORD",  ""),
        alert_desktop_enabled = _bool ("ALERT_DESKTOP_ENABLED",  False),
        paper_trading_enabled  = _bool ("PAPER_TRADING_ENABLED",   False),
        paper_starting_cash    = _float("PAPER_STARTING_CASH",     10_000.0),
        paper_risk_pct         = _float("PAPER_RISK_PCT",          0.10),
        rule_trading_enabled   = _bool ("RULE_TRADING_ENABLED",    True),
        rule_whitelist_str     = _str  ("RULE_WHITELIST",          ""),
        paper_min_confidence   = _int  ("PAPER_MIN_CONFIDENCE",    65),
        paper_min_confidence_sell  = _int("PAPER_MIN_CONFIDENCE_SELL",   55),
        paper_sell_streak_min_conf = _int("PAPER_SELL_STREAK_MIN_CONF",  50),
        paper_sell_streak_cycles   = _int("PAPER_SELL_STREAK_CYCLES",    2),
        paper_daily_loss_pct    = _float("PAPER_DAILY_LOSS_PCT",    0.03),
        paper_slippage_bps      = _int("PAPER_SLIPPAGE_BPS",      15),
        paper_stop_loss_pct    = _float("PAPER_STOP_LOSS_PCT",     0.05),
        paper_take_profit_pct  = _float("PAPER_TAKE_PROFIT_PCT",   0.12),
        paper_max_exposure_pct = _float("PAPER_MAX_EXPOSURE_PCT",  0.25),
        paper_max_positions    = _int  ("PAPER_MAX_POSITIONS",     4),
        universe_enabled       = _bool ("UNIVERSE_ENABLED",        False),
        universe_size          = _int  ("UNIVERSE_SIZE",           20),
        universe_refresh_hours = _int  ("UNIVERSE_REFRESH_HOURS",  24),
        universe_sources        = _str  ("UNIVERSE_SOURCES",
                                  "sp500,nasdaq100,sp400,tsx60,tsx_composite,etfs"),
        universe_etfs           = _str  ("UNIVERSE_ETFS",
                                  "XLK,XLF,XLE,XLV,XLI,XLY,XLP,XLB,XLU,XLRE,XLC,"
                                  "SPY,QQQ,IWM,DIA,VTI,ARKK,ARKG,ARKW,SMH,SOXX,"
                                  "EEM,EFA,GLD,SLV,USO,TLT,HYG,"
                                  "XIU.TO,XIC.TO,ZEB.TO,XEG.TO,XFN.TO"),
        universe_min_avg_volume = _int  ("UNIVERSE_MIN_AVG_VOLUME", 300_000),
        universe_min_price      = _float("UNIVERSE_MIN_PRICE",      1.0),
        universe_min_score      = _float("UNIVERSE_MIN_SCORE",      0.001),
        universe_weight_volume  = _float("UNIVERSE_WEIGHT_VOL",     0.35),
        universe_weight_mom5d   = _float("UNIVERSE_WEIGHT_MOM5D",   0.30),
        universe_weight_mom1d   = _float("UNIVERSE_WEIGHT_MOM1D",   0.20),
        universe_weight_relstr  = _float("UNIVERSE_WEIGHT_RELSTR",  0.15),
        screener_enabled       = _bool ("SCREENER_ENABLED",        True),
        ai_gate_rsi_max        = _float("AI_GATE_RSI_MAX",         75.0),
        ai_gate_adx_min        = _float("AI_GATE_ADX_MIN",         15.0),
        earnings_blackout_days = _int  ("EARNINGS_BLACKOUT_DAYS",  5),
        price_sanity_pct       = _float("PRICE_SANITY_PCT",        0.05),
        price_outlier_factor   = _float("PRICE_OUTLIER_FACTOR",    10.0),
        nvidia_api_key         = _str  ("NVIDIA_API_KEY",          ""),
        nvidia_model           = _str  ("NVIDIA_MODEL",            "nvidia/nemotron-3-ultra-550b-a55b"),
        regime_filter_enabled  = _bool ("REGIME_FILTER_ENABLED",   True),
        regime_ma_period       = _int  ("REGIME_MA_PERIOD",        200),
        regime_fast_ma         = _int  ("REGIME_FAST_MA",          50),
    )
