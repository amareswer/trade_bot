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
from bot.execution.executor import PaperExecutor, OrderStatus, OrderSide
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
from bot.signals.external_signals import ExternalSignalGate, ExternalSignalsConfig as _ExtSigsCfg
from bot.alerts.telegram import TelegramAlerter
from bot.data.trade_log import TradeLog
from bot.data.crypto_universe import CryptoUniverse

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
    capital_pool = CapitalPool(
        total_capital=_pool_total, max_concurrent=_max_conc, slot_cap=_slot_cap
    )
    _slot = capital_pool.slot_cash
    _uncapped_slot = _pool_total / _max_conc
    for _sym, _exc in executors.items():
        _exc._portfolio.cash = _slot
        if cfg.exchange.live_trading:
            try:
                _exc._save_state()
            except Exception as e:
                logger.warning("State save after pool init failed [%s]: %s", _sym, e)
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

    # ── External signal gate (live mode only) ─────────────────────────────────
    ext_gate: "ExternalSignalGate | None" = None
    if cfg.exchange.feed_mode == "live" and (cfg.signals.fng_enabled or cfg.signals.funding_enabled):
        ext_gate = ExternalSignalGate(_ExtSigsCfg(
            fng_enabled           = cfg.signals.fng_enabled,
            fng_bear_max          = cfg.signals.fng_bear_max,
            fng_bull_min          = cfg.signals.fng_bull_min,
            fng_cache_seconds     = cfg.signals.fng_cache_seconds,
            funding_enabled       = cfg.signals.funding_enabled,
            funding_symbol        = cfg.signals.funding_symbol,
            funding_max           = cfg.signals.funding_max,
            funding_cache_seconds = cfg.signals.funding_cache_seconds,
        ))

    # ── Persistent trade log + Telegram alerts ────────────────────────────────
    trade_log = TradeLog()
    alerter   = TelegramAlerter(
        bot_token = cfg.alerts.telegram_bot_token,
        chat_id   = cfg.alerts.telegram_chat_id,
        enabled   = cfg.alerts.telegram_enabled,
    )

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
    _check_orphaned_positions(set(executors.keys()), alerter)

    # ── Derive live candle timeframe from CANDLE_MINUTES ─────────────────────
    # This is the timeframe used for ALL live candle operations:
    # warmup fetch, candle polling, and countdown display.
    # BACKTEST_TIMEFRAME is only used for backtesting, not live trading.
    _LIVE_TF = _minutes_to_timeframe(cfg.exchange.candle_minutes)

    # ── Multi-symbol state initialisation ────────────────────────────────────
    symbol_state: dict[str, dict] = {}

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
                'last_price':       0.0,
                'err_count':        0,            # consecutive price-fetch failures
                'drift_count':      0,            # consecutive drift detections
                'drift_acked':      0.0,          # drift amount already escalated (no re-alert until it changes)
                'last_candle_time': time.time(),  # candle watchdog timer
                'mtf_1d_closes':    [],           # daily closes cache — refreshed at gate 2c
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
            'last_price':       0.0,
            'err_count':        0,
            'drift_count':      0,
            'drift_acked':      0.0,
            'last_candle_time': time.time(),
            'mtf_1d_closes':    [],
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

    def _account_value() -> float:
        """Aggregate account value across ALL symbol slots (cash + marked positions).
        Fed to the risk gate so daily-loss/drawdown breakers measure the whole
        account, not just the slot being evaluated. With one symbol this equals
        that slot's portfolio value — behavior unchanged."""
        total = 0.0
        for _s, _e in executors.items():
            _px = symbol_state[_s]['last_price'] if _s in symbol_state else 0.0
            if not _px:
                _px = getattr(_e, "avg_entry", 0.0) or 0.0
            total += _e.cash + _e.position * _px
        return total

    def _resync_native_stop(ss: dict) -> None:
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
        """
        if not ss['pm'].has_position:
            ss['executor'].sync_protective_stop(None)
            ss['native_stop_is_trailing'] = False
            return
        if ss['native_stop_is_trailing']:
            ss['executor'].sync_protective_stop(
                None, trailing_pct=cfg.backtest.trail_stop_pct,
            )
        else:
            ss['executor'].sync_protective_stop(ss.get('native_stop_price'))

    # Trailing stop and partial TP state — reset on each new trade
    _trail_peak:      float = 0.0
    _partial_tp_done: bool  = False
    _atr_sl_price:    float = 0.0

    # Sticky indicator values — updated each candle close, displayed between closes
    _dash_signal = "HOLD"
    _dash_rsi    = None
    _dash_trend  = None
    _dash_filter = ""
    _dash_block  = ""

    def _render_dashboard(sig: str, rsi_v, trend_v) -> None:
        if not cfg.dashboard.enabled:
            return
        fills_data = [
            {
                "time":  o.filled_at.astimezone().strftime("%H:%M:%S") if o.filled_at else "—",
                "side":  o.side.value,
                "qty":   o.quantity,
                "price": o.price,
                "total": o.total_value,
                "pnl":   next(
                    (r.pnl for r in reversed(position_manager.history)
                     if r.action == o.side.value and abs(r.price - o.price) < 0.01),
                    None,
                ),
            }
            for o in executor.filled_orders()
        ]
        try:
            _dashboard.write(
                path            = _DASHBOARD_PATH,
                exchange        = cfg.exchange.exchange,
                symbol          = cfg.exchange.symbol,
                strategy        = cfg.strategy.mode,
                tick            = tick,
                price           = price,
                signal          = sig,
                rsi             = rsi_v,
                trend           = trend_v,
                state           = state_machine.state.value,
                cooldown        = state_machine.cooldown_remaining,
                last_trade      = state_machine.last_trade_label,
                cash            = executor.cash,
                position        = position_manager.quantity,
                avg_entry       = position_manager.avg_entry,
                unrealized_pnl  = position_manager.unrealized_pnl(price),
                realized_pnl    = position_manager.realized_pnl,
                total_value     = executor.portfolio.total_value(price),
                fills           = fills_data,
                tick_log        = list(tick_log),
                candle_log      = list(candle_log),
                refresh_s       = cfg.dashboard.refresh_s,
                live_trading    = cfg.exchange.live_trading,
                dry_run         = cfg.exchange.dry_run,
                stop_loss_pct      = cfg.backtest.stop_loss_pct,
                take_profit_pct    = cfg.backtest.take_profit_pct,
                fees_paid          = getattr(executor, "fees_paid", 0.0),
                rsi_filter_enabled = cfg.strategy.rsi_filter_enabled,
                volume_k           = cfg.strategy.volume_k,
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

        # ── Per-symbol processing ─────────────────────────────────────
        for sym, ss in symbol_state.items():

            # ── 1. Fetch live price ───────────────────────────────────
            if live_exchange is not None:
                try:
                    price = float(fetch_with_retry(
                        lambda: live_exchange.fetch_ticker(sym)['last'],
                        label=f"price fetch [{sym}]",
                    ))
                    ss['last_price'] = price
                    ss['err_count'] = 0
                except Exception as exc:
                    ss['err_count'] += 1
                    if ss['err_count'] >= 5:
                        alerter.error(
                            f"Price feed down [{sym}] {ss['err_count']} consecutive ticks — {exc}"
                        )
                    logger.warning("price fetch failed for %s: %s", sym, exc)
                    price = ss['last_price']
                    if not price:
                        continue
            else:
                try:
                    price = feed.get_price()
                    ss['last_price'] = price
                    ss['err_count'] = 0
                except Exception as exc:
                    ss['err_count'] += 1
                    if ss['err_count'] >= 5:
                        alerter.error(
                            f"Price feed down [{sym}] {ss['err_count']} consecutive ticks — {exc}"
                        )
                    print(f"  TICK {tick:04d} | price fetch failed: {exc}")
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
                    _trail_stop_pct = cfg.backtest.trail_stop_pct
                    _trail_act_pct  = cfg.backtest.trail_stop_activation_pct
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
                        ss['executor'].sync_protective_stop(
                            None, trailing_pct=_trail_stop_pct,
                        )
                        ss['native_stop_is_trailing'] = True

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
                                _p_order = ss['executor'].execute(Signal.SELL, price, quantity=_p_qty)
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
                                    _resync_native_stop(ss)
                                    print(f"           📊 PARTIAL TP [{sym}]:  {_p_qty:.6f} @ {price:,.2f}  PnL={_p_pnl:+.2f}", flush=True)
                                    logger.warning("PARTIAL TP [%s]: sold %.6f @ %.2f  pnl=%.2f", sym, _p_qty, price, _p_pnl)
                                    trade_log.log_fill(
                                        side          = "SELL",
                                        symbol        = sym,
                                        quantity      = _p_qty,
                                        price         = _p_order.price,
                                        pnl           = _p_pnl,
                                        exchange      = cfg.exchange.exchange,
                                        signal_reason = "partial_tp",
                                        fee_cost      = _p_order.fee_cost,
                                        fee_currency  = _p_order.fee_currency,
                                    )
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
                        cfg.backtest.take_profit_pct > 0
                        and price >= _ic_entry * (1 + cfg.backtest.take_profit_pct)
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
                                sym, price, _ic_entry, cfg.backtest.take_profit_pct * 100,
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
                            _ic_order = ss['executor'].execute(
                                Signal.SELL, price, quantity=_ic_qty, urgent=True,
                            )
                            if _ic_order and _ic_order.status == OrderStatus.FILLED:
                                risk.record_fill(sym)
                                ss['sm'].on_fill(Signal.SELL, _ic_order.price)
                                _ic_pnl = ss['pm'].on_sell(_ic_order.price, _ic_order.quantity)
                                ss['trail_peak'] = 0.0
                                ss['partial_done'] = False
                                if not ss['pm'].has_position:
                                    capital_pool.release(sym, ss['executor'].cash)
                                    # Best-effort cancel of the native stop backstop.
                                    # If the native stop itself fired first (raced
                                    # this software exit), this call cancels an
                                    # already-filled order — harmless, logged at
                                    # info level by _cancel_native_stop.
                                    ss['executor'].sync_protective_stop(None)
                                    ss['native_stop_is_trailing'] = False
                                else:
                                    # Urgent market SELL only partially filled — a
                                    # residual position remains. The resting native
                                    # stop is still sized to the ORIGINAL (larger)
                                    # quantity, so re-sync it down to what's actually
                                    # held. Without this the backstop would try to
                                    # sell more than the position. Same resize the
                                    # partial-TP and strategy-SELL paths already do.
                                    _resync_native_stop(ss)
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
                                )
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
                            "rsi":    _dash_rsi,
                            "trend":  _dash_trend,
                            "state":  ss['sm'].state.value,
                            "reason": "trail_stop" if _ic_sl else "take_profit",
                        })
                        _render_dashboard("SELL", _dash_rsi, _dash_trend)
                        continue  # skip candle eval for this symbol this tick

                # Live mode: evaluate only when a new candle has closed
                candle, new_ts = _fetch_completed_candle(
                    live_exchange, ss['last_ts_ms'], _LIVE_TF, symbol=sym
                )
                if candle is None:
                    if sym == _active_symbol:
                        countdown = _candle_countdown(_LIVE_TF)
                        display.next_candle(price, tick, countdown)
                        tick_log.append({
                            "tick":   tick,
                            "time":   datetime.now().strftime("%H:%M:%S"),
                            "price":  price,
                            "signal": _dash_signal,
                            "rsi":    _dash_rsi,
                            "trend":  _dash_trend,
                            "state":  ss['sm'].state.value,
                            "reason": _dash_filter or _dash_block,
                        })
                        _render_dashboard(_dash_signal, _dash_rsi, _dash_trend)
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

            # ── Blocked-gate tracking (initialise per-candle) ─────────
            # Records the first gate (in priority order) that blocked a BUY.
            # Priority: trend → RSI → ADX → EMA_spread → MACD → regime
            #           → correlation → candle_watchdog → state_machine
            #           → risk_manager → capital_pool
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
            # closes; with no cache the gate fails open (same as before).
            if is_indicator and raw_signal == Signal.BUY and live_exchange is not None:
                try:
                    _raw_1d = live_exchange.fetch_ohlcv(sym, timeframe="1d", limit=30)
                    if _raw_1d:
                        ss['mtf_1d_closes'] = [float(r[4]) for r in _raw_1d[:-1]]
                except Exception as _mtf_exc:
                    logger.warning(
                        "MTF gate [%s]: daily fetch failed (%s) — %s",
                        sym, _mtf_exc,
                        "using cached closes" if ss['mtf_1d_closes']
                        else "no cache, gate skipped",
                    )
                if ss['mtf_1d_closes']:
                    _mtf_trend = _trend_fn(ss['mtf_1d_closes'])
                    if _mtf_trend == "BEARISH":
                        raw_signal = Signal.HOLD
                        if not _buy_block_gate:
                            _buy_block_gate = "regime"
                        print(f"  [{sym}] MTF gate: 1D trend BEARISH — BUY suppressed", flush=True)
                        logger.info("MTF gate [%s]: BUY suppressed — daily trend BEARISH", sym)

            # ── 2d. External signal gate ──────────────────────────────
            if raw_signal == Signal.BUY and ext_gate is not None:
                _ext_approved, _ext_reason = ext_gate.approve_buy()
                if not _ext_approved:
                    raw_signal = Signal.HOLD
                    if not _buy_block_gate:
                        _buy_block_gate = "regime"
                    print(f"  [{sym}] EXT gate: {_ext_reason}", flush=True)
                    logger.info("External signal gate blocked BUY [%s]: %s", sym, _ext_reason)

            # ── 2e. Regime gate ───────────────────────────────────────
            # Independent check: ADX ≥ threshold AND EMA spread ≥ MIN_EMA_SPREAD_PCT.
            # Runs after strategy evaluation so it can override BUY → HOLD when the
            # broader regime is degraded even if this specific candle passed strategy filters.
            if is_indicator and live_exchange is not None:
                _rg_adx_ok    = _adx_live is not None and _adx_live >= cfg.strategy.adx_threshold
                _rg_spread_ok = _spread >= cfg.strategy.min_ema_spread_pct * 100
                _rg_ok        = _rg_adx_ok and _rg_spread_ok

                if not _rg_ok:
                    _rg_parts = []
                    if not _rg_adx_ok:
                        _rg_parts.append(
                            f"ADX {_adx_live:.1f} < {cfg.strategy.adx_threshold:.0f}"
                            if _adx_live is not None else "ADX n/a"
                        )
                    if not _rg_spread_ok:
                        _rg_parts.append(
                            f"EMA spread {_spread:.3f}% < {cfg.strategy.min_ema_spread_pct * 100:.1f}%"
                        )
                    _rg_status_msg = "  ".join(_rg_parts)

                    if raw_signal == Signal.BUY:
                        raw_signal = Signal.HOLD
                        if not _buy_block_gate:
                            _buy_block_gate = "regime"
                        print(
                            f"  [{sym}] REGIME GATE: BUY overridden → HOLD"
                            f"  ({_rg_status_msg})",
                            flush=True,
                        )
                        logger.warning(
                            "REGIME GATE [%s]: BUY overridden → HOLD  %s", sym, _rg_status_msg
                        )
                    else:
                        print(
                            f"  [{sym}] REGIME GATE: degraded  ({_rg_status_msg})"
                            f"  — BUY would be blocked",
                            flush=True,
                        )
                        logger.info(
                            "REGIME GATE [%s]: degraded  %s  — no BUY to override",
                            sym, _rg_status_msg,
                        )
                else:
                    logger.info("REGIME GATE [%s]: OK  ADX=%.1f  spread=%.3f%%", sym, _adx_live, _spread)

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
                        break

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

            # ── 3. Warmup guard ───────────────────────────────────────
            if is_indicator and not ss['strategy'].is_warmed_up:
                if sym == _active_symbol:
                    display.warmup(tick, ss['strategy'].tick_count, ss['strategy']._warmup, price)
                continue

            rsi_val   = ss['strategy'].last_rsi   if is_indicator else None
            trend_val = ss['strategy'].last_trend if is_indicator else None

            # ── 4. State machine filter + tick ────────────────────────
            filtered_signal, filter_reason = ss['sm'].filter_signal(raw_signal)
            if raw_signal == Signal.BUY and filtered_signal != Signal.BUY and not _buy_block_gate:
                _buy_block_gate = "state_machine"
            ss['sm'].tick()

            # ── 5. Dynamic position sizing ────────────────────────────
            # executor.cash is already capped to its pool slot — no division needed.
            # Block BUY when the pool has no slot available for a new position.
            if filtered_signal == Signal.BUY and not capital_pool.can_open_position(sym):
                if not _buy_block_gate:
                    _buy_block_gate = "capital_pool"
                filtered_signal = Signal.HOLD
                logger.info(
                    "CapitalPool: BUY blocked for %s — pool exhausted (%d/%d slots used)",
                    sym, len(capital_pool.allocated_symbols), _max_conc,
                )
            _max_cash_for_sym = ss['executor'].cash
            if filtered_signal == Signal.SELL:
                trade_qty = ss['pm'].quantity
            else:
                trade_qty = cfg.calc_trade_qty(_max_cash_for_sym, price)
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
                max_affordable = (_max_cash_for_sym * 0.98) / price
                trade_qty = min(trade_qty, max_affordable)
                trade_qty = round(trade_qty, 6)

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
            if approval:
                order = ss['executor'].execute(final_signal, price, quantity=trade_qty)
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
                                if cfg.backtest.trail_stop_activation_pct <= 0
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
                            ss['executor'].sync_protective_stop(ss['native_stop_price'])
                        else:
                            pnl = ss['pm'].on_sell(order.price, order.quantity)
                            ss['trail_peak'] = 0.0
                            ss['partial_done'] = False
                            ss['atr_sl'] = 0.0
                            if not ss['pm'].has_position:
                                capital_pool.release(sym, ss['executor'].cash)
                                ss['executor'].sync_protective_stop(None)
                                ss['native_stop_is_trailing'] = False
                            else:
                                # Partial fill leaving a residual position (not
                                # currently reachable with strategy SELLs, which
                                # always close in full — defensive parity with
                                # the partial-TP path below).
                                _resync_native_stop(ss)

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
                        )
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

            # ── 10. Position summary ──────────────────────────────────
            display.position_line(
                quantity       = ss['pm'].quantity,
                symbol         = sym,
                avg_entry      = ss['pm'].avg_entry,
                unrealized_pnl = ss['pm'].unrealized_pnl(price),
                realized_pnl   = ss['pm'].realized_pnl,
                cash           = ss['executor'].cash,
            )

            # ── 11. Tick log + dashboard (active symbol only) ─────────
            if sym == _active_symbol:
                _dash_signal = final_signal.value
                _dash_rsi    = rsi_val
                _dash_trend  = trend_val
                _dash_filter = filter_reason
                _dash_block  = block_reason
                tick_log.append({
                    "tick":   tick,
                    "time":   datetime.now().strftime("%H:%M:%S"),
                    "price":  price,
                    "signal": final_signal.value,
                    "rsi":    rsi_val,
                    "trend":  trend_val,
                    "state":  ss['sm'].state.value,
                    "reason": filter_reason or block_reason,
                })
                _render_dashboard(final_signal.value, rsi_val, trend_val)

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