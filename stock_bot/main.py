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

import dataclasses
import json
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
from stock_bot.data.price_feed    import (
    fetch_candles, reset_price_cache, get_sector,
    get_usd_cad_rate, is_cad_symbol,
)
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
from stock_bot.alerts.evaluator     import AlertEvaluator
from stock_bot.alerts.notifier      import AlertNotifier
from stock_bot.execution.paper      import StockPaperExecutor
from stock_bot.execution.base       import OrderStatus
from stock_bot.execution.exit_policy import ExitPolicy
from stock_bot.strategy.rules       import rule_signal
from stock_bot.risk.correlation     import CORRELATION_THRESHOLD, fetch_correlation_from_closes
from stock_bot.risk.macro_calendar  import is_macro_blackout, parse_user_event_dates
from stock_bot.risk.vix_crisis      import is_vix_crisis
from stock_bot.fast_validator       import FastValidator
from stock_bot.analysis.accuracy_tracker import LiveTradingGate
from stock_bot.analysis.checkpoint_tracker import compute_checkpoint_status

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
      {"screened": True, ...} — screener rejected this symbol. "screen_reason"
                                 is only populated when the rejection is the
                                 in-distribution ATR%/liquidity filter
                                 (stock_bot/data/screener.py) — surfaced on
                                 the dashboard; the pre-existing "boring
                                 stock" rejections stay silent as before.
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

    if symbol not in watchlist_set and screener is not None:
        _screen_ok, _screen_reason = screener.screen(symbol, candles)
        if not _screen_ok:
            return {"screened": True, "price": closes[-1], "screen_reason": _screen_reason}

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


def _check_correlation_gate(
    symbol: str, executor, price_data: dict,
) -> tuple[str | None, float | None]:
    """
    Returns (peer_symbol, correlation) for the first currently-open position
    whose 30-day daily-return correlation with `symbol` exceeds
    CORRELATION_THRESHOLD (0.70) — or (None, None) when nothing crosses it.

    Fail-open, like the crypto bot's equivalent gate: a peer with no candle
    data in this cycle's price_data (e.g. a held symbol dropped from the
    watchlist) is skipped rather than blocking the BUY. Zero extra network
    calls — reuses candle closes this scan cycle already fetched.
    """
    my_data = price_data.get(symbol.upper())
    if not my_data or not my_data.get("candles"):
        return None, None
    my_closes = [c.close for c in my_data["candles"]]
    for peer in executor.positions_snapshot():
        if peer.upper() == symbol.upper():
            continue
        peer_data = price_data.get(peer.upper())
        if not peer_data or not peer_data.get("candles"):
            continue
        peer_closes = [c.close for c in peer_data["candles"]]
        corr = fetch_correlation_from_closes(my_closes, peer_closes)
        if corr is not None and corr > CORRELATION_THRESHOLD:
            return peer, corr
    return None, None


def _is_macro_event_blackout(cfg) -> tuple[bool, date | None]:
    """
    Market-wide blackout — unlike earnings blackout, not per-symbol. Blocks
    ALL new BUYs within cfg.macro_blackout_days of a jobs-report date or a
    user-supplied FOMC/CPI/GDP date (MACRO_EVENT_DATES in stock_bot/.env).
    Fail-open on a bad config value, same philosophy as the earnings gate.
    """
    try:
        user_dates = parse_user_event_dates(cfg.macro_event_dates_str)
        return is_macro_blackout(date.today(), cfg.macro_blackout_days, user_dates)
    except Exception as exc:
        logger.warning("Macro event blackout check failed (%s) — allowing trade", exc)
        return False, None


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
    _checked = 0
    _priced  = 0
    for symbol, (shares, avg_cost) in list(executor.positions_snapshot().items()):
        if shares <= 0:
            continue
        _checked += 1
        live = get_live_price(symbol)
        if live is None:
            logger.debug("SL/TP check: skipping %s — no live price", symbol)
            continue
        _priced += 1
        pct_change = (live - avg_cost) / avg_cost
        # Per-position ATR stop overrides the flat baseline when one was set
        # at entry (PAPER_ATR_SIZING_ENABLED) — must match what sizing used,
        # or the risk cap computed at entry time means nothing at exit time.
        _effective_stop_pct = (
            executor.get_position_stop_pct(symbol, cfg.paper_stop_loss_pct)
            if hasattr(executor, "get_position_stop_pct")
            else cfg.paper_stop_loss_pct
        )
        if pct_change <= -abs(_effective_stop_pct):
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

    # INFO-level (not debug) on purpose — added 2026-08-06 after a yfinance
    # outage (fetch_candles/yf.download, "possibly delisted" for real tickers)
    # broke the main scan loop for a full trading day and there was no direct
    # evidence either way whether this watcher's separate get_live_price()
    # path (fast_info, a different yfinance endpoint) was also blind — only
    # per-symbol failures logged, at debug, which isn't captured anywhere.
    # This one line, every 30s during market hours, is the audit trail: if a
    # future outage shows 0/N priced here, that's the smoking gun; a steady
    # N/N proves this watcher stayed healthy independent of Phase 1's fate.
    if _checked > 0:
        logger.info("SL/TP check: %d/%d positions priced", _priced, _checked)


def _mark_positions_to_market(executor, price_data: dict[str, dict | None]) -> None:
    """
    Re-mark open positions to this scan cycle's live prices, before any
    buy/sell decision runs.

    Without this, the daily-loss breaker (_is_daily_loss_tripped) checks
    drawdown against whatever price was current at the last fill — stale
    for days between trades — and can miss real intraday drawdown on a held
    position that moves with no new fill this cycle. price_data is Phase 1's
    {symbol: _fetch_symbol_data(...) result} map, which already covers held
    positions (they're always included in cycle_symbols).
    """
    if executor is None:
        return
    live_price_map = {
        sym: pd["price"]
        for sym, pd in price_data.items()
        if isinstance(pd, dict) and pd.get("price")
    }
    executor.refresh_position_marks(live_price_map)


def _update_ai_health(
    ai_health: dict,
    success: bool,
    consecutive_failures: int,
    threshold: int,
    notifier,
    detail: str = "",
) -> int:
    """One AI-health evaluation, called once per scan cycle that actually
    attempted at least one AI call (skip calling this on a cycle where
    everything was gated out — e.g. all-RANGING or market closed — that's
    not a signal either way).

    Mirrors bot/main.py's `_update_auth_health()` (built 2026-08-18 after
    the Kraken auth outage went undetected for days). Added 2026-08-25 to
    close the stock-bot analog of that same gap: the AI provider
    (nvidia_nim) has degraded three separate times on this project — each
    one only ever caught by manually testing the API by hand, never by
    anything the bot itself surfaced. `_ai_failed_n`/`_ai_nvidia_n`/
    `_ai_fallback_n` were already being computed every cycle (Phase 3, main
    loop) but only ever printed to the console, never alerted.

    Deliberately NOT wired into either heartbeat's healthy_fn — unlike the
    Kraken auth case, AI here is advisory-only (RULE_TRADING_ENABLED=true
    means the rule engine trades regardless of AI's state), so a degraded
    AI provider should not page "the bot is down." It gets its own
    edge-triggered ops_alert instead, kept distinct from a real outage.

    Alerts once per ok->failing / failing->ok transition (edge-triggered,
    not every cycle). Returns the new consecutive_failures count: 0 on
    success, or after `threshold` consecutive fully-failed cycles have just
    been evaluated (whether or not that evaluation produced a fresh alert).
    """
    if success:
        if not ai_health["ok"]:
            ai_health["ok"] = True
            notifier.ops_alert(
                "AI provider RECOVERED",
                "nvidia_nim (or its fallback) is producing verdicts again. "
                "Rule-based trading was unaffected throughout — this only "
                "restores the AI shadow-vote data.",
            )
        return 0

    consecutive_failures += 1
    if consecutive_failures >= threshold:
        logger.warning(
            "AI health: %d consecutive fully-failed scan cycles — "
            "nvidia_nim (and any configured fallback) unreachable",
            consecutive_failures,
        )
        if ai_health["ok"]:
            ai_health["ok"] = False
            notifier.ops_alert(
                "AI provider degraded",
                f"{consecutive_failures} consecutive scan cycles with zero "
                f"successful AI calls{f' ({detail})' if detail else ''}. "
                "Rule-based BUY/SELL signals are unaffected — this only "
                "means the AI shadow-vote log is going dark. If this "
                "persists, check the provider/credentials the same way the "
                "prior nvidia_nim degradations were diagnosed.",
            )
        consecutive_failures = 0
    return consecutive_failures


_BLOCKED_BUY_ABSENT_CYCLES_TO_CLEAR = 3   # a symbol must be gone this many cycles before a
                                          # reappearance re-alerts — debounces marginal
                                          # setups that flap BUY/HOLD cycle to cycle


def _evaluate_blocked_rule_buys_alert(current: dict, state: dict, notifier) -> None:
    """End-of-cycle ops_alert for rule BUY signals a gate held — debounced.

    `current` is {symbol: gate_label} built during the scan loop. `state` carries
    `state['alerted']` = {symbol: {'gate': str, 'absent': int}} across cycles.

    Alerts (one digest of the current set) only when a symbol is NEWLY blocked or
    its blocking gate changed. A symbol dropping OUT of the set does NOT alert and
    is NOT forgotten immediately — it must be absent `_BLOCKED_BUY_ABSENT_CYCLES_TO_CLEAR`
    consecutive cycles before it's cleared, so a marginal setup that flaps
    BUY↔HOLD every other cycle (2026-08-27: BNS/GM near the MAX_EXPOSURE ceiling)
    alerts once, not on every toggle. One "all clear" when the set fully empties.
    """
    alerted: dict = state.setdefault("alerted", {})

    fresh = []   # symbols that should trigger an alert this cycle
    for sym, gate in current.items():
        rec = alerted.get(sym)
        if rec is None or rec["gate"] != gate:
            fresh.append(sym)
        alerted[sym] = {"gate": gate, "absent": 0}

    # Age out symbols no longer blocked; only truly forget after N absent cycles.
    for sym in list(alerted):
        if sym in current:
            continue
        alerted[sym]["absent"] += 1
        if alerted[sym]["absent"] >= _BLOCKED_BUY_ABSENT_CYCLES_TO_CLEAR:
            del alerted[sym]

    if not alerted and not current and state.pop("_had_blocks", False):
        notifier.ops_alert(
            "Rule BUY signals no longer blocked",
            "Every rule BUY signal that a gate was holding has cleared the gate "
            "or stopped signalling.",
        )
        return

    if current:
        state["_had_blocks"] = True
    if not fresh:
        return

    lines = [f"• {sym}: {gate}" for sym, gate in sorted(current.items())]
    notifier.ops_alert(
        f"{len(current)} rule BUY signal(s) blocked by a gate",
        "The rules wanted to enter these but a gate held them "
        "(no further alert unless a new symbol is blocked or its gate changes):\n"
        + "\n".join(lines),
    )


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

_last_universe_refresh = None                                        # datetime | None — last SUCCESSFUL daily refresh
_last_universe_attempt = None                                        # datetime | None — throttles failed retries
_UNIVERSE_REFRESH_HOUR = int(_os.getenv("UNIVERSE_REFRESH_HOUR", "16"))  # legacy — refresh is now first-LIVE-cycle-of-day, not a fixed hour
_UNIVERSE_RETRY_COOLDOWN_S = 900   # min seconds between top-movers pre_filter attempts while it keeps failing
_MOVERS_STATE_FILE = _os.path.join(_os.path.dirname(__file__), "universe_movers.json")


def _load_persisted_movers(today_iso: str) -> list[str]:
    """Today's persisted top-movers list, or [] if the file is missing, stale
    (different date), or unreadable. Lets a restart keep the wider scan universe
    instead of dropping back to watchlist-only until the next daily refresh."""
    try:
        with open(_MOVERS_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == today_iso and isinstance(data.get("movers"), list):
            return [str(s) for s in data["movers"]]
    except (OSError, ValueError, TypeError):
        pass
    return []


def _persist_movers(today_iso: str, movers: list[str]) -> None:
    """Best-effort — a write failure never blocks the scan loop."""
    try:
        from bot.atomic_json import atomic_write_json
        atomic_write_json(_MOVERS_STATE_FILE, {"date": today_iso, "movers": list(movers)})
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("Could not persist universe movers: %s", exc)


def _handle_sigterm(sig, frame):
    # Route SIGTERM (launchd/systemd/kill default) into the existing
    # KeyboardInterrupt path so the loop exits gracefully, not mid-write.
    raise KeyboardInterrupt


def run() -> None:
    global _last_universe_refresh, _last_universe_attempt
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

    # Restore today's top-movers across a restart so the bot doesn't drop back to
    # watchlist-only until the next daily refresh (it restarts often — VPS deploys,
    # config changes, incident recovery).
    if _universe is not None:
        _today_et = datetime.now(_pytz.timezone("US/Eastern")).date().isoformat()
        top_movers = _load_persisted_movers(_today_et)
        if top_movers:
            _last_universe_refresh = datetime.now(_pytz.timezone("US/Eastern"))
            logger.info("Universe: restored %d persisted movers for %s", len(top_movers), _today_et)

    all_symbols = list(dict.fromkeys(watchlist_symbols + top_movers))

    screener = StockScreener() if cfg.screener_enabled else None

    # Initialise components once at startup
    ai_engine = AIEngine() if cfg.ai_enabled else None
    renderer  = DashboardRenderer(loop_interval=cfg.loop_interval)
    evaluator = AlertEvaluator()
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
        executor.set_weekly_loss_limit(cfg.paper_weekly_loss_pct)
        executor.set_drawdown_limits(
            cfg.paper_drawdown_warning_pct,
            cfg.paper_drawdown_halt_pct,
            cfg.paper_kill_switch_pct,
        )
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
    # NOTE: RULE_WHITELIST no longer gates rule-based BUYs (removed 2026-08-23 —
    # see CLAUDE_HISTORY.md). cfg.rule_whitelist_str is still loaded and still
    # consulted by LiveTradingGate.check_gate1() (stock_bot/analysis/
    # accuracy_tracker.py) for IBKR live-trading readiness — an unrelated,
    # code-enforced gate this change does not touch.

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
        print(f"  Universe     : S&P500 + TSX60 → top {cfg.universe_size} movers (refreshes on the first scan cycle each day)")
        print(f"  Top Movers   : {', '.join(top_movers) if top_movers else '(restoring / first refresh pending)'}")
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
    _dd_warning_active = False

    def _check_drawdown_warning() -> None:
        # Non-blocking tier (10% drawdown from peak by default) — the
        # blocking tiers (weekly loss, 15% halt, 20% kill switch) live inside
        # the executor's buy() gate and need no alert wiring here; this is
        # the one tier that never rejects an order, so it has no reject_reason
        # to piggyback an alert on and gets its own explicit check instead.
        nonlocal _dd_warning_active
        if executor is None or not hasattr(executor, "drawdown_status"):
            return
        status = executor.drawdown_status()
        if status["warning"] and not _dd_warning_active:
            _dd_warning_active = True
            notifier.ops_alert(
                "Drawdown warning",
                f"Portfolio down {status['drawdown_pct']:.1%} from peak "
                f"${status['peak_equity']:,.2f} (current ${status['current_equity']:,.2f}). "
                f"Trading continues — this is a non-blocking warning tier.",
            )
        elif not status["warning"]:
            _dd_warning_active = False

    def _sl_tp_watcher() -> None:
        while True:
            try:
                if _get_market_status()["any_open"]:
                    _check_open_positions_sl_tp(executor, cfg, notifier)
                    _check_drawdown_warning()
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
    # HEARTBEAT_URL: process alive AND the main scan loop is still making
    # progress (2026-07-22 incident: the swing-book thread hung silently for
    # 5+ hours with no exception — process-alive alone can't catch that;
    # _liveness.touch() is called after every symbol's AI call and once per
    # full cycle as a fallback — see bot/alerts/liveness.py).
    # HEARTBEAT_TWS_URL: pinged only while the IBKR connection is up, so
    # "TWS logged off" alerts separately from "bot died".  Both off when unset.
    from bot.alerts.heartbeat import start_heartbeat_thread
    from bot.alerts.liveness import LivenessTracker
    _liveness = LivenessTracker()
    _LIVENESS_MAX_STALE_S = 1800   # 30 min — AI calls have been observed taking ~13 min

    # AI provider health (see _update_ai_health docstring) — tracked across
    # cycles, evaluated once per cycle that actually attempted an AI call.
    _ai_health = {"ok": True}
    _ai_consecutive_failures = 0
    _AI_HEALTH_THRESHOLD = 3   # consecutive fully-failed cycles before alerting
    # Blocked rule-BUY digest state — {symbol: gate_label} last reported, so the
    # end-of-cycle ops_alert only fires when the set changes (see
    # _evaluate_blocked_rule_buys_alert).
    _blocked_rule_buys_state: dict = {}   # {alerted: {sym: {gate, absent}}, _had_blocks: bool}
    _ibkr_sync_ok_prev: bool = True   # edge-trigger for the IBKR data-sync alert
    _ibkr_csv_ok_prev:  bool = True   # edge-trigger for the ibkr_trades.csv write alert
    _hb_interval = int(os.getenv("HEARTBEAT_INTERVAL_S", "60"))
    start_heartbeat_thread(
        os.getenv("HEARTBEAT_URL", ""),
        interval_s=_hb_interval,
        healthy_fn=lambda: _liveness.is_alive(_LIVENESS_MAX_STALE_S),
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

        # Fetch VIX once per cycle for crisis-mode gate (same pattern as SPY above).
        # A fetch failure fails open (crisis mode off) — same philosophy as every
        # other gate in this codebase: missing data allows trading, doesn't block it.
        _vix_now: float | None = None
        if cfg.vix_crisis_enabled:
            _vix_raw = fetch_with_retry(
                lambda: yf.download(
                    "^VIX", interval="1d", period="5d",
                    auto_adjust=True, actions=False, progress=False,
                ),
                label="VIX:crisis",
            )
            if _vix_raw is not None and not _vix_raw.empty:
                if hasattr(_vix_raw.columns, "nlevels") and _vix_raw.columns.nlevels > 1:
                    _vix_raw.columns = [c[0] if isinstance(c, tuple) else c for c in _vix_raw.columns]
                _vix_closes = [float(v) for v in _vix_raw["Close"].dropna().tolist()]
                _vix_now = _vix_closes[-1] if _vix_closes else None
        _cycle_crisis_mode = cfg.vix_crisis_enabled and is_vix_crisis(_vix_now, cfg.vix_crisis_threshold)

        _regime_icons = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡", "UNKNOWN": "⚪"}
        print(f"  ── Scan #{tick:04d}  {now} {'─' * 30}")
        print(f"  😨 Market: Fear & Greed {fear_greed_data.score} — {fear_greed_data.label}  |  📈 Trends: {market_trends_score}/100")
        _vix_str = f"{_vix_now:.1f}" if _vix_now is not None else "n/a"
        _vix_icon = "🚨" if _cycle_crisis_mode else ("⚪" if _vix_now is None else "🟢")
        print(f"  Regime: {_cycle_regime} {_regime_icons.get(_cycle_regime, '⚪')}   VIX: {_vix_str} {_vix_icon}")
        print(f"  {'Symbol':<10}  {'Price':>10}  {'RSI':^7}  {'Trend':<10}  {'ADX':^13}  MACD")
        print(f"  {'─'*10}  {'─'*10}  {'─'*7}  {'─'*10}  {'─'*13}  {'─'*30}")

        # Refresh the top-movers universe once per trading day, on the first
        # LIVE scan cycle of the day.
        #
        # This used to be gated on the ET clock hour matching
        # _UNIVERSE_REFRESH_HOUR (16), which was UNREACHABLE: this block only
        # runs in LIVE mode (market open), but by 16:00 ET the market is closed
        # and the loop is already in AFTER_HOURS mode and has `continue`d.
        # Result: the refresh never fired and the bot ran watchlist-only
        # indefinitely (found 2026-08-27 — 179 "waiting" log lines, 0
        # "refreshed", across 3 days). pre_filter ranks on 30-day daily bars /
        # 5-day momentum / 20-day avg volume, so a partial current-day candle is
        # immaterial and a mid-session refresh is fine.
        if cfg.universe_enabled and _universe is not None:
            _needs_refresh = (_last_universe_refresh is None
                              or _last_universe_refresh.date() != now_et.date())
            _retry_ok = (_last_universe_attempt is None
                         or (now_et - _last_universe_attempt).total_seconds() >= _UNIVERSE_RETRY_COOLDOWN_S)
            if _needs_refresh and _retry_ok:
                _last_universe_attempt = now_et
                raw_symbols = _universe.get_universe()
                _fresh = _universe.pre_filter(raw_symbols, cfg.universe_size, market_status=market_status)
                _fallback_slice = list(_UNIVERSE_FALLBACK[:cfg.universe_size])
                if _fresh and _fresh != _fallback_slice:
                    top_movers  = _fresh
                    all_symbols = list(dict.fromkeys(watchlist_symbols + top_movers))
                    _last_universe_refresh = now_et
                    _persist_movers(now_et.date().isoformat(), top_movers)
                    logger.info("Universe refreshed: %d movers — %s",
                                len(top_movers), ", ".join(top_movers))
                    print(f"  Universe refreshed: {len(top_movers)} movers")
                    print(f"  My Watchlist : {', '.join(watchlist_symbols)}")
                    print(f"  Top Movers   : {', '.join(top_movers)}")
                else:
                    # Keep whatever top_movers we already had (persisted or from a
                    # prior success) — a transient pre_filter failure must not wipe
                    # the scan universe. Retries are throttled by _retry_ok above.
                    logger.warning(
                        "Universe refresh returned no/fallback symbols — keeping %d "
                        "existing movers, retry in %ds", len(top_movers), _UNIVERSE_RETRY_COOLDOWN_S
                    )

        watchlist_set = set(cfg.watchlist)
        # Held positions must always stay in the scan and bypass the screener:
        # a symbol that rotates out of the universe (DLTR, Jul 2026) otherwise
        # gets no price refresh and no AI verdict — the book can never exit it.
        held_symbols  = list(executor.positions_snapshot().keys()) if executor else []
        cycle_symbols = list(dict.fromkeys(all_symbols + held_symbols))
        watchlist_set |= set(held_symbols)
        scan_results: list[ScanResult] = []
        screen_skips: list[dict]       = []   # in-distribution filter rejections — dashboard visibility
        # {symbol: GATE_LABEL} for rule BUY signals a gate held this cycle —
        # end-of-cycle edge-triggered ops_alert so "the rules wanted in but X
        # blocked it" isn't print()-only (the recurring "why didn't it buy Y"
        # question). Parallel to the crypto bot's _evaluate_blocked_buy_alert.
        _blocked_rule_buys: dict[str, str] = {}

        # Reset per-cycle state
        _fv_earnings_blocked = set()
        reset_price_cache()

        # ── Phase 1: prices + indicators (all symbols, parallel) ───────────
        # Per-symbol fetch failures are already logged individually below.
        # These two guards catch cycle-level failure: the fetch phase itself
        # raising (ThreadPoolExecutor/orchestration failure), and a "clean"
        # completion where every symbol failed (total outage / global rate
        # limit) — previously both left the loop silently completing an
        # empty cycle and going back to sleep, undetectable without lsof.
        price_data: dict[str, dict | None] = {}
        try:
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
        except Exception as exc:
            logger.error(
                "cycle %d failed: price-fetch phase raised %s: %s",
                tick, type(exc).__name__, exc,
            )
            print(f"  ⚠️  cycle {tick} failed during price fetch: {exc} — skipping to next cycle")
            notifier.ops_alert(
                "Price-fetch cycle failed",
                f"Cycle {tick} raised {type(exc).__name__}: {exc}",
            )
            time.sleep(cfg.loop_interval)
            continue

        if cycle_symbols and not any(v is not None for v in price_data.values()):
            logger.error(
                "cycle %d failed: 0/%d symbols returned data — likely a total fetch outage",
                tick, len(cycle_symbols),
            )
            print(f"  ⚠️  cycle {tick} failed: 0/{len(cycle_symbols)} symbols returned data — skipping cycle")
            notifier.ops_alert(
                "Price-fetch cycle failed",
                f"Cycle {tick}: 0/{len(cycle_symbols)} symbols returned data — likely a total fetch outage",
            )
            time.sleep(cfg.loop_interval)
            continue

        active_symbols = [
            s for s in cycle_symbols
            if isinstance(price_data.get(s), dict) and not price_data[s].get("screened")
        ]

        _mark_positions_to_market(executor, price_data)

        # IBKR TWS-query health — edge-triggered ops_alert if accountValues()/
        # positions() start failing (executor now serves last-good cache, but
        # stale cash/positions shouldn't be silent — see IBKRExecutor._note_sync).
        _sync_ok_now = getattr(executor, "sync_healthy", True)
        if _sync_ok_now != _ibkr_sync_ok_prev:
            _ibkr_sync_ok_prev = _sync_ok_now
            if _sync_ok_now:
                notifier.ops_alert(
                    "IBKR data sync recovered",
                    "accountValues()/positions() are responding again — cash and "
                    "the position book are live rather than cached.",
                )
            else:
                notifier.ops_alert(
                    "IBKR data sync failing",
                    "TWS is not answering accountValues()/positions(). The bot is "
                    "running on the last-good cached cash + position book — trades "
                    "still gate correctly but on possibly-stale figures. Check TWS.",
                )

        # ibkr_trades.csv write health — a buffered (unwritten) filled-trade row
        # means the readiness gate under-counts until the disk recovers.
        _csv_ok_now = getattr(executor, "csv_write_healthy", True)
        if _csv_ok_now != _ibkr_csv_ok_prev:
            _ibkr_csv_ok_prev = _csv_ok_now
            if _csv_ok_now:
                notifier.ops_alert(
                    "ibkr_trades.csv writing again",
                    "Buffered trade rows have been flushed to disk — the trade "
                    "log and readiness gate are complete again.",
                )
            else:
                notifier.ops_alert(
                    "ibkr_trades.csv write failing",
                    "A real filled trade could not be written to ibkr_trades.csv "
                    "(disk full / path gone). The row is buffered in memory and "
                    "retried each fill, but it's lost on a restart and the "
                    "readiness gate under-counts until this clears. Check disk.",
                )

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
                _liveness.touch()
        _ai_elapsed    = time.time() - _ai_start
        _ai_primary    = getattr(ai_engine, "_primary_provider", "nvidia_nim")
        # Verdicts from the configured primary provider. (Name kept as
        # `_ai_nvidia_n` for the dashboard/test that read it — it's a count now,
        # the provider is `_ai_primary`.)
        _ai_nvidia_n   = sum(1 for v in ai_verdicts.values() if v.provider == _ai_primary)
        _ai_failed_n   = sum(1 for v in ai_verdicts.values() if v.provider in ("unavailable", "unknown"))
        # A non-primary provider that actually answered = a failover verdict.
        # "skipped" is gated-out, not attempted.
        _ai_fallback_n = sum(
            1 for v in ai_verdicts.values()
            if v.provider not in (_ai_primary, "unavailable", "unknown", "skipped")
        )
        _ai_attempted_n = _ai_nvidia_n + _ai_fallback_n + _ai_failed_n
        if _ai_attempted_n > 0:
            # Healthy = the MAJORITY of attempted calls produced a real verdict.
            # 2026-08-27: nemotron parse-failed ~75% of calls but a handful
            # succeeded, so an "any success = healthy" check stayed silent
            # through a broken model.
            _ai_ok_n = _ai_nvidia_n + _ai_fallback_n
            _ai_consecutive_failures = _update_ai_health(
                _ai_health, _ai_ok_n * 2 >= _ai_attempted_n,
                _ai_consecutive_failures, _AI_HEALTH_THRESHOLD, notifier,
                detail=f"{_ai_failed_n}/{_ai_attempted_n} calls failed this cycle",
            )

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
                    _reason = data.get("screen_reason")
                    if _reason:
                        # In-distribution ATR%/liquidity rejection — visible, not
                        # just dropped (logs/HISTORY: whitelist gate removed
                        # 2026-08-23, this filter is the replacement safety net).
                        print(f"  {symbol:<10}  ${data['price']:>10,.2f}  — {_reason}")
                        logger.warning(_reason)
                        screen_skips.append({
                            "symbol": symbol, "price": data["price"], "reason": _reason,
                        })
                    else:
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
                    # Mirrored to the log file (added 2026-08-26) — this line
                    # used to be console-only (print()), so "why didn't it
                    # buy symbol X today" was only answerable from whichever
                    # terminal happened to have the scrollback, not from
                    # logs/stock_bot.log. Symbol is included here (the print
                    # above relies on a symbol header printed just before it
                    # in the console, which doesn't carry over to a log file
                    # read out of that visual order).
                    logger.info(
                        "RULES [%s]: %s%s", symbol, '  '.join(_rv_parts), _rv_note,
                    )

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
                              and rule_v.warmed_up)
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
                        _macro_blocked, _macro_event = _is_macro_event_blackout(cfg)
                        if _macro_blocked:
                            logger.info(
                                "MACRO BLACKOUT: %s blocked — event on %s (within %d days)",
                                symbol, _macro_event, cfg.macro_blackout_days,
                            )
                            print(
                                f"  🚫 MACRO BLACKOUT: {symbol} — "
                                f"event on {_macro_event} | {_trigger} blocked"
                            )
                            print(f"  {'─' * 70}")
                            if _rule_buy:
                                _blocked_rule_buys[symbol] = "MACRO_BLACKOUT"
                            # Market-wide, not per-symbol — reuses the earnings-block
                            # set so the swing book also sits out (see its comment above).
                            _fv_earnings_blocked.add(symbol.upper())
                            continue
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
                            if _rule_buy:
                                _blocked_rule_buys[symbol] = "EARNINGS_BLACKOUT"
                            # Also block swing book from entering this symbol
                            _fv_earnings_blocked.add(symbol.upper())
                            continue
                        _regime_ok = True
                        if cfg.regime_filter_enabled and (not spy_closes or _cycle_regime != "BULL"):
                            logger.info("REGIME_SKIP: %s — market is %s", symbol, _cycle_regime)
                            print(f"  📛 REGIME_SKIP: {symbol} — market is {_cycle_regime}")
                            _regime_ok = False
                            if _rule_buy:
                                _blocked_rule_buys[symbol] = f"REGIME_SKIP (market {_cycle_regime})"
                        if _cycle_crisis_mode:
                            logger.info(
                                "VIX_CRISIS_SKIP: %s — VIX %.1f >= %.0f",
                                symbol, _vix_now, cfg.vix_crisis_threshold,
                            )
                            print(f"  🚨 VIX CRISIS: {symbol} — VIX {_vix_now:.1f} >= {cfg.vix_crisis_threshold:.0f} — no new BUYs")
                            _regime_ok = False
                            if _rule_buy:
                                _blocked_rule_buys[symbol] = f"VIX_CRISIS (VIX {_vix_now:.0f})"
                        _fv_occupied = fast_validator.state.open_symbols() if fast_validator else set()
                        if symbol.upper() in _fv_occupied:
                            logger.info(
                                "POSITION_SKIP: %s — already held in swing book (dual-exposure guard)",
                                symbol,
                            )
                            print(f"  📄 SKIP: {symbol} — held in swing book (no double exposure)")
                        elif _regime_ok and executor.position(symbol) == 0:
                            _price_map_now = {r.symbol: r.price for r in scan_results}
                            _corr_peer, _corr_val = _check_correlation_gate(symbol, executor, price_data)
                            # Account cash/exposure figures are CAD (the account's base
                            # currency — IBKRExecutor.cash reads IBKR's BASE/CAD row).
                            # US-listed share prices are USD. Without converting, a USD
                            # buy's target allocation was being spent as if $1 USD == $1
                            # CAD, silently running ~15-35% over the intended risk_pct
                            # (found live 2026-07-31 on RY: $842 USD spent against a
                            # $1,002 CAD target — actually ~$1,150+ CAD, ~23% not 20%).
                            #
                            # Computed here (before the exposure gate below), not inside
                            # the sizing branch, so check_exposure can project exposure
                            # AFTER this trade rather than only checking current state —
                            # a current-state-only check can't see a single large BUY
                            # blow past the cap in one shot; it only catches it on the
                            # *next* BUY attempt, once already over (found 2026-08-05).
                            fx_rate = get_usd_cad_rate()
                            snap    = executor.positions_snapshot()
                            pos_val = sum(
                                sh * co * (1.0 if is_cad_symbol(sym) else fx_rate)
                                for sym, (sh, co) in snap.items()
                            )
                            alloc   = (executor.cash + pos_val) * cfg.paper_risk_pct
                            if not executor.check_exposure(_price_map_now, pending_trade_value=alloc):
                                print(f"  📄 SKIP: {symbol} — max exposure ({cfg.paper_max_exposure_pct*100:.0f}%) reached")
                                if _rule_buy:
                                    _blocked_rule_buys[symbol] = "MAX_EXPOSURE"
                            elif len(executor.positions_snapshot()) >= cfg.paper_max_positions:
                                print(f"  📄 SKIP: {symbol} — max {cfg.paper_max_positions} positions reached")
                                if _rule_buy:
                                    _blocked_rule_buys[symbol] = "MAX_POSITIONS"
                            elif _corr_peer is not None:
                                logger.warning(
                                    "CORRELATION GATE: %s blocked — correlation %.2f with open %s",
                                    symbol, _corr_val, _corr_peer,
                                )
                                print(f"  📄 SKIP: {symbol} — correlation {_corr_val:.2f} with open {_corr_peer} (> {CORRELATION_THRESHOLD:.2f})")
                                if _rule_buy:
                                    _blocked_rule_buys[symbol] = f"CORRELATION ({_corr_val:.2f} with {_corr_peer})"
                            else:
                                price_cad = (
                                    execution_price if is_cad_symbol(symbol)
                                    else execution_price * fx_rate
                                )
                                shares  = int(alloc / price_cad) if price_cad > 0 else 0
                                # ATR-aware sizing (opt-in, PAPER_ATR_SIZING_ENABLED):
                                # cap shares so a stop at atr_sl_mult*ATR away never
                                # risks more $ than the flat-% baseline stop would,
                                # and remember that same ATR stop distance for THIS
                                # position so the exit check (below) uses it instead
                                # of the flat baseline — sizing and the actual stop
                                # must agree, or the risk cap here is meaningless.
                                _atr_stop_pct = None
                                if cfg.paper_atr_sizing_enabled:
                                    _atr_now = data.get("atr") if data else None
                                    if _atr_now and _atr_now > 0 and execution_price > 0:
                                        _atr_cad = (
                                            _atr_now if is_cad_symbol(symbol)
                                            else _atr_now * fx_rate
                                        )
                                        shares = cfg.calc_shares_atr_risk(
                                            executor.cash + pos_val, price_cad,
                                            _atr_cad, cfg.paper_atr_sl_mult,
                                            cfg.paper_stop_loss_pct,
                                        )
                                        _atr_stop_pct = min(
                                            (_atr_now * cfg.paper_atr_sl_mult) / execution_price,
                                            0.50,   # sanity cap — never a >50% stop
                                        )
                                if shares == 0 and execution_price > 0:
                                    logger.info(
                                        "SIZE_SKIP: %s — target allocation $%.2f CAD "
                                        "(%.0f%% of $%.2f CAD) buys 0 shares @ $%.2f "
                                        "(%.2f CAD)",
                                        symbol, alloc, cfg.paper_risk_pct * 100,
                                        executor.cash + pos_val, execution_price, price_cad,
                                    )
                                    print(
                                        f"  📄 SKIP: {symbol} — ${alloc:.2f} allocation "
                                        f"({cfg.paper_risk_pct*100:.0f}% risk) can't buy "
                                        f"1 share @ ${execution_price:,.2f}"
                                    )
                                    if _rule_buy:
                                        _blocked_rule_buys[symbol] = "SIZE_SKIP (allocation < 1 share)"
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
                                        if _atr_stop_pct is not None:
                                            executor.set_position_stop_pct(symbol, _atr_stop_pct)
                                            print(f"                 ATR stop: {_atr_stop_pct:.1%} (vs flat {cfg.paper_stop_loss_pct:.1%})")
                                        notifier.fill("BUY", symbol, shares,
                                                      execution_price, total,
                                                      reason=reason)
                                        print(f"                 Cash remaining: ${executor.cash:,.2f}")
                                    else:
                                        print(f"  📄 REJECTED:   {symbol} — {order.reject_reason}")
                                        notifier.ops_alert(
                                            "Order rejected",
                                            f"BUY {symbol} — {order.reject_reason}",
                                        )
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
                            else:
                                print(f"  📄 REJECTED:   {symbol} — {order.reject_reason}")
                                logger.warning(
                                    "SELL REJECTED %s — %s (position remains open, %.4f sh)",
                                    symbol, order.reject_reason, held,
                                )
                                notifier.ops_alert(
                                    "Order rejected",
                                    f"SELL {symbol} — {order.reject_reason} "
                                    f"— position remains open ({held:.4f} sh)",
                                )
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
            print(f"  ✅ {_ai_primary}:   {_ai_nvidia_n} calls succeeded")
            if _ai_fallback_n:
                _fb_prov = next(
                    (v.provider for v in ai_verdicts.values()
                     if v.provider not in (_ai_primary, "unavailable", "unknown", "skipped")),
                    "fallback",
                )
                print(f"  ⚠️  {_fb_prov}:   {_ai_fallback_n} failover calls used")
            if _ai_failed_n:
                print(f"  ❌ unavailable:   {_ai_failed_n} failed")
            print(f"  ⏱  Total AI time: {_ai_elapsed:.1f}s")
            print(f"  {'─' * 44}")

        # Build portfolio summary from the live executor (no executor →
        # PAPER_TRADING_ENABLED=false → no portfolio to summarize).
        portfolio_summary = None
        paper_summary     = None
        try:
            if executor is not None:
                portfolio_summary = executor.build_summary(scan_results)
                paper_summary     = executor.build_paper_summary(scan_results)
                executor.log_state({r.symbol: r.price for r in scan_results})
                executor.save_state()
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

        # Blocked rule-BUY digest — edge-triggered ops_alert (see helper).
        try:
            _evaluate_blocked_rule_buys_alert(
                _blocked_rule_buys, _blocked_rule_buys_state, notifier,
            )
        except Exception as exc:
            logger.warning("Blocked rule-BUY digest failed: %s", exc)

        # Write dashboard
        try:
            try:
                _gate_status = LiveTradingGate().get_gate_status()
            except Exception as _ge:
                logger.debug("Gate status check failed: %s", _ge)
                _gate_status = None

            try:
                _checkpoint_status = dataclasses.asdict(compute_checkpoint_status())
            except Exception as _cse:
                logger.debug("Checkpoint status check failed: %s", _cse)
                _checkpoint_status = None

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
                screen_skips  = screen_skips,
                checkpoint_status = _checkpoint_status,
            )
        except Exception as exc:
            logger.warning("Dashboard render failed: %s", exc)

        print()
        _liveness.touch()   # safety net — fires every full cycle even if AI is disabled
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
