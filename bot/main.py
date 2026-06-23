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
import logging
import os
import signal as _signal_module
import time
from collections import deque
from datetime import datetime, timedelta, timezone as _tz

import ccxt as _ccxt
from dotenv import load_dotenv
load_dotenv()

# ── Logging setup ────────────────────────────────────────────────────────────
_log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)
_file_handler = logging.FileHandler(os.path.join(_log_dir, "trade_bot.log"))
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.WARNING)
_root_logger = logging.getLogger()
_root_logger.handlers.clear()
_root_logger.setLevel(logging.INFO)
_root_logger.addHandler(_console_handler)
_root_logger.addHandler(_file_handler)

logger = logging.getLogger(__name__)

# ── Imports ───────────────────────────────────────────────────────────────────
from config import cfg

from bot.data.price_feed import SimulatedFeed, CcxtFeed
from bot.data.historical_feed import Candle as _Candle
from bot.strategy.threshold_strategy import ThresholdStrategy, Signal
from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig
from bot.execution.executor import PaperExecutor, OrderStatus, OrderSide
from bot.execution.live_executor import LiveExecutor
from bot.risk.risk_manager import RiskManager, RiskConfig
from bot.state.trade_state import TradingStateMachine
from bot.portfolio.position_manager import PositionManager
from bot.indicators.indicators import ema as _ema_fn, trend as _trend_fn, atr as _atr_fn
from bot.ai.ai_engine import AIEngine, merge_signals
from bot import display
from bot.dashboard import renderer as _dashboard
from bot.signals.external_signals import ExternalSignalGate, ExternalSignalsConfig as _ExtSigsCfg
from bot.alerts.telegram import TelegramAlerter
from bot.data.trade_log import TradeLog

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


def _warmup_strategy(strategy, exchange, timeframe: str = None) -> "int | None":
    """
    Fetch completed candles and warm up the strategy indicators.
    Returns the timestamp_ms of the last candle fed, or None on failure.
    timeframe: ccxt timeframe string (e.g. '1h', '4h'). Defaults to cfg.backtest.timeframe.
    """
    if timeframe is None:
        timeframe = cfg.backtest.timeframe

    print(f"\n  Fetching historical {timeframe} candles for warmup …", flush=True)
    try:
        _WARMUP_CANDLES = max(strategy._warmup + 100, 150)
        raw = exchange.fetch_ohlcv(cfg.exchange.symbol, timeframe=timeframe, limit=_WARMUP_CANDLES + 1)
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
) -> "tuple[_Candle | None, int | None]":
    """
    Fetch the most recently completed candle.
    Returns (Candle, ts_ms) when a new candle is available, else (None, None).
    raw[-1] is still forming; raw[-2] is the last fully closed candle.
    """
    try:
        raw = exchange.fetch_ohlcv(cfg.exchange.symbol, timeframe=timeframe, limit=2)
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
            max_ema_spread_pct       = cfg.strategy.max_ema_spread_pct,
            rsi_filter_enabled       = cfg.strategy.rsi_filter_enabled,
            macd_enabled             = cfg.strategy.macd_enabled,
            regime_ema_period        = cfg.strategy.regime_ema_period,
            regime_ema_slope_filter  = cfg.strategy.regime_ema_slope_filter,
            volume_k                 = cfg.strategy.volume_k,
        ))
    return ThresholdStrategy(
        buy_threshold  = cfg.strategy.buy_threshold,
        sell_threshold = cfg.strategy.sell_threshold,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run():
    cfg.log_startup()

    feed     = build_feed()
    strategy = build_strategy()
    if cfg.exchange.live_trading:
        executor = LiveExecutor(
            exchange_id   = cfg.exchange.exchange,
            symbol        = cfg.exchange.symbol,
            api_key       = cfg.exchange.api_key,
            api_secret    = cfg.exchange.api_secret,
            starting_cash = cfg.portfolio.starting_cash,
            dry_run       = cfg.exchange.dry_run,
            order_type    = cfg.exchange.order_type,
        )
        mode_str = "[DRY RUN] " if cfg.exchange.dry_run else ""
        print(
            f"\n  {mode_str}LIVE TRADING ENABLED"
            f" — real orders will be placed on"
            f" {cfg.exchange.exchange.upper()}\n",
            flush=True,
        )
    else:
        executor = PaperExecutor(
            symbol        = cfg.exchange.symbol,
            starting_cash = cfg.portfolio.starting_cash,
        )
    risk = RiskManager(RiskConfig(
        max_position_pct     = cfg.risk.max_position_pct,
        daily_loss_limit_pct = cfg.risk.daily_loss_limit_pct,
        max_drawdown_pct     = cfg.risk.max_drawdown_pct,
        max_trades_per_day   = cfg.risk.max_trades_per_day,
    ))
    state_machine    = TradingStateMachine(cooldown_ticks=cfg.risk.cooldown_ticks)
    position_manager = PositionManager()

    # ── Restart recovery ──────────────────────────────────────────────────────
    if cfg.exchange.live_trading and executor.position > 1e-9:
        position_manager.seed(
            quantity     = executor.position,
            avg_entry    = executor.avg_entry,
            realized_pnl = executor.portfolio.realized_pnl,
        )
        state_machine.recover_long(executor.avg_entry)
        logger.warning(
            "Recovered position seeded: qty=%.6f entry=%.2f — state machine set to LONG",
            executor.position, executor.avg_entry,
        )
        print(
            f"  POSITION RECOVERED: {executor.position:.6f}"
            f" {cfg.exchange.symbol.split('/')[0]}"
            f" @ ${executor.avg_entry:,.2f}"
            f" — state machine set to LONG",
            flush=True,
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

    is_indicator = isinstance(strategy, IndicatorStrategy)

    # ── Derive live candle timeframe from CANDLE_MINUTES ─────────────────────
    # This is the timeframe used for ALL live candle operations:
    # warmup fetch, candle polling, and countdown display.
    # BACKTEST_TIMEFRAME is only used for backtesting, not live trading.
    _LIVE_TF = _minutes_to_timeframe(cfg.exchange.candle_minutes)

    # ── Historical warmup (live + indicator mode only) ────────────────────────
    live_exchange = None
    last_candle_ts_ms: "int | None" = None

    if is_indicator and cfg.exchange.feed_mode == "live":
        live_exchange     = _build_exchange()
        last_candle_ts_ms = _warmup_strategy(strategy, live_exchange, _LIVE_TF)
        try:
            _raw_1d = live_exchange.fetch_ohlcv(cfg.exchange.symbol, timeframe="1d", limit=30)
            _mtf_1d_closes = [float(r[4]) for r in _raw_1d[:-1]]
            print(f"  MTF: loaded {len(_mtf_1d_closes)} daily candles for regime check.", flush=True)
        except Exception as _mtf_exc:
            print(f"  MTF: daily candle fetch failed ({_mtf_exc}) — MTF disabled this session.", flush=True)

    tick        = 0
    tick_log:   deque[dict] = deque(maxlen=200)
    _consecutive_errors = 0
    candle_log: deque[dict] = deque(maxlen=50)
    _last_candle_time = time.time()

    # Trailing stop and partial TP state — reset on each new trade
    _trail_peak:      float = 0.0
    _partial_tp_done: bool  = False
    _atr_sl_price:    float = 0.0
    _atr_tp_price:    float = 0.0
    # MTF 1D closes for regime check — loaded once at startup
    _mtf_1d_closes: list[float] = []

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

    while _running:
        tick += 1

        # ── 1. Advance state machine ──────────────────────────────────
        state_machine.tick()

        # ── 2. Fetch live price ───────────────────────────────────────
        try:
            price = feed.get_price()
            _consecutive_errors = 0
        except Exception as exc:
            _consecutive_errors += 1
            if _consecutive_errors >= 5:
                alerter.error(f"Price feed down {_consecutive_errors} consecutive ticks — {exc}")
            print(f"  TICK {tick:04d} | price fetch failed: {exc}")
            time.sleep(cfg.exchange.loop_interval)
            continue

        # ── 2b. Candle watchdog (live mode) ──────────────────────────
        if cfg.exchange.feed_mode == "live":
            _candle_stale_s = cfg.exchange.candle_minutes * 60 * 2
            if time.time() - _last_candle_time > _candle_stale_s:
                alerter.error(
                    f"Candle watchdog: no new {cfg.exchange.candle_minutes}min candle "
                    f"for {int((time.time()-_last_candle_time)/60)} minutes — feed may be stale"
                )
                _last_candle_time = time.time()  # reset so we don't spam every tick

        # ── 2c. Position drift reconciliation (every 60 ticks, live only) ──
        if cfg.exchange.live_trading and tick % 60 == 0:
            try:
                balance = executor._exchange.fetch_balance()
                base = cfg.exchange.symbol.split("/")[0]
                exchange_pos = float(balance.get("free", {}).get(base, 0))
                bot_pos = executor.position
                drift = abs(exchange_pos - bot_pos)
                if drift > 0.000010:  # 10 satoshi threshold
                    logger.warning(
                        "POSITION DRIFT: exchange=%.6f bot=%.6f drift=%.6f %s",
                        exchange_pos, bot_pos, drift, base,
                    )
                    alerter.error(
                        f"Position drift detected: exchange={exchange_pos:.6f} "
                        f"bot={bot_pos:.6f} {base} — check logs/live_state.json"
                    )
            except Exception as _drift_exc:
                logger.warning("Position drift check failed: %s", _drift_exc)

        # ── 3. Strategy signal ────────────────────────────────────────
        if is_indicator:
            if live_exchange is not None:
                # ── Intra-candle SL/TP + Trailing Stop + Partial TP ──────
                if position_manager.has_position and position_manager.avg_entry > 0:
                    _ic_entry = position_manager.avg_entry
                    # Update trailing peak on every tick
                    _trail_peak = max(_trail_peak, price)
                    _trail_sl_level = (
                        _atr_sl_price if _atr_sl_price > 0
                        else (
                            _trail_peak * (1 - cfg.backtest.stop_loss_pct)
                            if _trail_peak > 0 and cfg.backtest.stop_loss_pct > 0 else 0.0
                        )
                    )

                    # Partial TP — sell cfg.backtest.partial_tp_size at partial_tp_pct gain
                    _partial_tp_level = (
                        _ic_entry * (1 + cfg.backtest.partial_tp_pct)
                        if cfg.backtest.partial_tp_pct > 0 else None
                    )
                    if (
                        _partial_tp_level is not None
                        and price >= _partial_tp_level
                        and not _partial_tp_done
                        and executor.position > 0
                    ):
                        _p_qty = round(executor.position * cfg.backtest.partial_tp_size, 6)
                        if _p_qty > 0:
                            _p_approval = risk.evaluate(Signal.SELL, price, executor.portfolio, _p_qty)
                            if _p_approval.approved:
                                _p_order = executor.execute(Signal.SELL, price, quantity=_p_qty)
                                if _p_order and _p_order.status == OrderStatus.FILLED:
                                    risk.record_fill()
                                    state_machine.on_fill(Signal.SELL, _p_order.price)
                                    _p_pnl = position_manager.on_sell(_p_order.price, _p_order.quantity)
                                    _partial_tp_done = True
                                    state_machine.recover_long(_p_order.price)
                                    print(f"           📊 PARTIAL TP:  {_p_qty:.6f} @ {price:,.2f}  PnL={_p_pnl:+.2f}", flush=True)
                                    logger.warning("PARTIAL TP: sold %.6f @ %.2f  pnl=%.2f", _p_qty, price, _p_pnl)
                                    trade_log.log_fill(
                                        side          = "SELL",
                                        symbol        = cfg.exchange.symbol,
                                        quantity      = _p_qty,
                                        price         = _p_order.price,
                                        pnl           = _p_pnl,
                                        exchange      = cfg.exchange.exchange,
                                        signal_reason = "partial_tp",
                                    )
                                    alerter.fill(
                                        side        = "SELL",
                                        symbol      = cfg.exchange.symbol,
                                        quantity    = _p_qty,
                                        price       = _p_order.price,
                                        total_value = _p_order.total_value,
                                        pnl         = _p_pnl,
                                        exchange    = cfg.exchange.exchange,
                                    )

                    _ic_sl = _trail_sl_level > 0 and price <= _trail_sl_level
                    _ic_tp = (
                        price >= _atr_tp_price if _atr_tp_price > 0
                        else (cfg.backtest.take_profit_pct > 0
                              and price >= _ic_entry * (1 + cfg.backtest.take_profit_pct))
                    )
                    if _ic_sl or _ic_tp:
                        if _ic_sl:
                            logger.warning(
                                "TRAIL STOP triggered: price=%.2f peak=%.2f trail_sl=%.2f",
                                price, _trail_peak, _trail_sl_level,
                            )
                            print(f"           🛑 TRAIL STOP  price={price:,.2f}  peak={_trail_peak:,.2f}  sl={_trail_sl_level:,.2f}", flush=True)
                        else:
                            logger.warning(
                                "TAKE PROFIT triggered: price=%.2f entry=%.2f tp=%.1f%%",
                                price, _ic_entry, cfg.backtest.take_profit_pct * 100,
                            )
                            print(f"           ✅ TAKE PROFIT  price={price:,.2f}  entry={_ic_entry:,.2f}", flush=True)
                        _ic_qty      = executor.position
                        _ic_approval = risk.evaluate(Signal.SELL, price, executor.portfolio, _ic_qty)
                        if _ic_approval.approved:
                            _ic_order = executor.execute(Signal.SELL, price, quantity=_ic_qty)
                            if _ic_order and _ic_order.status == OrderStatus.FILLED:
                                risk.record_fill()
                                state_machine.on_fill(Signal.SELL, _ic_order.price)
                                _ic_pnl = position_manager.on_sell(_ic_order.price, _ic_order.quantity)
                                _trail_peak = 0.0
                                _partial_tp_done = False
                                _ic_reason = "trail_stop" if _ic_sl else "take_profit"
                                display.fill(
                                    _ic_order.side.value, _ic_order.quantity,
                                    cfg.exchange.symbol, _ic_order.price,
                                    _ic_order.total_value, _ic_pnl,
                                )
                                trade_log.log_fill(
                                    side          = "SELL",
                                    symbol        = cfg.exchange.symbol,
                                    quantity      = _ic_order.quantity,
                                    price         = _ic_order.price,
                                    pnl           = _ic_pnl,
                                    exchange      = cfg.exchange.exchange,
                                    signal_reason = _ic_reason,
                                )
                                alerter.fill(
                                    side        = "SELL",
                                    symbol      = cfg.exchange.symbol,
                                    quantity    = _ic_order.quantity,
                                    price       = _ic_order.price,
                                    total_value = _ic_order.total_value,
                                    pnl         = _ic_pnl,
                                    exchange    = cfg.exchange.exchange,
                                )
                        else:
                            logger.warning(
                                "SL/TP SELL blocked by risk gate: %s", _ic_approval.message,
                            )
                        display.position_line(
                            quantity       = position_manager.quantity,
                            symbol         = cfg.exchange.symbol,
                            avg_entry      = position_manager.avg_entry,
                            unrealized_pnl = position_manager.unrealized_pnl(price),
                            realized_pnl   = position_manager.realized_pnl,
                            cash           = executor.cash,
                        )
                        tick_log.append({
                            "tick":   tick,
                            "time":   datetime.now().strftime("%H:%M:%S"),
                            "price":  price,
                            "signal": "SELL",
                            "rsi":    _dash_rsi,
                            "trend":  _dash_trend,
                            "state":  state_machine.state.value,
                            "reason": "trail_stop" if _ic_sl else "take_profit",
                        })
                        _render_dashboard("SELL", _dash_rsi, _dash_trend)
                        time.sleep(cfg.exchange.loop_interval)
                        continue

                # Live mode: evaluate only when a new candle has closed
                # Uses _LIVE_TF (derived from CANDLE_MINUTES) — NOT backtest timeframe
                candle, new_ts = _fetch_completed_candle(
                    live_exchange, last_candle_ts_ms, _LIVE_TF
                )
                if candle is None:
                    countdown = _candle_countdown(_LIVE_TF)
                    display.next_candle(price, tick, countdown)
                    tick_log.append({
                        "tick":   tick,
                        "time":   datetime.now().strftime("%H:%M:%S"),
                        "price":  price,
                        "signal": _dash_signal,
                        "rsi":    _dash_rsi,
                        "trend":  _dash_trend,
                        "state":  state_machine.state.value,
                        "reason": _dash_filter or _dash_block,
                    })
                    _render_dashboard(_dash_signal, _dash_rsi, _dash_trend)
                    time.sleep(cfg.exchange.loop_interval)
                    continue
                last_candle_ts_ms = new_ts
                _last_candle_time = time.time()  # watchdog: new candle received
                raw_signal = strategy.evaluate(candle)
            else:
                # Simulated mode: flat fake candle per tick
                fake_candle = _Candle(
                    timestamp=datetime.now(_tz.utc),
                    open=price, high=price, low=price, close=price, volume=0.0,
                )
                raw_signal = strategy.evaluate(fake_candle)
        else:
            raw_signal = strategy.evaluate(price)

        # ── 3b. Candle-close diagnostic log ──────────────────────────
        if is_indicator and live_exchange is not None:
            _adx_live  = strategy.last_adx
            _rsi_live  = strategy.last_rsi
            _trnd_live = strategy.last_trend or "UNKNOWN"
            _cl = list(strategy._closes)
            _ef = _ema_fn(_cl, strategy.config.fast_ema_period)
            _es = _ema_fn(_cl, strategy.config.slow_ema_period)
            _spread = abs(_ef - _es) / _es * 100 if (_ef and _es and _es > 0) else 0.0
            _sig_str = raw_signal.value if hasattr(raw_signal, 'value') else str(raw_signal)

            _reason = ""
            if _sig_str == "HOLD":
                if _adx_live is not None and _adx_live < strategy.config.adx_threshold:
                    _reason = f"ADX {_adx_live:.1f} < {strategy.config.adx_threshold}"
                elif _trnd_live == "NEUTRAL":
                    _reason = "trend NEUTRAL"
                elif _spread > strategy.config.max_ema_spread_pct * 100 and strategy.config.max_ema_spread_pct > 0:
                    _reason = f"EMA spread {_spread:.3f}% > {strategy.config.max_ema_spread_pct*100:.1f}%"
                elif _rsi_live is not None:
                    _reason = f"RSI {_rsi_live:.1f} filtered"
                else:
                    _reason = "warmup"

            _rsi_str = f"  RSI={_rsi_live:.1f}" if _rsi_live is not None else "  RSI=n/a"
            _adx_str = f"  ADX={_adx_live:.1f}" if _adx_live is not None else "  ADX=n/a"
            print(
                f"  candle {candle.timestamp.strftime('%Y-%m-%d %H:%M')} UTC"
                f"  close={price:,.2f}"
                + _rsi_str
                + _adx_str,
                flush=True
            )
            print(
                f"  trend={_trnd_live}"
                f"  EMA_spread={_spread:.3f}%"
                f"  signal={_sig_str}"
                + (f"  [{_reason}]" if _reason else ""),
                flush=True
            )

            _live_log = os.path.join(_log_dir, "live_signals.csv")
            _write_header = not os.path.exists(_live_log)
            with open(_live_log, "a", newline="") as _f:
                _w = csv.writer(_f)
                if _write_header:
                    _w.writerow([
                        "timestamp", "close", "rsi", "adx",
                        "trend", "ema_spread_pct", "signal", "reason"
                    ])
                _w.writerow([
                    candle.timestamp.strftime("%Y-%m-%d %H:%M"),
                    round(price, 2),
                    round(_rsi_live, 2) if _rsi_live is not None else "",
                    round(_adx_live, 2) if _adx_live is not None else "",
                    _trnd_live,
                    round(_spread, 4),
                    _sig_str,
                    _reason,
                ])

        # ── 3d. MTF gate: block BUY when daily trend is BEARISH ─────────
        if is_indicator and raw_signal == Signal.BUY and _mtf_1d_closes:
            _mtf_trend = _trend_fn(_mtf_1d_closes)
            if _mtf_trend == "BEARISH":
                raw_signal = Signal.HOLD
                print(f"  MTF gate: 1D trend BEARISH — BUY suppressed", flush=True)
                logger.info("MTF gate: BUY suppressed — daily trend BEARISH")
            # Refresh 1D closes each candle close
            try:
                _raw_1d_refresh = live_exchange.fetch_ohlcv(cfg.exchange.symbol, timeframe="1d", limit=30) if live_exchange else None
                if _raw_1d_refresh:
                    _mtf_1d_closes = [float(r[4]) for r in _raw_1d_refresh[:-1]]
            except Exception:
                pass  # keep stale 1D data if refresh fails

        # ── 3e. External signal gate (live only) ──────────────────────
        if raw_signal == Signal.BUY and ext_gate is not None:
            _ext_approved, _ext_reason = ext_gate.approve_buy()
            if not _ext_approved:
                raw_signal = Signal.HOLD
                print(f"  EXT gate: {_ext_reason}", flush=True)
                logger.info("External signal gate blocked BUY: %s", _ext_reason)

        # ── 4. Warmup guard ───────────────────────────────────────────
        if is_indicator and not strategy.is_warmed_up:
            display.warmup(tick, strategy.tick_count, strategy._warmup, price)
            time.sleep(cfg.exchange.loop_interval)
            continue

        rsi_val   = strategy.last_rsi   if is_indicator else None
        trend_val = strategy.last_trend if is_indicator else None

        # ── 5. Position-aware filter + deduplication ──────────────────
        filtered_signal, filter_reason = state_machine.filter_signal(raw_signal)

        # ── 6. Dynamic position sizing ─────────────────────────────────
        if filtered_signal == Signal.SELL:
            trade_qty = executor.position
        else:
            trade_qty = cfg.calc_trade_qty(executor.cash, price)
            # Safety: never use more than 98% of available cash.
            # Prevents "Insufficient funds" from rounding at exchange.
            max_affordable = (executor.cash * 0.98) / price
            trade_qty = min(trade_qty, max_affordable)
            trade_qty = round(trade_qty, 6)

        # ── 7. AI advisory (optional) ──────────────────────────────────
        advice       = None
        final_signal = filtered_signal
        if ai and ai.enabled and filtered_signal != Signal.HOLD:
            advice = ai.advise(
                price           = price,
                rsi             = rsi_val,
                trend           = trend_val,
                strategy_signal = filtered_signal,
                recent_prices   = list(strategy._closes) if is_indicator else [price],
                portfolio       = executor.portfolio,
                symbol          = cfg.exchange.symbol,
            )
            final_signal = merge_signals(filtered_signal, advice)

        # ── 8. Risk gate ───────────────────────────────────────────────
        approval     = risk.evaluate(final_signal, price, executor.portfolio, trade_qty)
        block_reason = "" if approval else approval.message

        # ── 8b. Candle-close structured log ───────────────────────────
        if is_indicator and live_exchange is not None:
            _rsi_log = f"{_rsi_live:.1f}" if _rsi_live is not None else "n/a"
            _adx_log = f"{_adx_live:.1f}" if _adx_live is not None else "n/a"
            _action  = (
                final_signal.value
                if approval else
                f"BLOCKED[{approval.block_reason.value if approval.block_reason else '?'}]"
            )
            logger.info(
                "CANDLE %s UTC | close=%.2f RSI=%s ADX=%s trend=%s spread=%.3f%% signal=%s -> %s",
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
                "ts":     candle.timestamp.strftime("%Y-%m-%d %H:%M"),
                "close":  price,
                "rsi":    round(_rsi_live, 1) if _rsi_live is not None else None,
                "adx":    round(_adx_live, 1) if _adx_live is not None else None,
                "trend":  _trnd_live,
                "spread": round(_spread, 3),
                "signal": _sig_str,
                "action": _action,
                "reason": _reason,
            })

        # ── 9. Display tick ────────────────────────────────────────────
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
            state      = state_machine.state.value,
            cooldown   = state_machine.cooldown_remaining,
            last_trade = state_machine.last_trade_label,
        )

        if advice:
            vetoed = final_signal != filtered_signal
            display.ai_advice(
                advice.signal.value, advice.confidence,
                advice.reasoning, advice.latency_ms, vetoed,
            )

        # ── 10. Execute ────────────────────────────────────────────────
        if approval:
            order = executor.execute(final_signal, price, quantity=trade_qty)
            if order:
                if order.status == OrderStatus.FILLED:
                    risk.record_fill()
                    state_machine.on_fill(final_signal, order.price)

                    pnl = None
                    if order.side == OrderSide.BUY:
                        position_manager.on_buy(order.price, order.quantity)
                        _trail_peak = order.price  # seed trailing peak at entry
                        _partial_tp_done = False
                        _atr_sl_price = 0.0
                        _atr_tp_price = 0.0
                        if is_indicator:
                            _atr_val = _atr_fn(
                                list(strategy._highs),
                                list(strategy._lows),
                                list(strategy._closes),
                                cfg.strategy.atr_period,
                            )
                            if _atr_val is None or _atr_val <= 0 or cfg.strategy.atr_sl_mult <= 0:
                                _atr_sl_price = 0.0
                                logger.info("ATR SL disabled or unavailable — using fixed SL/TP")
                            else:
                                _atr_sl_price = order.price - _atr_val * cfg.strategy.atr_sl_mult
                                logger.info(
                                    "ATR SL/TP: entry=%.2f atr=%.2f sl=%.2f mult=%.1f",
                                    order.price, _atr_val, _atr_sl_price, cfg.strategy.atr_sl_mult,
                                )
                    else:
                        pnl = position_manager.on_sell(order.price, order.quantity)
                        _trail_peak = 0.0
                        _partial_tp_done = False
                        _atr_sl_price = 0.0
                        _atr_tp_price = 0.0

                    display.fill(
                        order.side.value, order.quantity,
                        cfg.exchange.symbol, order.price, order.total_value, pnl,
                    )
                    trade_log.log_fill(
                        side          = order.side.value,
                        symbol        = cfg.exchange.symbol,
                        quantity      = order.quantity,
                        price         = order.price,
                        pnl           = pnl,
                        exchange      = cfg.exchange.exchange,
                        signal_reason = filter_reason or raw_signal.value,
                    )
                    alerter.fill(
                        side        = order.side.value,
                        symbol      = cfg.exchange.symbol,
                        quantity    = order.quantity,
                        price       = order.price,
                        total_value = order.total_value,
                        pnl         = pnl,
                        exchange    = cfg.exchange.exchange,
                    )
                else:
                    display.reject(order.reject_reason or "")

        # ── 11. Position summary ───────────────────────────────────────
        display.position_line(
            quantity       = position_manager.quantity,
            symbol         = cfg.exchange.symbol,
            avg_entry      = position_manager.avg_entry,
            unrealized_pnl = position_manager.unrealized_pnl(price),
            realized_pnl   = position_manager.realized_pnl,
            cash           = executor.cash,
        )

        # ── 12. Tick log + dashboard ───────────────────────────────────
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
            "state":  state_machine.state.value,
            "reason": filter_reason or block_reason,
        })
        _render_dashboard(final_signal.value, rsi_val, trend_val)

        time.sleep(cfg.exchange.loop_interval)
        _now_utc = datetime.now(_tz.utc)
        if _now_utc.hour == 0 and _now_utc.minute == 0:
            alerter.daily_pnl(
                symbol       = cfg.exchange.symbol,
                realized_pnl = position_manager.realized_pnl,
                total_value  = executor.portfolio.total_value(price),
                trade_count  = risk._fills_today,
            )

    display.stopped(
        ticks        = tick,
        fills        = len(executor.filled_orders()),
        rejects      = len(executor.rejected_orders()),
        pos          = position_manager.quantity,
        cash         = executor.cash,
        realized_pnl = position_manager.realized_pnl,
    )


if __name__ == "__main__":
    run()