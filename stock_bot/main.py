"""
Stock bot — Phase 4 entry point.

Advisory only. No orders, no execution, no real money.
Runs 24/7 with three modes:
  LIVE        — full scan every LOOP_INTERVAL seconds (market open)
  PRE_MARKET  — news scan every 15 min (6:00–9:30am ET weekdays)
  AFTER_HOURS — news scan every 30 min (4:00pm–midnight ET weekdays)
  WEEKEND     — idle check every hour (Sat–Sun)

Run from the repo root:
    python -m stock_bot.main
Keep alive on Mac:
    caffeinate -i python -m stock_bot.main
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import signal as _signal_module
import sys
import threading
import time
from collections import Counter

import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as _dt
import pytz as _pytz
from datetime import datetime, date, timedelta

from stock_bot.config import load
from stock_bot.data.price_feed    import fetch_candles, reset_price_cache, get_sector
from stock_bot.data.yf_client     import fetch_with_retry
from stock_bot.data.intraday_price import get_live_price
from stock_bot.data.universe  import StockUniverse, _FALLBACK_SYMBOLS as _UNIVERSE_FALLBACK
from stock_bot.data.screener  import StockScreener
from stock_bot.indicators.indicators import (
    adx    as calc_adx,
    atr    as calc_atr,
    macd   as calc_macd,
    regime as regime,
    rsi    as calc_rsi,
    trend  as calc_trend,
)
from stock_bot.research.aggregator  import fetch_research, ResearchReport, get_company_name
from stock_bot.research.fear_greed   import fetch_fear_greed
from stock_bot.research.google_trends import fetch_market_trends
from stock_bot.ai.ai_engine         import AIEngine
from stock_bot.ai.verdict           import AIVerdict
from stock_bot.dashboard.renderer   import DashboardRenderer, ScanResult
from stock_bot.portfolio.tracker    import PortfolioTracker
from stock_bot.alerts.evaluator     import AlertEvaluator
from stock_bot.alerts.notifier      import AlertNotifier
from stock_bot.execution.paper      import StockPaperExecutor
from stock_bot.execution.base       import OrderStatus
from stock_bot.execution.exit_policy import ExitPolicy
from stock_bot.strategy.rules       import rule_signal
from stock_bot.fast_validator       import FastValidator
from stock_bot.analysis.accuracy_tracker import LiveTradingGate

from colorama import Fore, Style, init as _colorama_init
_colorama_init(autoreset=True)

# ---------------------------------------------------------------------------
# Logging — file only at INFO, stderr at WARNING
# ---------------------------------------------------------------------------
import os as _os
_LOG_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "logs")
_os.makedirs(_LOG_DIR, exist_ok=True)

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    """Install root handlers — called from run(), NOT at import time.

    Import-time installation means anything that imports stock_bot.main
    (tests, tooling) writes into the production logs/stock_bot.log —
    the same failure mode fixed in bot/main.py on 2026-07-05 (polluted
    forensics, faked heartbeat, live-log rotation from under the bot).
    Only the actual bot process may touch this file."""
    _fh = logging.handlers.RotatingFileHandler(
        _os.path.join(_LOG_DIR, "stock_bot.log"),
        maxBytes=10_000_000,
        backupCount=7,
    )
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))

    _ch = logging.StreamHandler(sys.stderr)
    _ch.setLevel(logging.WARNING)

    _root = logging.getLogger()
    _root.handlers.clear()
    _root.setLevel(logging.INFO)
    _root.addHandler(_fh)
    _root.addHandler(_ch)

    # Silence yfinance/urllib noise in the terminal
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("peewee").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_TREND_ICON = {"BULLISH": "▲", "BEARISH": "▼", "NEUTRAL": "—"}
_RSI_WARN   = 70.0
_RSI_OVER   = 30.0


def _fmt_rsi(val: float | None) -> str:
    if val is None:
        return "RSI=  — "
    flag = "⚠" if val >= _RSI_WARN or val <= _RSI_OVER else " "
    return f"RSI={val:4.1f}{flag}"


def _fmt_macd(result: tuple | None) -> str:
    if result is None:
        return "MACD=—                "
    m, s, h = result
    hist_sign = "+" if h >= 0 else ""
    return f"MACD={m:+6.2f}  sig={s:+6.2f}  hist={hist_sign}{h:.2f}"


def _fmt_adx(val: float | None) -> str:
    if val is None:
        return "ADX=  — "
    label = "trending" if val >= 25 else "ranging "
    return f"ADX={val:4.1f} {label}"


_SIGNAL_ICON = {"BUY": "✅ BUY", "SELL": "❌ SELL", "HOLD": "⏸  HOLD"}
_AI_PREFIX   = "                   "   # aligns continuation lines under the signal


def _print_verdict(verdict: AIVerdict) -> None:
    """Print the AI verdict block (3 lines)."""
    icon = _SIGNAL_ICON.get(verdict.signal, verdict.signal)

    _p = verdict.provider
    if _p == "nvidia_nim":
        ptag = "[nvidia]"
    elif _p == "openrouter":
        ptag = "[openrouter]"
    elif _p == "unavailable":
        ptag = "[fallback]"
    else:
        ptag = f"[{_p}]"

    if verdict.confidence >= 70:
        conf_col = Fore.GREEN
    elif verdict.confidence >= 50:
        conf_col = Fore.YELLOW
    else:
        conf_col = Style.DIM

    conf_str = f"{conf_col}{verdict.confidence}%{Style.RESET_ALL}"
    print(f"  🤖 AI ({verdict.trading_style:<8}) {ptag}:  {icon}  | Confidence: {conf_str}")

    if verdict.target_price is not None or verdict.stop_loss is not None:
        parts = []
        if verdict.target_price is not None:
            parts.append(f"Target: ${verdict.target_price:,.2f}")
        if verdict.stop_loss is not None:
            parts.append(f"Stop: ${verdict.stop_loss:,.2f}")
        print(f"  {_AI_PREFIX}{' | '.join(parts)}")

    if verdict.reasoning:
        print(f"  {_AI_PREFIX}\"{verdict.reasoning[:300]}\"")


def _print_research(report: ResearchReport) -> None:
    """Print the research block for one symbol (3 lines)."""
    if report.news:
        headlines = " · ".join(f'"{n.title[:60]}"' for n in report.news[:3])
        print(f"  📰 News ({len(report.news)}):  {headlines}")
    else:
        print("  📰 News:      no headlines found")

    s = report.sentiment
    if s.post_count > 0:
        print(f"  💬 Sentiment:  {s.label} (score: {s.score:+.2f}) | {s.post_count} headline{'s' if s.post_count != 1 else ''} scored")
    else:
        print("  💬 Sentiment:  no headlines to score")

    e = report.earnings
    next_str = str(e.next_earnings_date) if e.next_earnings_date else "unknown"
    print(f"  📅 Earnings:  Next: {next_str} | Last: {e.earnings_note}")


# ---------------------------------------------------------------------------
# Per-symbol worker functions (called from ThreadPoolExecutor)
# ---------------------------------------------------------------------------

def _fetch_symbol_data(
    symbol:        str,
    cfg,
    screener:      StockScreener | None,
    watchlist_set: set[str],
    market_status: dict | None = None,
) -> dict | None:
    """
    Fetch candles + compute indicators. Returns:
      None                    — no data (market closed / unknown symbol)
      {"screened": True, ...} — screener rejected this symbol
      full data dict          — ready for research + AI
    """
    if market_status is not None:
        is_ca = symbol.upper().endswith(".TO")
        if is_ca and not market_status["ca_open"]:
            logger.info("%s skipped — TSX closed", symbol)
            return None
        if not is_ca and not market_status["us_open"]:
            logger.info("%s skipped — NYSE closed", symbol)
            return None

    candles = fetch_candles(symbol, interval=cfg.interval, lookback_days=cfg.lookback_days)
    if candles is None:
        return None

    closes = [c.close for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]

    rsi_val   = calc_rsi(closes)
    # 2-candle confirmation: both latest and prior candle must show same EMA crossover
    trend_val = "NEW IPO" if len(closes) < 21 else calc_trend(closes, fast_period=9, slow_period=21, confirmation_candles=2)
    adx_val   = calc_adx(highs, lows, closes)
    macd_val  = calc_macd(closes)
    atr_val   = calc_atr(highs, lows, closes, period=14)

    if symbol not in watchlist_set and screener is not None and not screener.screen(symbol, candles):
        return {"screened": True, "price": closes[-1]}

    return {
        "screened":    False,
        "candles":     candles,
        "last_candle": candles[-1],
        "price":       closes[-1],
        "rsi":         rsi_val,
        "trend":       trend_val,
        "adx":         adx_val,
        "macd":        macd_val,
        "atr":         atr_val,
    }


def _run_ai_call(
    symbol:          str,
    data:            dict,
    report:          ResearchReport,
    engine:          AIEngine,
    stop_loss_pct:   float = 0.05,
    take_profit_pct: float = 0.12,
) -> AIVerdict:
    """Run one AI analysis call (sequential — rate limit enforced inside engine.analyze)."""
    indicators = {
        "rsi":         data["rsi"],
        "trend":       data["trend"],
        "adx":         data["adx"],
        "macd_line":   data["macd"][0] if data["macd"] else None,
        "macd_signal": data["macd"][1] if data["macd"] else None,
        "atr":         data.get("atr"),
    }
    return engine.analyze(symbol, data["last_candle"], indicators, report,
                          stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct)


# ---------------------------------------------------------------------------
# Market hours guard
# ---------------------------------------------------------------------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """
    Return the nth occurrence of weekday (0=Mon...6=Sun) in the given month/year.
    e.g. _nth_weekday(2026, 1, 0, 3) = 3rd Monday of January 2026
    """
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return first + timedelta(days=delta + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of weekday in month/year."""
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    delta = (last.weekday() - weekday) % 7
    return last - timedelta(days=delta)


def _observed(d: date) -> date:
    """
    NYSE/TSX rule: if a holiday falls on Saturday, observe Friday.
    If it falls on Sunday, observe Monday.
    """
    if d.weekday() == 5:   # Saturday → Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:   # Sunday → Monday
        return d + timedelta(days=1)
    return d


def _easter(year: int) -> date:
    """
    Anonymous Gregorian algorithm for Easter Sunday. No external library needed.
    Accurate for 1900–2099.
    """
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day   = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _victoria_day(year: int) -> date:
    """Monday immediately preceding May 25 (Canadian statutory definition)."""
    may24 = date(year, 5, 24)   # strictly before May 25
    days_since_monday = may24.weekday()   # 0=Mon
    return may24 - timedelta(days=days_since_monday)


def _us_holidays(year: int) -> dict[date, str]:
    """
    Compute NYSE holidays for the given year. All rules are floating/relative —
    no hardcoded dates. Works for any year 2025+.
    """
    h: dict[date, str] = {}

    def add(d: date, name: str) -> None:
        h[_observed(d)] = name

    add(date(year,  1,  1),                           "New Year's Day")
    add(_nth_weekday(year,  1, 0, 3),                 "MLK Day")           # 3rd Monday Jan
    add(_nth_weekday(year,  2, 0, 3),                 "Presidents' Day")   # 3rd Monday Feb
    easter = _easter(year)
    add(easter - timedelta(days=2),                   "Good Friday")
    add(_last_weekday(year, 5, 0),                    "Memorial Day")      # Last Monday May
    add(date(year,  6, 19),                           "Juneteenth")
    add(date(year,  7,  4),                           "Independence Day")
    add(_nth_weekday(year,  9, 0, 1),                 "Labor Day")         # 1st Monday Sep
    add(_nth_weekday(year, 11, 3, 4),                 "Thanksgiving")      # 4th Thursday Nov
    add(date(year, 12, 25),                           "Christmas")

    return h


def _ca_holidays(year: int) -> dict[date, str]:
    """
    Compute TSX (Ontario) holidays for the given year.
    Uses Ontario rules — the most conservative (most holidays) of all provinces.
    """
    h: dict[date, str] = {}

    def add(d: date, name: str) -> None:
        h[_observed(d)] = name

    add(date(year,  1,  1),                           "New Year's Day")
    add(_nth_weekday(year,  2, 0, 3),                 "Family Day")        # 3rd Monday Feb (ON)
    easter = _easter(year)
    add(easter - timedelta(days=2),                   "Good Friday")
    add(_victoria_day(year),                          "Victoria Day")
    add(date(year,  7,  1),                           "Canada Day")
    add(_nth_weekday(year,  8, 0, 1),                 "Civic Holiday")     # 1st Monday Aug (ON)
    add(_nth_weekday(year,  9, 0, 1),                 "Labour Day")        # 1st Monday Sep
    add(_nth_weekday(year, 10, 0, 2),                 "Thanksgiving")      # 2nd Monday Oct
    add(date(year, 11, 11),                           "Remembrance Day")
    # Christmas + Boxing Day: if observed dates collide, advance Boxing Day
    # until it lands on a weekday that isn't already taken (e.g. Dec 25=Fri
    # in 2026 → Sat Dec 26 observes to Fri Dec 25, collision → push to Mon Dec 28).
    xmas_obs   = _observed(date(year, 12, 25))
    boxing_obs = _observed(date(year, 12, 26))
    while boxing_obs == xmas_obs or boxing_obs.weekday() >= 5:
        boxing_obs += timedelta(days=1)
    h[xmas_obs]   = "Christmas Day"
    h[boxing_obs] = "Boxing Day"

    return h


def _get_market_status() -> dict:
    """
    Return independent open/closed status for US (NYSE) and Canadian (TSX) markets.

    Keys:
      us_open    — bool: NYSE/NASDAQ open right now
      ca_open    — bool: TSX open right now
      any_open   — bool: at least one market is open (drives scan loop gate)
      is_weekend — bool
      us_holiday — str | None: holiday name if NYSE closed for a holiday today
      ca_holiday — str | None: holiday name if TSX closed for a holiday today
      in_hours   — bool: current time is within 9:30–16:00 ET window
    """
    eastern = _pytz.timezone("US/Eastern")
    now     = datetime.now(eastern)
    today   = now.date()

    is_weekend   = now.weekday() >= 5
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    in_hours     = market_open <= now <= market_close

    _us = _us_holidays(today.year)
    _ca = _ca_holidays(today.year)
    us_holiday = _us.get(today)
    ca_holiday = _ca.get(today)

    us_open = in_hours and not is_weekend and us_holiday is None
    ca_open = in_hours and not is_weekend and ca_holiday is None

    return {
        "us_open":    us_open,
        "ca_open":    ca_open,
        "any_open":   us_open or ca_open,
        "is_weekend": is_weekend,
        "us_holiday": us_holiday,
        "ca_holiday": ca_holiday,
        "in_hours":   in_hours,
    }


def _get_loop_mode(market_status: dict) -> str:
    """
    Return one of: "LIVE" | "PRE_MARKET" | "AFTER_HOURS" | "WEEKEND"

    LIVE        — at least one market is open right now
    PRE_MARKET  — weekday, before 9:30am ET
    AFTER_HOURS — weekday, after 4:00pm ET
    WEEKEND     — Saturday, Sunday, or a full holiday blackout
    """
    if market_status["any_open"]:
        return "LIVE"

    eastern = _pytz.timezone("US/Eastern")
    now = datetime.now(eastern)

    if now.weekday() < 5 and not (market_status["us_holiday"] and market_status["ca_holiday"]):
        market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
        if now < market_open and now.hour >= 6:
            return "PRE_MARKET"
        if now >= market_close:
            return "AFTER_HOURS"

    return "WEEKEND"


def _check_price_uniformity(scan_results: list) -> bool:
    """
    If 3+ symbols show the exact same price, the data feed is corrupted
    (holiday bleed-through). Returns False to signal the cycle should be aborted.
    """
    prices = [r.price for r in scan_results if r and r.price]
    if len(prices) < 3:
        return True
    price_counts = Counter(round(p, 2) for p in prices)
    most_common_price, count = price_counts.most_common(1)[0]
    if count >= 3:
        logger.error(
            "ABORT: %d symbols showing same price $%.2f — corrupted data feed, skipping cycle",
            count, most_common_price,
        )
        return False
    return True


def _is_earnings_blackout(symbol: str, research, cfg) -> bool:
    """
    Returns True if symbol is within earnings_blackout_days of its next earnings date.

    Fail-open: any exception, missing date, or None research → returns False (allow trade).
    Boundary inclusive: exactly N days away → blocked.
    Zero extra network calls — uses already-fetched ResearchReport.earnings.next_earnings_date.
    """
    try:
        blackout_days = getattr(cfg, "earnings_blackout_days", 7)
        if blackout_days <= 0:
            return False

        if research is None:
            return False

        earnings = getattr(research, "earnings", None)
        if earnings is None:
            return False

        next_date = getattr(earnings, "next_earnings_date", None)
        if next_date is None:
            return False

        from datetime import date as _date, datetime as _datetime
        if isinstance(next_date, _datetime):
            next_date = next_date.date()
        elif not isinstance(next_date, _date):
            try:
                next_date = _date.fromisoformat(str(next_date)[:10])
            except Exception:
                return False

        days_until = (next_date - _date.today()).days
        return 0 <= days_until <= blackout_days

    except Exception as exc:
        logger.debug("Earnings blackout check failed for %s: %s", symbol, exc)
        return False


def _check_open_positions_sl_tp(executor, cfg, notifier=None) -> None:
    """
    Lightweight stop-loss / take-profit check for all open paper positions.
    Fetches only the live price (fast_info) — no OHLCV, no indicators, no AI.
    Called every 30s by a background thread independently of the main scan loop.
    """
    if executor is None:
        return
    for symbol, (shares, avg_cost) in list(executor.positions_snapshot().items()):
        if shares <= 0:
            continue
        live = get_live_price(symbol)
        if live is None:
            logger.debug("SL/TP check: skipping %s — no live price", symbol)
            continue
        pct_change = (live - avg_cost) / avg_cost
        if pct_change <= -abs(cfg.paper_stop_loss_pct):
            order = executor.sell(symbol, shares, live, reason="STOP_LOSS_HIT")
            if order.status == OrderStatus.FILLED:
                print(f"  🛑 STOP LOSS triggered: {symbol} @ ${live:.2f} ({pct_change:+.1%})")
                if notifier:
                    notifier.fill("SELL", symbol, shares, live, shares * live,
                                  pnl=round((live - avg_cost) * shares, 2),
                                  reason="stop loss")
        elif pct_change >= cfg.paper_take_profit_pct:
            order = executor.sell(symbol, shares, live, reason="TAKE_PROFIT_HIT")
            if order.status == OrderStatus.FILLED:
                print(f"  ✅ TAKE PROFIT triggered: {symbol} @ ${live:.2f} ({pct_change:+.1%})")
                if notifier:
                    notifier.fill("SELL", symbol, shares, live, shares * live,
                                  pnl=round((live - avg_cost) * shares, 2),
                                  reason="take profit")


def _run_news_scan(symbols: list[str]) -> None:
    """
    Lightweight pre/after-hours news scan — no prices, no AI, no trades.
    Prints major catalyst alerts for symbols with strongly positive/negative news.
    """
    from stock_bot.research.news_fetcher      import fetch_news
    from stock_bot.research.sentiment_scraper import score_headlines

    print(f"  📰 Scanning {len(symbols)} symbols for news catalysts...")
    for symbol in symbols:
        try:
            news = fetch_news(symbol)
            if not news:
                continue
            sentiment = score_headlines(news)
            if sentiment.score >= 0.8:
                print(f"  📈 {symbol}: Strongly positive — {news[0].title[:60]}")
            elif sentiment.score <= -0.8:
                print(f"  📉 {symbol}: Strongly negative — {news[0].title[:60]}")
        except Exception as exc:
            logger.debug("News scan failed for %s: %s", symbol, exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_last_universe_refresh = None                                        # datetime | None
_UNIVERSE_REFRESH_HOUR = int(_os.getenv("UNIVERSE_REFRESH_HOUR", "16"))  # 4pm ET default


def _handle_sigterm(sig, frame):
    # Route SIGTERM (launchd/systemd/kill default) into the existing
    # KeyboardInterrupt path so the loop exits gracefully, not mid-write.
    raise KeyboardInterrupt


def run() -> None:
    global _last_universe_refresh
    _setup_logging()
    _signal_module.signal(_signal_module.SIGTERM, _handle_sigterm)
    cfg = load()
    cfg.log_startup()

    # ── Universe / watchlist setup ────────────────────────────────────────────
    watchlist_symbols = list(cfg.watchlist)

    # Pre-warm sector cache so first scan cycle has no per-symbol delay
    logger.info("Pre-warming sector cache for %d watchlist symbols...", len(watchlist_symbols))
    for sym in watchlist_symbols:
        get_sector(sym)
    logger.info("Sector cache ready.")

    if cfg.universe_enabled:
        _universe = StockUniverse(cfg=cfg, refresh_hours=cfg.universe_refresh_hours)
    else:
        _universe = None
    top_movers: list[str] = []

    all_symbols = list(dict.fromkeys(watchlist_symbols + top_movers))

    screener = StockScreener() if cfg.screener_enabled else None

    # Initialise components once at startup
    ai_engine = AIEngine() if cfg.ai_enabled else None
    renderer  = DashboardRenderer(loop_interval=cfg.loop_interval)
    tracker   = PortfolioTracker(cfg.portfolio)
    evaluator = AlertEvaluator(tracker)
    notifier  = AlertNotifier(cfg)
    # Executor selection: STOCK_EXECUTOR=paper (in-memory sim, default) or
    # ibkr (real fills on the TWS paper API — requires TWS running, port 7497).
    # A failed IBKR connection raises and stops startup — the bot must never
    # silently fall back to simulated fills when real ones were requested.
    if not cfg.paper_trading_enabled:
        executor = None
    elif cfg.executor_type == "ibkr":
        from stock_bot.execution.ibkr import IBKRExecutor
        executor = IBKRExecutor(
            host             = cfg.ibkr_host,
            port             = cfg.ibkr_port,
            client_id        = cfg.ibkr_client_id,
            allow_live       = cfg.ibkr_allow_live,
            max_exposure_pct = cfg.paper_max_exposure_pct,
        )
    else:
        executor = StockPaperExecutor(cfg.paper_starting_cash, max_exposure_pct=cfg.paper_max_exposure_pct)
    if executor:
        executor.set_daily_loss_limit(cfg.paper_daily_loss_pct)
    if executor:
        executor.set_slippage_bps(cfg.paper_slippage_bps)
    if executor:
        try:
            notifier.startup(
                cfg.executor_type,
                executor.cash,
                len(executor.positions_snapshot()),
            )
        except Exception as _exc:
            logger.warning("Startup notification failed: %s", _exc)
    # Asymmetric exit bars: BUY needs paper_min_confidence, but exiting a HELD
    # position needs only paper_min_confidence_sell OR a streak of consecutive
    # SELL verdicts. Exits reduce risk — never harder than entries.
    exit_policy = ExitPolicy(
        min_confidence_sell = cfg.paper_min_confidence_sell,
        streak_min_conf     = cfg.paper_sell_streak_min_conf,
        streak_cycles       = cfg.paper_sell_streak_cycles,
    )
    # Symbols whose rule-based walk-forward PASSED (stock_backtest.py) —
    # only these may be BOUGHT by the rules. Exits apply to anything held.
    _rule_whitelist: set[str] = {
        s.strip().upper() for s in cfg.rule_whitelist_str.split(",") if s.strip()
    }

    _fast_enabled       = _os.getenv("FAST_ENABLED", "false").strip().lower() in ("1", "true", "yes")
    _fast_loop_interval = int(_os.getenv("FAST_LOOP_INTERVAL", "300").strip() or "300")
    # How many universe top movers the fast validator scans in addition to the
    # watchlist. Capped to bound yfinance fetch volume per cycle (rate limits).
    _fast_movers_count  = int(_os.getenv("FAST_MOVERS_COUNT", "5").strip() or "5")
    # Symbols currently in earnings blackout — populated each scan cycle and
    # shared with the swing book via closure so it also skips pre-earnings entries.
    _fv_earnings_blocked: set[str] = set()

    fast_validator      = FastValidator(
        blocked_symbols_fn=(
            (lambda: set(executor.positions_snapshot().keys())) if executor else None
        ),
        earnings_blocked_fn=(lambda: _fv_earnings_blocked) if _fast_enabled else None,
    ) if _fast_enabled else None

    print()
    print("  Stock Bot — Running 24/7")
    print(f"  {'─' * 45}")
    print(f"  🟢 LIVE trading:    Mon-Fri 9:30am–4:00pm EST")
    print(f"  🌅 Pre-market scan: Mon-Fri 6:00am–9:30am EST")
    print(f"  🌙 After-hours:     Mon-Fri 4:00pm–midnight EST")
    print(f"  📅 Weekend idle:    Sat–Sun (no scanning)")
    print(f"  🏖  Holidays:        Per-market routing active")
    print(f"  {'─' * 45}")
    print(f"  My Watchlist : {', '.join(watchlist_symbols)}")
    if cfg.universe_enabled:
        print(f"  Universe     : S&P500 + TSX60 → top {cfg.universe_size} movers (refreshes at {_UNIVERSE_REFRESH_HOUR}:00 ET)")
        print(f"  Top Movers   : {', '.join(top_movers) if top_movers else '(waiting for first refresh)'}")
    print(f"  Screener  : {'enabled' if screener else 'disabled'}")
    print(f"  Interval  : {cfg.interval}   Lookback: {cfg.lookback_days}d   Loop: {cfg.loop_interval}s")
    if ai_engine and ai_engine.enabled:
        print(f"  AI engine  : {ai_engine._provider} → {ai_engine._model}")
        print(f"  Fallback   : openrouter → meta-llama/llama-3.3-70b-instruct:free")
        print(f"  Rate limit : 3.0s between calls (40 rpm safe)")
    else:
        print(f"  AI engine  : disabled")
    if executor:
        # Show the executor's actual (restored) cash, not the .env starting
        # value — the banner said $1,000.00 while the restored book held $520.71.
        print(f"  Paper trading: ON  cash=${executor.cash:,.2f}  risk={cfg.paper_risk_pct*100:.0f}%/trade  "
              f"conf: BUY≥{cfg.paper_min_confidence}% · exit≥{cfg.paper_min_confidence_sell}% "
              f"or {cfg.paper_sell_streak_cycles}×SELL≥{cfg.paper_sell_streak_min_conf}%")
        if cfg.rule_trading_enabled:
            print(f"  Trade trigger: RULES (backtested) — BUY whitelist: "
                  f"{cfg.rule_whitelist_str or '(empty — no rule BUYs!)'}  ·  AI = advisory only")
        else:
            print(f"  Trade trigger: AI verdicts (legacy mode — RULE_TRADING_ENABLED=false)")
    if fast_validator:
        print(f"  Fast validator: ON  interval={_fast_loop_interval}s  state=fast_validator_state.json")
    print(f"  Dashboard : file://{_os.path.abspath('stock_dashboard.html')}")
    print(f"  Logs      : {_os.path.join(_LOG_DIR, 'stock_bot.log')}")
    print()

    # ── Background SL/TP watcher (every 30s, independent of main scan loop) ──
    # Market-hours gated: prices cannot move while markets are closed, and
    # unguarded weekend polling kept the IP permanently yfinance-rate-limited
    # (each 30s retry cycle re-tripped the limiter — observed 2026-07-04).
    def _sl_tp_watcher() -> None:
        while True:
            try:
                if _get_market_status()["any_open"]:
                    _check_open_positions_sl_tp(executor, cfg, notifier)
                    time.sleep(30)
                else:
                    time.sleep(300)   # closed — check the clock, not Yahoo
            except Exception as _exc:
                logger.warning("SL/TP watcher error: %s", _exc)
                time.sleep(30)

    if executor:
        _watcher = threading.Thread(target=_sl_tp_watcher, daemon=True)
        _watcher.start()
        logger.info("SL/TP watcher thread started (30s interval)")

    notifier.start_weekly_summary()

    # ── Heartbeat pings (dead-man's switch — see bot/alerts/heartbeat.py) ──
    # HEARTBEAT_URL: process alive. HEARTBEAT_TWS_URL: pinged only while the
    # IBKR connection is up, so "TWS logged off" alerts separately from
    # "bot died". Both off when unset.
    from bot.alerts.heartbeat import start_heartbeat_thread
    _hb_interval = int(os.getenv("HEARTBEAT_INTERVAL_S", "60"))
    start_heartbeat_thread(
        os.getenv("HEARTBEAT_URL", ""),
        interval_s=_hb_interval,
        name="heartbeat-stock",
    )
    if hasattr(executor, "is_connected"):
        start_heartbeat_thread(
            os.getenv("HEARTBEAT_TWS_URL", ""),
            interval_s=_hb_interval,
            # is_connected is a PROPERTY — wrap it so the check runs per beat,
            # not once at wiring time (caught live 2026-07-17)
            healthy_fn=lambda: bool(executor.is_connected),
            name="heartbeat-tws",
        )

        # ── TWS disconnect monitor (local alert leg — see tws_monitor.py) ──
        from stock_bot.alerts.tws_monitor import TwsConnectionMonitor

        def _tws_monitor_worker() -> None:
            mon = TwsConnectionMonitor(
                alert_after_s=float(os.getenv("TWS_DISCONNECT_ALERT_MIN", "10")) * 60
            )
            down_ticks = 0
            while True:
                try:
                    connected = bool(executor.is_connected)
                    if not connected:
                        # ib_async never redials on its own; probe every 5th
                        # tick (~5 min) so a relogged-in TWS is detected —
                        # and the "restored" notice fires — without waiting
                        # for the next order attempt.
                        down_ticks += 1
                        if down_ticks % 5 == 0 and executor.try_reconnect():
                            connected = bool(executor.is_connected)
                    else:
                        down_ticks = 0
                    event = mon.update(connected, time.time())
                    if event == "down":
                        notifier.ops_alert(
                            "TWS connection lost",
                            f"IBKR/TWS unreachable for {mon.alert_after_s / 60:.0f}+ "
                            "minutes — orders will fail until TWS is logged back in.",
                        )
                    elif event == "recovered":
                        notifier.ops_alert(
                            "TWS connection restored",
                            "IBKR/TWS is reachable again — order routing resumed.",
                        )
                except Exception as _exc:
                    logger.warning("TWS monitor error: %s", _exc)
                time.sleep(60)

        _tws_mon_thread = threading.Thread(
            target=_tws_monitor_worker, daemon=True, name="tws-monitor"
        )
        _tws_mon_thread.start()
        logger.info(
            "TWS disconnect monitor started (alert after %s min)",
            os.getenv("TWS_DISCONNECT_ALERT_MIN", "10"),
        )

    def _fast_validator_worker() -> None:
        # Market-hours gated for the same reason as the SL/TP watcher — its
        # candle fetches for exit checks were part of the weekend rate-limit spiral.
        while True:
            try:
                if _get_market_status()["any_open"]:
                    # Read top_movers live from the enclosing scope — the main
                    # loop rebinds it on each universe refresh, and the closure
                    # sees the updated binding. Cap keeps fetch volume bounded.
                    _fv_symbols = list(dict.fromkeys(
                        watchlist_symbols + top_movers[:_fast_movers_count]
                    ))
                    result  = fast_validator.run_cycle(_fv_symbols, ai_engine)
                    open_c  = result["open_count"]
                    exits_c = len(result["exits"])
                    logger.info("FastValidator: %d open, %d completed today", open_c, exits_c)
                    time.sleep(_fast_loop_interval)
                else:
                    time.sleep(max(_fast_loop_interval, 300))
            except Exception as _exc:
                logger.warning("FastValidator error: %s", _exc)
                time.sleep(_fast_loop_interval)

    if fast_validator:
        _fv_thread = threading.Thread(target=_fast_validator_worker, daemon=True)
        _fv_thread.start()
        logger.info("FastValidator thread started (%ds interval)", _fast_loop_interval)

    tick = 0
    try:
      while True:
        market_status = _get_market_status()
        mode = _get_loop_mode(market_status)

        eastern  = _pytz.timezone("US/Eastern")
        now_et   = datetime.now(eastern)
        time_str = now_et.strftime("%I:%M%p EST").lstrip("0")

        if mode == "PRE_MARKET":
            print(f"🌅 Pre-market ({time_str}) — monitoring news...")
            _run_news_scan(watchlist_symbols + top_movers)
            time.sleep(900)
            continue

        elif mode == "AFTER_HOURS":
            print(f"🌙 After hours ({time_str}) — monitoring news...")
            _run_news_scan(watchlist_symbols + top_movers)
            time.sleep(1800)
            continue

        elif mode == "WEEKEND":
            eastern_now = datetime.now(eastern)
            day_name    = eastern_now.strftime("%A")
            wd = eastern_now.weekday()
            market_open_today = wd < 5 and (eastern_now.hour < 9 or (eastern_now.hour == 9 and eastern_now.minute < 30))
            if market_open_today:
                next_trading_day = "Today"
            elif wd <= 3:
                next_trading_day = (eastern_now + timedelta(days=1)).strftime("%A")
            else:
                next_trading_day = "Monday"
            print(f"📅 {day_name} {time_str} — markets closed. Next open: {next_trading_day} 9:30am EST")
            time.sleep(3600)
            continue

        # ── LIVE mode ────────────────────────────────────────────────────────
        tick += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Both fetched once per cycle and shared across all symbols
        fear_greed_data      = fetch_fear_greed()
        market_trends_score  = fetch_market_trends()

        # Fetch SPY closes once per cycle for regime filter
        spy_closes: list[float] = []
        if cfg.regime_filter_enabled:
            _spy_raw = fetch_with_retry(
                lambda: yf.download(
                    "SPY", interval="1d", period="1y",
                    auto_adjust=True, actions=False, progress=False,
                ),
                label="SPY:regime",
            )
            if _spy_raw is not None and not _spy_raw.empty:
                if hasattr(_spy_raw.columns, "nlevels") and _spy_raw.columns.nlevels > 1:
                    _spy_raw.columns = [c[0] if isinstance(c, tuple) else c for c in _spy_raw.columns]
                spy_closes = [float(v) for v in _spy_raw["Close"].dropna().tolist()]
            elif _spy_raw is None:
                logger.warning("Regime filter: SPY fetch failed — BUY signals blocked this cycle")
        _cycle_regime = regime(spy_closes, cfg.regime_ma_period, cfg.regime_fast_ma) if spy_closes else "UNKNOWN"

        _regime_icons = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡", "UNKNOWN": "⚪"}
        print(f"  ── Scan #{tick:04d}  {now} {'─' * 30}")
        print(f"  😨 Market: Fear & Greed {fear_greed_data.score} — {fear_greed_data.label}  |  📈 Trends: {market_trends_score}/100")
        print(f"  Regime: {_cycle_regime} {_regime_icons.get(_cycle_regime, '⚪')}")
        print(f"  {'Symbol':<10}  {'Price':>10}  {'RSI':^7}  {'Trend':<10}  {'ADX':^13}  MACD")
        print(f"  {'─'*10}  {'─'*10}  {'─'*7}  {'─'*10}  {'─'*13}  {'─'*30}")

        # Refresh universe once per day at UNIVERSE_REFRESH_HOUR ET
        if cfg.universe_enabled and _universe is not None:
            if (now_et.hour == _UNIVERSE_REFRESH_HOUR
                    and (_last_universe_refresh is None
                         or _last_universe_refresh.date() != now_et.date())):
                raw_symbols = _universe.get_universe()
                top_movers  = _universe.pre_filter(raw_symbols, cfg.universe_size, market_status=market_status)
                all_symbols = list(dict.fromkeys(watchlist_symbols + top_movers))
                # Only mark today's refresh done when pre_filter returned real scored data.
                # Fallback symbols mean the circuit breaker was active — leave
                # _last_universe_refresh unset so the bot retries every cycle until real
                # data arrives instead of running on stale fallback symbols for 24h.
                _fallback_slice = list(_UNIVERSE_FALLBACK[:cfg.universe_size])
                if top_movers != _fallback_slice:
                    _last_universe_refresh = now_et
                else:
                    logger.warning(
                        "Universe refresh returned fallback symbols — will retry next cycle"
                    )
                print(f"  Universe refreshed: {len(top_movers)} new movers")
                print(f"  My Watchlist : {', '.join(watchlist_symbols)}")
                print(f"  Top Movers   : {', '.join(top_movers)}")
            elif not top_movers:
                logger.info("Universe: waiting for %d:00 ET refresh", _UNIVERSE_REFRESH_HOUR)

        watchlist_set = set(cfg.watchlist)
        # Held positions must always stay in the scan and bypass the screener:
        # a symbol that rotates out of the universe (DLTR, Jul 2026) otherwise
        # gets no price refresh and no AI verdict — the book can never exit it.
        held_symbols  = list(executor.positions_snapshot().keys()) if executor else []
        cycle_symbols = list(dict.fromkeys(all_symbols + held_symbols))
        watchlist_set |= set(held_symbols)
        scan_results: list[ScanResult] = []

        # Reset per-cycle state
        _fv_earnings_blocked = set()
        reset_price_cache()

        # ── Phase 1: prices + indicators (all symbols, parallel) ───────────
        price_data: dict[str, dict | None] = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {
                ex.submit(
                    _fetch_symbol_data, sym, cfg, screener, watchlist_set,
                    market_status,
                ): sym
                for sym in cycle_symbols
            }
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    price_data[sym] = fut.result()
                except Exception as exc:
                    logger.warning("Price fetch failed for %s: %s", sym, exc)
                    price_data[sym] = None

        active_symbols = [
            s for s in cycle_symbols
            if isinstance(price_data.get(s), dict) and not price_data[s].get("screened")
        ]

        # ── Phase 2: research (active symbols only, parallel) ──────────────
        research_data: dict[str, ResearchReport] = {}
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {
                ex.submit(
                    fetch_research, sym,
                    fear_greed_data=fear_greed_data,
                    market_trends_score=market_trends_score,
                ): sym
                for sym in active_symbols
            }
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    research_data[sym] = fut.result()
                except Exception as exc:
                    logger.warning("Research failed for %s: %s", sym, exc)

        # ── Phase 3: AI calls (active symbols with research, sequential) ────
        # Sequential to respect NVIDIA NIM's 40 rpm limit.
        # The 3s delay is enforced inside ai_engine.analyze() after each call.
        ai_verdicts: dict[str, AIVerdict] = {}
        _ai_start = time.time()
        if cfg.ai_enabled and ai_engine and ai_engine.enabled:
            for sym in active_symbols:
                if sym not in research_data:
                    continue
                try:
                    _d        = price_data[sym]
                    _rsi_g    = _d.get("rsi")
                    _adx_g    = _d.get("adx")
                    _trend_g  = _d.get("trend")
                    _skip_why = None
                    if _rsi_g is not None and _rsi_g > cfg.ai_gate_rsi_max:
                        _skip_why = f"RSI={_rsi_g:.1f} overbought"
                    elif _adx_g is not None and _adx_g < cfg.ai_gate_adx_min:
                        _skip_why = f"ADX={_adx_g:.1f} ranging"
                    elif _trend_g == "NEUTRAL":
                        _skip_why = "trend NEUTRAL"
                    if _skip_why:
                        logger.info("AI skipped for %s — %s", sym, _skip_why)
                        ai_verdicts[sym] = AIVerdict(
                            symbol=sym, signal="HOLD", confidence=0,
                            target_price=None, stop_loss=None,
                            reasoning=f"AI skipped — {_skip_why}",
                            trading_style="SWING", timestamp=datetime.now(),
                            provider="skipped",
                        )
                        continue
                    ai_verdicts[sym] = _run_ai_call(
                        sym, price_data[sym], research_data[sym], ai_engine,
                        stop_loss_pct=cfg.paper_stop_loss_pct,
                        take_profit_pct=cfg.paper_take_profit_pct,
                    )
                except Exception as exc:
                    logger.warning("AI failed for %s: %s", sym, exc)
        _ai_elapsed    = time.time() - _ai_start
        _ai_nvidia_n   = sum(1 for v in ai_verdicts.values() if v.provider == "nvidia_nim")
        _ai_fallback_n = sum(1 for v in ai_verdicts.values() if v.provider == "openrouter")
        _ai_failed_n   = sum(1 for v in ai_verdicts.values() if v.provider in ("unavailable", "unknown"))

        # ── Phase 4: print results + paper trading + build scan list ────────
        for symbol in cycle_symbols:
            data    = price_data.get(symbol)
            report  = research_data.get(symbol)
            verdict: AIVerdict | None = ai_verdicts.get(symbol)
            try:
                if data is None:
                    print(f"  {symbol} — no data available (market may be closed)")
                    print(f"  {'─' * 70}")
                    continue

                if data.get("screened"):
                    print(f"  {symbol:<10}  ${data['price']:>10,.2f}  — no signal (screened out)")
                    print(f"  {'─' * 70}")
                    continue

                # Indicator line
                price     = data["price"]
                rsi_val   = data["rsi"]
                trend_val = data["trend"]
                adx_val   = data["adx"]
                macd_val  = data["macd"]
                icon = _TREND_ICON.get(trend_val, "—")
                print(
                    f"  {symbol:<10}  ${price:>10,.2f}"
                    f"  {_fmt_rsi(rsi_val)}"
                    f"  {icon} {trend_val:<8}"
                    f"  {_fmt_adx(adx_val)}"
                    f"  {_fmt_macd(macd_val)}"
                )
                logger.info(
                    "%s  price=%.2f  rsi=%s  trend=%s  adx=%s  macd=%s",
                    symbol, price,
                    f"{rsi_val:.1f}" if rsi_val else "n/a",
                    trend_val,
                    f"{adx_val:.1f}" if adx_val else "n/a",
                    f"{macd_val[0]:+.2f}" if macd_val else "n/a",
                )

                if report:
                    _print_research(report)

                if not cfg.ai_enabled:
                    print("  🤖 AI: disabled (AI_ENABLED=false in stock_bot/.env)")
                elif verdict is not None:
                    _print_verdict(verdict)
                elif ai_engine and ai_engine.enabled:
                    print("  🤖 AI: unavailable — check credentials in .env")
                else:
                    print("  🤖 AI: disabled (AI_ENABLED=false in stock_bot/.env)")

                # ── Paper trading execution ───────────────────────────────
                # ── Rule-based signal (the trade trigger when enabled) ─────
                # Recomputed statelessly from the candle history each cycle —
                # identical to the backtest by construction. Today's still-
                # forming candle is excluded while the market is open (the
                # backtest only ever saw completed candles).
                rule_v = None
                if cfg.rule_trading_enabled and data.get("candles"):
                    _mkt      = market_status or {}
                    _is_ca    = symbol.upper().endswith(".TO")
                    _open_now = bool(_mkt.get("ca_open" if _is_ca else "us_open"))
                    _last_c   = data["candles"][-1]
                    rule_v = rule_signal(
                        data["candles"],
                        drop_last=_open_now and _last_c.timestamp.date() == date.today(),
                    )
                    _rv_note = ""
                    if not rule_v.warmed_up:
                        _rv_note = "  (warming up — need more history)"
                    elif rule_v.signal == "BUY" and symbol.upper() not in _rule_whitelist:
                        _rv_note = "  (not in RULE_WHITELIST — no entry)"
                    _rv_parts = [f"📐 RULES: {rule_v.signal}"]
                    if rule_v.rsi is not None:
                        _rv_parts.append(f"RSI={rule_v.rsi:.1f}")
                    if rule_v.adx is not None:
                        _rv_parts.append(f"ADX={rule_v.adx:.1f}")
                    if rule_v.trend:
                        _rv_parts.append(rule_v.trend)
                    if rule_v.regime:
                        _rv_parts.append(rule_v.regime)
                    print(f"  {'  '.join(_rv_parts)}{_rv_note}")

                # AI exit policy runs on EVERY verdict (a HOLD/BUY verdict
                # breaks a SELL streak). With rule trading on, AI cannot OPEN
                # positions but may still CLOSE them (risk reduction only).
                _exit_dec = None
                _held_qty = 0.0
                if executor is not None:
                    _held_qty = executor.position(symbol)
                    if verdict is not None:
                        _exit_dec = exit_policy.decide(
                            symbol, verdict.signal, verdict.confidence, _held_qty > 0
                        )

                _rule_buy  = (rule_v is not None and rule_v.signal == "BUY"
                              and rule_v.warmed_up and symbol.upper() in _rule_whitelist)
                _rule_sell = rule_v is not None and rule_v.signal == "SELL"
                _ai_buy    = (
                    verdict is not None
                    and verdict.confidence > 0
                    and verdict.signal == "BUY"
                    and verdict.confidence >= cfg.paper_min_confidence
                )
                _act_buy  = _rule_buy if cfg.rule_trading_enabled else _ai_buy
                _ai_exit  = (
                    verdict is not None and verdict.confidence > 0
                    and _exit_dec is not None and _exit_dec.should_exit
                )
                _act_sell = (_rule_sell and _held_qty > 0) or _ai_exit
                if executor is not None and (_act_buy or _act_sell):
                    px              = data["last_candle"].close
                    live_price      = get_live_price(symbol)
                    raw_live_price  = live_price  # preserve original before sanity null
                    # Sanity-check live price against the daily candle close.
                    # fast_info.last_price can return a wrong currency (USD vs CAD)
                    # or stale/corrupt data for TSX tickers — cap at ±5% deviation.
                    if live_price and px and abs(live_price - px) / px > cfg.price_sanity_pct:
                        logger.warning(
                            "%s live_price $%.2f deviates >%.0f%% from candle close $%.2f — using candle close",
                            symbol, live_price, cfg.price_sanity_pct * 100, px,
                        )
                        live_price = None
                    execution_price = live_price if live_price else px
                    _trigger = (
                        f"RULE {rule_v.signal}" if (_rule_buy or (_rule_sell and _act_sell))
                        else f"AI {verdict.confidence}%" if verdict else "?"
                    )
                    if _act_buy:
                        if _is_earnings_blackout(symbol, report, cfg):
                            _ned = report.earnings.next_earnings_date if report else None
                            _days_left = (_ned - date.today()).days if _ned else "?"
                            logger.info(
                                "EARNINGS BLACKOUT: %s blocked — earnings in %s days (%s)",
                                symbol, _days_left, _ned,
                            )
                            print(
                                f"  🚫 EARNINGS BLACKOUT: {symbol} — "
                                f"earnings in {_days_left}d ({_ned}) | "
                                f"{_trigger} blocked"
                            )
                            print(f"  {'─' * 70}")
                            # Also block swing book from entering this symbol
                            _fv_earnings_blocked.add(symbol.upper())
                            continue
                        _regime_ok = True
                        if cfg.regime_filter_enabled and (not spy_closes or _cycle_regime != "BULL"):
                            logger.info("REGIME_SKIP: %s — market is %s", symbol, _cycle_regime)
                            print(f"  📛 REGIME_SKIP: {symbol} — market is {_cycle_regime}")
                            _regime_ok = False
                        _fv_occupied = fast_validator.state.open_symbols() if fast_validator else set()
                        if symbol.upper() in _fv_occupied:
                            logger.info(
                                "POSITION_SKIP: %s — already held in swing book (dual-exposure guard)",
                                symbol,
                            )
                            print(f"  📄 SKIP: {symbol} — held in swing book (no double exposure)")
                        elif _regime_ok and executor.position(symbol) == 0:
                            _price_map_now = {r.symbol: r.price for r in scan_results}
                            if not executor.check_exposure(_price_map_now):
                                print(f"  📄 SKIP: {symbol} — max exposure ({cfg.paper_max_exposure_pct*100:.0f}%) reached")
                            elif len(executor.positions_snapshot()) >= cfg.paper_max_positions:
                                print(f"  📄 SKIP: {symbol} — max {cfg.paper_max_positions} positions reached")
                            else:
                                snap    = executor.positions_snapshot()
                                pos_val = sum(sh * co for sh, co in snap.values())
                                alloc   = (executor.cash + pos_val) * cfg.paper_risk_pct
                                shares  = int(alloc / execution_price) if execution_price > 0 else 0
                                if shares == 0 and execution_price > 0:
                                    logger.info(
                                        "SIZE_SKIP: %s — target allocation $%.2f "
                                        "(%.0f%% of $%.2f) buys 0 shares @ $%.2f",
                                        symbol, alloc, cfg.paper_risk_pct * 100,
                                        executor.cash + pos_val, execution_price,
                                    )
                                    print(
                                        f"  📄 SKIP: {symbol} — ${alloc:.2f} allocation "
                                        f"({cfg.paper_risk_pct*100:.0f}% risk) can't buy "
                                        f"1 share @ ${execution_price:,.2f}"
                                    )
                                if shares > 0:
                                    if _rule_buy:
                                        reason = (f"RULE BUY rsi={rule_v.rsi:.0f} "
                                                  f"adx={rule_v.adx:.0f}"
                                                  if rule_v.rsi is not None and rule_v.adx is not None
                                                  else "RULE BUY")
                                        # AI shadow vote: record what the advisor
                                        # thought at this exact moment. After ~30
                                        # trades, compare outcomes where the AI
                                        # agreed vs disagreed — if agreement is
                                        # predictive, the AI earns veto power.
                                        if verdict is not None and verdict.confidence > 0:
                                            reason += f" | ai={verdict.signal}{verdict.confidence}"
                                        else:
                                            reason += " | ai=NONE"
                                    else:
                                        reason = f"BUY {verdict.confidence}% {verdict.trading_style}"
                                    order  = executor.buy(
                                        symbol, shares, execution_price, reason=reason,
                                        confidence=verdict.confidence if verdict else 0,
                                        candle_close=px, live_price=raw_live_price,
                                    )
                                    if order.status == OrderStatus.FILLED:
                                        total = round(shares * execution_price, 2)
                                        print(f"  📄 PAPER BUY:  {symbol}  {shares} shares")
                                        print(f"                 @ ${execution_price:,.2f} = ${total:,.2f}")
                                        notifier.fill("BUY", symbol, shares,
                                                      execution_price, total,
                                                      reason=reason)
                                        print(f"                 Cash remaining: ${executor.cash:,.2f}")
                                    else:
                                        print(f"  📄 REJECTED:   {symbol} — {order.reject_reason}")
                    else:  # SELL
                        held = executor.position(symbol)
                        if held > 0:
                            avg = executor.avg_cost(symbol)
                            if _rule_sell:
                                reason = "RULE SELL trend-exit"
                                if verdict is not None and verdict.confidence > 0:
                                    reason += f" | ai={verdict.signal}{verdict.confidence}"
                                else:
                                    reason += " | ai=NONE"
                            else:
                                reason = f"SELL {verdict.confidence}% {verdict.trading_style}"
                                if _exit_dec is not None and _exit_dec.reason.startswith("SELL streak"):
                                    reason += " [streak]"
                            order  = executor.sell(symbol, held, execution_price, reason=reason)
                            if order.status == OrderStatus.FILLED:
                                exit_policy.clear(symbol)
                                proceeds  = round(held * execution_price, 2)
                                trade_pnl = round((execution_price - avg) * held, 2)
                                notifier.fill("SELL", symbol, held, execution_price,
                                              proceeds, pnl=trade_pnl, reason=reason)
                                pnl_pct   = round((execution_price - avg) / avg * 100, 1) if avg else 0.0
                                logger.info(
                                    "EXIT (%s): %s %.4f sh @ %.2f — %s",
                                    "rule" if _rule_sell else "ai",
                                    symbol, held, execution_price,
                                    reason if _rule_sell else (_exit_dec.reason if _exit_dec else reason),
                                )
                                print(f"  📄 PAPER SELL: {symbol}  {held:.4f} shares")
                                print(f"                 @ ${execution_price:,.2f} = ${proceeds:,.2f}")
                                print(f"                 Realized P&L: {trade_pnl:+.2f} ({pnl_pct:+.1f}%)")
                                print(f"                 Cash remaining: ${executor.cash:,.2f}")
                elif (
                    executor is not None
                    and verdict is not None
                    and verdict.signal == "SELL"
                    and _held_qty > 0
                    and _exit_dec is not None
                ):
                    # Held position with an AI SELL verdict that did NOT clear
                    # the exit bars — say so loudly instead of silently ignoring
                    # it (the AC.TO 58-60% incident, 2026-07-10).
                    logger.info("AI SELL (advisory) below exit bar: %s — %s", symbol, _exit_dec.reason)
                    print(f"  ⏳ HELD, not exiting: {symbol} — AI advisory: {_exit_dec.reason}")

                # Build ScanResult for dashboard
                if report is not None:
                    macd_note: str | None = None
                    if macd_val:
                        ml, ms, _ = macd_val
                        if abs(ml - ms) < 0.001 * max(abs(ml), abs(ms), 0.01):
                            macd_note = "flat"
                        elif ml > ms:
                            macd_note = "bullish cross"
                        else:
                            macd_note = "bearish cross"

                    if verdict is None:
                        verdict = AIVerdict(
                            symbol=symbol, signal="HOLD", confidence=0,
                            target_price=None, stop_loss=None,
                            reasoning="AI disabled", trading_style="SWING",
                            timestamp=datetime.now(),
                            provider="unavailable",
                        )

                    scan_results.append(ScanResult(
                        symbol       = symbol,
                        company_name = get_company_name(symbol),
                        price        = data["last_candle"].close,
                        currency     = "CAD" if symbol.upper().endswith(".TO") else "USD",
                        rsi          = rsi_val,
                        trend        = trend_val,
                        macd_note    = macd_note,
                        research     = report,
                        verdict      = verdict,
                        source       = "watchlist" if symbol in cfg.watchlist else "universe",
                        rule_verdict = rule_v,
                        rule_whitelisted = symbol.upper() in _rule_whitelist,
                    ))

            except Exception as exc:
                print(f"  {symbol:<10}  ERROR: {exc}")
                logger.warning("scan failed for %s: %s", symbol, exc)
            print(f"  {'─' * 70}")

        # Batch sanity check — abort cycle if data feed is corrupted
        if not _check_price_uniformity(scan_results):
            print("  ⚠️  Corrupted data detected — skipping this cycle")
            continue

        # ── AI call summary ───────────────────────────────────────────────────
        if cfg.ai_enabled and ai_engine:
            print(f"  ── AI Summary {'─' * 39}")
            print(f"  ✅ nvidia_nim:   {_ai_nvidia_n} calls succeeded")
            if _ai_fallback_n:
                print(f"  ⚠️  openrouter:   {_ai_fallback_n} fallbacks used")
            if _ai_failed_n:
                print(f"  ❌ unavailable:   {_ai_failed_n} failed")
            print(f"  ⏱  Total AI time: {_ai_elapsed:.1f}s")
            print(f"  {'─' * 44}")

        # Build portfolio summary — paper executor takes precedence over static tracker
        portfolio_summary = None
        paper_summary     = None
        try:
            if executor is not None:
                portfolio_summary = executor.build_summary(scan_results)
                paper_summary     = executor.build_paper_summary(scan_results)
                executor.log_state({r.symbol: r.price for r in scan_results})
                executor.save_state()
            else:
                portfolio_summary = tracker.build_summary(scan_results)
        except Exception as exc:
            logger.warning("Portfolio build failed: %s", exc)

        # End-of-cycle paper portfolio summary
        if executor is not None:
            try:
                price_map_cycle = {r.symbol: r.price for r in scan_results}
                unr     = executor.unrealized_pnl(price_map_cycle)
                rea     = executor.realized_pnl()
                tv      = executor.total_value(price_map_cycle)
                ret_pct = (tv - executor.starting_cash) / executor.starting_cash * 100 if executor.starting_cash else 0.0
                open_syms = list(executor.positions_snapshot().keys())
                unr_s   = "+" if unr >= 0 else ""
                rea_s   = "+" if rea >= 0 else ""
                ret_s   = "+" if ret_pct >= 0 else ""
                syms_str = ", ".join(open_syms) if open_syms else "none"
                print(f"  {'─' * 44}")
                print(f"  📄 Paper Portfolio Summary")
                print(f"  💵 Cash:           ${executor.cash:>10,.2f}")
                print(f"  📦 Open positions: {len(open_syms)} ({syms_str})")
                print(f"  📈 Unrealized P&L: {unr_s}${unr:,.2f}")
                print(f"  ✅ Realized P&L:   {rea_s}${rea:,.2f}")
                print(f"  💼 Total Value:    ${tv:>10,.2f}  ({ret_s}{ret_pct:.2f}%)")
                print(f"  {'─' * 44}")
            except Exception as exc:
                logger.warning("Paper summary print failed: %s", exc)

        # Evaluate and deliver alerts
        alerts = []
        try:
            _held = executor.positions_snapshot() if executor is not None else None
            alerts = evaluator.evaluate(scan_results, held_positions=_held)
            notifier.notify(alerts)
            logger.info("Alerts: %d triggered this cycle", len(alerts))
        except Exception as exc:
            logger.warning("Alert evaluation/notification failed: %s", exc)

        # Write dashboard
        try:
            try:
                _gate_status = LiveTradingGate().get_gate_status()
            except Exception as _ge:
                logger.debug("Gate status check failed: %s", _ge)
                _gate_status = None

            # Current per-trade allocation — lets the rule strip flag
            # whitelisted BUYs that would SIZE_SKIP (1 share > allocation).
            _snap_r     = executor.positions_snapshot()
            _pos_val_r  = sum(sh * co for sh, co in _snap_r.values())
            _buy_alloc  = (executor.cash + _pos_val_r) * cfg.paper_risk_pct

            renderer.render(
                scan_results, fear_greed_data, portfolio_summary, alerts,
                paper         = paper_summary,
                ai_stats      = {
                    "nvidia":   _ai_nvidia_n,
                    "fallback": _ai_fallback_n,
                    "failed":   _ai_failed_n,
                    "elapsed":  _ai_elapsed,
                },
                market_status = market_status,
                loop_mode     = mode,
                gate_status   = _gate_status,
                exit_bars     = {
                    "buy":           cfg.paper_min_confidence,
                    "sell":          cfg.paper_min_confidence_sell,
                    "streak_conf":   cfg.paper_sell_streak_min_conf,
                    "streak_cycles": cfg.paper_sell_streak_cycles,
                    "rule_mode":     cfg.rule_trading_enabled,
                },
                buy_alloc     = _buy_alloc,
            )
        except Exception as exc:
            logger.warning("Dashboard render failed: %s", exc)

        print()
        time.sleep(cfg.loop_interval)
    except KeyboardInterrupt:
        print("\n⛔ Stock Bot stopped. Goodbye!")


def _send_crash_alert(tb: str) -> None:
    """Last-gasp Telegram alert on fatal crash (synchronous — the process is
    about to die). Never raises. Mirrors bot/main.py's handler."""
    try:
        from stock_bot.alerts.notifier import _make_telegram
        alerter = _make_telegram()
        if alerter is not None:
            alerter.send_now(f"💀 Stock bot CRASHED\n\n{tb[-900:]}")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback as _tb_mod
        _tb = _tb_mod.format_exc()
        logger.critical("FATAL CRASH — stock bot exiting:\n%s", _tb)
        _send_crash_alert(_tb)
        raise
