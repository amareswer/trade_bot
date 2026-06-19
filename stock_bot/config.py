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
    paper_min_confidence:  int   # min AI confidence to trigger a paper trade
    paper_daily_loss_pct:    float   # daily loss circuit breaker (fraction)
    paper_slippage_bps:      int     # simulated slippage in basis points
    paper_stop_loss_pct:   float # stop-loss threshold as a fraction (e.g. 0.05 = -5%)
    paper_take_profit_pct: float # take-profit threshold as a fraction (e.g. 0.12 = +12%)
    universe_enabled:      bool  # scan S&P500+TSX60 instead of fixed watchlist
    universe_size:         int   # top N symbols to scan per cycle
    universe_refresh_hours: int  # how often to refresh the symbol lists
    screener_enabled:      bool  # skip AI on stocks with no momentum signal
    nvidia_api_key:        str   # NVIDIA NIM API key (nvidia_nim provider)
    nvidia_model:          str   # NVIDIA NIM model name
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
        paper_min_confidence   = _int  ("PAPER_MIN_CONFIDENCE",    65),
        paper_daily_loss_pct    = _float("PAPER_DAILY_LOSS_PCT",    0.03),
        paper_slippage_bps      = _int("PAPER_SLIPPAGE_BPS",      15),
        paper_stop_loss_pct    = _float("PAPER_STOP_LOSS_PCT",     0.05),
        paper_take_profit_pct  = _float("PAPER_TAKE_PROFIT_PCT",   0.12),
        universe_enabled       = _bool ("UNIVERSE_ENABLED",        False),
        universe_size          = _int  ("UNIVERSE_SIZE",           20),
        universe_refresh_hours = _int  ("UNIVERSE_REFRESH_HOURS",  24),
        screener_enabled       = _bool ("SCREENER_ENABLED",        True),
        nvidia_api_key         = _str  ("NVIDIA_API_KEY",          ""),
        nvidia_model           = _str  ("NVIDIA_MODEL",            "nvidia/nemotron-3-ultra-550b-a55b"),
    )
