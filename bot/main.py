"""
Trading bot entry point.

All configuration is loaded from config.py (which reads .env).
Do not hardcode values here — change .env or config.py instead.

Architecture (top → bottom):
  Market Data  →  Indicators  →  Strategy
  →  Position State Machine   (position-aware filter + dedup)
  →  Risk Engine              (final authority)
  →  Execution Engine         (dynamic position sizing)
  →  Portfolio Manager
  →  Terminal Dashboard
"""
import csv
import glob
import json
import logging
import logging.handlers
import math
import os
import signal as _signal_module
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone as _tz

import ccxt as _ccxt
from dotenv import load_dotenv
load_dotenv()

# ── Logging setup ────────────────────────────────────────────────────────────
_log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    """Install root handlers — called from run(), NOT at import time.

    Import-time installation meant every test run that imported bot.main
    wrote into the production logs/trade_bot.log: it polluted forensics,
    faked the dashboard heartbeat (log mtime = "bot alive"), and a pytest
    run even rotated the live log at 10MB out from under the running bot
    (2026-07-05). Only the actual bot process may touch this file."""
    _file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(_log_dir, "trade_bot.log"),
        maxBytes=10_000_000,
        backupCount=5,
    )
    _file_handler.setLevel(logging.INFO)
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(logging.WARNING)
    _root_logger = logging.getLogger()
    _root_logger.handlers.clear()
    _root_logger.setLevel(logging.INFO)
    _root_logger.addHandler(_console_handler)
    _root_logger.addHandler(_file_handler)

# ── Imports ───────────────────────────────────────────────────────────────────
from config import cfg

from bot.data.price_feed import SimulatedFeed, CcxtFeed
from bot.data.historical_feed import Candle as _Candle
from bot.strategy.threshold_strategy import ThresholdStrategy, Signal
from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig
from bot.execution.executor import PaperExecutor, OrderStatus, OrderSide, Order
from bot.execution.live_executor import LiveExecutor
from bot.exchanges.retry import fetch_with_retry
from bot.risk.risk_manager import RiskManager, RiskConfig
from bot.risk.correlation import fetch_correlation, CORRELATION_THRESHOLD
from bot.state.trade_state import TradingStateMachine
from bot.portfolio.position_manager import PositionManager
from bot.portfolio.capital_pool import CapitalPool
from bot.indicators.indicators import ema as _ema_fn, trend as _trend_fn, atr as _atr_fn
from bot.ai.ai_engine import AIEngine, merge_signals
from bot import display
from bot.dashboard import renderer as _dashboard
from bot.alerts.telegram import TelegramAlerter
from bot.alerts.stuck_loop import StuckLoopDetector
from bot.data.trade_log import TradeLog
from bot.data.crypto_universe import CryptoUniverse
from bot.dynamic.eligibility import DynamicUniverseScreener
from bot.dynamic.ranking import RankableSignal, rank_buy_signals

# ── Dashboard path ────────────────────────────────────────────────────────────
_DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard.html")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True


def _handle_sigint(sig, frame):
    global _running
    _running = False


_signal_module.signal(_signal_module.SIGINT, _handle_sigint)
# SIGTERM too (launchd/systemd/kill default) — same graceful path as Ctrl-C,
# so state saves and the shutdown summary run instead of a hard kill.
_signal_module.signal(_signal_module.SIGTERM, _handle_sigint)


# ---------------------------------------------------------------------------
# Live candle helpers
# ---------------------------------------------------------------------------

def _build_exchange():
    """Create a ccxt exchange instance for candle fetching."""
    cls = getattr(_ccxt, cfg.exchange.exchange.lower())
    return cls({"timeout": 15_000})


def _minutes_to_timeframe(minutes: int) -> str:
    """Convert CANDLE_MINUTES integer to ccxt timeframe string."""
    mapping = {
        1: "1m", 5: "5m", 15: "15m", 30: "30m",
        60: "1h", 120: "2h", 240: "4h",
        360: "6h", 720: "12h", 1440: "1d",
    }
    return mapping.get(minutes, f"{minutes}m")


def _candle_countdown(timeframe: str) -> str:
    """Return countdown string until the next candle close at a round UTC boundary."""
    tf_minutes = {
        "1m": 1, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440,
    }
    period_m = tf_minutes.get(timeframe, cfg.exchange.candle_minutes)
    now = datetime.now(_tz.utc)
    now_m = now.hour * 60 + now.minute
    next_m = ((now_m // period_m) + 1) * period_m
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_close = base + timedelta(minutes=next_m)
    total_s = max(0, int((next_close - now).total_seconds()))
    h, rem = divmod(total_s, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _warmup_strategy(strategy, exchange, timeframe: str = None, symbol: str = None) -> "int | None":
    """
    Fetch completed candles and warm up the strategy indicators.
    Returns the timestamp_ms of the last candle fed, or None on failure.
    timeframe: ccxt timeframe string (e.g. '1h', '4h'). Defaults to cfg.backtest.timeframe.
    symbol: override exchange symbol; falls back to cfg.exchange.symbol.
    """
    if timeframe is None:
        timeframe = cfg.backtest.timeframe
    _sym = symbol if symbol is not None else cfg.exchange.symbol

    print(f"\n  Fetching historical {timeframe} candles for warmup …", flush=True)
    try:
        _WARMUP_CANDLES = max(strategy._warmup + 100, 150)
        raw = exchange.fetch_ohlcv(_sym, timeframe=timeframe, limit=_WARMUP_CANDLES + 1)
    except Exception as exc:
        print(f"  WARNING: historical warmup failed ({exc}) — starting cold", flush=True)
        return None

    if len(raw) < 2:
        print("  WARNING: too few candles returned — starting cold", flush=True)
        return None

    # raw[-1] is the currently-forming candle; drop it
    completed = raw[:-1]
    candles = [
        _Candle(
            timestamp=datetime.fromtimestamp(row[0] / 1000, tz=_tz.utc),
            open=float(row[1]), high=float(row[2]),
            low=float(row[3]), close=float(row[4]),
            volume=float(row[5]),
        )
        for row in completed
    ]

    total = len(candles)
    print(f"  Warming up with {total} × {timeframe} candles …", flush=True)
    for i, candle in enumerate(candles):
        strategy.evaluate(candle)
        display.warmup(i + 1, i + 1, total, candle.close)

    ready = "ready" if strategy.is_warmed_up else "NOT warmed up — too few candles"
    print(f"  Strategy {ready}.\n", flush=True)
    return completed[-1][0]   # ts_ms of last completed candle


def _fetch_completed_candle(
    exchange,
    last_ts_ms: "int | None",
    timeframe: str,
    symbol: str = None,
) -> "tuple[_Candle | None, int | None]":
    """
    Fetch the most recently completed candle.
    Returns (Candle, ts_ms) when a new candle is available, else (None, None).
    raw[-1] is still forming; raw[-2] is the last fully closed candle.
    symbol: override fetch symbol; falls back to cfg.exchange.symbol.
    """
    _sym = symbol if symbol is not None else cfg.exchange.symbol
    try:
        raw = fetch_with_retry(
            lambda: exchange.fetch_ohlcv(_sym, timeframe=timeframe, limit=2),
            label=f"candle fetch [{_sym}]",
        )
    except Exception as exc:
        logger.warning("live candle fetch error: %s", exc)
        return None, None

    if len(raw) < 2:
        return None, None

    row = raw[-2]
    ts_ms = row[0]
    if last_ts_ms is not None and ts_ms <= last_ts_ms:
        return None, None   # same candle as last evaluation

    _TF_MS_MAP = {
        "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
        "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000,
    }
    tf_ms  = _TF_MS_MAP.get(timeframe, cfg.exchange.candle_minutes * 60_000)
    age_ms = int(datetime.now(_tz.utc).timestamp() * 1000) - ts_ms
    if last_ts_ms is None and age_ms > 2 * tf_ms:
        logger.warning(
            "Stale candle on startup skipped (age=%.1fh) — waiting for next candle close",
            age_ms / 3_600_000,
        )
        return None, int(ts_ms)

    candle = _Candle(
        timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=_tz.utc),
        open=float(row[1]), high=float(row[2]),
        low=float(row[3]), close=float(row[4]),
        volume=float(row[5]),
    )
    return candle, ts_ms


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def build_feed():
    if cfg.exchange.feed_mode == "live":
        return CcxtFeed(exchange_id=cfg.exchange.exchange, symbol=cfg.exchange.symbol)
    return SimulatedFeed(
        symbol      = cfg.exchange.symbol,
        start_price = cfg.portfolio.sim_start_price,
        volatility  = cfg.portfolio.sim_volatility,
    )


def build_strategy():
    if cfg.strategy.mode == "indicator":
        return IndicatorStrategy(IndicatorConfig(
            rsi_period               = cfg.strategy.rsi_period,
            rsi_oversold             = cfg.strategy.rsi_oversold,
            rsi_overbought           = cfg.strategy.rsi_overbought,
            fast_ema_period          = cfg.strategy.fast_ema_period,
            slow_ema_period          = cfg.strategy.slow_ema_period,
            adx_period               = cfg.strategy.adx_period,
            adx_threshold            = cfg.strategy.adx_threshold,
            adx_max                  = cfg.strategy.adx_max,
            min_ema_spread_pct       = cfg.strategy.min_ema_spread_pct,
            max_ema_spread_pct       = cfg.strategy.max_ema_spread_pct,
            rsi_filter_enabled       = cfg.strategy.rsi_filter_enabled,
            macd_enabled             = cfg.strategy.macd_enabled,
            regime_ema_period        = cfg.strategy.regime_ema_period,
            regime_ema_slope_filter  = cfg.strategy.regime_ema_slope_filter,
            volume_k                 = cfg.strategy.volume_k,
            pullback_rsi_min         = cfg.strategy.pullback_rsi_min,
            pullback_rsi_max         = cfg.strategy.pullback_rsi_max,
            breakout_rsi_min         = cfg.strategy.breakout_rsi_min,
            breakout_rsi_max         = cfg.strategy.breakout_rsi_max,
            breakout_lookback        = cfg.strategy.breakout_lookback,
            max_price_extension_pct  = cfg.strategy.max_price_extension_pct,
            breakout_adx_threshold   = cfg.strategy.breakout_adx_threshold,
            atr_volatile_multiplier  = cfg.strategy.atr_volatile_multiplier,
        ))
    return ThresholdStrategy(
        buy_threshold  = cfg.strategy.buy_threshold,
        sell_threshold = cfg.strategy.sell_threshold,
    )


# ---------------------------------------------------------------------------
# Regime monitor background thread
# ---------------------------------------------------------------------------

def _regime_monitor_loop(symbols: list, exchange_id: str, interval_seconds: int = 14400) -> None:
    """Daemon thread: run regime health check for all live symbols on startup
    and then every interval_seconds (default 4 h).

    Spawns regime_monitor.py as a subprocess each cycle so its Kraken
    connections are fully isolated from the main process's connections.
    Running inside the same process causes intermittent OHLCV hangs because
    Kraken enforces per-IP concurrent connection limits that the bot's startup
    burst exhausts."""
    import subprocess
    import sys as _sys

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    monitor_script = os.path.join(project_root, "regime_monitor.py")

    env_override = {
        **os.environ,
        "MONITOR_SYMBOLS": ",".join(symbols),
        "MONITOR_EXCHANGE": exchange_id,
    }

    while True:
        try:
            result = subprocess.run(
                [_sys.executable, monitor_script],
                env=env_override,
                cwd=project_root,
                timeout=120,
            )
            if result.returncode != 0:
                logger.warning("Regime monitor subprocess exited with code %d", result.returncode)
        except subprocess.TimeoutExpired:
            logger.warning("Regime monitor subprocess timed out after 120s")
        except Exception as exc:
            logger.warning("Regime monitor error: %s", exc)
        time.sleep(interval_seconds)


# ---------------------------------------------------------------------------
# Unified dashboard background thread
# ---------------------------------------------------------------------------

def _unified_dashboard_loop(interval_s: int = 60) -> None:
    """Daemon thread: regenerate unified_dashboard.html every interval_s.

    Runs the generator as a subprocess for the same reasons as the regime
    monitor: its Kraken/yfinance calls stay isolated from the bot's own
    connections, and every run loads fresh code — no long-lived --watch
    process holding a stale module in memory."""
    import subprocess
    import sys as _sys

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(project_root, "unified_dashboard.py")

    while True:
        try:
            subprocess.run(
                [_sys.executable, script],
                cwd=project_root,
                timeout=90,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.warning("Unified dashboard refresh failed: %s", exc)
        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# Scheduled audits (in-process replacement for macOS cron, 2026-07-14)
# ---------------------------------------------------------------------------
# cron failed two independent ways on this laptop: no fire while the lid is
# closed (never catches up), and TCC denies /usr/sbin/cron access to ~/Desktop
# ("Operation not permitted" on every run since install — errors only visible
# in /var/mail). The bot is already a caffeinated long-running process, so the
# audits run here: same permissions as the bot, catch-up after restart,
# failures land in the bot log.
_AUDIT_STATE_PATH = os.path.join(_log_dir, "audit_state.json")


def _audit_due(
    last_run: str | None,
    now: datetime,
    run_at: str = "12:05",
    weekly_monday: bool = False,
    monthly_first: bool = False,
) -> bool:
    """Pure due-check (unit-tested — keep I/O out of here).

    Daily: due once per calendar day, any time at/after run_at (local) —
    a bot started at 15:00 still runs the 12:05 audit (catch-up).
    Weekly: due once per Mon-anchored week; past Monday's run_at, or any
    time Tue–Sun if that week's run was missed.
    Monthly: due once per calendar month; past the 1st's run_at, or any
    later day that month if the 1st was missed.
    """
    try:
        hh, mm = (int(x) for x in run_at.split(":"))
    except ValueError:
        hh, mm = 12, 5
    past_time_today = (now.hour, now.minute) >= (hh, mm)
    last = date.fromisoformat(last_run) if last_run else None

    if monthly_first:
        first = now.date().replace(day=1)
        if last is not None and last >= first:
            return False
        return now.date() > first or past_time_today

    if weekly_monday:
        monday = now.date() - timedelta(days=now.weekday())
        if last is not None and last >= monday:
            return False
        return now.date() > monday or past_time_today

    if last is not None and last >= now.date():
        return False
    return past_time_today


def _scheduled_audits_loop() -> None:
    """Daemon thread: run shadow_signal.py daily and live_comparison.py weekly.

    Fresh subprocess per run (same isolation rationale as the dashboard loop);
    output appends to the same log files the cron jobs targeted. The run date
    is recorded even when the script fails, so a broken audit retries next
    period instead of every minute — the failure is in the bot log either way.
    """
    import subprocess
    import sys as _sys

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # (name, script, run_at, due_kwargs, log_name, timeout_s)
    jobs = [
        ("shadow_signal", "shadow_signal.py",
         os.getenv("SHADOW_AUDIT_TIME", "12:05"), {}, "shadow_signal.log", 600),
        ("live_comparison", "live_comparison.py",
         os.getenv("WEEKLY_AUDIT_TIME", "12:10"), {"weekly_monday": True},
         "weekly.log", 600),
        # Monthly re-screen (2026-07-16): re-runs the crypto CAD screen and the
        # stock walk-forward so edge decay and new qualifiers surface on their
        # own instead of waiting for someone to remember. Report + alert only —
        # whitelists never change automatically.
        ("monthly_rescreen", "rescreen.py",
         os.getenv("RESCREEN_AUDIT_TIME", "12:20"), {"monthly_first": True},
         "rescreen.log", 5400),
    ]
    if os.getenv("RESCREEN_ENABLED", "true").lower() != "true":
        jobs = [j for j in jobs if j[0] != "monthly_rescreen"]
    while True:
        try:
            state: dict = {}
            if os.path.exists(_AUDIT_STATE_PATH):
                with open(_AUDIT_STATE_PATH, encoding="utf-8") as f:
                    state = json.load(f) or {}
            now = datetime.now()  # local time, like the cron schedule it replaces
            for name, script, run_at, due_kwargs, log_name, timeout_s in jobs:
                if not _audit_due(state.get(name), now, run_at, **due_kwargs):
                    continue
                logger.info("Scheduled audit %s starting (in-bot scheduler)", name)
                log_path = os.path.join(_log_dir, log_name)
                try:
                    with open(log_path, "a", encoding="utf-8") as lf:
                        rc = subprocess.run(
                            [_sys.executable, os.path.join(project_root, script)],
                            cwd=project_root, timeout=timeout_s, stdout=lf, stderr=lf,
                        ).returncode
                except Exception as exc:
                    logger.warning("Scheduled audit %s failed to launch: %s", name, exc)
                    rc = -1
                state[name] = now.date().isoformat()
                from bot.atomic_json import atomic_write_json
                atomic_write_json(_AUDIT_STATE_PATH, state, indent=0)
                if rc == 0:
                    logger.info("Scheduled audit %s completed → logs/%s", name, log_name)
                else:
                    logger.warning(
                        "Scheduled audit %s exited rc=%s — see logs/%s", name, rc, log_name
                    )
        except Exception as exc:
            logger.warning("Scheduled audit loop error: %s", exc)
        time.sleep(60)


# ---------------------------------------------------------------------------
# Crash-loop detection helper
# ---------------------------------------------------------------------------
_STARTUP_LOG = os.path.join(_log_dir, "startup_timestamps.txt")
_CRASH_LOOP_WINDOW_S  = 300   # 5 minutes
_CRASH_LOOP_THRESHOLD = 3     # 3+ restarts in window = crash-loop


def _record_startup_and_check_crash_loop(alerter: "TelegramAlerter") -> None:
    """Append current timestamp to startup log; fire an error alert if crash-loop detected."""
    now = datetime.now(_tz.utc)
    try:
        with open(_STARTUP_LOG, "a") as fh:
            fh.write(now.isoformat() + "\n")
        with open(_STARTUP_LOG) as fh:
            lines = [l.strip() for l in fh if l.strip()]
        cutoff = now.timestamp() - _CRASH_LOOP_WINDOW_S
        recent = [l for l in lines if datetime.fromisoformat(l).timestamp() > cutoff]
        # Trim file to last 50 entries
        if len(lines) > 50:
            with open(_STARTUP_LOG, "w") as fh:
                fh.write("\n".join(lines[-50:]) + "\n")
        if len(recent) >= _CRASH_LOOP_THRESHOLD:
            alerter.error(
                f"Crash-loop: {len(recent)} restarts in {_CRASH_LOOP_WINDOW_S // 60} min "
                f"— check logs/trade_bot.log for root cause"
            )
            logger.warning("CRASH-LOOP detected: %d restarts in %ds", len(recent), _CRASH_LOOP_WINDOW_S)
    except Exception as exc:
        logger.warning("Could not check crash-loop state: %s", exc)


def _check_orphaned_positions(
    initialized_symbols: "set[str]",
    alerter: "TelegramAlerter",
    log_dir: str = _log_dir,
) -> list[str]:
    """
    Scan all logs/live_state_*.json for open positions whose symbol is NOT
    being initialized this run (e.g. removed from UNIVERSE_WHITELIST while
    holding). Such positions get no SL/TP checks, no drift reconciliation and
    no alerts — alert loudly so a human closes or re-whitelists them.
    Returns the list of orphaned symbols (for tests).
    """
    orphaned: list[str] = []
    try:
        for path in sorted(glob.glob(os.path.join(log_dir, "live_state_*.json"))):
            try:
                with open(path) as fh:
                    state = json.load(fh)
            except Exception as exc:
                logger.warning("Orphan check: could not read %s: %s", path, exc)
                continue
            sym = state.get("symbol", "")
            pos = float(state.get("position", 0.0) or 0.0)
            if pos > 0 and sym and sym not in initialized_symbols:
                orphaned.append(sym)
                logger.error(
                    "ORPHANED POSITION: %s holds %s but is not in this run's symbol list "
                    "— NO SL/TP or drift monitoring. Close it manually or re-add to "
                    "UNIVERSE_WHITELIST. State: %s", sym, pos, path,
                )
                alerter.error(
                    f"ORPHANED POSITION: {sym} holds {pos} but is not monitored this run "
                    f"(removed from whitelist?). No SL/TP will fire — close manually or "
                    f"re-add to UNIVERSE_WHITELIST."
                )
    except Exception as exc:
        logger.warning("Orphan position check failed: %s", exc)
    return orphaned


def _replay_pending_journal_entries(executors: dict, trade_log, alerter: "TelegramAlerter") -> list[str]:
    """
    2026-09-18 review finding (P1-3): LiveExecutor.execute() persists a
    fill's accounting effect (cash/position/pnl) to its own state file
    BEFORE the caller separately writes that fill to trade_log (SQLite) —
    a crash between those two writes leaves the portfolio state correctly
    updated but the fill invisible to trade_log/reporting forever.

    Called once at startup, before any new trading: every executor's
    pending_journal_entries lists fills whose accounting is already real
    and persisted, but whose trade_log row may be missing. Replays each
    into trade_log (tagged, with its ORIGINAL execution timestamp and P&L
    preserved — not replay time / NULL) and acks it so a later restart
    doesn't replay it again.

    2026-09-18 FOLLOW-UP review finding (P1): a crash between the DB
    insert succeeding and the ack being persisted used to duplicate the
    row on the NEXT replay — trade_log.log_fill()'s exec_key parameter
    makes this insert idempotent (a second attempt with the same exec_key
    is a confirmed no-op, not a duplicate row), so retrying a replay whose
    ack didn't survive is safe. Multiple entries per executor (a second
    fill recorded before the first was acked) are now each replayed and
    acked independently, in order — not just one, silently overwritten.

    Never raises — a replay failure is alerted and left pending for the
    next startup rather than silently dropped or crashing the boot.
    Returns the list of symbols that had at least one pending entry (for
    tests/logs) — may list a symbol more than once if it had more than one.
    """
    recovered: list[str] = []
    for sym, executor in executors.items():
        entries = list(getattr(executor, "pending_journal_entries", []) or [])
        for entry in entries:
            try:
                trade_log.log_fill(
                    side          = entry["side"],
                    symbol        = entry["symbol"],
                    quantity      = entry["quantity"],
                    price         = entry["price"],
                    pnl           = entry.get("pnl"),
                    exchange      = cfg.exchange.exchange,
                    signal_reason = "recovered_from_crash",
                    notes         = f"replayed from pending_journal_entry, filled_at={entry.get('filled_at')}",
                    fee_cost      = entry.get("fee_cost", 0.0),
                    fee_currency  = entry.get("fee_currency", ""),
                    exec_key      = entry.get("exec_key", ""),
                    timestamp     = entry.get("filled_at"),
                )
                executor.ack_journal_entry(entry["order_id"])
                recovered.append(sym)
                logger.warning(
                    "FILL JOURNAL RECOVERED [%s]: replayed missed trade_log row "
                    "for order %s (a crash previously interrupted logging it)",
                    sym, entry.get("order_id"),
                )
                alerter.message(
                    f"ℹ️ FILL JOURNAL RECOVERED [{sym}]: a fill from a prior crash "
                    f"({entry['side']} {entry['quantity']:.8f} @ {entry['price']:,.2f}) "
                    f"had its accounting already applied but was missing from "
                    f"trade_log — replayed now."
                )
            except Exception as exc:
                logger.error(
                    "FILL JOURNAL REPLAY FAILED [%s]: %s — entry stays pending, "
                    "will retry on next startup", sym, exc,
                )
                alerter.error(
                    f"FILL JOURNAL REPLAY FAILED [{sym}]: {exc} — a crash-recovered "
                    f"fill could not be logged; will retry on the next restart."
                )
    return recovered


# ---------------------------------------------------------------------------
# Candle watchdog — circuit breaker (extracted for unit-testability)
#
# Upgraded 2026-08-07: was alert-only before (fired a Telegram notice, reset
# its own timer to avoid spam, but never changed trading behavior — a stale
# feed and a healthy one were treated identically by the strategy). Now
# blocks new BUYs for as long as the feed stays stale, same "BUY-only, SELL
# always allowed" shape as every other breaker in this codebase — SL/TP
# exits read the independent live-tick price feed, not the candle feed, and
# must always be able to close a position regardless of candle staleness.
# ---------------------------------------------------------------------------

def _check_candle_watchdog(
    ss: dict,
    candle_minutes: int,
    now: float,
    alerter: "TelegramAlerter",
    symbol: str = "",
) -> bool:
    """
    Reads ss['last_candle_time'] (only ever advanced by the real candle-fetch
    path — untouched here) and transitions ss['candle_feed_stale'] (this
    breaker's own persistent state). Alerts once per stale->fresh transition,
    not every tick — the flag itself is what prevents re-alerting while
    continuously stale, replacing the old "reset the timer" spam guard.

    Returns True if new BUYs should be blocked this tick.
    """
    stale_s  = candle_minutes * 60 * 2
    age_s    = now - ss['last_candle_time']
    is_stale = age_s > stale_s
    _sym_label = f" [{symbol}]" if symbol else ""

    if is_stale and not ss['candle_feed_stale']:
        ss['candle_feed_stale'] = True
        alerter.error(
            f"Candle watchdog{_sym_label}: no new {candle_minutes}min candle "
            f"for {int(age_s / 60)} minutes — feed is stale. New BUYs blocked "
            f"until a fresh candle arrives (SELL/exits unaffected)."
        )
    elif not is_stale and ss['candle_feed_stale']:
        ss['candle_feed_stale'] = False
        alerter.error(
            f"Candle watchdog{_sym_label}: feed recovered — new BUYs re-enabled."
        )

    return ss['candle_feed_stale']


def _update_price_feed_staleness(
    ss: dict, ok: bool, symbol: str, alerter: "TelegramAlerter", threshold: int = 5,
) -> bool:
    """
    2026-09-18 review finding (P1-7): a ticker-fetch failure reused
    ss['last_price'] (a stale value) and let the tick continue exactly as
    if a fresh price had been read — including evaluating a brand-new BUY
    signal against data that may no longer reflect the market. Mirrors
    _check_candle_watchdog's edge-trigger pattern for a second, independent
    freshness signal: the live intra-candle price tick, not the candle feed.

    Call with ok=True on every successful price fetch, ok=False on every
    failure (after ss['err_count'] has already been updated by the caller).
    Alerts once per fresh<->stale transition, not every tick. SELL/exits
    are never affected by this flag — same standing breaker rule as the
    candle watchdog (a stale price must not block getting OUT of a
    position, only entries into a new one).

    Returns the resulting ss['price_feed_stale'] value.
    """
    _sym_label = f" [{symbol}]" if symbol else ""
    if ok:
        if ss['price_feed_stale']:
            ss['price_feed_stale'] = False
            alerter.message(
                f"✅ Price feed{_sym_label}: fresh ticks resumed — new BUYs re-enabled."
            )
        return False
    if ss['err_count'] >= threshold and not ss['price_feed_stale']:
        ss['price_feed_stale'] = True
        alerter.error(
            f"Price feed{_sym_label}: {ss['err_count']} consecutive fetch "
            f"failures — reusing a stale price. New BUYs blocked until a "
            f"fresh tick arrives (SELL/exits unaffected)."
        )
    return ss['price_feed_stale']


# ---------------------------------------------------------------------------
# Position drift evaluation (extracted for unit-testability)
# ---------------------------------------------------------------------------
_DRIFT_MIN = 0.000010


def _evaluate_drift(
    sym: str,
    base: str,
    exchange_pos: float,
    bot_pos: float,
    ss: dict,
    threshold: int,
    alerter,
) -> None:
    """One drift-reconciliation evaluation for one symbol.

    Escalates via alerter.error after `threshold` consecutive detections, then
    ACKNOWLEDGES that drift amount (ss['drift_acked']): an unchanged drift —
    e.g. a manual deposit sitting in the account as external holdings, which
    the bot will never trade — stays quiet instead of re-alerting every
    `threshold` checks forever (incident: 0.000085 BTC deposit spammed
    Telegram every ~3h from Jul 6–10, 2026). A drift that CHANGES re-arms
    the counter; a resolved drift clears the acknowledgment.
    """
    drift = abs(exchange_pos - bot_pos)
    if drift > _DRIFT_MIN:
        if abs(drift - ss.get('drift_acked', 0.0)) <= _DRIFT_MIN:
            logger.info(
                "Known drift [%s] unchanged (%.6f %s) — acknowledged, not re-alerting",
                sym, drift, base,
            )
            return
        ss['drift_count'] += 1
        logger.warning(
            "POSITION DRIFT [%s] [%d/%d]: exchange=%.6f bot=%.6f"
            " drift=%.6f %s",
            sym, ss['drift_count'], threshold,
            exchange_pos, bot_pos, drift, base,
        )
        if ss['drift_count'] >= threshold:
            alerter.error(
                f"PERSISTENT position drift [{sym}] after"
                f" {ss['drift_count']} consecutive checks:"
                f" exchange={exchange_pos:.6f}"
                f" bot={bot_pos:.6f} {base}"
                f" — check logs/live_state_{sym.replace('/', '_')}.json."
                f" If this is a manual deposit it is safe (external holdings"
                f" are never traded); no further alerts unless the amount changes."
            )
            ss['drift_acked'] = drift
            ss['drift_count'] = 0
    else:
        if ss['drift_count'] > 0 or ss.get('drift_acked', 0.0) > 0:
            logger.info(
                "Position drift resolved [%s]: exchange=%.6f bot=%.6f %s",
                sym, exchange_pos, bot_pos, base,
            )
        ss['drift_count'] = 0
        ss['drift_acked'] = 0.0


# ---------------------------------------------------------------------------
# Auth-health tracking for the position-drift-check block (extracted
# 2026-08-18 for unit-testability — this logic previously lived inline in
# run()'s tick loop with zero direct test coverage, flagged as a known gap
# after the 2026-08-15 Kraken auth incident; test_drift_escalation.py and
# test_heartbeat.py passed unchanged throughout that incident without ever
# exercising these branches).
# ---------------------------------------------------------------------------
def _update_auth_health(
    auth_health: dict,
    success: bool,
    consecutive_failures: int,
    threshold: int,
    alerter,
    exc: Exception | None = None,
) -> int:
    """One auth-health evaluation, called once per drift-check attempt.

    Tracks whether Kraken's *authenticated* endpoints (balance/position sync)
    are reachable — separately from the public price/candle feed and from
    plain liveness (the loop ticking). Added 2026-08-15 after an IP-
    restriction auth failure went undetected for days: the drift-check
    failure only ever logged, and the heartbeat's healthy_fn only checked
    that the loop was ticking — public candle/ticker calls kept succeeding
    the whole time, so healthchecks.io stayed green.

    Alerts once per ok->failing / failing->ok transition (edge-triggered,
    not every check) and mutates auth_health['ok'] in place so the
    heartbeat's healthy_fn (a closure created before the tick loop starts)
    observes the same flag.

    Returns the new consecutive_failures count: 0 on success, or after
    `threshold` consecutive failures have just been evaluated (whether or
    not that evaluation produced a fresh alert).
    """
    if success:
        if not auth_health["ok"]:
            auth_health["ok"] = True
            alerter.error(
                "Kraken auth RECOVERED — position drift check succeeded "
                "again. BUYs/exits should work normally now."
            )
        return 0

    consecutive_failures += 1
    if consecutive_failures >= threshold:
        logger.warning(
            "WARNING: Position drift check: %d consecutive failures"
            " — Kraken BalanceEx may be rate-limited or session expired",
            consecutive_failures,
        )
        if auth_health["ok"]:
            auth_health["ok"] = False
            alerter.error(
                f"Kraken authenticated API calls failing ({consecutive_failures} "
                f"consecutive position-drift-check failures): "
                f"{exc}. Public price/candle data is "
                "unaffected — this looks like an auth or IP-"
                "restriction issue, not a network outage. If a BUY "
                "signal fires while this persists, order placement "
                "will likely fail the same way. Heartbeat now "
                "reports unhealthy until this recovers."
            )
        consecutive_failures = 0
    return consecutive_failures


# ---------------------------------------------------------------------------
# Blocked-BUY alert (extracted for unit-testability). The 2026-08-18 incident
# — the bot sat flat through a $90k→$108k BTC rally while a real BUY signal
# fired on 08-18 and was correctly vetoed by the MTF daily-trend gate — was
# only recoverable after the fact from logs/live_signals.csv; nothing pushed
# it. This closes that: when the raw strategy signal is BUY but a gate blocks
# it, fire ONE Telegram alert, edge-triggered on (symbol, gate) so a
# persistently-blocked signal (e.g. SOL firing BUY every 4h while already
# holding) doesn't spam. Strategy-internal HOLDs (RSI/ADX/MACD/trend not
# aligned) are NOT blocked BUYs and never reach here — raw_signal is only BUY
# once the strategy itself wants in.
# ---------------------------------------------------------------------------
_BUY_BLOCK_REASONS = {
    "state_machine":   "already holding a position, or in post-trade cooldown",
    "capital_pool":    "no capital slot free (raise STARTING_CASH / add a deposit)",
    "risk_manager":    "a risk breaker is active (halt / daily-loss / drawdown / kill-switch / trade-cap / position-size)",
    "correlation":     "too correlated (>0.70) with an already-open position",
    "candle_watchdog": "the candle feed is stale — BUYs paused until it recovers",
    "mtf_trend":       "the daily (1D) trend is BEARISH",
    "regime":          "the market regime is not favourable for a new entry (strategy 200-EMA / volatile)",
}


def _evaluate_buy_signal_alert(
    ss: dict, sym: str, raw_signal_was_buy: bool, price: float, alerter,
) -> None:
    """Edge-triggered Telegram heads-up the moment the strategy signals BUY.

    Fires ONE alerter.message() when the raw strategy signal first turns BUY
    for this symbol — before any gate or execution. The existing fill alert
    (BUY executed) or blocked-BUY alert (a gate held it) then reports the
    outcome. Resets ss['last_buy_signal_alerted'] as soon as the raw signal is
    no longer BUY, so the next fresh BUY episode re-alerts. Not persisted —
    resets on restart like the other per-process edge flags.
    """
    if not raw_signal_was_buy:
        ss['last_buy_signal_alerted'] = False
        return
    if ss.get('last_buy_signal_alerted'):
        return
    ss['last_buy_signal_alerted'] = True
    alerter.message(
        f"🔔 BUY signal [{sym}] — the strategy wants to enter"
        + (f" near ${price:,.2f}" if price else "")
        + ". Checking risk gates / placing the order; a fill or blocked-BUY "
        "alert follows with the outcome."
    )


def _evaluate_blocked_buy_alert(
    ss: dict, sym: str, raw_signal_was_buy: bool, block_gate: str, alerter,
) -> None:
    """Edge-triggered Telegram alert for a strategy BUY that a gate blocked.

    Fires once when (sym, block_gate) first blocks a BUY and again only if the
    blocking gate changes. Clears ss['last_buy_block_alert'] the moment the
    strategy stops signalling BUY or the BUY goes through, so the next fresh
    block re-alerts.
    """
    if not (raw_signal_was_buy and block_gate):
        ss['last_buy_block_alert'] = ""
        return
    if ss.get('last_buy_block_alert') == block_gate:
        return
    ss['last_buy_block_alert'] = block_gate
    why = _BUY_BLOCK_REASONS.get(block_gate, block_gate)
    alerter.error(
        f"BUY signal blocked [{sym}]: the strategy wants to enter but the "
        f"'{block_gate}' gate is holding it — {why}. No further alert unless the "
        f"blocking gate changes or the BUY clears. See logs/live_signals.csv."
    )


# ---------------------------------------------------------------------------
# Manual halt flag file (extracted for unit-testability)
# ---------------------------------------------------------------------------
_HALT_FLAG_PATH = os.path.join(_log_dir, "HALT")


def _check_halt_flag(
    risk: "RiskManager",
    flag_path: str,
    halt_file_active: bool,
    alerter: "TelegramAlerter",
) -> bool:
    """Engage/lift the manual halt based on presence of the HALT flag file.

    Operational kill-switch: `touch logs/HALT` halts new trades without a
    restart; `rm logs/HALT` resumes. Returns the updated halt_file_active
    so a halt engaged elsewhere (e.g. a future Telegram command) is never
    lifted by this helper.
    """
    exists = os.path.exists(flag_path)
    if exists and not risk.config.halt:
        risk.halt()
        alerter.error(
            f"Manual HALT engaged — {flag_path} detected. "
            f"BUY and strategy SELL blocked; SL/TP exits still fire. "
            f"Remove the file to resume."
        )
        return True
    if not exists and halt_file_active:
        if risk.config.halt:
            risk.resume()
        alerter.error(f"Manual HALT lifted — {flag_path} removed. Trading resumed.")
        return False
    return halt_file_active


def _seed_native_stop_state(executor) -> tuple[float | None, bool]:
    """
    Restart-recovery helper (2026-08-20): mirror the executor's own
    already-reconciled native-stop bookkeeping into a symbol_state (ss)
    entry's 'native_stop_price' / 'native_stop_is_trailing' fields.

    Why this exists: LiveExecutor.__init__ already confirms its resting
    native stop against Kraken's real open orders on startup
    (_verify_resting_stop_on_startup — see live_executor.py), so
    executor.native_stop_price / executor.native_stop_is_trailing /
    executor.has_resting_stop are correct by the time this runs. But
    bot/main.py's OWN symbol_state dict initializes its copies of these two
    fields to None/False unconditionally and never re-read the executor's
    reconciled values — a gap flagged during the 2026-08-19 native
    trailing-stop session and left unfixed at the time. The practical risk:
    _resync_native_stop(ss) (fired by a partial TP or a partial fill on an
    urgent SL/TP exit) trusts ss's copy, not the executor's — if a
    quantity-changing event fires after a restart but before the next BUY
    fill re-seeds ss (the only other place these fields get set), it would
    call sync_protective_stop(None) with no trailing_pct either, which
    unconditionally CANCELS whatever's actually resting on Kraken (via the
    executor's correctly-tracked order id) and then places nothing —
    leaving a real, previously-protected position naked.

    Deliberately does NOT recompute a fresh price from avg_entry/ATR, and
    does NOT try to resolve an ambiguous multi-order state itself (that's
    handled, separately, by _verify_resting_stop_on_startup's own alert) —
    this is a pure, passive mirror of whatever the executor already
    determined is really resting. Matches the existing documented decision
    ("a still-open saved order is kept as-is — level never touches down",
    see CLAUDE.md's native stop-loss section) rather than introducing a
    second, inconsistent source of truth.

    Extracted as a standalone function (same pattern as _evaluate_drift,
    _update_auth_health, _check_candle_watchdog) purely for direct unit
    testability — no side effects, pure read of the executor's public
    properties.
    """
    if not executor.has_resting_stop:
        return None, False
    return executor.native_stop_price, executor.native_stop_is_trailing


def _resync_native_stop(ss: dict) -> "Order | None":
    """
    Re-place the native backstop sized to the position AFTER a quantity-
    changing event that doesn't close it (partial TP, a partial fill on
    an urgent SL/TP exit) — preserving whichever kind (static or native
    trailing) is currently resting. Quantity is the one thing a resting
    Kraken order can't be amended in place for via create_order, so this
    always cancels and re-places; a trailing order loses its
    exchange-tracked peak on the re-place (a fresh trail starts from the
    price at re-placement) — accepted, same precision loss the static
    order already takes on every resize. Full-close paths call
    sync_protective_stop(None) directly instead of this helper.

    Hoisted from a run()-local closure to module level (2026-09-13, for
    dynamic-universe integration) — it only ever touched `ss` and the
    module-level `cfg`, no other run()-local state, so this is a pure
    relocation with identical behavior (same pattern as
    _seed_native_stop_state / _evaluate_drift above): every existing call
    site (bare `_resync_native_stop(ss)`, unchanged) now resolves to this
    module-level definition instead of the nested one, and it's directly
    unit-testable without invoking run().

    Returns whatever LiveExecutor.sync_protective_stop() returns — non-None
    when a native stop was discovered to have filled (fully or partially)
    during the cancel-and-verify cycle this triggers. 2026-09-18 follow-up
    review finding (P1): every call site used to discard this return value
    entirely — callers must now route a non-None result through
    _process_discovered_sell_fill so PositionManager/state machine/capital
    pool/risk/trade log all learn about it, not just the executor's own
    internal accounting.
    """
    if not ss['pm'].has_position:
        _fo = ss['executor'].sync_protective_stop(None)
        ss['native_stop_is_trailing'] = False
        return _fo
    if ss['native_stop_is_trailing']:
        return ss['executor'].sync_protective_stop(
            None,
            trailing_pct=cfg.backtest.exit_params_for(
                ss['executor'].symbol)["trail_stop_pct"],
        )
    return ss['executor'].sync_protective_stop(ss.get('native_stop_price'))


def _compute_account_value(capital_pool: CapitalPool, executors: dict, symbol_state: dict) -> float:
    """
    Aggregate account value: the pool's own unallocated cash (counted
    ONCE) plus, for every symbol that actually holds an allocated slot,
    that symbol's real cash + marked position value.

    FIXED 2026-09-13 (real bug, confirmed by external review): the
    previous version summed executor.cash across EVERY executor in
    `executors`, unconditionally. In fixed mode this happened to be
    correct only because slot_cash_for() partitions the whole pool across
    EXACTLY as many executors as there are permanent slots, with no
    overlap. Once dynamic admission can fund MANY MORE executors than
    there are slots (every admitted-but-flat candidate gets told "you
    could have up to $X" via slot_cash_for(), purely as a sizing basis —
    see _admit_dynamic_symbol), summing all of their .cash double- (triple-,
    N-) counted the same underlying pool total: admitting one extra symbol
    with zero trades or deposits measurably inflated account value, which
    would have corrupted every drawdown/kill-switch/position-size check
    reading this number. Reproduced and fixed same-review: admitting ETH
    turned $453 into $530 with no trade. capital_pool.available_cash is the
    single source of truth for unallocated shared cash; only symbols with a
    REAL allocated slot (capital_pool.allocated_symbols) contribute their
    own cash+position on top of it. With the fixed 2-symbol roster and none
    of them holding a position (today's actual live state), this returns
    capital_pool.available_cash alone — the same number the old formula
    coincidentally also produced in that specific case, which is why the
    bug was invisible until a dynamic admission exercised the code path
    where the two formulas diverge.

    Extracted to module level (was a run()-local closure) purely for
    direct unit testability — same pattern as every other function in
    this section.
    """
    total = capital_pool.available_cash
    for _s in capital_pool.allocated_symbols:
        _e = executors.get(_s)
        if _e is None:
            continue
        _px = symbol_state[_s]['last_price'] if _s in symbol_state else 0.0
        if not _px:
            _px = getattr(_e, "avg_entry", 0.0) or 0.0
        total += _e.cash + _e.position * _px
    return total


def _execute_approved_signal(
    sym: str,
    ss: dict,
    final_signal: Signal,
    price: float,
    trade_qty: float,
    raw_signal: Signal,
    filter_reason: str,
    *,
    capital_pool: CapitalPool,
    risk: RiskManager,
    alerter,
    trade_log,
    stuck_detector,
    is_indicator: bool,
):
    """
    Execute an approved (risk-gate-passed, non-HOLD) signal and apply every
    downstream effect exactly as bot.main.run()'s tick loop always has:
    fee-aware fill recording, PnL, native-stop sync, ATR-SL computation,
    trailing-stop seeding, capital-pool allocate/release, trade log,
    Telegram fill/reject alert, and the stuck-loop watchdog.

    Extracted verbatim from the inline "9. Execute" block (2026-09-13, for
    dynamic-universe integration) — same "extract for testability" pattern
    as _seed_native_stop_state / _resync_native_stop / _evaluate_drift
    above. This is what lets a dynamically-ranked BUY (executed later,
    after a cross-symbol ranking pass) and a fixed-roster immediate
    BUY/SELL share IDENTICAL fill-processing, instead of two copies of this
    logic silently drifting apart over time — every parameter here beyond
    the signal itself (capital_pool, risk, alerter, trade_log,
    stuck_detector, is_indicator) is exactly what the original inline block
    closed over; cfg/_atr_fn/display/OrderStatus/OrderSide/Signal/logger
    are module-level already and don't need to be passed.

    Caller is responsible for checking approval (risk.evaluate() or
    equivalent) BEFORE calling this — it unconditionally executes.
    Returns the Order (whatever its final status), or None if execute()
    itself raised.
    """
    try:
        order = ss['executor'].execute(final_signal, price, quantity=trade_qty)
    except Exception as _exec_exc:
        logger.error(
            "EXECUTOR EXCEPTION [%s] %s: %s", sym, final_signal.value, _exec_exc,
            exc_info=True,
        )
        alerter.error(
            f"EXECUTOR EXCEPTION [{sym}] {final_signal.value}: {_exec_exc} — "
            f"order not confirmed placed or filled, check the exchange manually"
        )
        order = None
    if order:
        if order.status == OrderStatus.FILLED and order.quantity <= 0:
            logger.error(
                "FILLED order returned with qty=0 for %s %s — skipping fill record."
                " Check Kraken manually.",
                order.side.value, sym,
            )
            order = None
        if order and order.status == OrderStatus.FILLED:
            risk.record_fill(sym)
            ss['sm'].on_fill(final_signal, order.price)

            pnl = None
            if order.side == OrderSide.BUY:
                ss['pm'].on_buy(order.price, order.quantity)
                capital_pool.allocate(sym)
                # Seed the trail peak only when there is no activation
                # threshold — otherwise the intra-candle block arms it
                # once price reaches entry × (1 + activation_pct).
                # Seeding unconditionally here bypassed that gate.
                ss['trail_peak'] = (
                    order.price
                    if cfg.backtest.exit_params_for(sym)["trail_stop_activation_pct"] <= 0
                    else 0.0
                )
                ss['partial_done'] = False
                ss['atr_sl'] = 0.0
                if is_indicator:
                    _atr_val = _atr_fn(
                        list(ss['strategy']._highs),
                        list(ss['strategy']._lows),
                        list(ss['strategy']._closes),
                        cfg.strategy.atr_period,
                    )
                    if _atr_val is None or _atr_val <= 0 or cfg.strategy.atr_sl_mult <= 0:
                        ss['atr_sl'] = 0.0
                        logger.info("ATR SL disabled or unavailable — using fixed SL")
                    else:
                        ss['atr_sl'] = order.price - _atr_val * cfg.strategy.atr_sl_mult
                        logger.info(
                            "ATR SL [%s]: entry=%.2f atr=%.2f sl=%.2f mult=%.1f",
                            sym, order.price, _atr_val, ss['atr_sl'], cfg.strategy.atr_sl_mult,
                        )
                # Native stop-loss backstop (static — see
                # sync_protective_stop docstring): mirrors whatever
                # level the software SL just armed for this fill.
                # Always static at entry — a native trailing-stop
                # is only swapped in later, once trail_peak arms
                # (intra-candle block above), matching the
                # software trailing logic's own activation delay.
                ss['native_stop_price'] = (
                    ss['atr_sl'] if ss['atr_sl'] > 0
                    else (
                        order.price * (1 - cfg.backtest.stop_loss_pct)
                        if cfg.backtest.stop_loss_pct > 0 else None
                    )
                )
                ss['native_stop_is_trailing'] = False
                _discovered = ss['executor'].sync_protective_stop(ss['native_stop_price'])
                if _discovered is not None:
                    _process_discovered_sell_fill(
                        sym, ss, _discovered, "native_stop_discovered",
                        capital_pool=capital_pool, risk=risk,
                        alerter=alerter, trade_log=trade_log,
                    )
            else:
                pnl = ss['pm'].on_sell(order.price, order.quantity)
                ss['trail_peak'] = 0.0
                ss['partial_done'] = False
                ss['atr_sl'] = 0.0
                if not ss['pm'].has_position:
                    capital_pool.release(sym, ss['executor'].cash)
                    _discovered = ss['executor'].sync_protective_stop(None)
                    ss['native_stop_is_trailing'] = False
                    if _discovered is not None:
                        _process_discovered_sell_fill(
                            sym, ss, _discovered, "native_stop_discovered",
                            capital_pool=capital_pool, risk=risk,
                            alerter=alerter, trade_log=trade_log,
                        )
                else:
                    # Partial fill leaving a residual position (not
                    # currently reachable with strategy SELLs, which
                    # always close in full — defensive parity with
                    # the partial-TP path below).
                    _discovered = _resync_native_stop(ss)
                    if _discovered is not None:
                        _process_discovered_sell_fill(
                            sym, ss, _discovered, "native_stop_discovered",
                            capital_pool=capital_pool, risk=risk,
                            alerter=alerter, trade_log=trade_log,
                        )

            display.fill(
                order.side.value, order.quantity,
                sym, order.price, order.total_value, pnl,
            )
            trade_log.log_fill(
                side          = order.side.value,
                symbol        = sym,
                quantity      = order.quantity,
                price         = order.price,
                pnl           = pnl,
                exchange      = cfg.exchange.exchange,
                signal_reason = filter_reason or raw_signal.value,
                fee_cost      = order.fee_cost,
                fee_currency  = order.fee_currency,
                # 2026-09-18 PASS-3 review finding: pass the SAME identity
                # journal replay would use for this exact fill — makes a
                # normal write and a later replay of the same fill (crash
                # between them) mutually idempotent instead of colliding
                # only replay-to-replay.
                exec_key      = order.exec_key,
            )
            # 2026-09-18 review finding (P1-3, durable fill journal): the
            # trade_log write above and the accounting update inside
            # execute() are two separate writes — ack once the trade_log
            # row is actually written so a restart doesn't find this fill
            # still "pending" and replay it a second time.
            if hasattr(ss['executor'], 'ack_journal_entry'):
                ss['executor'].ack_journal_entry(order.order_id)
            alerter.fill(
                side        = order.side.value,
                symbol      = sym,
                quantity    = order.quantity,
                price       = order.price,
                total_value = order.total_value,
                pnl         = pnl,
                exchange    = cfg.exchange.exchange,
                reason      = f"strategy {order.side.value.lower()} signal"
                              + (f" — {filter_reason}" if filter_reason else ""),
            )
        else:
            # order can be None here (see the qty<=0-after-FILLED guard
            # above) — guard against .reject_reason on None instead of
            # crashing the loop.
            _reject_reason = (
                order.reject_reason if order
                else "internal: FILLED order returned qty<=0 — see log for detail"
            )
            _reject_side = order.side.value if order else final_signal.value
            display.reject(_reject_reason or "")
            alerter.error(
                f"ORDER REJECTED [{sym}] {_reject_side}: "
                f"{_reject_reason or 'unknown reason'}"
            )

    # Generic stuck-loop watchdog: a strategy BUY/SELL that keeps
    # failing every tick (not just SL/TP — that has its own counter).
    _exec_filled = bool(
        order and order.status == OrderStatus.FILLED and order.quantity > 0
    )
    stuck_detector.record(
        f"execute:{sym}:{final_signal.value}",
        ok=_exec_filled,
        detail="" if _exec_filled else (
            getattr(order, "reject_reason", None) or "no order returned"
        ),
    )
    return order


def _process_discovered_sell_fill(
    sym: str, ss: dict, order: Order, reason: str,
    *, capital_pool: CapitalPool, risk: RiskManager, alerter, trade_log,
) -> None:
    """
    Route a SELL fill DISCOVERED outside the normal execute() call path —
    specifically, a native stop found to have filled (fully or partially)
    during sync_protective_stop()'s own cancel-and-verify cycle, called via
    _resync_native_stop() — through the SAME PositionManager / state
    machine / capital pool / risk / trade-log bookkeeping a strategy-driven
    SELL gets via _execute_approved_signal.

    2026-09-18 follow-up review finding (P1): sync_protective_stop() and
    _resync_native_stop() were returning this fill_order, and every call
    site was discarding it. The EXECUTOR's own cash/position updated
    correctly (via LiveExecutor._record_stop_triggered_fill), but
    PositionManager, the state machine, the capital pool, the risk fill
    counter, and trade_log never learned about it at the time — only
    reachable (for trade_log alone) via a restart replaying the pending
    journal entry, which does nothing for the other four in-memory
    representations. This closes that gap for the RUNTIME case (mid-run,
    after this symbol's pm/sm/capital_pool already exist) — the startup
    case (_reconcile_resting_stop_quantity, called from inside
    LiveExecutor.__init__ before any of this exists yet) is unaffected and
    self-consistent for a different reason — see that method's own comment.

    order.side must be SELL — a native stop never fires on a BUY.
    """
    if order.side != OrderSide.SELL:
        logger.error(
            "_process_discovered_sell_fill called with a non-SELL order "
            "for %s (%s) — ignoring, this should never happen.",
            sym, order.side,
        )
        return

    pnl = ss['pm'].on_sell(order.price, order.quantity)
    risk.record_fill(sym)
    ss['sm'].on_fill(Signal.SELL, order.price)

    # 2026-09-18 PASS-3 review finding (P1): this used to unconditionally
    # clear trail_peak/atr_sl and let on_fill's COOLDOWN transition stand,
    # regardless of whether a residual position actually remained after a
    # PARTIAL discovered exit. Reproduced: LONG with 0.002 BTC and a trail
    # peak of 95000, a discovered 0.001 BTC stop fill left 0.001 BTC still
    # held, but state became COOLDOWN with trail_peak/atr_sl reset to 0 —
    # the residual position's own exit levels, erased. Fixed to derive the
    # transition from remaining inventory, mirroring EXACTLY the pattern
    # the existing partial-TP block (same file) already uses for the
    # identical situation (a SELL that doesn't fully close the position).
    if not ss['pm'].has_position:
        # Full exit — the COOLDOWN transition on_fill already made is correct.
        ss['trail_peak']   = 0.0
        ss['partial_done'] = False
        ss['atr_sl']       = 0.0
        capital_pool.release(sym, ss['executor'].cash)
        ss['native_stop_is_trailing'] = False
    else:
        # Genuine residual remains — force back to LONG (same
        # on_fill-then-recover_long sequence the partial-TP block uses)
        # and PRESERVE the residual's existing trailing/ATR exit state
        # rather than erasing it; the stop that produced this fill is,
        # per sync_protective_stop's own contract, either already
        # resolved (fully closed — an unusual under-sized-stop edge case,
        # flagged below) or still correctly tracked with its remaining
        # quantity auto-adjusted by the exchange itself (a "partial"
        # outcome deliberately keeps the order's id) — no additional
        # resync is triggered here to avoid re-entering the same
        # cancel/verify cycle this fill was already discovered inside of.
        ss['partial_done'] = True
        ss['sm'].recover_long(order.price)
        if not ss['executor'].has_resting_stop:
            logger.error(
                "UNPROTECTED RESIDUAL [%s]: a discovered stop fill closed "
                "to a CONFIRMED TERMINAL state but left %.8f still held with "
                "no resting native stop — the stop was sized smaller than "
                "the position. Manual review needed.",
                sym, ss['pm'].quantity,
            )
            alerter.error(
                f"UNPROTECTED RESIDUAL [{sym}]: {ss['pm'].quantity:.8f} still "
                f"held after a discovered stop fill, but no native stop is "
                f"resting — check Kraken and re-arm manually if needed."
            )

    display.fill(order.side.value, order.quantity, sym, order.price, order.total_value, pnl)
    trade_log.log_fill(
        side          = "SELL",
        symbol        = sym,
        quantity      = order.quantity,
        price         = order.price,
        pnl           = pnl,
        exchange      = cfg.exchange.exchange,
        signal_reason = reason,
        fee_cost      = order.fee_cost,
        fee_currency  = order.fee_currency,
        exec_key      = order.exec_key,
    )
    if hasattr(ss['executor'], 'ack_journal_entry'):
        ss['executor'].ack_journal_entry(order.order_id)
    alerter.fill(
        side        = "SELL",
        symbol      = sym,
        quantity    = order.quantity,
        price       = order.price,
        total_value = order.total_value,
        pnl         = pnl,
        exchange    = cfg.exchange.exchange,
        reason      = reason,
    )


def _execute_ranked_dynamic_buys(
    buy_queue: list,
    *,
    capital_pool: CapitalPool,
    risk: RiskManager,
    account_value_fn,
    alerter,
    trade_log,
    stuck_detector,
    is_indicator: bool,
    max_concurrent: int,
    symbol_state: "dict | None" = None,
    live_exchange=None,
    correlation_fn=None,
    correlation_threshold: "float | None" = None,
    refresh_price_fn=None,
    max_price_deviation_pct: "float | None" = None,
) -> "tuple[list, dict]":
    """
    Rank every BUY signal gathered this tick (bot.dynamic.ranking — ADX
    then 24h volume, both already known at decision time, no future data)
    and execute in that order, checking capital_pool.can_open_position(),
    the correlation gate, and risk.evaluate() FRESH for each candidate —
    never reusing an earlier snapshot — so an earlier-ranked fill in this
    same batch (which changes account_value(), the slot count, AND which
    symbols now hold a position) is correctly reflected before the next
    candidate is decided.

    Correlation recheck (FIXED 2026-09-13, real gap confirmed by external
    review): section 2f's own correlation gate runs once per symbol at
    GATHER time, checking peers that ALREADY hold a position — but two
    simultaneously-ranked candidates correlated with EACH OTHER both pass
    that check (neither holds a position yet at gather time), then both
    could fill here. Rechecking here, after each fill, catches this: once
    the first of two correlated candidates fills, the second's recheck
    sees it as a newly-open peer and blocks. symbol_state/live_exchange/
    correlation_fn are optional (default no-op) purely so unit tests that
    don't care about correlation can omit them without a network
    dependency — bot.main.run() always passes the real ones.

    Unresolved-order capital reservation (FIXED 2026-09-13, real gap
    confirmed by external review): LiveExecutor.execute() can return None
    for a BUY not because nothing happened, but because the fill quantity
    is genuinely AMBIGUOUS (a limit order with filled=0 that isn't
    confirmed closed/cancelled either — see live_executor.py's own
    "qty=0 GUARD" comments) — the order may still be resting live on the
    exchange. Treating None as "nothing happened, slot still free" (the
    original design) let a DIFFERENT ranked candidate claim that same slot
    in the same batch, risking two real orders both able to consume the
    same capital. Fixed: an ambiguous (None) BUY outcome now conservatively
    calls capital_pool.allocate(rsym) itself — holding the slot until the
    ambiguity resolves. This does not create a NEW way to get stuck: once
    the symbol is confirmed flat, _retire_dynamic_symbol_if_eligible's own
    flat-check (unaffected by this) still frees it the moment it drops out
    of eligibility, exactly as it already does for any other flat admitted
    symbol. Full automatic reconciliation of what the ambiguous order
    actually did still relies on LiveExecutor's existing untracked-order
    adoption firing on this symbol's next real submission attempt (the
    same mechanism the fixed roster already depends on) — not deepened,
    not fully closed, documented like the analogous accepted stock-bot gap.

    This is what "reserve capital for pending orders and prevent double
    allocation" otherwise reduces to under this bot's existing design for
    a CLEANLY resolved fill: execute() is synchronous/blocking (a
    limit-chase fully resolves before returning in the non-ambiguous case)
    and this function processes candidates strictly sequentially (one at a
    time, never concurrently), so capital_pool.allocate() firing on a
    CONFIRMED fill can never be double-claimed.

    Price refresh (FIXED 2026-09-13, real gap confirmed by external
    review: "refresh liquidity checks and prices before executing queued
    orders"): each candidate's `price` was captured at GATHER time, during
    the per-symbol loop — but the ranked pass runs AFTER every other
    symbol in that loop has already been processed (each doing its own
    network round trips), so a candidate ranked last could execute against
    a meaningfully stale price. refresh_price_fn (optional — defaults to a
    no-op so unit tests can omit it) re-fetches the current price
    immediately before each candidate's risk-check/execute; if it has
    moved more than max_price_deviation_pct since gather time, the
    candidate is skipped this tick (reason "stale_price") rather than
    executed on stale sizing math or a limit order routed far from the
    current market — it re-qualifies naturally on a fresh signal next
    tick. Within tolerance, the refreshed price (not the gather-time one)
    is what actually gets risk-evaluated and executed against; trade_qty
    itself is NOT re-derived from the new price (a bounded, deliberately
    narrower fix than re-running the full ATR/notional sizing pipeline —
    a small in-tolerance price move doesn't materially change the
    position's risk profile the way an out-of-tolerance one would).

    buy_queue entries are dicts with keys: sym, ss, final_signal, price,
    trade_qty, raw_signal, filter_reason, adx, quote_volume (exactly what
    bot.main.run()'s section 9 gathers). Returns (filled, blocked): the
    list of symbols that actually filled this pass, and a {symbol: reason}
    map of every OTHER candidate skipped this pass — the latter exists so
    the dashboard snapshot (written AFTER this call, not before — FIXED
    2026-09-13, external review: "its blocked-reasons snapshot is written
    before execution") reflects what actually happened this tick instead
    of an always-empty dict frozen before any candidate was evaluated.
    """
    correlation_fn = correlation_fn or fetch_correlation
    correlation_threshold = correlation_threshold if correlation_threshold is not None else CORRELATION_THRESHOLD
    max_price_deviation_pct = (
        max_price_deviation_pct if max_price_deviation_pct is not None
        else (cfg.exchange.max_slippage_pct or 0.02)
    )

    ranked = rank_buy_signals([
        RankableSignal(symbol=c['sym'], adx=c['adx'], quote_volume=c['quote_volume'])
        for c in buy_queue
    ])
    queue_by_sym = {c['sym']: c for c in buy_queue}
    filled: list = []
    blocked: dict = {}

    for rsym in ranked:
        cand = queue_by_sym[rsym]
        if not capital_pool.can_open_position(rsym):
            if not cand['ss'].get('last_buy_block_alert'):
                cand['ss']['last_buy_block_alert'] = "capital_pool"
            blocked[rsym] = "capital_pool"
            logger.info(
                "Dynamic ranked BUY [%s]: capital_pool has no free slot"
                " (%d/%d used) — skipped this tick",
                rsym, len(capital_pool.allocated_symbols), max_concurrent,
            )
            continue

        exec_price = cand['price']
        if refresh_price_fn is not None:
            try:
                _fresh_price = refresh_price_fn(rsym)
            except Exception as _price_exc:
                _fresh_price = None
                logger.warning(
                    "Dynamic ranked BUY [%s]: price refresh failed (%s) —"
                    " using gather-time price", rsym, _price_exc,
                )
            if _fresh_price and _fresh_price > 0 and cand['price'] > 0:
                _deviation = abs(_fresh_price - cand['price']) / cand['price']
                if _deviation > max_price_deviation_pct:
                    blocked[rsym] = "stale_price"
                    logger.warning(
                        "Dynamic ranked BUY [%s]: price moved %.2f%% since"
                        " gather (%.6f → %.6f) > %.2f%% tolerance — skipped"
                        " this tick, will re-qualify on a fresh signal next tick",
                        rsym, _deviation * 100, cand['price'], _fresh_price,
                        max_price_deviation_pct * 100,
                    )
                    continue
                exec_price = _fresh_price

        if symbol_state is not None and live_exchange is not None:
            _open_peers = [
                _peer for _peer, _pss in symbol_state.items()
                if _peer != rsym and _pss['pm'].has_position
            ]
            _corr_blocked = False
            for _peer in _open_peers:
                _corr = correlation_fn(live_exchange, rsym, _peer)
                if _corr is not None and _corr > correlation_threshold:
                    logger.warning(
                        "Dynamic ranked BUY [%s]: correlation %.2f with"
                        " %s (filled earlier this batch or already open)"
                        " > %.2f — skipped this tick",
                        rsym, _corr, _peer, correlation_threshold,
                    )
                    if not cand['ss'].get('last_buy_block_alert'):
                        cand['ss']['last_buy_block_alert'] = "correlation"
                    blocked[rsym] = "correlation"
                    _corr_blocked = True
                    break
            if _corr_blocked:
                continue

        fresh_approval = risk.evaluate(
            cand['final_signal'], exec_price,
            cand['ss']['executor'].portfolio, cand['trade_qty'],
            account_value=account_value_fn(), symbol=rsym,
        )
        if not fresh_approval:
            blocked[rsym] = "risk_manager"
            logger.info(
                "Dynamic ranked BUY [%s]: risk gate re-check failed at"
                " execution time (%s) — skipped this tick",
                rsym, fresh_approval.message,
            )
            continue
        order = _execute_approved_signal(
            rsym, cand['ss'], cand['final_signal'], exec_price,
            cand['trade_qty'], cand['raw_signal'], cand['filter_reason'],
            capital_pool=capital_pool, risk=risk, alerter=alerter,
            trade_log=trade_log, stuck_detector=stuck_detector,
            is_indicator=is_indicator,
        )
        if order is not None and order.status == OrderStatus.FILLED:
            filled.append(rsym)
        elif order is None:
            # Ambiguous outcome — see docstring. Conservatively hold the
            # slot rather than let another candidate claim it. No-op if
            # already allocated.
            if not capital_pool.is_allocated(rsym):
                capital_pool.allocate(rsym)
                logger.warning(
                    "Dynamic ranked BUY [%s]: execute() returned an"
                    " AMBIGUOUS outcome (order status uncertain) —"
                    " conservatively holding its capital-pool slot until"
                    " a future reconciliation confirms the real outcome.",
                    rsym,
                )
            blocked[rsym] = "unresolved_order"
        else:
            blocked[rsym] = "order_rejected"

    return filled, blocked


# ---------------------------------------------------------------------------
# Dynamic universe integration (2026-09-13) — discovery/screening/ranking
# reused unchanged from bot/dynamic/; everything below is admission,
# retirement, and lifecycle wiring INTO this file's existing symbol_state /
# executors / capital_pool / risk-gate / execute() machinery. No separate
# trading engine. Gated entirely behind cfg.dynamic.enabled — when False
# (the default), none of this is ever called and the fixed BTC/CAD+SOL/CAD
# roster behaves byte-identically to before this feature existed.
# ---------------------------------------------------------------------------

def _new_symbol_state_dict(strategy, sm, pm, executor, last_ts_ms=None) -> dict:
    """
    The exact symbol_state[sym] shape every part of run()'s tick loop
    expects — single source of truth for both the initial static-roster
    init (bot startup) and dynamic runtime admission, so the two paths can
    never drift into producing different shapes (extracted 2026-09-13;
    previously this was a literal dict typed out at each startup call site
    with no dynamic-admission equivalent at all).
    """
    return {
        'strategy':          strategy,
        'sm':                sm,
        'pm':                pm,
        'executor':          executor,
        'last_ts_ms':        last_ts_ms,
        'trail_peak':        0.0,
        'partial_done':      False,
        'atr_sl':            0.0,
        'native_stop_price': None,
        'native_stop_is_trailing': False,
        'candle_feed_stale': False,
        'price_feed_stale':  False,   # 2026-09-18: live-tick fetch failing — blocks new BUYs
        'last_price':        0.0,
        'err_count':         0,
        'drift_count':       0,
        'drift_acked':       0.0,
        'last_candle_time':  time.time(),
        'mtf_1d_closes':     [],
        'dash_signal':       "HOLD",
        'dash_rsi':          None,
        'dash_trend':        None,
        'dash_filter':       "",
        'dash_block':        "",
        'last_buy_block_alert':    "",
        'last_buy_signal_alerted': False,
        'exit_fail_count':   0,
    }


def _make_dynamic_executor(sym: str, state_path: "str | None" = None) -> LiveExecutor:
    """
    Build a LiveExecutor for a dynamically-admitted symbol using EXACTLY
    the same construction parameters (exchange, order type, adopt-external-
    holdings, native-stop-loss, slippage guard) as the fixed startup
    roster's own executor-construction block — see run()'s "Capital pool"
    setup above — so a dynamically-admitted symbol's fee accounting,
    slippage guard, and native-stop handling are identical to BTC/CAD's the
    moment this feature is ever activated. dry_run mirrors the fixed
    roster's own rule exactly: True whenever paper_mode is on, else
    whatever LIVE_TRADING/DRY_RUN resolves to in .env — no dynamic-specific
    safety flag is introduced here; the same logs/HALT + risk-gate
    protection that already covers BTC/CAD and SOL/CAD covers this too.
    starting_cash is always 0.0 — the caller funds it via
    capital_pool.slot_cash_for(sym) right after construction, matching the
    fixed roster's own pattern, so scanning more coins never changes any
    single position's sizing basis.
    """
    if state_path is None:
        state_path = f"logs/live_state_{sym.replace('/', '_')}.json"
    return LiveExecutor(
        exchange_id              = cfg.exchange.exchange,
        symbol                   = sym,
        api_key                  = cfg.exchange.api_key,
        api_secret               = cfg.exchange.api_secret,
        starting_cash            = 0.0,
        dry_run                  = cfg.paper.paper_mode or cfg.exchange.dry_run,
        order_type               = cfg.exchange.order_type,
        state_path               = state_path,
        adopt_external_holdings  = cfg.exchange.adopt_external_holdings,
        native_stop_loss_enabled = cfg.exchange.native_stop_loss_enabled,
        max_slippage_pct         = cfg.exchange.max_slippage_pct,
    )


def _admit_dynamic_symbol(
    sym: str, live_exchange, timeframe: str, capital_pool: CapitalPool,
) -> "tuple[dict | None, str | None]":
    """
    Initialize strategy, historical warmup, candle timestamp, executor,
    position manager, and trading state for a newly-eligible symbol —
    WITHOUT restarting the process. Reuses build_strategy() /
    _warmup_strategy() unchanged (the same functions the fixed startup
    roster calls), so a dynamically-admitted symbol's strategy is
    byte-identical in behavior to BTC/CAD's or SOL/CAD's — this is the
    "preserve the complete strategy, new coins get explicit defaults"
    requirement: exit_params_for(sym) (used throughout the tick loop, not
    here) already merges any TAKE_PROFIT_PCT_<BASE>-style override for this
    symbol's base over the shared defaults, or falls through to the shared
    defaults untouched if none exists — a newly admitted coin with no
    override gets exactly the documented shared TAKE_PROFIT_PCT/
    STOP_LOSS_PCT/ATR_SL_MULT etc., not a silently different behavior.

    If the executor's OWN persisted state (loaded inside its constructor)
    shows an existing position — a restart re-admitting a symbol that was
    already trading, not a genuinely fresh candidate — the exact same
    recovery seeding the fixed roster's restart-recovery block applies
    (pm.seed/sm.recover_long/native-stop mirror) is applied here too, PLUS
    capital_pool.allocate(sym) — closing the "restart recovery restores
    executor positions but not capital-pool allocations" gap for dynamic
    symbols. (The fixed roster gets the equivalent fix in run()'s own
    restart-recovery block — see the call there.)

    Never raises: returns (None, error_str) on any failure (most likely a
    warmup network error) so one bad candidate can never prevent the tick
    loop from continuing to manage every OTHER symbol's existing positions
    and pending orders this cycle.
    """
    try:
        strat = build_strategy()
        last_ts = _warmup_strategy(strat, live_exchange, timeframe, symbol=sym)
        executor = _make_dynamic_executor(sym)
        executor._portfolio.cash = capital_pool.slot_cash_for(sym)
        try:
            executor._save_state()
        except Exception as _save_exc:
            logger.warning("Dynamic symbol %s: state save after funding failed: %s", sym, _save_exc)
        sm = TradingStateMachine(cooldown_ticks=cfg.risk.cooldown_ticks)
        pm = PositionManager()
        ss = _new_symbol_state_dict(strategy=strat, sm=sm, pm=pm, executor=executor, last_ts_ms=last_ts)

        if executor.position > 1e-9:
            pm.seed(
                quantity=executor.position, avg_entry=executor.avg_entry,
                realized_pnl=executor.portfolio.realized_pnl,
            )
            sm.recover_long(executor.avg_entry)
            ss['native_stop_price'], ss['native_stop_is_trailing'] = _seed_native_stop_state(executor)
            capital_pool.allocate(sym)
            logger.warning(
                "Dynamic symbol %s admitted WITH an existing position"
                " (qty=%.6f @ %.2f) — restart-recovery seeding applied",
                sym, executor.position, executor.avg_entry,
            )
        return ss, None
    except Exception as exc:
        return None, str(exc)


def _retire_dynamic_symbol_if_eligible(sym: str, ss: dict, capital_pool: CapitalPool) -> bool:
    """
    Retire a dynamically-admitted symbol from active management ONLY once
    it is genuinely flat (no open position) AND has no resting protective
    order left outstanding — never while either is true, no matter how
    long it's been ineligible. Releases its capital-pool slot (a no-op if
    it was never allocated one). Returns whether it was retired.
    """
    executor = ss['executor']
    if executor.position > 1e-9:
        return False
    if getattr(executor, 'has_resting_stop', False):
        return False
    capital_pool.release(sym, executor.cash)
    return True


def _sync_dynamic_universe(
    symbol_state: dict,
    executors: dict,
    dynamic_admitted: set,
    screener: DynamicUniverseScreener,
    live_exchange,
    timeframe: str,
    capital_pool: CapitalPool,
    slot_cash_estimate: float,
) -> "tuple[list, list, object]":
    """
    One full dynamic-universe refresh cycle: discover eligible symbols,
    admit new ones, retire flat-and-no-longer-eligible ones. Mutates
    symbol_state/executors/dynamic_admitted IN PLACE (same convention as
    the rest of run()'s setup code) and returns
    (admitted_this_cycle, retired_this_cycle, screen_result) for
    logging/dashboard/testing.

    Only ever retires a symbol previously admitted by THIS function
    (tracked via dynamic_admitted) — the original fixed roster (BTC/CAD,
    SOL/CAD, or whatever UNIVERSE_WHITELIST/registry seeded at startup) is
    never touched here regardless of what the screener says about it.

    A discovery failure (screener.discover() itself never raises — see its
    own fail-safe/cache docstring) or a single candidate's admission
    failure never raises out of this function — existing positions are
    always still managed by the caller's tick loop regardless of what
    happens here.
    """
    screen = screener.discover(live_exchange, slot_cash=slot_cash_estimate)
    eligible = set(screen.eligible_symbols)

    admitted: list[str] = []
    for sym in eligible:
        if sym in symbol_state:
            continue
        ss, err = _admit_dynamic_symbol(sym, live_exchange, timeframe, capital_pool)
        if ss is None:
            logger.warning("Dynamic universe: failed to admit %s: %s", sym, err)
            continue
        symbol_state[sym] = ss
        executors[sym] = ss['executor']
        dynamic_admitted.add(sym)
        admitted.append(sym)
        logger.info("Dynamic universe: admitted %s", sym)

    retired: list[str] = []
    for sym in list(dynamic_admitted):
        if sym in eligible:
            continue
        ss = symbol_state.get(sym)
        if ss is None:
            dynamic_admitted.discard(sym)
            continue
        if _retire_dynamic_symbol_if_eligible(sym, ss, capital_pool):
            del symbol_state[sym]
            del executors[sym]
            dynamic_admitted.discard(sym)
            retired.append(sym)
            logger.info("Dynamic universe: retired %s (flat, no longer eligible)", sym)

    return admitted, retired, screen


def _write_dynamic_universe_dashboard(
    path: str, screen, symbol_state: dict, dynamic_admitted: set,
    capital_pool: CapitalPool, blocked: dict, dry_run: bool = True,
) -> None:
    """
    Snapshot for unified_dashboard.py's dynamic-universe card — same JSON
    shape the (now-retired-for-evaluation) standalone paper runner wrote,
    so the existing dashboard card keeps working unchanged against the
    LIVE integration's output too. Never raises.

    dry_run (FIXED 2026-09-13, external review — "the dashboard still
    labels live integration PAPER — not live"): the CARD's hardcoded label
    was accurate for the retired standalone runner (dry_run always True,
    unconditionally) but not for this integration, where dry_run mirrors
    whatever the real fixed-roster executors use. The renderer reads this
    field to show the true mode instead of an always-"paper" label.
    """
    try:
        payload = {
            "generated_at": datetime.now(_tz.utc).isoformat(),
            "enabled": cfg.dynamic.enabled,
            "dry_run": dry_run,
            "discovered": len(screen.all_candidates()) if screen else 0,
            "eligible": screen.eligible_symbols if screen else [],
            "rejected": [
                {"symbol": c.symbol, "reasons": c.reasons} for c in (screen.rejected if screen else [])
            ][:50],
            "admitted": sorted(dynamic_admitted),
            "open_positions": [
                s for s in dynamic_admitted
                if s in symbol_state and symbol_state[s]['executor'].position > 1e-9
            ],
            "blocked_this_cycle": blocked,
            "paper_cash_available": capital_pool.available_cash,
            "paper_total_capital": capital_pool.total_capital,
            "fills_count": None,   # live integration — see trade_log/ibkr_trades.csv for real fill history, not tracked separately here
            "screen_stale": screen.stale if screen else True,
            "live_integration": True,  # distinguishes this from the retired standalone paper runner's own dashboard writes
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)
    except Exception as exc:
        logger.warning("Dynamic universe: dashboard snapshot write failed: %s", exc)


# ---------------------------------------------------------------------------
# Two-way Telegram control — command bodies (2026-08-20)
#
# Extracted as standalone functions (same "extract for testability" pattern
# as _check_halt_flag/_seed_native_stop_state/_evaluate_drift above) so they
# can be unit-tested and, critically, so it's mechanically checkable that
# none of them import or call any LiveExecutor TRADING method — every
# function here takes plain data (executors/symbol_state dicts, a
# RiskManager, a flag-file path) and either only READS attributes or only
# does open()/os.remove() on the halt flag file. run() wires these into
# closures over its own real objects and registers them with
# TelegramCommandPoller — see the "Two-way Telegram control" block below.
# ---------------------------------------------------------------------------

def _format_symbol_status(sym: str, exc, ss: dict) -> str:
    """Read-only formatting of one symbol's position/cash/PF/regime.
    Touches only: exc.avg_entry/.position/.cash/.portfolio.total_value()
    (all read-only properties), pm.unrealized_pnl()/.realized_pnl/.history
    (read-only), ss['strategy'].last_regime (read-only property)."""
    pm    = ss.get('pm')
    px    = ss.get('last_price') or getattr(exc, 'avg_entry', 0.0) or 0.0
    pos   = getattr(exc, 'position', 0.0)
    entry = getattr(exc, 'avg_entry', 0.0)
    cash  = getattr(exc, 'cash', 0.0)
    total = exc.portfolio.total_value(px) if px else cash
    upnl  = pm.unrealized_pnl(px) if pm and px else 0.0
    rpnl  = pm.realized_pnl if pm else 0.0
    sell_pnls = [r.pnl for r in pm.history if r.pnl is not None] if pm else []
    wins   = sum(p for p in sell_pnls if p > 0)
    losses = sum(p for p in sell_pnls if p < 0)
    if losses < 0:
        pf_s = f"{wins / abs(losses):.2f}"
    elif wins > 0:
        pf_s = "inf (no losses yet)"
    else:
        pf_s = "n/a (no closed trades)"
    regime = getattr(ss.get('strategy'), 'last_regime', None) or "n/a"
    return (
        f"{sym}\n"
        f"  Position: {pos:.6f} @ avg ${entry:,.2f}\n"
        f"  Cash: ${cash:,.2f}  Total: ${total:,.2f}\n"
        f"  Realized P&L: ${rpnl:+.2f}  Unrealized: ${upnl:+.2f}\n"
        f"  PF: {pf_s}  Regime: {regime}"
    )


def _status_crypto_text(
    executors: dict, symbol_state: dict, risk: "RiskManager",
    live_trading: bool, dry_run: bool,
) -> str:
    """/status_crypto body — read-only across every tracked symbol."""
    mode = "LIVE" if live_trading else ("DRY RUN" if dry_run else "PAPER")
    halt_s = "🔴 ENGAGED" if risk.config.halt else "🟢 clear"
    if risk.kill_switch_tripped:
        halt_s += " (kill switch TRIPPED — sticky, needs manual clear)"
    lines = [f"📊 Crypto bot — {mode}", f"Halt: {halt_s}"]
    for sym, exc in executors.items():
        lines.append("")
        lines.append(_format_symbol_status(sym, exc, symbol_state.get(sym, {})))
    return "\n".join(lines)


def _pause_crypto_flag(flag_path: str, loop_interval: int) -> str:
    """/pause_crypto body — writes logs/HALT only. Reuses the SAME manual
    halt mechanism _check_halt_flag() already polls every tick; does not
    call risk.halt() directly and does not add a second halt path."""
    try:
        with open(flag_path, "a"):
            pass
    except Exception as exc:
        return f"⚠️ Could not write {flag_path}: {exc}"
    return (
        f"⏸️ Halt flag written ({flag_path}). Takes effect on the next tick "
        f"(≤{loop_interval}s). BUY and strategy SELL will be blocked; "
        f"SL/TP exits still fire."
    )


def _resume_crypto_flag(flag_path: str, loop_interval: int) -> str:
    """/resume_crypto body — removes logs/HALT only. Same single-mechanism
    reasoning as _pause_crypto_flag."""
    try:
        if os.path.exists(flag_path):
            os.remove(flag_path)
            return (
                f"▶️ Halt flag removed ({flag_path}). Trading resumes on "
                f"the next tick (≤{loop_interval}s)."
            )
        return "Halt flag was not set — nothing to resume."
    except Exception as exc:
        return f"⚠️ Could not remove {flag_path}: {exc}"


def _status_stock_text(load_stock_state=None) -> str:
    """/status_stock body — read-only direct file read of the STOCK bot's
    own state, same pattern unified_dashboard.py already uses to render
    both bots' cards from one process. Deliberately NOT a second Telegram
    poller: the stock bot has no getUpdates consumer, and this crypto
    process must stay the ONLY consumer of the shared bot token (see
    bot/alerts/telegram_control.py's module docstring). load_stock_state is
    injectable for tests; defaults to the real unified_dashboard reader."""
    try:
        if load_stock_state is None:
            from unified_dashboard import _load_stock_state as load_stock_state
        state = load_stock_state()
    except Exception as exc:
        return f"⚠️ Could not read stock bot state: {exc}"
    if state is None:
        return "📈 Stock bot: no state file found (offline or never traded)."
    cash      = float(state.get("cash", 0) or 0)
    starting  = float(state.get("starting_cash", 0) or 0)
    rpnl      = float(state.get("realized_pnl", 0) or 0)
    positions = state.get("positions", {}) or {}
    pos_val = sum(
        float(p.get("shares", 0)) * float(p.get("avg_cost", 0))
        for p in positions.values()
    )
    total = cash + pos_val
    badge = "IBKR PAPER" if state.get("executor") == "ibkr" else "PAPER"
    return (
        f"📈 Stock bot — {badge}\n"
        f"Cash: ${cash:,.2f}  Open positions: {len(positions)}\n"
        f"Position value (est.): ${pos_val:,.2f}  Total: ${total:,.2f}\n"
        f"Realized P&L: ${rpnl:+.2f}  Starting cash: ${starting:,.2f}"
    )


def _help_crypto_text() -> str:
    return (
        "Commands:\n"
        "/status_crypto — position, cash, P&L, PF, regime, halt state\n"
        "/pause_crypto — engage the manual halt (logs/HALT)\n"
        "/resume_crypto — lift the manual halt\n"
        "/status_stock — read-only stock bot snapshot\n"
        "/help_crypto — this message"
    )


# ---------------------------------------------------------------------------
# Daily health digest — a proactive once-a-day both-bots status push, so a VPS
# deployment gives a "yes it's fine" every morning (and its absence is itself a
# signal) instead of only reactive alerts. Built 2026-08-27 after the native-
# stop deadlock ran 8 min invisibly.
# ---------------------------------------------------------------------------

def _recent_error_count(log_path: str, ref: datetime, hours: int = 24,
                        max_bytes: int = 800_000) -> int:
    """Count ' ERROR ' log lines in the last `hours` before `ref` (naive local
    time, matching the log's own timestamp format). Reads only the file tail."""
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()   # drop the partial first line
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return 0
    cutoff = ref - timedelta(hours=hours)
    n = 0
    for line in data.splitlines():
        if " ERROR " not in line:
            continue
        try:
            ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts >= cutoff:
            n += 1
    return n


def _health_digest_text(
    now: datetime, crypto_status: str, stock_status: str,
    open_orders: list, crypto_errs: int, stock_errs: int,
    attention: list[str],
) -> str:
    """Pure composer for the daily digest — attention header + both bots +
    open-order list + 24h error counts."""
    head = "⚠️ NEEDS ATTENTION" if attention else "✅ all systems normal"
    parts = [
        "📋 DAILY HEALTH DIGEST",
        now.strftime("%Y-%m-%d %H:%M local"),
        head,
    ]
    if attention:
        parts.append("• " + "\n• ".join(attention))
    parts += ["", crypto_status]
    if open_orders:
        parts.append("")
        parts.append(f"Open exchange orders ({len(open_orders)}):")
        for o in open_orders[:8]:
            parts.append(
                f"  {o.get('symbol','?')} {o.get('type','?')} "
                f"{o.get('side','?')} {o.get('amount','?')}"
            )
    else:
        parts.append("\nOpen exchange orders: none")
    parts += ["", stock_status, ""]
    parts.append(f"Errors last 24h — crypto: {crypto_errs}  stock: {stock_errs}")
    return "\n".join(parts)


def _maybe_send_health_digest(
    executors: dict, symbol_state: dict, risk, alerter,
    live_trading: bool, dry_run: bool, now: datetime,
    stuck_detector=None,
) -> None:
    """Once/day at HEALTH_DIGEST_TIME (local, default 08:00; 'off' disables).
    Records the run date BEFORE composing so a send failure can't re-fire every
    tick. Never raises."""
    _time = os.getenv("HEALTH_DIGEST_TIME", "08:00").strip()
    if _time.lower() in ("", "off", "0", "false"):
        return
    try:
        state: dict = {}
        if os.path.exists(_AUDIT_STATE_PATH):
            with open(_AUDIT_STATE_PATH, encoding="utf-8") as f:
                state = json.load(f) or {}
        if not _audit_due(state.get("health_digest"), now, _time):
            return
        state["health_digest"] = now.date().isoformat()
        from bot.atomic_json import atomic_write_json
        atomic_write_json(_AUDIT_STATE_PATH, state, indent=0)
    except Exception as exc:
        logger.warning("Health digest schedule check failed: %s", exc)
        return

    try:
        crypto_status = _status_crypto_text(
            executors, symbol_state, risk, live_trading, dry_run,
        )
        stock_status = _status_stock_text()
        try:
            _ex = next(iter(executors.values()))._exchange
            open_orders = _ex.fetch_open_orders() or []
        except Exception:
            open_orders = []
        crypto_errs = _recent_error_count(os.path.join(_log_dir, "trade_bot.log"), now)
        stock_errs  = _recent_error_count(
            os.path.join(os.path.dirname(_log_dir), "logs", "stock_bot.log"), now,
        )

        attention: list[str] = []
        if risk.config.halt:
            attention.append("manual HALT is engaged")
        if risk.kill_switch_tripped:
            attention.append("kill switch TRIPPED (sticky)")
        for _sym, _ss in symbol_state.items():
            if _ss.get("exit_fail_count", 0) > 0:
                attention.append(f"{_sym}: {_ss['exit_fail_count']} failed SL/TP exits")
            if _ss.get("candle_feed_stale"):
                attention.append(f"{_sym}: candle feed stale")
            # 2026-09-18 review finding (P2-10): surface the same execution-
            # health signals the review calls for (quote freshness,
            # protection state, unresolved-order/reconciliation age) in the
            # one place a human already checks daily, rather than only in
            # scattered per-event alerts.
            if _ss.get("price_feed_stale"):
                attention.append(f"{_sym}: live price feed stale")
            _exc = _ss.get("executor")
            if _exc is not None:
                if not getattr(_exc, "state_write_healthy", True):
                    attention.append(f"{_sym}: last state save failed — new BUYs blocked")
                if not getattr(_exc, "startup_sync_healthy", True):
                    attention.append(f"{_sym}: startup balance/position sync failed this run")
                _pending_entries = getattr(_exc, "pending_journal_entries", None) or []
                if _pending_entries:
                    attention.append(
                        f"{_sym}: {len(_pending_entries)} unacked fill journal "
                        f"entr{'y' if len(_pending_entries) == 1 else 'ies'} — "
                        f"trade_log may be missing a fill"
                    )
                if (
                    getattr(_exc, "position", 0) > 0
                    and cfg.exchange.native_stop_loss_enabled
                    and not getattr(_exc, "has_resting_stop", True)
                ):
                    attention.append(f"{_sym}: holding a position with no resting native stop")
        if stuck_detector is not None:
            for _k, _n in stuck_detector.failing_keys().items():
                attention.append(f"stuck loop: {_k} ({_n} consecutive failures)")
        if crypto_errs >= 20:
            attention.append(f"{crypto_errs} crypto errors in 24h")
        if stock_errs >= 20:
            attention.append(f"{stock_errs} stock errors in 24h")

        alerter.message(_health_digest_text(
            now, crypto_status, stock_status, open_orders,
            crypto_errs, stock_errs, attention,
        ))
        logger.info("Daily health digest sent (%d attention item(s))", len(attention))
    except Exception as exc:
        logger.warning("Health digest compose/send failed: %s", exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run():
    _setup_logging()
    cfg.log_startup()

    strategy = build_strategy()
    is_indicator = isinstance(strategy, IndicatorStrategy)

    # Universe state — scan runs before build_feed() so CcxtFeed.symbol is correct at init
    _universe = CryptoUniverse()
    _universe_last_refresh = 0.0
    _UNIVERSE_REFRESH_S = 86400
    _active_symbol = cfg.exchange.symbol
    _universe_symbols = [cfg.exchange.symbol]

    # Dynamic universe state (2026-09-13) — entirely independent of the
    # legacy top-movers mechanism above (UNIVERSE_ENABLED/get_top_movers),
    # which only ever selects among symbols already initialized at startup
    # ("A brand-new symbol would trade cold ... skip until restart" — see
    # its own comment below). This is the fixed-vs-dynamic switch: when
    # cfg.dynamic.enabled is False (the default), _dynamic_screener/
    # _dynamic_admitted are simply never touched again and the tick loop's
    # dynamic-mode branches never activate — fixed mode is unchanged.
    #
    # _dynamic_mode_active ALSO requires cfg.exchange.live_trading (FIXED
    # 2026-09-13, real bug confirmed by external review): _make_dynamic_
    # executor always builds a LiveExecutor, but the FIXED roster only does
    # that when live_trading is True — when it's False, the fixed roster
    # uses a completely different, simpler PaperExecutor (no ccxt, no
    # native stops, no state file) instead. Gating cfg.dynamic.enabled
    # alone would have let dynamic admission build a real, live-capable
    # LiveExecutor (with real API keys) in a config where the REST of the
    # bot is deliberately not live-capable at all — confirmed reproducible:
    # the factory received dry_run=False with LIVE_TRADING=False. Dynamic
    # admission's own code (native-stop sync, _save_state) also assumes
    # LiveExecutor's interface, which PaperExecutor doesn't have, so
    # dynamic mode simply requires live_trading=True rather than trying to
    # support both executor shapes.
    _dynamic_mode_active = cfg.dynamic.enabled and cfg.exchange.live_trading
    if cfg.dynamic.enabled and not cfg.exchange.live_trading:
        logger.warning(
            "DYNAMIC_UNIVERSE_ENABLED=true but LIVE_TRADING is not enabled — "
            "dynamic universe discovery/admission is disabled this run. It "
            "requires a LiveExecutor-based roster (live_trading=True), not "
            "the plain PaperExecutor this config uses."
        )
    # Quote-currency enforcement (FIXED 2026-09-13, real gap confirmed by
    # external review): DynamicUniverseConfig.quote_currencies accepts any
    # string, and DynamicUniverseScreener would happily scan a configured
    # USD leg — but this live integration has exactly ONE capital_pool,
    # implicitly denominated in the account's real funding currency (CAD).
    # There is no separate USD accounting/pool built. "Do not assume CAD
    # can fund USD orders" means refusing to run with an unsupported quote
    # currency, not silently scanning (and potentially trying to trade) it
    # against the wrong pool. CAD is the only currency this integration
    # actually funds today.
    if _dynamic_mode_active:
        _unsupported_quotes = [q for q in cfg.dynamic.quote_list if q != "CAD"]
        if _unsupported_quotes:
            logger.error(
                "DYNAMIC_QUOTE_CURRENCIES includes %s, which this live "
                "integration does not have separate capital accounting for "
                "— dynamic universe discovery/admission is DISABLED this "
                "run rather than risk funding a non-CAD order from the CAD "
                "pool. Set DYNAMIC_QUOTE_CURRENCIES=CAD (or remove the "
                "unsupported entries) to re-enable.",
                _unsupported_quotes,
            )
            _dynamic_mode_active = False
    _dynamic_screener   = DynamicUniverseScreener(cfg.dynamic) if _dynamic_mode_active else None
    _dynamic_admitted:   set = set()     # symbols admitted BY the dynamic screener — never includes the fixed roster
    _dynamic_last_refresh = 0.0
    _dynamic_last_screen  = None         # most recent ScreenResult, for the dashboard snapshot

    # Build live exchange + run universe scan BEFORE feed init
    live_exchange = None
    last_candle_ts_ms: "int | None" = None

    if is_indicator and cfg.exchange.feed_mode == "live":
        live_exchange = _build_exchange()
        if cfg.universe.enabled:
            print(f"[UNIVERSE] Scanning for top movers on {cfg.exchange.exchange.upper()} …", flush=True)
            _universe_symbols = _universe.get_top_movers(live_exchange, cfg.universe.size)
            _active_symbol = _universe_symbols[0]
            print(f"[UNIVERSE] Active symbol: {_active_symbol}", flush=True)
            if _active_symbol != cfg.exchange.symbol:
                logger.info("Universe override: %s → %s", cfg.exchange.symbol, _active_symbol)
                cfg.exchange.symbol = _active_symbol
        else:
            _universe_symbols = [cfg.exchange.symbol]
        _universe_last_refresh = time.time()

    feed = build_feed()

    if cfg.exchange.live_trading:
        if cfg.paper.paper_mode:
            executors = {
                sym: LiveExecutor(
                    exchange_id              = cfg.exchange.exchange,
                    symbol                   = sym,
                    api_key                  = cfg.exchange.api_key,
                    api_secret               = cfg.exchange.api_secret,
                    starting_cash            = cfg.paper.paper_starting_cash,
                    dry_run                  = True,
                    order_type               = cfg.exchange.order_type,
                    state_path               = f"logs/live_state_{sym.replace('/', '_')}.json",
                    adopt_external_holdings  = cfg.exchange.adopt_external_holdings,
                    native_stop_loss_enabled = cfg.exchange.native_stop_loss_enabled,
                    max_slippage_pct         = cfg.exchange.max_slippage_pct,
                )
                for sym in _universe_symbols
            }
            logger.info("PAPER MODE active — $%.2f virtual cash per symbol", cfg.paper.paper_starting_cash)
        else:
            _universe_list = list(_universe_symbols)
            # Pass 1: create first executor to establish account balance.
            # In dry-run mode _sync_cash() is skipped, so cash comes from the
            # state file (or starting_cash if no state exists yet).
            _sym0 = _universe_list[0]
            _exc0 = LiveExecutor(
                exchange_id              = cfg.exchange.exchange,
                symbol                   = _sym0,
                api_key                  = cfg.exchange.api_key,
                api_secret               = cfg.exchange.api_secret,
                starting_cash            = cfg.portfolio.starting_cash,
                dry_run                  = cfg.exchange.dry_run,
                order_type               = cfg.exchange.order_type,
                state_path               = f"logs/live_state_{_sym0.replace('/', '_')}.json",
                adopt_external_holdings  = cfg.exchange.adopt_external_holdings,
                native_stop_loss_enabled = cfg.exchange.native_stop_loss_enabled,
                max_slippage_pct         = cfg.exchange.max_slippage_pct,
            )
            # Derive slot_cash for new symbols from the first executor's balance
            # so their "ready" log matches the actual pool slot instead of showing
            # the full cfg.portfolio.starting_cash.
            _slot_for_new = _exc0.cash / max(1, cfg.portfolio.max_concurrent_positions)
            # Pass 2: remaining executors — new symbols get slot_for_new, existing
            # symbols get starting_cash (overridden by _load_state anyway).
            executors = {_sym0: _exc0}
            for _s in _universe_list[1:]:
                _sp = f"logs/live_state_{_s.replace('/', '_')}.json"
                executors[_s] = LiveExecutor(
                    exchange_id              = cfg.exchange.exchange,
                    symbol                   = _s,
                    api_key                  = cfg.exchange.api_key,
                    api_secret               = cfg.exchange.api_secret,
                    starting_cash            = cfg.portfolio.starting_cash if os.path.exists(_sp) else _slot_for_new,
                    dry_run                  = cfg.exchange.dry_run,
                    order_type               = cfg.exchange.order_type,
                    state_path               = _sp,
                    adopt_external_holdings  = cfg.exchange.adopt_external_holdings,
                    native_stop_loss_enabled = cfg.exchange.native_stop_loss_enabled,
                    max_slippage_pct         = cfg.exchange.max_slippage_pct,
                )
        executor = executors[_active_symbol]   # alias for pre-loop header/recovery code
        mode_str = "[DRY RUN] " if (cfg.exchange.dry_run or cfg.paper.paper_mode) else ""
        print(
            f"\n  {mode_str}LIVE TRADING ENABLED"
            f" — real orders will be placed on"
            f" {cfg.exchange.exchange.upper()}\n",
            flush=True,
        )
    else:
        executors = {
            cfg.exchange.symbol: PaperExecutor(
                symbol        = cfg.exchange.symbol,
                starting_cash = cfg.portfolio.starting_cash,
            )
        }
        executor = executors[cfg.exchange.symbol]
    # ── Capital pool — single cash pool shared across all symbols ─────────────
    # For live trading: use actual Kraken balance from the first executor as
    # the pool total (both executors read the same account, so we only count it once).
    # For paper/simulated: use STARTING_CASH as the total pool.
    _max_conc = cfg.portfolio.max_concurrent_positions
    if cfg.exchange.live_trading and not cfg.paper.paper_mode:
        _first_exec = next(iter(executors.values()))
        _pool_total = _first_exec.cash   # real Kraken CAD balance
    else:
        _pool_total = cfg.portfolio.starting_cash
    _slot_cap = cfg.portfolio.max_slot_cash_cad
    # Per-symbol overrides (MAX_SLOT_CASH_CAD_<BASE>, e.g. MAX_SLOT_CASH_CAD_SOL) —
    # keyed by base asset in config, remapped here to the full symbol strings
    # CapitalPool actually tracks. A symbol with no override falls back to the
    # shared _slot_cap above — added 2026-08-24, no-op when .env only sets the
    # old single MAX_SLOT_CASH_CAD (the dict below is then empty).
    _slot_caps_by_symbol = {
        _sym: cfg.portfolio.max_slot_cash_cad_by_base[_sym.split("/")[0].upper()]
        for _sym in executors
        if _sym.split("/")[0].upper() in cfg.portfolio.max_slot_cash_cad_by_base
    }
    capital_pool = CapitalPool(
        total_capital=_pool_total, max_concurrent=_max_conc, slot_cap=_slot_cap,
        slot_caps=_slot_caps_by_symbol,
    )
    _uncapped_slot = _pool_total / _max_conc
    _per_symbol_slots: dict[str, float] = {}
    for _sym, _exc in executors.items():
        _slot = capital_pool.slot_cash_for(_sym)
        _per_symbol_slots[_sym] = _slot
        _exc._portfolio.cash = _slot
        if cfg.exchange.live_trading:
            try:
                _exc._save_state()
            except Exception as e:
                logger.warning("State save after pool init failed [%s]: %s", _sym, e)
    if _slot_caps_by_symbol:
        # Multi-symbol per-symbol-cap case — report each slot individually
        # rather than the single "X per symbol" line, since slots now differ.
        _slots_desc = ", ".join(f"{s}=${v:.2f}" for s, v in _per_symbol_slots.items())
        print(
            f"\n  Capital pool: ${_pool_total:.2f} total"
            f" / {_max_conc} slots — {_slots_desc}\n",
            flush=True,
        )
        logger.info(
            "CapitalPool init: total=%.2f  slots=%d  per_symbol=%s  shared_slot_cap=%.2f",
            _pool_total, _max_conc, _per_symbol_slots, _slot_cap,
        )
    else:
        # Original single-shared-cap case — output byte-identical to before
        # this feature existed.
        _slot = capital_pool.slot_cash
        _cap_note = (
            f" (capped from ${_uncapped_slot:.2f})"
            if _slot_cap > 0 and _uncapped_slot > _slot_cap
            else " (uncapped)"
        )
        print(
            f"\n  Capital pool: ${_pool_total:.2f} total"
            f" / {_max_conc} slots = ${_slot:.2f} per symbol{_cap_note}\n",
            flush=True,
        )
        logger.info(
            "CapitalPool init: total=%.2f  slots=%d  slot_cash=%.2f  slot_cap=%.2f",
            _pool_total, _max_conc, _slot, _slot_cap,
        )
    if cfg.exchange.live_trading:
        logger.info(
            "Executor cash after pool correction: %s",
            {s: f"${e.cash:.2f}" for s, e in executors.items()},
        )

    # ── Native stop-loss startup reconciliation ─────────────────────────
    # A held position with no confirmed resting stop after restart (feature
    # just enabled, or the bot crashed before placing one after a BUY). The
    # original ATR SL level lived only in the previous run's in-memory state
    # and is gone — fall back to flat STOP_LOSS_PCT off cost_basis, same
    # fallback the software SL path itself uses when ATR is unavailable.
    # Still static once placed: the next real BUY on this symbol replaces it
    # with a fresh ATR-based level via the normal per-fill sync below.
    if (
        cfg.exchange.native_stop_loss_enabled
        and cfg.exchange.live_trading
        and not cfg.paper.paper_mode
        and not cfg.exchange.dry_run
    ):
        for _nsl_sym, _nsl_exc in executors.items():
            if _nsl_exc.position > 0 and not _nsl_exc.has_resting_stop:
                _fallback_sl = (
                    _nsl_exc.avg_entry * (1 - cfg.backtest.stop_loss_pct)
                    if cfg.backtest.stop_loss_pct > 0 and _nsl_exc.avg_entry > 0
                    else None
                )
                if _fallback_sl:
                    logger.warning(
                        "NATIVE STOP STARTUP FALLBACK [%s]: placing backstop at "
                        "%.2f (flat %.1f%% off cost_basis %.2f) — no resting "
                        "stop survived restart.",
                        _nsl_sym, _fallback_sl, cfg.backtest.stop_loss_pct * 100,
                        _nsl_exc.avg_entry,
                    )
                    _nsl_exc.sync_protective_stop(_fallback_sl)
                else:
                    logger.warning(
                        "NATIVE STOP STARTUP GAP [%s]: position=%.6f open, no "
                        "resting stop, and STOP_LOSS_PCT=0 — cannot compute a "
                        "fallback level. Unprotected until the next software "
                        "SL/TP evaluation or a manual order.",
                        _nsl_sym, _nsl_exc.position,
                    )

    risk = RiskManager(
        RiskConfig(
            max_position_pct      = cfg.risk.max_position_pct,
            daily_loss_limit_pct  = cfg.risk.daily_loss_limit_pct,
            max_drawdown_pct      = cfg.risk.max_drawdown_pct,
            max_trades_per_day    = cfg.risk.max_trades_per_day,
            weekly_loss_limit_pct = cfg.risk.weekly_loss_limit_pct,
            drawdown_warning_pct  = cfg.risk.drawdown_warning_pct,
            kill_switch_pct       = cfg.risk.kill_switch_pct,
        ),
        # Persist breaker state (drawdown peak, daily counters) across restarts
        # in live mode only — backtests/paper runs stay stateless.
        state_path = os.path.join(_log_dir, "risk_state.json")
                     if cfg.exchange.live_trading else None,
    )
    ai = AIEngine(
        model          = cfg.ai.model,
        min_confidence = cfg.ai.min_confidence,
        timeout_s      = cfg.ai.timeout_s,
    ) if cfg.ai.enabled else None

    # ── External signal (Fear&Greed / funding) gate — REMOVED 2026-09-02 ──────
    # `mtf_overlay_backtest.py` showed the FNG>75 BUY veto was net-negative or a
    # wash in every window tested (2022–24 BTC PF 1.47→1.21; 2024–26 BTC +0.08 /
    # SOL −0.19) and it had never once fired on the live bot, while costing a
    # third-party API dependency (alternative.me) plus a fail-open
    # risk-gate-bypass alert path. Funding was already dead (Kraken is spot).
    # Full trail: CLAUDE_HISTORY.md "Crypto BUY-overlay audit — 2026-09-02".

    # ── Persistent trade log + Telegram alerts ────────────────────────────────
    trade_log = TradeLog()
    alerter   = TelegramAlerter(
        bot_token = cfg.alerts.telegram_bot_token,
        chat_id   = cfg.alerts.telegram_chat_id,
        enabled   = cfg.alerts.telegram_enabled,
    )
    # Generic "the same operation keeps failing" watchdog — catches a stuck
    # retry loop (native-stop deadlock 2026-08-27 was one) regardless of the
    # specific error string, complementing the per-case counters
    # (exit_fail_count, drift_count, _auth_health).
    stuck_detector = StuckLoopDetector(alerter.error)

    # In live mode: show real Kraken balance, not starting_cash from .env.
    # executor.cash and executor.position are already synced from the exchange
    # by the time LiveExecutor.__init__() returns (lines above).
    if cfg.exchange.live_trading:
        _header_cash = executor.cash
        try:
            _header_price = feed.get_price()
            _header_total = executor.cash + executor.position * _header_price
        except Exception:
            _header_total = None
    else:
        _header_cash  = cfg.portfolio.starting_cash
        _header_total = None

    display.header(
        cfg.exchange.exchange,
        cfg.exchange.symbol,
        _header_cash,
        cfg.strategy.mode,
        live_trading = cfg.exchange.live_trading,
        dry_run      = cfg.exchange.dry_run,
        total_value  = _header_total,
    )
    if cfg.dashboard.enabled:
        print(f"  Dashboard → file://{_DASHBOARD_PATH}\n")

    _mode_label = "LIVE" if cfg.exchange.live_trading else ("DRY RUN" if cfg.exchange.dry_run else "PAPER")
    alerter.startup(cfg.exchange.exchange, cfg.exchange.symbol, _mode_label)
    _record_startup_and_check_crash_loop(alerter)
    _orphaned_symbols = _check_orphaned_positions(set(executors.keys()), alerter)

    # ── Fill journal replay (2026-09-18 review finding, P1-3) ───────────
    # Before any new trading this run: recover any fill whose accounting
    # was already persisted by a prior process but never made it into
    # trade_log because the process crashed between those two writes.
    _replay_pending_journal_entries(executors, trade_log, alerter)

    # ── Derive live candle timeframe from CANDLE_MINUTES ─────────────────────
    # This is the timeframe used for ALL live candle operations:
    # warmup fetch, candle polling, and countdown display.
    # BACKTEST_TIMEFRAME is only used for backtesting, not live trading.
    _LIVE_TF = _minutes_to_timeframe(cfg.exchange.candle_minutes)

    # ── Multi-symbol state initialisation ────────────────────────────────────
    symbol_state: dict[str, dict] = {}

    # Recover orphaned positions FOUND ABOVE — independent of cfg.dynamic.
    # enabled and of current screener eligibility (FIXED 2026-09-13, real
    # gap confirmed by external review: "the orphan check merely alerts...
    # a held coin that fails screening after restart can lose software exit
    # management. Disabling dynamic mode has the same problem."). This is
    # about not losing track of a REAL position, not about the discover-
    # new-coins feature being on — so it runs unconditionally whenever the
    # roster is LiveExecutor-based (live_trading=True; PaperExecutor mode
    # never produces these state files and has never had any recovery
    # mechanism, unchanged). Reuses _admit_dynamic_symbol exactly (same
    # warmup/pm-seed/sm-recover/native-stop-mirror/capital-pool-allocate
    # logic already proven for a restart-recovered dynamic admission) and
    # tracks the recovered symbol in _dynamic_admitted so normal retirement
    # rules apply to it once flat again — regardless of whether dynamic
    # discovery is itself enabled.
    if _orphaned_symbols and cfg.exchange.live_trading:
        for _osym in _orphaned_symbols:
            _oss, _oerr = _admit_dynamic_symbol(_osym, live_exchange, _LIVE_TF, capital_pool)
            if _oss is None:
                logger.error(
                    "Failed to recover orphaned position %s: %s — still UNMONITORED,"
                    " see the ORPHANED POSITION alert above. Close manually or fix"
                    " and restart.", _osym, _oerr,
                )
                continue
            symbol_state[_osym] = _oss
            executors[_osym] = _oss['executor']
            _dynamic_admitted.add(_osym)
            logger.warning(
                "Orphaned position %s RECOVERED into active management"
                " (qty=%.6f) — SL/TP/drift monitoring resumed.",
                _osym, _oss['executor'].position,
            )
            alerter.message(
                f"✅ Orphaned position {_osym} recovered into active management"
                f" — SL/TP monitoring resumed."
            )

    if is_indicator and cfg.exchange.feed_mode == "live":
        for sym in _universe_symbols:
            strat = build_strategy()
            sm    = TradingStateMachine(cooldown_ticks=cfg.risk.cooldown_ticks)
            pm    = PositionManager()

            print(f"  Warming up {sym} …", flush=True)
            last_ts = _warmup_strategy(strat, live_exchange, _LIVE_TF, symbol=sym)

            symbol_state[sym] = {
                'strategy':         strat,
                'sm':               sm,
                'pm':               pm,
                'executor':         executors[sym],
                'last_ts_ms':       last_ts,
                'trail_peak':       0.0,
                'partial_done':     False,
                'atr_sl':           0.0,
                'native_stop_price': None,        # static native-stop backstop price for the current position
                'native_stop_is_trailing': False, # has the backstop been swapped to a native Kraken trailing-stop this fill?
                'candle_feed_stale': False,       # candle watchdog circuit-breaker state
                'price_feed_stale': False,        # 2026-09-18: live-tick fetch failing — blocks new BUYs
                'last_price':       0.0,
                'err_count':        0,            # consecutive price-fetch failures
                'drift_count':      0,            # consecutive drift detections
                'drift_acked':      0.0,          # drift amount already escalated (no re-alert until it changes)
                'last_candle_time': time.time(),  # candle watchdog timer
                'mtf_1d_closes':    [],           # daily closes cache — refreshed at gate 2c
                # Sticky dashboard display values (added 2026-08-26, made
                # per-symbol for the multi-symbol dashboard combine — these
                # used to be shared module-level globals, correct only
                # because the dashboard only ever rendered one symbol) —
                # updated at candle-close, read on the "between closes"
                # dashboard-refresh ticks so the page shows the last known
                # values rather than blanking out mid-candle.
                'dash_signal':      "HOLD",
                'dash_rsi':         None,
                'dash_trend':       None,
                'dash_filter':      "",
                'dash_block':       "",
                # Edge-trigger for the blocked-BUY Telegram alert: the gate name
                # last alerted for this symbol. Cleared when a BUY is approved or
                # the raw signal is no longer BUY. Not persisted — resets on
                # restart (same as the other per-process flags above).
                'last_buy_block_alert': "",
                'last_buy_signal_alerted': False,  # edge-trigger for the raw-BUY-signal heads-up alert
                'exit_fail_count': 0,   # consecutive failed urgent SL/TP exits (edge-alert)
            }
            logger.info("Symbol ready: %s", sym)

        print(f"\n  {len(symbol_state)} symbols ready: {list(symbol_state.keys())}", flush=True)

        # ── Regime monitor background thread ──────────────────────────────────
        _rm_interval = int(os.getenv("REGIME_MONITOR_INTERVAL", "14400"))
        _monitor_thread = threading.Thread(
            target=_regime_monitor_loop,
            args=(list(_universe_symbols), cfg.exchange.exchange, _rm_interval),
            daemon=True,
            name="regime-monitor",
        )
        _monitor_thread.start()
        logger.info("Regime monitor thread started (interval=%ds)", _rm_interval)

        # MTF daily closes are fetched per symbol at decision time (gate 2c) —
        # no startup prefetch: it added to the Kraken connection burst and the
        # data went stale between BUY signals anyway.
    else:
        symbol_state[_active_symbol] = {
            'strategy':         strategy,
            'sm':               TradingStateMachine(cooldown_ticks=cfg.risk.cooldown_ticks),
            'pm':               PositionManager(),
            'executor':         executor,
            'last_ts_ms':       None,
            'trail_peak':       0.0,
            'partial_done':     False,
            'atr_sl':           0.0,
            'native_stop_price': None,        # static native-stop backstop price for the current position
            'native_stop_is_trailing': False, # has the backstop been swapped to a native Kraken trailing-stop this fill?
            'candle_feed_stale': False,       # candle watchdog circuit-breaker state
            'price_feed_stale': False,        # 2026-09-18: live-tick fetch failing — blocks new BUYs
            'last_price':       0.0,
            'err_count':        0,
            'drift_count':      0,
            'drift_acked':      0.0,
            'last_candle_time': time.time(),
            'mtf_1d_closes':    [],
            'dash_signal':      "HOLD",
            'dash_rsi':         None,
            'dash_trend':       None,
            'dash_filter':      "",
            'dash_block':       "",
            'last_buy_block_alert': "",   # edge-trigger for the blocked-BUY alert
            'last_buy_signal_alerted': False,  # edge-trigger for the raw-BUY-signal heads-up alert
            'exit_fail_count': 0,         # consecutive failed urgent SL/TP exits (edge-alert)
        }

    # ── Restart recovery ──────────────────────────────────────────────────────
    if cfg.exchange.live_trading:
        for _rsym, _rexc in executors.items():
            if _rexc.position > 1e-9:
                # Dust position guard: skip recovery when position value < threshold
                try:
                    _rec_price = float(live_exchange.fetch_ticker(_rsym)['last'])
                except Exception:
                    _rec_price = _rexc.avg_entry if _rexc.avg_entry > 0 else 1.0
                _rec_pos_value = _rexc.position * _rec_price
                if _rec_pos_value < cfg.portfolio.live_dust_value_cad:
                    logger.warning(
                        "Dust position detected [%s]: %.6f × %.2f = %.4f CAD"
                        " < %.2f threshold — keeping state machine IDLE",
                        _rsym, _rexc.position, _rec_price,
                        _rec_pos_value, cfg.portfolio.live_dust_value_cad,
                    )
                    print(
                        f"  DUST POSITION [{_rsym}]: {_rexc.position:.6f}"
                        f" × {_rec_price:,.2f} = {_rec_pos_value:.4f} CAD"
                        f" < {cfg.portfolio.live_dust_value_cad:.2f} threshold"
                        f" — state machine stays IDLE, not recovering",
                        flush=True,
                    )
                    continue

                _rec_ss = symbol_state[_rsym]
                _rec_ss['pm'].seed(
                    quantity     = _rexc.position,
                    avg_entry    = _rexc.avg_entry,
                    realized_pnl = _rexc.portfolio.realized_pnl,
                )
                _rec_ss['sm'].recover_long(_rexc.avg_entry)
                logger.warning(
                    "Recovered position seeded [%s]: qty=%.6f entry=%.2f — state machine set to LONG",
                    _rsym, _rexc.position, _rexc.avg_entry,
                )
                print(
                    f"  POSITION RECOVERED [{_rsym}]: {_rexc.position:.6f}"
                    f" {_rsym.split('/')[0]}"
                    f" @ ${_rexc.avg_entry:,.2f}"
                    f" — state machine set to LONG",
                    flush=True,
                )

                # Mirror the executor's own already-reconciled native-stop
                # state into ss — see _seed_native_stop_state docstring for
                # why this matters (2026-08-20 restart-seeding gap fix).
                # Safe to call unconditionally: when native stop-loss is
                # disabled, executor.has_resting_stop is always False, so
                # this correctly seeds (None, False) — a no-op matching ss's
                # own pre-existing defaults.
                (
                    _rec_ss['native_stop_price'],
                    _rec_ss['native_stop_is_trailing'],
                ) = _seed_native_stop_state(_rexc)
                logger.warning(
                    "NATIVE STOP RECOVERED [%s]: ss synced — price=%s trailing=%s",
                    _rsym, _rec_ss['native_stop_price'], _rec_ss['native_stop_is_trailing'],
                )

                # capital_pool starts with NO slots allocated (a fresh
                # CapitalPool() instance every process start) — a recovered
                # open position must claim its slot explicitly, or
                # can_open_position() would (incorrectly) report every slot
                # free even though this symbol already holds one. Latent in
                # the fixed 2-symbol roster (there's never a 3rd symbol to
                # wrongly admit into "the same" slot, so this was invisible
                # in practice) but a real, active gap once dynamic
                # admission means MORE candidates than slots can compete
                # for one — fixed here for both modes uniformly (2026-09-13,
                # same fix applied in _admit_dynamic_symbol for a
                # dynamically re-admitted position). Zero behavior change
                # for fixed mode: with exactly max_concurrent fixed symbols,
                # allocating both/all of them still leaves can_open_position()
                # returning the same answer it always effectively did.
                capital_pool.allocate(_rsym)

    # Aliases for _render_dashboard closure and display.stopped()
    state_machine    = symbol_state[_active_symbol]['sm']
    position_manager = symbol_state[_active_symbol]['pm']
    executor         = symbol_state[_active_symbol]['executor']  # alias for dashboard closure

    tick        = 0
    tick_log:   deque[dict] = deque(maxlen=200)
    _drift_consecutive_failures = 0
    # Mutable so both the heartbeat healthy_fn closure (defined before the
    # tick loop starts) and the drift-check block (inside the loop) share
    # the same flag. 2026-08-15: a Kraken auth failure (IP-restriction —
    # traveling changed the bot host's public IP) went undetected for days
    # because the drift-check failure only logged, never alerted, and the
    # heartbeat only checks that the loop is *ticking* — public price/candle
    # calls kept succeeding so liveness stayed green throughout.
    _auth_health = {"ok": True}
    _dd_warning_active = False   # non-blocking drawdown-warning tier — alert-once-per-episode
    candle_log: deque[dict] = deque(maxlen=50)
    # Per-symbol dashboard snapshot cache (added 2026-08-26, SOL/CAD addition —
    # dashboard.html used to only ever render _active_symbol; now combines
    # every live symbol on one page). Updated in-place per symbol as each
    # ticks; write_multi() gets the merged set on every render call so the
    # page always reflects the latest known state for ALL symbols, not just
    # whichever one just ticked.
    _dash_snapshots: dict[str, dict] = {}

    def _account_value() -> float:
        """Aggregate account value — see _compute_account_value's own
        docstring for the fixed-2026-09-13 account-value-inflation bug this
        replaced. A thin closure so callers inside run() keep the
        zero-argument call they already use everywhere."""
        return _compute_account_value(capital_pool, executors, symbol_state)

    # _resync_native_stop is now a module-level function (hoisted 2026-09-13
    # for dynamic-universe integration/testability) — see its definition
    # near _seed_native_stop_state above. Every call site below is unchanged.

    # Trailing stop and partial TP state — reset on each new trade
    _trail_peak:      float = 0.0
    _partial_tp_done: bool  = False
    _atr_sl_price:    float = 0.0

    # Sticky indicator values now live per-symbol in symbol_state[sym]['dash_*']
    # (see the 'dash_signal'/'dash_rsi'/etc. keys at symbol_state init) —
    # was a set of shared module-level globals here until 2026-08-26, correct
    # only when the dashboard rendered a single symbol.

    def _render_dashboard(sym: str, sig: str, rsi_v, trend_v) -> None:
        """Update sym's snapshot and re-render the COMBINED dashboard.html
        (all live symbols on one page — added 2026-08-26 for the SOL/CAD
        addition; previously only ever rendered _active_symbol, leaving
        every other live symbol with zero dashboard visibility). Each call
        only recomputes the calling symbol's own data; _dash_snapshots
        retains every other symbol's last-known state so the page always
        shows the full set, not just whoever ticked most recently."""
        if not cfg.dashboard.enabled:
            return
        _ss    = symbol_state[sym]
        _exc   = _ss['executor']
        _sm    = _ss['sm']
        _pm    = _ss['pm']
        _price = _ss.get('last_price') or 0.0
        fills_data = [
            {
                "time":  o.filled_at.astimezone().strftime("%H:%M:%S") if o.filled_at else "—",
                "side":  o.side.value,
                "qty":   o.quantity,
                "price": o.price,
                "total": o.total_value,
                "pnl":   next(
                    (r.pnl for r in reversed(_pm.history)
                     if r.action == o.side.value and abs(r.price - o.price) < 0.01),
                    None,
                ),
            }
            for o in _exc.filled_orders()
        ]
        _dash_snapshots[sym] = {
            "symbol":             sym,
            "price":              _price,
            "signal":             sig,
            "rsi":                rsi_v,
            "trend":              trend_v,
            "state":              _sm.state.value,
            "cooldown":           _sm.cooldown_remaining,
            "last_trade":         _sm.last_trade_label,
            "cash":               _exc.cash,
            "position":           _pm.quantity,
            "avg_entry":          _pm.avg_entry,
            "unrealized_pnl":     _pm.unrealized_pnl(_price),
            "realized_pnl":       _pm.realized_pnl,
            "total_value":        _exc.portfolio.total_value(_price),
            "fills":              fills_data,
            "tick_log":           [t for t in tick_log if t.get("sym") == sym],
            "candle_log":         [c for c in candle_log if c.get("sym") == sym],
            "stop_loss_pct":      cfg.backtest.stop_loss_pct,
            "take_profit_pct":    cfg.backtest.exit_params_for(sym)["take_profit_pct"],
            "fees_paid":          getattr(_exc, "fees_paid", 0.0),
            "rsi_filter_enabled": cfg.strategy.rsi_filter_enabled,
            "volume_k":           cfg.strategy.volume_k,
        }
        try:
            _dashboard.write_multi(
                path         = _DASHBOARD_PATH,
                exchange     = cfg.exchange.exchange,
                strategy     = cfg.strategy.mode,
                tick         = tick,
                symbols      = [_dash_snapshots[s] for s in _universe_symbols if s in _dash_snapshots],
                refresh_s    = cfg.dashboard.refresh_s,
                live_trading = cfg.exchange.live_trading,
                dry_run      = cfg.exchange.dry_run,
            )
        except Exception as exc:
            logger.warning("Dashboard render failed: %s", exc)

    # ── Unified dashboard background thread ───────────────────────────────
    # UNIFIED_DASHBOARD_INTERVAL=0 disables (e.g. when running
    # `python unified_dashboard.py --watch` manually instead).
    _ud_interval = int(os.getenv("UNIFIED_DASHBOARD_INTERVAL", "60"))
    if cfg.dashboard.enabled and _ud_interval > 0:
        _ud_thread = threading.Thread(
            target=_unified_dashboard_loop,
            args=(_ud_interval,),
            daemon=True,
            name="unified-dashboard",
        )
        _ud_thread.start()
        logger.info("Unified dashboard thread started (interval=%ds)", _ud_interval)
        print(f"  Unified dashboard → file://{os.path.join(os.path.dirname(_DASHBOARD_PATH), 'unified_dashboard.html')}"
              f"  (refreshes every {_ud_interval}s)\n", flush=True)

    # ── Scheduled audits thread (replaces macOS cron — see ops/crontab.txt) ──
    if os.getenv("AUDIT_SCHEDULER_ENABLED", "true").lower() == "true":
        _audit_thread = threading.Thread(
            target=_scheduled_audits_loop,
            daemon=True,
            name="scheduled-audits",
        )
        _audit_thread.start()
        logger.info(
            "Scheduled audits thread started (shadow daily %s · comparison Mon %s"
            " · rescreen monthly %s)",
            os.getenv("SHADOW_AUDIT_TIME", "12:05"),
            os.getenv("WEEKLY_AUDIT_TIME", "12:10"),
            os.getenv("RESCREEN_AUDIT_TIME", "12:20"),
        )

    # ── Heartbeat ping thread (dead-man's switch — see bot/alerts/heartbeat.py) ──
    # healthy_fn requires the main loop to have completed a full tick within
    # the last 10 minutes — a hung thread (2026-07-22 incident: the stock
    # bot's swing-book thread froze silently for 5+ hours with no exception)
    # is technically "running" but makes no progress; process-alive alone
    # can't detect that. _liveness.touch() is called once per completed tick
    # below (LOOP_INTERVAL is normally 30s — 10 minutes is a wide safety margin).
    from bot.alerts.heartbeat import start_heartbeat_thread
    from bot.alerts.liveness import LivenessTracker
    _liveness = LivenessTracker()
    _LIVENESS_MAX_STALE_S = 600
    start_heartbeat_thread(
        os.getenv("HEARTBEAT_URL", ""),
        interval_s=int(os.getenv("HEARTBEAT_INTERVAL_S", "60")),
        name="heartbeat-crypto",
        healthy_fn=lambda: _liveness.is_alive(_LIVENESS_MAX_STALE_S) and _auth_health["ok"],
    )

    # ── Two-way Telegram control (getUpdates poller — 2026-08-20) ──────────
    # Opt-in (TELEGRAM_CONTROL_ENABLED, separate from telegram_enabled/
    # outbound alerts). See bot/alerts/telegram_control.py's module
    # docstring for the shared-token-with-the-stock-bot constraint and why
    # this MUST stay the only getUpdates poller against this bot token.
    # Every handler below either only READS symbol_state/executor/risk
    # attributes, or only touches the logs/HALT flag file — never a
    # LiveExecutor trading method (execute/sync_protective_stop/cancel/...).
    # That's a structural property, not a convention: this file's import of
    # TelegramCommandPoller carries no reference to those methods for a
    # handler to even reach.
    if cfg.alerts.telegram_control_enabled:
        from bot.alerts.telegram_control import (
            TelegramCommandPoller, start_telegram_control_thread,
        )

        _tg_control_poller = TelegramCommandPoller(
            bot_token = cfg.alerts.telegram_bot_token,
            chat_id   = cfg.alerts.telegram_chat_id,
            handlers  = {
                "/status_crypto": lambda: _status_crypto_text(
                    executors, symbol_state, risk,
                    cfg.exchange.live_trading, cfg.exchange.dry_run,
                ),
                "/pause_crypto":  lambda: _pause_crypto_flag(
                    _HALT_FLAG_PATH, cfg.exchange.loop_interval,
                ),
                "/resume_crypto": lambda: _resume_crypto_flag(
                    _HALT_FLAG_PATH, cfg.exchange.loop_interval,
                ),
                "/status_stock":  _status_stock_text,
                "/help_crypto":   _help_crypto_text,
            },
        )
        start_telegram_control_thread(_tg_control_poller, name="telegram-control-crypto")

    _halt_file_active = False
    _last_daily_pnl_date = datetime.now(_tz.utc).date()

    while _running:
        tick += 1

        # ── 0a. Manual halt flag file (touch logs/HALT to kill-switch) ──
        _halt_file_active = _check_halt_flag(
            risk, _HALT_FLAG_PATH, _halt_file_active, alerter
        )

        # ── 0. Universe refresh (every 24h) ──────────────────────────
        if (cfg.universe.enabled and
                time.time() - _universe_last_refresh > _UNIVERSE_REFRESH_S):
            try:
                _universe_symbols = _universe.get_top_movers(
                    live_exchange,
                    cfg.universe.size,
                )
                new_symbol = _universe_symbols[0]
                if new_symbol != _active_symbol:
                    # Only switch to symbols initialized at startup — they have an
                    # executor, warmed strategy, and state machine. A brand-new
                    # symbol would trade cold with no executor: skip until restart.
                    if new_symbol in symbol_state:
                        logger.info(
                            "Universe refresh: switching %s → %s",
                            _active_symbol, new_symbol,
                        )
                        _active_symbol = new_symbol
                        cfg.exchange.symbol = new_symbol
                    else:
                        logger.warning(
                            "Universe refresh: %s not initialized at startup"
                            " — keeping %s (restart to trade new symbols)",
                            new_symbol, _active_symbol,
                        )
                _universe_last_refresh = time.time()
            except Exception as _univ_exc:
                logger.warning(
                    "Universe refresh failed: %s — keeping %s",
                    _univ_exc, _active_symbol,
                )

        # ── 0b. Dynamic universe — per-tick state reset only ──────────────
        # Discovery/admission/retirement itself is no longer run here (FIXED
        # 2026-09-13, external review: "move expensive screening off the
        # exit-processing loop; slow discovery currently delays position
        # management" — this used to call _sync_dynamic_universe() BEFORE
        # the per-symbol SL/TP loop below, so a slow discovery cycle
        # (load_markets + fetch_tickers + per-candidate order-book/OHLCV
        # calls, up to DYNAMIC_MAX_CANDIDATES of them) could delay checking
        # every existing position's stop-loss by however long that took).
        # The sync call itself now runs AFTER the per-symbol loop AND the
        # ranked-BUY execution pass, at the very end of the tick — see
        # "0c." below — so existing-position risk management always runs
        # first, unconditionally, every tick. The one-time cost: a symbol
        # newly admitted at the end of a refresh tick isn't evaluated for a
        # BUY until the NEXT tick, since it isn't in symbol_state yet
        # during THIS tick's per-symbol loop. A one-tick (loop_interval)
        # delay on a brand-new candidate is a trivial price for never
        # delaying an existing position's exit management.
        _dynamic_blocked_this_cycle: dict = {}

        # BUY signals gathered this tick under dynamic mode (see section 9
        # inside the per-symbol loop below) — ranked and executed AFTER
        # every symbol has been scanned, not inline per-symbol. Reset every
        # tick. quote_volume lookup feeds the ranking tiebreak, from
        # whichever screen result is current as of the END of the PREVIOUS
        # tick (see "0c." below for when it's actually refreshed).
        _dynamic_buy_queue: list = []
        _dynamic_quote_volume: dict = (
            {c.symbol: c.quote_volume for c in _dynamic_last_screen.eligible if c.quote_volume}
            if (_dynamic_mode_active and _dynamic_last_screen is not None) else {}
        )

        # ── Per-symbol processing ─────────────────────────────────────
        for sym, ss in symbol_state.items():

            # Per-symbol exit params (2026-09-03): BTC sustains trends and its
            # flat 10% take-profit was cutting winners short (exit-logic
            # research) → BTC runs a wider TP; SOL is choppy → keeps the 10% TP.
            # exit_params_for() merges any TAKE_PROFIT_PCT_<BASE> /
            # TRAILING_STOP_PCT_<BASE> override over the shared defaults, and
            # engine_kwargs_from_cfg() resolves the identical dict for the
            # backtest, so validated and live behaviour stay in lockstep.
            _ep = cfg.backtest.exit_params_for(sym)

            # ── 1. Fetch live price ───────────────────────────────────
            if live_exchange is not None:
                try:
                    price = float(fetch_with_retry(
                        lambda: live_exchange.fetch_ticker(sym)['last'],
                        label=f"price fetch [{sym}]",
                    ))
                    # 2026-09-18 review finding (P1-7): a NaN/inf/non-positive
                    # ticker value (a real, if rare, exchange/API glitch) used
                    # to be accepted as a genuine fresh price — treat it as a
                    # failed fetch instead, same as an exception, so the
                    # staleness/err_count machinery below handles it.
                    if not math.isfinite(price) or price <= 0:
                        raise ValueError(f"invalid ticker price for {sym}: {price!r}")
                    ss['last_price'] = price
                    ss['err_count'] = 0
                    _update_price_feed_staleness(ss, True, sym, alerter)
                except Exception as exc:
                    ss['err_count'] += 1
                    if ss['err_count'] >= 5:
                        alerter.error(
                            f"Price feed down [{sym}] {ss['err_count']} consecutive ticks — {exc}"
                        )
                    logger.warning("price fetch failed for %s: %s", sym, exc)
                    price = ss['last_price']
                    _update_price_feed_staleness(ss, False, sym, alerter)
                    if not price:
                        continue
            else:
                try:
                    price = feed.get_price()
                    if not math.isfinite(price) or price <= 0:
                        raise ValueError(f"invalid feed price for {sym}: {price!r}")
                    ss['last_price'] = price
                    ss['err_count'] = 0
                    _update_price_feed_staleness(ss, True, sym, alerter)
                except Exception as exc:
                    ss['err_count'] += 1
                    if ss['err_count'] >= 5:
                        alerter.error(
                            f"Price feed down [{sym}] {ss['err_count']} consecutive ticks — {exc}"
                        )
                    print(f"  TICK {tick:04d} | price fetch failed: {exc}")
                    _update_price_feed_staleness(ss, False, sym, alerter)
                    continue

            # ── 1b. Candle watchdog — circuit breaker (every symbol, live only) ──
            if cfg.exchange.feed_mode == "live":
                _check_candle_watchdog(
                    ss, cfg.exchange.candle_minutes, time.time(), alerter, symbol=sym,
                )

            # ── 1c. Position drift reconciliation (every symbol, every 120 ticks, live) ──
            if cfg.exchange.live_trading and not cfg.exchange.dry_run and tick % 120 == 0:
                _drift_delays = [5, 15, 30]
                _drift_succeeded = False
                for _attempt, _delay in enumerate(_drift_delays):
                    try:
                        balance = ss['executor']._exchange.fetch_balance()
                        base = sym.split("/")[0]
                        # Compare against `total`, matching _sync_position: during
                        # Kraken's settlement window a fresh fill sits in total but
                        # not yet in free, and `free` here caused false drift alerts.
                        exchange_pos = float(balance.get("total", {}).get(base, 0))
                        bot_pos = ss['executor'].position
                        _evaluate_drift(
                            sym, base, exchange_pos, bot_pos, ss,
                            cfg.exchange.drift_alert_threshold, alerter,
                        )
                        _drift_succeeded = True
                        _drift_consecutive_failures = _update_auth_health(
                            _auth_health, True, _drift_consecutive_failures, 5, alerter,
                        )
                        break
                    except Exception as _drift_exc:
                        if _attempt < len(_drift_delays) - 1:
                            time.sleep(_delay)
                        else:
                            logger.warning("Position drift check failed: %s", _drift_exc)
                            _drift_consecutive_failures = _update_auth_health(
                                _auth_health, False, _drift_consecutive_failures,
                                5, alerter, _drift_exc,
                            )

            # ── 1d. Drawdown warning (non-blocking, informational) ────
            # The blocking tiers (kill switch, drawdown halt, weekly loss)
            # already fire from inside risk.evaluate() at step 7 with no
            # extra wiring needed — this is the one tier that never blocks
            # a trade, so it gets its own explicit check + alert-once-per-
            # episode guard (same pattern as the candle watchdog / drift
            # detection above, and the stock bot's identically-named tier).
            if is_indicator and live_exchange is not None:
                _dd_status = risk.drawdown_status(_account_value())
                if _dd_status["warning"] and not _dd_warning_active:
                    _dd_warning_active = True
                    alerter.error(
                        f"DRAWDOWN WARNING: portfolio down {_dd_status['drawdown_pct']:.1%} "
                        f"from peak ${_dd_status['peak_value']:,.2f} "
                        f"(current ${_dd_status['current_value']:,.2f}). Trading continues — "
                        f"this is a non-blocking warning tier."
                    )
                elif not _dd_status["warning"]:
                    _dd_warning_active = False

            # ── 2. Intra-candle SL/TP + Trailing Stop + Partial TP ───
            # This block is the only SL/TP evaluation path. A second
            # candle-close SL check that existed in an earlier version of
            # this file has been removed — all stop/take-profit logic lives here.
            if is_indicator and live_exchange is not None:
                if ss['pm'].has_position and ss['pm'].avg_entry > 0:
                    _ic_entry       = ss['pm'].avg_entry
                    _trail_stop_pct = _ep["trail_stop_pct"]
                    _trail_act_pct  = _ep["trail_stop_activation_pct"]
                    if _trail_stop_pct > 0:
                        if ss['trail_peak'] == 0.0:
                            if _trail_act_pct == 0.0 or price >= _ic_entry * (1 + _trail_act_pct):
                                ss['trail_peak'] = price
                        else:
                            ss['trail_peak'] = max(ss['trail_peak'], price)
                    _trail_sl_level = (
                        ss['atr_sl'] if ss['atr_sl'] > 0
                        else (
                            ss['trail_peak'] * (1 - _trail_stop_pct)
                            if ss['trail_peak'] > 0 and _trail_stop_pct > 0 else 0.0
                        )
                    )

                    # Native trailing-stop backstop: only relevant when the
                    # software trailing level is itself in control (atr_sl==0
                    # — ATR SL otherwise always wins _trail_sl_level above, so
                    # a flat native stop already mirrors it exactly with no
                    # gap). One-shot swap the instant trail_peak arms — from
                    # then on Kraken's own engine tracks the peak, so no
                    # further re-sync is needed for price alone, only for a
                    # quantity change (handled by _resync_native_stop below).
                    if (
                        _trail_stop_pct > 0
                        and ss['atr_sl'] == 0.0
                        and ss['trail_peak'] > 0.0
                        and not ss['native_stop_is_trailing']
                        and cfg.exchange.native_stop_loss_enabled
                    ):
                        _tr_discovered = ss['executor'].sync_protective_stop(
                            None, trailing_pct=_trail_stop_pct,
                        )
                        ss['native_stop_is_trailing'] = True
                        if _tr_discovered is not None:
                            _process_discovered_sell_fill(
                                sym, ss, _tr_discovered, "native_stop_discovered",
                                capital_pool=capital_pool, risk=risk,
                                alerter=alerter, trade_log=trade_log,
                            )

                    _partial_tp_level = (
                        _ic_entry * (1 + cfg.backtest.partial_tp_pct)
                        if cfg.backtest.partial_tp_pct > 0 else None
                    )
                    if (
                        _partial_tp_level is not None
                        and price >= _partial_tp_level
                        and not ss['partial_done']
                        and ss['pm'].quantity > 0
                    ):
                        _p_qty = round(ss['pm'].quantity * cfg.backtest.partial_tp_size, 6)
                        if _p_qty > 0:
                            # Partial TP is an exit — classified with SL/TP, not with
                            # strategy SELLs: it bypasses the risk gate (only the halt
                            # check can block a SELL there, and a manual HALT must not
                            # freeze profit-taking exits). RISK_HALT_BLOCKS_STOPS=true
                            # suppresses it, same as the SL/TP block below.
                            _p_halted = (
                                cfg.risk.risk_halt_blocks_stops and risk.config.halt
                            )
                            if not _p_halted:
                                # 2026-08-24 (same pattern as the primary execute()
                                # call site below): guard against a genuinely
                                # unhandled exception crashing the loop or going
                                # unnoticed — log + Telegram-alert + degrade to
                                # "no order this tick" instead.
                                try:
                                    _p_order = ss['executor'].execute(Signal.SELL, price, quantity=_p_qty)
                                except Exception as _exec_exc:
                                    logger.error(
                                        "EXECUTOR EXCEPTION [%s] partial_tp: %s", sym, _exec_exc,
                                        exc_info=True,
                                    )
                                    alerter.error(
                                        f"EXECUTOR EXCEPTION [{sym}] partial_tp: {_exec_exc} — "
                                        f"order not confirmed placed or filled, check the exchange manually"
                                    )
                                    _p_order = None
                                if _p_order and _p_order.status == OrderStatus.FILLED:
                                    risk.record_fill(sym)
                                    ss['sm'].on_fill(Signal.SELL, _p_order.price)
                                    _p_pnl = ss['pm'].on_sell(_p_order.price, _p_order.quantity)
                                    ss['partial_done'] = True
                                    ss['sm'].recover_long(_p_order.price)
                                    # Resize the native stop backstop to the
                                    # reduced position (same static price or
                                    # same trailing % — partial TP changes
                                    # quantity, not level).
                                    _pt_discovered = _resync_native_stop(ss)
                                    if _pt_discovered is not None:
                                        _process_discovered_sell_fill(
                                            sym, ss, _pt_discovered, "native_stop_discovered",
                                            capital_pool=capital_pool, risk=risk,
                                            alerter=alerter, trade_log=trade_log,
                                        )
                                    print(f"           📊 PARTIAL TP [{sym}]:  {_p_qty:.6f} @ {price:,.2f}  PnL={_p_pnl:+.2f}", flush=True)
                                    logger.warning("PARTIAL TP [%s]: sold %.6f @ %.2f  pnl=%.2f", sym, _p_qty, price, _p_pnl)
                                    trade_log.log_fill(
                                        # 2026-09-18: log the ACTUAL filled
                                        # quantity, not the request — now
                                        # that LiveExecutor.execute() honors
                                        # a partial SELL instead of silently
                                        # liquidating the full position
                                        # (finding #6), _p_order.quantity is
                                        # trustworthy and should match the
                                        # other two log_fill call sites'
                                        # convention.
                                        side          = "SELL",
                                        symbol        = sym,
                                        quantity      = _p_order.quantity,
                                        price         = _p_order.price,
                                        pnl           = _p_pnl,
                                        exchange      = cfg.exchange.exchange,
                                        signal_reason = "partial_tp",
                                        fee_cost      = _p_order.fee_cost,
                                        fee_currency  = _p_order.fee_currency,
                                        exec_key      = _p_order.exec_key,
                                    )
                                    if hasattr(ss['executor'], 'ack_journal_entry'):
                                        ss['executor'].ack_journal_entry(_p_order.order_id)
                                    alerter.fill(
                                        side        = "SELL",
                                        symbol      = sym,
                                        quantity    = _p_qty,
                                        price       = _p_order.price,
                                        total_value = _p_order.total_value,
                                        pnl         = _p_pnl,
                                        exchange    = cfg.exchange.exchange,
                                        reason      = "partial take-profit — banking profit, position trimmed",
                                    )

                    _fixed_sl_level = (
                        _ic_entry * (1 - cfg.backtest.stop_loss_pct)
                        if cfg.backtest.stop_loss_pct > 0 else 0.0
                    )
                    _ic_sl = (
                        (_trail_sl_level > 0 and price <= _trail_sl_level)
                        or (_fixed_sl_level > 0 and price <= _fixed_sl_level)
                    )
                    _ic_tp = (
                        _ep["take_profit_pct"] > 0
                        and price >= _ic_entry * (1 + _ep["take_profit_pct"])
                    )
                    if _ic_sl or _ic_tp:
                        if _ic_sl:
                            _sl_label = "TRAIL STOP" if _trail_sl_level > 0 else "FIXED SL"
                            _sl_level = _trail_sl_level if _trail_sl_level > 0 else _fixed_sl_level
                            logger.warning(
                                "%s [%s]: price=%.2f entry=%.2f sl=%.2f",
                                _sl_label, sym, price, _ic_entry, _sl_level,
                            )
                            print(f"           🛑 {_sl_label} [{sym}]  price={price:,.2f}  entry={_ic_entry:,.2f}  sl={_sl_level:,.2f}", flush=True)
                        else:
                            logger.warning(
                                "TAKE PROFIT [%s]: price=%.2f entry=%.2f tp=%.1f%%",
                                sym, price, _ic_entry, _ep["take_profit_pct"] * 100,
                            )
                            print(f"           ✅ TAKE PROFIT [{sym}]  price={price:,.2f}  entry={_ic_entry:,.2f}", flush=True)
                        _ic_qty      = ss['pm'].quantity
                        # SL/TP bypasses the risk gate so stops always fire.
                        # Only when RISK_HALT_BLOCKS_STOPS=true does a manual halt suppress them.
                        _sl_tp_halted = (
                            cfg.risk.risk_halt_blocks_stops and risk.config.halt
                        )
                        if not _sl_tp_halted:
                            # urgent=True → always a market order. A stop exit must
                            # never sit in the limit-chase while price runs away.
                            # 2026-08-24 (same pattern as the primary execute() call
                            # site below): guard against a genuinely unhandled
                            # exception crashing the loop or going unnoticed — log +
                            # Telegram-alert + degrade to "no order this tick" instead.
                            try:
                                _ic_order = ss['executor'].execute(
                                    Signal.SELL, price, quantity=_ic_qty, urgent=True,
                                )
                            except Exception as _exec_exc:
                                logger.error(
                                    "EXECUTOR EXCEPTION [%s] urgent_sl_tp: %s", sym, _exec_exc,
                                    exc_info=True,
                                )
                                alerter.error(
                                    f"EXECUTOR EXCEPTION [{sym}] urgent_sl_tp: {_exec_exc} — "
                                    f"order not confirmed placed or filled, check the exchange manually"
                                )
                                _ic_order = None
                            if _ic_order and _ic_order.status == OrderStatus.FILLED:
                                risk.record_fill(sym)
                                ss['sm'].on_fill(Signal.SELL, _ic_order.price)
                                _ic_pnl = ss['pm'].on_sell(_ic_order.price, _ic_order.quantity)
                                ss['trail_peak'] = 0.0
                                ss['partial_done'] = False
                                if not ss['pm'].has_position:
                                    capital_pool.release(sym, ss['executor'].cash)
                                    # execute() already cancelled the native stop
                                    # BEFORE the SELL (2026-08-27 fix — see
                                    # LiveExecutor.execute). This is now a
                                    # no-op belt-and-suspenders for the full-close
                                    # case; kept so a future refactor that skips
                                    # the executor-side cancel still clears it.
                                    _ic_discovered = ss['executor'].sync_protective_stop(None)
                                    ss['native_stop_is_trailing'] = False
                                    if _ic_discovered is not None:
                                        _process_discovered_sell_fill(
                                            sym, ss, _ic_discovered, "native_stop_discovered",
                                            capital_pool=capital_pool, risk=risk,
                                            alerter=alerter, trade_log=trade_log,
                                        )
                                else:
                                    # Urgent market SELL only partially filled — a
                                    # residual position remains. execute() cancelled
                                    # the original (full-size) native stop before the
                                    # sell; re-place one sized to what's actually
                                    # still held. Same resize the partial-TP and
                                    # strategy-SELL paths already do.
                                    _ic_discovered = _resync_native_stop(ss)
                                    if _ic_discovered is not None:
                                        _process_discovered_sell_fill(
                                            sym, ss, _ic_discovered, "native_stop_discovered",
                                            capital_pool=capital_pool, risk=risk,
                                            alerter=alerter, trade_log=trade_log,
                                        )
                                _ic_reason = (
                                    "trail_stop" if (_trail_sl_level > 0 and price <= _trail_sl_level)
                                    else "stop_loss" if _ic_sl
                                    else "take_profit"
                                )
                                display.fill(
                                    _ic_order.side.value, _ic_order.quantity,
                                    sym, _ic_order.price,
                                    _ic_order.total_value, _ic_pnl,
                                )
                                trade_log.log_fill(
                                    side          = "SELL",
                                    symbol        = sym,
                                    quantity      = _ic_order.quantity,
                                    price         = _ic_order.price,
                                    pnl           = _ic_pnl,
                                    exchange      = cfg.exchange.exchange,
                                    signal_reason = _ic_reason,
                                    fee_cost      = _ic_order.fee_cost,
                                    fee_currency  = _ic_order.fee_currency,
                                    exec_key      = _ic_order.exec_key,
                                )
                                if hasattr(ss['executor'], 'ack_journal_entry'):
                                    ss['executor'].ack_journal_entry(_ic_order.order_id)
                                _ic_reason_label = {
                                    "trail_stop":  "trailing stop hit — protecting gained profit",
                                    "stop_loss":   "stop-loss hit — cutting the loss",
                                    "take_profit": "take-profit hit — target reached",
                                }.get(_ic_reason, _ic_reason)
                                alerter.fill(
                                    side        = "SELL",
                                    symbol      = sym,
                                    quantity    = _ic_order.quantity,
                                    price       = _ic_order.price,
                                    total_value = _ic_order.total_value,
                                    pnl         = _ic_pnl,
                                    exchange    = cfg.exchange.exchange,
                                    reason      = _ic_reason_label,
                                )
                                ss['exit_fail_count'] = 0
                            else:
                                # The urgent SL/TP exit did NOT go through
                                # (rejected / unconfirmed / exception). This is
                                # an emergency — the position is stuck and the
                                # stop/target can't be honoured. Silent until
                                # 2026-08-27: the native-stop deadlock rejected
                                # 200 exits over 8 min with zero alert. Edge-
                                # escalated (1st, 3rd, 10th, then every 20th
                                # failure) so a persistent problem keeps nagging
                                # without spamming every ~30s tick.
                                ss['exit_fail_count'] += 1
                                _n = ss['exit_fail_count']
                                _rr = getattr(_ic_order, 'reject_reason', None) or "no order returned"
                                _fail_kind = "STOP-LOSS" if _ic_sl else "TAKE-PROFIT"
                                logger.error(
                                    "SL/TP EXIT FAILED [%s] #%d (%s): %s (price=%.2f)",
                                    sym, _n, _fail_kind, _rr, price,
                                )
                                if _n in (1, 3, 10) or _n % 20 == 0:
                                    alerter.error(
                                        f"SL/TP EXIT FAILED [{sym}] ({_n} consecutive): {_rr}. "
                                        f"Price {price:,.2f}, entry {_ic_entry:,.2f}. The "
                                        f"position is stuck — the stop/target cannot execute. "
                                        f"Check Kraken for a resting order holding the balance."
                                    )
                        else:
                            logger.warning(
                                "SL/TP SELL halted (RISK_HALT_BLOCKS_STOPS=true) [%s]", sym,
                            )
                        display.position_line(
                            quantity       = ss['pm'].quantity,
                            symbol         = sym,
                            avg_entry      = ss['pm'].avg_entry,
                            unrealized_pnl = ss['pm'].unrealized_pnl(price),
                            realized_pnl   = ss['pm'].realized_pnl,
                            cash           = ss['executor'].cash,
                        )
                        tick_log.append({
                            "tick":   tick,
                            "time":   datetime.now().strftime("%H:%M:%S"),
                            "price":  price,
                            "signal": "SELL",
                            "rsi":    ss['dash_rsi'],
                            "trend":  ss['dash_trend'],
                            "state":  ss['sm'].state.value,
                            "reason": "trail_stop" if _ic_sl else "take_profit",
                            "sym":    sym,
                        })
                        _render_dashboard(sym, "SELL", ss['dash_rsi'], ss['dash_trend'])
                        continue  # skip candle eval for this symbol this tick

                # Live mode: evaluate only when a new candle has closed
                candle, new_ts = _fetch_completed_candle(
                    live_exchange, ss['last_ts_ms'], _LIVE_TF, symbol=sym
                )
                if candle is None:
                    # 2026-09-15 fix: risk.evaluate() — and with it the
                    # peak/kill-switch update — is never reached on a tick
                    # with no new candle (this `continue` skips straight past
                    # it). On a 4h timeframe that's most ticks, so a severe
                    # drawdown-and-recovery entirely between two candle
                    # closes could still escape the kill switch even after
                    # the 2026-09-14 fix made the trip check run on every
                    # evaluate() call — evaluate() just wasn't being called.
                    # mark_valuation() uses the live tick price already
                    # fetched into ss['last_price'] above, independent of any
                    # signal or candle, so peak/kill-switch state now tracks
                    # every tick's real valuation, not just candle-close ticks.
                    risk.mark_valuation(_account_value())
                    if sym == _active_symbol:
                        countdown = _candle_countdown(_LIVE_TF)
                        display.next_candle(price, tick, countdown)
                    # Dashboard update covers every symbol (2026-08-26) —
                    # console countdown print above stays active-symbol-only.
                    tick_log.append({
                        "tick":   tick,
                        "time":   datetime.now().strftime("%H:%M:%S"),
                        "price":  price,
                        "signal": ss['dash_signal'],
                        "rsi":    ss['dash_rsi'],
                        "trend":  ss['dash_trend'],
                        "state":  ss['sm'].state.value,
                        "reason": ss['dash_filter'] or ss['dash_block'],
                        "sym":    sym,
                    })
                    _render_dashboard(sym, ss['dash_signal'], ss['dash_rsi'], ss['dash_trend'])
                    continue  # no new candle for this symbol
                ss['last_ts_ms'] = new_ts
                ss['last_candle_time'] = time.time()
                raw_signal = ss['strategy'].evaluate(candle)
            elif is_indicator:
                # Simulated mode: flat fake candle per tick
                fake_candle = _Candle(
                    timestamp=datetime.now(_tz.utc),
                    open=price, high=price, low=price, close=price, volume=0.0,
                )
                raw_signal = ss['strategy'].evaluate(fake_candle)
            else:
                raw_signal = ss['strategy'].evaluate(price)

            # ── 2b. Candle-close diagnostic log (console) ────────────
            # CSV write is deferred to after all gates so blocked_gate is complete.
            if is_indicator and live_exchange is not None:
                _adx_live  = ss['strategy'].last_adx
                _rsi_live  = ss['strategy'].last_rsi
                _trnd_live = ss['strategy'].last_trend or "UNKNOWN"
                _cl = list(ss['strategy']._closes)
                _ef = _ema_fn(_cl, ss['strategy'].config.fast_ema_period)
                _es = _ema_fn(_cl, ss['strategy'].config.slow_ema_period)
                _spread = abs(_ef - _es) / _es * 100 if (_ef and _es and _es > 0) else 0.0
                _sig_str = raw_signal.value if hasattr(raw_signal, 'value') else str(raw_signal)

                _rsi_str = f"  RSI={_rsi_live:.1f}" if _rsi_live is not None else "  RSI=n/a"
                _adx_str = f"  ADX={_adx_live:.1f}" if _adx_live is not None else "  ADX=n/a"
                print(
                    f"  [{sym}] candle {candle.timestamp.strftime('%Y-%m-%d %H:%M')} UTC"
                    f"  close={price:,.2f}"
                    + _rsi_str
                    + _adx_str,
                    flush=True
                )
                print(
                    f"  trend={_trnd_live}"
                    f"  EMA_spread={_spread:.3f}%"
                    f"  signal={_sig_str}",
                    flush=True
                )

                # Heads-up Telegram alert the moment the raw strategy signal
                # turns BUY — before gates / execution. Edge-triggered per
                # symbol; the fill or blocked-BUY alert reports the outcome.
                _evaluate_buy_signal_alert(
                    ss, sym,
                    raw_signal_was_buy=(raw_signal == Signal.BUY),
                    price=price,
                    alerter=alerter,
                )

            # ── Blocked-gate tracking (initialise per-candle) ─────────
            # Records the first gate (in priority order) that blocked a BUY.
            # Priority: trend → RSI → ADX → EMA_spread → MACD → mtf_trend
            #           → correlation → candle_watchdog → state_machine
            #           → risk_manager → capital_pool
            # (external_signal and the independent regime gate were both
            # removed 2026-09-02 — see sections 2d/2e below. mtf_trend was
            # split from a shared "regime" label 2026-08-24 after the
            # 2026-08-18 missed-BUY investigation. "regime" as a label now
            # comes only from the strategy's own 200-EMA / VOLATILE path. See
            # .memory/decisions/2026-08-18-missed-buy-signal.md.)
            _signal_raw_for_csv = raw_signal
            _buy_block_gate: str = ""
            if is_indicator and live_exchange is not None:
                # Strategy-internal block (trend, RSI, ADX, EMA_spread, MACD)
                if _trnd_live == "BULLISH" and raw_signal != Signal.BUY:
                    _buy_block_gate = ss['strategy'].last_buy_block_gate or "RSI"
                elif _trnd_live != "BULLISH" and _trnd_live != "BEARISH":
                    # NEUTRAL trend — BUY was "considered" (regime trending) but trend rejected
                    if (ss['strategy'].last_regime == "TRENDING"
                            and raw_signal != Signal.BUY):
                        _buy_block_gate = "trend"

            # ── 2c. MTF gate ──────────────────────────────────────────
            # Daily closes are fetched per symbol at decision time so the veto
            # never runs on stale data. (Previously: loaded once at startup and
            # only refreshed AFTER a BUY had already been judged — the gate
            # could veto on daily candles that were weeks old, and it applied
            # the active symbol's daily trend to every symbol.)
            # BUY signals are rare, so this is at most one extra API call per
            # BUY-signal candle. On fetch failure fall back to the cached
            # closes; with no cache the gate fails open (same as before) — but
            # that's a risk gate silently bypassed on a live BUY, so alert.
            if is_indicator and raw_signal == Signal.BUY and live_exchange is not None:
                try:
                    _raw_1d = live_exchange.fetch_ohlcv(sym, timeframe="1d", limit=30)
                    if _raw_1d:
                        ss['mtf_1d_closes'] = [float(r[4]) for r in _raw_1d[:-1]]
                except Exception as _mtf_exc:
                    _mtf_has_cache = bool(ss['mtf_1d_closes'])
                    logger.warning(
                        "MTF gate [%s]: daily fetch failed (%s) — %s",
                        sym, _mtf_exc,
                        "using cached closes" if _mtf_has_cache
                        else "no cache, gate skipped",
                    )
                    if not _mtf_has_cache:
                        try:
                            alerter.error(
                                f"MTF GATE BYPASSED [{sym}]: daily-candle fetch failed "
                                f"({type(_mtf_exc).__name__}) and no cached closes exist — "
                                f"the 1D BEARISH veto was skipped and this BUY proceeded "
                                f"unchecked by it. Fail-open is by design; this alert is so "
                                f"it isn't silent."
                            )
                        except Exception:
                            pass
                if ss['mtf_1d_closes']:
                    _mtf_trend = _trend_fn(ss['mtf_1d_closes'])
                    if _mtf_trend == "BEARISH":
                        raw_signal = Signal.HOLD
                        if not _buy_block_gate:
                            # 2026-08-24: was "regime" — split from the regime-proper
                            # gate (2e) and the external-signal gate (2d) below so
                            # live_signals.csv's blocked_gate column can distinguish
                            # which of the three actually fired (see .memory/decisions/
                            # 2026-08-18-missed-buy-signal.md for why this mattered).
                            _buy_block_gate = "mtf_trend"
                        print(f"  [{sym}] MTF gate: 1D trend BEARISH — BUY suppressed", flush=True)
                        logger.info("MTF gate [%s]: BUY suppressed — daily trend BEARISH", sym)
                    else:
                        logger.info("MTF gate [%s]: OK — daily trend %s", sym, _mtf_trend)

            # ── 2d. (removed 2026-09-02) external Fear&Greed / funding gate ──
            # Backtested net-negative-to-wash on every window, 0 live vetoes
            # ever, cost a third-party API + a bypass-alert path. See the
            # removal note further up in run() and CLAUDE_HISTORY.md.

            # ── 2e. (removed 2026-09-02) independent "regime gate" ─────
            # This re-checked ADX ≥ adx_threshold AND EMA spread ≥
            # min_ema_spread_pct using the SAME strategy.last_adx and the SAME
            # closes deque the strategy itself gates on
            # (IndicatorStrategy._trend_signal enforces ADX at ~line 341 and
            # EMA spread via `ema_strong` at ~line 367/395). A strategy BUY has
            # therefore already cleared both — this block could never flip one
            # to HOLD. Its only live effect was a log line plus sharing the
            # "regime" blocked-gate label with the strategy's own 200-EMA macro
            # filter, an ambiguity that cost real time in the 2026-08-18
            # missed-BUY investigation. Removed as dead code; the strategy's
            # ADX/spread rejections still surface via the section-2b candle
            # diagnostic and `last_buy_block_gate`. Do not re-add without a
            # check that actually differs from the strategy's own.

            # ── 2f. Correlation gate ──────────────────────────────────
            # Block BUY when this symbol's 30-day returns are highly correlated
            # (> 0.70) with any currently-open position. Prevents simultaneous
            # exposure to assets that move together during a drawdown.
            # Only runs in live mode where we can fetch daily closes; skipped
            # when there are no other open positions (nothing to correlate against).
            if raw_signal == Signal.BUY and live_exchange is not None:
                _open_peers = [
                    other_sym
                    for other_sym, other_ss in symbol_state.items()
                    if other_sym != sym and other_ss['pm'].has_position
                ]
                _corr_blocked = False
                for _peer in _open_peers:
                    _corr = fetch_correlation(live_exchange, sym, _peer)
                    if _corr is not None and _corr > CORRELATION_THRESHOLD:
                        raw_signal = Signal.HOLD
                        if not _buy_block_gate:
                            _buy_block_gate = "correlation"
                        _corr_msg = (
                            f"CORRELATION GATE: BUY blocked — {sym} correlation"
                            f" {_corr:.2f} with open {_peer}"
                        )
                        print(f"  [{sym}] {_corr_msg}", flush=True)
                        logger.warning(_corr_msg)
                        _corr_blocked = True
                        break
                if not _corr_blocked:
                    logger.info(
                        "Correlation gate [%s]: OK — checked %d open peer(s), none > %.2f",
                        sym, len(_open_peers), CORRELATION_THRESHOLD,
                    )

            # ── 2g. Candle watchdog gate ───────────────────────────────
            # Circuit breaker (upgraded 2026-08-07 — previously alert-only,
            # see _check_candle_watchdog). A stale feed means the strategy
            # would be evaluating against data that may no longer reflect
            # the market — block new BUYs until a fresh candle arrives.
            # SELL/exits are untouched: they run off the independent
            # live-tick price feed, not the candle feed, and must always be
            # allowed to close a position per the standing breaker rule.
            if raw_signal == Signal.BUY and ss['candle_feed_stale']:
                raw_signal = Signal.HOLD
                if not _buy_block_gate:
                    _buy_block_gate = "candle_watchdog"
                print(f"  [{sym}] CANDLE WATCHDOG: BUY blocked — feed stale", flush=True)
                logger.warning("CANDLE WATCHDOG [%s]: BUY blocked — feed stale", sym)
            elif raw_signal == Signal.BUY:
                logger.info("Candle watchdog [%s]: OK — feed fresh", sym)

            # ── 2h. Price feed staleness gate (2026-09-18 review finding) ──
            # Same standing breaker rule as the candle watchdog, for the
            # independent live-tick price feed: a run of ticker-fetch
            # failures means `price` this tick is reused from the last
            # successful fetch, not fresh — do not authorize a new entry
            # against it. SELL/exits are untouched.
            if raw_signal == Signal.BUY and ss['price_feed_stale']:
                raw_signal = Signal.HOLD
                if not _buy_block_gate:
                    _buy_block_gate = "price_feed_stale"
                print(f"  [{sym}] PRICE FEED: BUY blocked — stale price", flush=True)
                logger.warning("PRICE FEED [%s]: BUY blocked — stale price", sym)

            # ── 3. Warmup guard ───────────────────────────────────────
            if is_indicator and not ss['strategy'].is_warmed_up:
                if sym == _active_symbol:
                    display.warmup(tick, ss['strategy'].tick_count, ss['strategy']._warmup, price)
                continue

            rsi_val   = ss['strategy'].last_rsi   if is_indicator else None
            trend_val = ss['strategy'].last_trend if is_indicator else None

            # ── 4. State machine filter + tick ────────────────────────
            filtered_signal, filter_reason = ss['sm'].filter_signal(raw_signal)
            if raw_signal == Signal.BUY:
                if filtered_signal != Signal.BUY:
                    if not _buy_block_gate:
                        _buy_block_gate = "state_machine"
                    logger.info(
                        "State machine [%s]: BUY -> %s  (%s)",
                        sym, filtered_signal.value, filter_reason or "filtered",
                    )
                else:
                    logger.info("State machine [%s]: OK — BUY passed through  (state=%s)",
                                sym, ss['sm'].state.value)
            ss['sm'].tick()

            # ── 5. Dynamic position sizing ────────────────────────────
            # executor.cash is already capped to its pool slot — no division needed.
            # Block BUY when the pool has no slot available for a new position.
            if filtered_signal == Signal.BUY:
                if not capital_pool.can_open_position(sym):
                    if not _buy_block_gate:
                        _buy_block_gate = "capital_pool"
                    filtered_signal = Signal.HOLD
                    logger.info(
                        "CapitalPool: BUY blocked for %s — pool exhausted (%d/%d slots used)",
                        sym, len(capital_pool.allocated_symbols), _max_conc,
                    )
                else:
                    logger.info(
                        "CapitalPool [%s]: OK — slot available (%d/%d slots used)",
                        sym, len(capital_pool.allocated_symbols), _max_conc,
                    )
            _max_cash_for_sym = ss['executor'].cash
            if filtered_signal == Signal.SELL:
                trade_qty = ss['pm'].quantity
            else:
                _requested_qty = cfg.calc_trade_qty(_max_cash_for_sym, price)
                trade_qty       = _requested_qty
                _sizing_method  = "notional (calc_trade_qty)"
                # ATR-aware sizing (ATR_SIZING_ENABLED): cap qty so an ATR
                # stop-out never risks more $ than the fixed-SL baseline.
                if (
                    is_indicator
                    and cfg.strategy.atr_sizing_enabled
                    and cfg.strategy.atr_sl_mult > 0
                ):
                    _atr_sz = _atr_fn(
                        list(ss['strategy']._highs),
                        list(ss['strategy']._lows),
                        list(ss['strategy']._closes),
                        cfg.strategy.atr_period,
                    )
                    if _atr_sz is not None and _atr_sz > 0:
                        trade_qty = cfg.calc_trade_qty_atr_risk(
                            _max_cash_for_sym, price, _atr_sz,
                            cfg.strategy.atr_sl_mult,
                            cfg.backtest.stop_loss_pct or 0.015,
                        )
                        _sizing_method = "ATR-risk-capped (calc_trade_qty_atr_risk)"
                max_affordable = (_max_cash_for_sym * 0.98) / price
                trade_qty = min(trade_qty, max_affordable)
                trade_qty = round(trade_qty, 6)
                if filtered_signal == Signal.BUY:
                    logger.info(
                        "Sizing [%s]: requested=%.8f (%s)  cash_available=$%.2f  "
                        "max_affordable=%.8f  final_qty=%.8f",
                        sym, _requested_qty, _sizing_method, _max_cash_for_sym,
                        max_affordable, trade_qty,
                    )

            # ── 6. AI advisory ────────────────────────────────────────
            advice       = None
            final_signal = filtered_signal
            if ai and ai.enabled and filtered_signal != Signal.HOLD:
                advice = ai.advise(
                    price           = price,
                    rsi             = rsi_val,
                    trend           = trend_val,
                    strategy_signal = filtered_signal,
                    recent_prices   = list(ss['strategy']._closes) if is_indicator else [price],
                    portfolio       = ss['executor'].portfolio,
                    symbol          = sym,
                )
                final_signal = merge_signals(filtered_signal, advice)

            # ── 7. Risk gate ──────────────────────────────────────────
            approval     = risk.evaluate(
                final_signal, price, ss['executor'].portfolio, trade_qty,
                account_value=_account_value(), symbol=sym,
            )
            block_reason = "" if approval else approval.message
            if not approval and final_signal == Signal.BUY and not _buy_block_gate:
                _buy_block_gate = "risk_manager"

            # ── 7b. Candle-close structured log + blocked-BUY CSV ────
            if is_indicator and live_exchange is not None:
                _rsi_log = f"{_rsi_live:.1f}" if _rsi_live is not None else "n/a"
                _adx_log = f"{_adx_live:.1f}" if _adx_live is not None else "n/a"
                _action  = (
                    final_signal.value
                    if approval else
                    f"BLOCKED[{approval.block_reason.value if approval.block_reason else '?'}]"
                )
                logger.info(
                    "CANDLE [%s] %s UTC | close=%.2f RSI=%s ADX=%s trend=%s spread=%.3f%% signal=%s -> %s",
                    sym,
                    candle.timestamp.strftime("%Y-%m-%d %H:%M"),
                    price,
                    _rsi_log,
                    _adx_log,
                    _trnd_live,
                    _spread,
                    _sig_str,
                    _action,
                )
                candle_log.append({
                    "sym":    sym,
                    "ts":     candle.timestamp.strftime("%Y-%m-%d %H:%M"),
                    "close":  price,
                    "rsi":    round(_rsi_live, 1) if _rsi_live is not None else None,
                    "adx":    round(_adx_live, 1) if _adx_live is not None else None,
                    "trend":  _trnd_live,
                    "spread": round(_spread, 3),
                    "signal": _sig_str,
                    "action": _action,
                    "reason": _buy_block_gate,
                })

                # Write to live_signals.csv only when a BUY was considered but blocked
                _buy_was_blocked = bool(_buy_block_gate) and not (
                    approval and final_signal == Signal.BUY
                )
                if _buy_was_blocked:
                    _live_log     = os.path.join(_log_dir, "live_signals.csv")
                    _csv_schema   = [
                        "timestamp", "symbol", "price", "RSI", "ADX",
                        "EMA_spread", "trend", "signal_raw",
                        "blocked_gate", "signal_final",
                    ]
                    _write_hdr    = True
                    if os.path.exists(_live_log):
                        try:
                            with open(_live_log, newline="") as _chk:
                                _existing_hdr = next(csv.reader(_chk), None)
                            if _existing_hdr == _csv_schema:
                                _write_hdr = False
                            else:
                                _legacy_ts  = datetime.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
                                _legacy_path = os.path.join(
                                    _log_dir, f"live_signals_legacy_{_legacy_ts}.csv"
                                )
                                os.rename(_live_log, _legacy_path)
                                logger.warning(
                                    "live_signals.csv header mismatch — renamed to %s",
                                    _legacy_path,
                                )
                        except Exception as _hdr_exc:
                            logger.warning("live_signals.csv header check failed: %s", _hdr_exc)
                    _signal_final_str = (
                        final_signal.value if (approval and final_signal == Signal.BUY) else "HOLD"
                    )
                    with open(_live_log, "a", newline="") as _f:
                        _w = csv.writer(_f)
                        if _write_hdr:
                            _w.writerow(_csv_schema)
                        _w.writerow([
                            candle.timestamp.strftime("%Y-%m-%d %H:%M"),
                            sym,
                            round(price, 2),
                            round(_rsi_live, 2) if _rsi_live is not None else "",
                            round(_adx_live, 2) if _adx_live is not None else "",
                            round(_spread, 4),
                            _trnd_live,
                            _signal_raw_for_csv.value if hasattr(_signal_raw_for_csv, 'value')
                                else str(_signal_raw_for_csv),
                            _buy_block_gate,
                            _signal_final_str,
                        ])

                # Edge-triggered Telegram alert when the strategy signals BUY
                # but a gate holds it — closes the 2026-08-18 "sat flat through
                # a rally, nobody knew a BUY had been vetoed" gap.
                _evaluate_blocked_buy_alert(
                    ss, sym,
                    raw_signal_was_buy=(_signal_raw_for_csv == Signal.BUY),
                    block_gate=(_buy_block_gate if not (approval and final_signal == Signal.BUY) else ""),
                    alerter=alerter,
                )

            # ── 8. Display tick (active symbol only) ──────────────────
            if sym == _active_symbol:
                display.tick(
                    tick_n        = tick,
                    price         = price,
                    raw_signal    = raw_signal.value,
                    final_signal  = final_signal.value,
                    rsi           = rsi_val,
                    trend         = trend_val,
                    filter_reason = filter_reason,
                    block_reason  = block_reason,
                )
                display.state_line(
                    state      = ss['sm'].state.value,
                    cooldown   = ss['sm'].cooldown_remaining,
                    last_trade = ss['sm'].last_trade_label,
                )
                if advice:
                    vetoed = final_signal != filtered_signal
                    display.ai_advice(
                        advice.signal.value, advice.confidence,
                        advice.reasoning, advice.latency_ms, vetoed,
                    )

            # ── 9. Execute ────────────────────────────────────────────
            # Dynamic-mode BUYs are deferred to a ranked, cross-symbol pass
            # AFTER this per-symbol loop finishes (see "Dynamic universe:
            # ranked BUY execution" below _account_value-scope, right after
            # the loop) — one symbol must not consume a shared capital slot
            # before every OTHER symbol's simultaneous BUY signal has even
            # been seen this tick. Fixed-mode BUYs and EVERY SELL (dynamic
            # or fixed — exits are never ranked/deferred, only entries
            # compete for a slot) still execute immediately, exactly as
            # before this refactor — _execute_approved_signal is a verbatim
            # extraction of what used to be inline here (see its docstring).
            order = None
            if approval and _dynamic_mode_active and final_signal == Signal.BUY:
                _dynamic_buy_queue.append(dict(
                    sym=sym, ss=ss, final_signal=final_signal, price=price,
                    trade_qty=trade_qty, raw_signal=raw_signal, filter_reason=filter_reason,
                    adx=(ss['strategy'].last_adx if is_indicator else None),
                    quote_volume=_dynamic_quote_volume.get(sym),
                ))
            elif approval:
                order = _execute_approved_signal(
                    sym, ss, final_signal, price, trade_qty, raw_signal, filter_reason,
                    capital_pool=capital_pool, risk=risk, alerter=alerter,
                    trade_log=trade_log, stuck_detector=stuck_detector,
                    is_indicator=is_indicator,
                )

            # ── 10. Position summary ──────────────────────────────────
            display.position_line(
                quantity       = ss['pm'].quantity,
                symbol         = sym,
                avg_entry      = ss['pm'].avg_entry,
                unrealized_pnl = ss['pm'].unrealized_pnl(price),
                realized_pnl   = ss['pm'].realized_pnl,
                cash           = ss['executor'].cash,
            )

            # ── 11. Tick log + dashboard (every symbol — 2026-08-26: used to
            # be active-symbol-only; the dashboard now covers all live
            # symbols on one page) ─────────────────────────────────────────
            ss['dash_signal'] = final_signal.value
            ss['dash_rsi']    = rsi_val
            ss['dash_trend']  = trend_val
            ss['dash_filter'] = filter_reason
            ss['dash_block']  = block_reason
            tick_log.append({
                "tick":   tick,
                "time":   datetime.now().strftime("%H:%M:%S"),
                "price":  price,
                "signal": final_signal.value,
                "rsi":    rsi_val,
                "trend":  trend_val,
                "state":  ss['sm'].state.value,
                "reason": filter_reason or block_reason,
                "sym":    sym,
            })
            _render_dashboard(sym, final_signal.value, rsi_val, trend_val)

        # ── Dynamic universe: ranked BUY execution ────────────────────────
        # Every symbol's BUY signal this tick has been gathered into
        # _dynamic_buy_queue (section 9 above deferred them instead of
        # executing inline) without yet touching capital_pool or the
        # exchange — see _execute_ranked_dynamic_buys' own docstring for
        # why sequential, freshly-re-checked execution here is what
        # "reserve capital for pending orders and prevent double
        # allocation" reduces to under this bot's existing design.
        if _dynamic_mode_active and _dynamic_buy_queue:
            def _refresh_dynamic_price(_sym, _ex=live_exchange):
                return float(fetch_with_retry(
                    lambda: _ex.fetch_ticker(_sym)['last'],
                    label=f"dynamic ranked-buy price refresh [{_sym}]",
                ))
            _, _dynamic_blocked_this_cycle = _execute_ranked_dynamic_buys(
                _dynamic_buy_queue, capital_pool=capital_pool, risk=risk,
                account_value_fn=_account_value, alerter=alerter,
                trade_log=trade_log, stuck_detector=stuck_detector,
                is_indicator=is_indicator, max_concurrent=_max_conc,
                symbol_state=symbol_state, live_exchange=live_exchange,
                refresh_price_fn=_refresh_dynamic_price,
            )

        # ── 0c. Dynamic universe discovery + admission/retirement ─────────
        # Runs LAST, after every existing position's SL/TP/exit management
        # (the per-symbol loop above) AND this tick's ranked-BUY execution
        # — moved here 2026-09-13 (see "0b." above for why). A discovery/
        # admission failure here never raises (both functions are
        # internally exception-safe) and, because it's now positioned
        # after all position management for this tick, can never delay it
        # regardless of how long discovery takes.
        if _dynamic_mode_active and live_exchange is not None:
            if time.time() - _dynamic_last_refresh > cfg.dynamic.refresh_hours * 3600:
                try:
                    # Single shared capital pool/position limit for BOTH the
                    # fixed roster and dynamic candidates — cfg.dynamic has
                    # NO separate max_concurrent_positions of its own for
                    # this live path (that field only matters to the
                    # retired standalone paper runner's own isolated pool).
                    # A human wanting dynamic symbols to have room beyond
                    # the current fixed-roster slots must raise
                    # MAX_CONCURRENT_POSITIONS (and STARTING_CASH together,
                    # per the existing documented capital-sizing rule) —
                    # see CLAUDE.md "Dynamic Crypto Universe" activation steps.
                    _slot_est = capital_pool.total_capital / max(1, _max_conc)
                    _admitted, _retired, _dynamic_last_screen = _sync_dynamic_universe(
                        symbol_state, executors, _dynamic_admitted,
                        _dynamic_screener, live_exchange, _LIVE_TF,
                        capital_pool, _slot_est,
                    )
                    if _admitted:
                        logger.info("Dynamic universe: admitted this cycle: %s", _admitted)
                    if _retired:
                        logger.info("Dynamic universe: retired this cycle: %s", _retired)
                except Exception as _dyn_exc:
                    logger.warning("Dynamic universe sync failed: %s — existing positions unaffected", _dyn_exc)
                _dynamic_last_refresh = time.time()

        # Dashboard snapshot — written HERE, after admission/retirement AND
        # ranked execution have both happened this tick, so blocked-reason
        # data is real rather than frozen-empty (see the "0b" step above).
        if _dynamic_mode_active:
            try:
                _write_dynamic_universe_dashboard(
                    "logs/dynamic_universe_dashboard.json", _dynamic_last_screen,
                    symbol_state, _dynamic_admitted, capital_pool,
                    _dynamic_blocked_this_cycle,
                    dry_run=(cfg.paper.paper_mode or cfg.exchange.dry_run),
                )
            except Exception:
                pass

        # ── End of per-symbol loop ────────────────────────────────────
        _liveness.touch()
        time.sleep(cfg.exchange.loop_interval)
        _now_utc = datetime.now(_tz.utc)
        # Fire exactly once per UTC day — date-change check instead of a
        # minute-0 window, which double-fired on a 30s loop interval and
        # skipped entirely when a tick ran past the minute.
        if _now_utc.date() != _last_daily_pnl_date:
            _last_daily_pnl_date = _now_utc.date()
            for _dp_sym, _dp_ss in symbol_state.items():
                _dp_ex = executors.get(_dp_sym)
                if _dp_ex is None:
                    continue
                alerter.daily_pnl(
                    symbol       = _dp_sym,
                    realized_pnl = _dp_ss['pm'].realized_pnl,
                    total_value  = _dp_ex.portfolio.total_value(
                        _dp_ss.get('last_price') or 0
                    ),
                    trade_count  = risk.fills_today_for(_dp_sym),
                )

        # Daily both-bots health digest (local-time scheduled, once/day).
        _maybe_send_health_digest(
            executors, symbol_state, risk, alerter,
            cfg.exchange.live_trading, cfg.exchange.dry_run, datetime.now(),
            stuck_detector=stuck_detector,
        )

    display.stopped(
        ticks        = tick,
        fills        = sum(len(exc.filled_orders()) for exc in executors.values()),
        rejects      = sum(len(exc.rejected_orders()) for exc in executors.values()),
        pos          = position_manager.quantity,
        cash         = sum(exc.cash for exc in executors.values()),
        realized_pnl = position_manager.realized_pnl,
    )


def _send_crash_alert(bot_name: str, tb: str) -> None:
    """Last-gasp Telegram alert on fatal crash. Synchronous (the process is
    about to die — an async send would be lost). Never raises."""
    try:
        from bot.alerts.telegram import TelegramAlerter
        alerter = TelegramAlerter(
            cfg.alerts.telegram_bot_token,
            cfg.alerts.telegram_chat_id,
            enabled=cfg.alerts.telegram_enabled,
        )
        alerter.send_now(f"💀 {bot_name} CRASHED\n\n{tb[-900:]}")
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
        logger.critical("FATAL CRASH — bot exiting:\n%s", _tb)
        _send_crash_alert("Crypto bot", _tb)
        raise