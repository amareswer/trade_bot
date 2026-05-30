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
import logging
import os
import signal as _signal_module
import time
from collections import deque
from datetime import datetime, timezone as _tz

from dotenv import load_dotenv
load_dotenv()

# ── Persistent file logging (INFO level) ─────────────────────────────────────
_log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)
_file_handler = logging.FileHandler(os.path.join(_log_dir, "trade_bot.log"))
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.WARNING, handlers=[logging.StreamHandler()])
logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger(__name__)

# ── Imports ───────────────────────────────────────────────────────────────────
from config import cfg

from bot.data.price_feed import SimulatedFeed, CcxtFeed
from bot.data.historical_feed import Candle as _Candle
from bot.strategy.threshold_strategy import ThresholdStrategy, Signal
from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig
from bot.execution.executor import PaperExecutor, OrderStatus, OrderSide
from bot.risk.risk_manager import RiskManager, RiskConfig
from bot.state.trade_state import TradingStateMachine
from bot.portfolio.position_manager import PositionManager
from bot.ai.ai_engine import AIEngine, merge_signals
from bot import display
from bot.dashboard import renderer as _dashboard

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
# Candle aggregator
# ---------------------------------------------------------------------------

class CandleAggregator:
    """Accumulates live price ticks into OHLCV candles over a fixed time window.

    Activated only in live + indicator mode so that ADX/RSI receive proper
    high/low data instead of flat tick-by-tick fake candles.
    """

    def __init__(self, period_minutes: int) -> None:
        self._period_s  = period_minutes * 60
        self._period_m  = period_minutes
        self._start_ts: float | None = None
        self._open:  float = 0.0
        self._high:  float = 0.0
        self._low:   float = 0.0
        self._close: float = 0.0
        self._ticks: int   = 0

    def add_tick(self, price: float) -> "_Candle | None":
        """Feed one price tick. Returns a complete Candle when the period elapses, else None."""
        now = time.time()
        if self._start_ts is None:
            self._start_ts = now
            self._open = self._high = self._low = price

        self._high  = max(self._high, price)
        self._low   = min(self._low,  price)
        self._close = price
        self._ticks += 1

        if now - self._start_ts >= self._period_s:
            candle = _Candle(
                timestamp = datetime.now(_tz.utc),
                open      = self._open,
                high      = self._high,
                low       = self._low,
                close     = self._close,
                volume    = float(self._ticks),
            )
            # Reset for the next candle, carrying the current tick as the opening
            self._start_ts = now
            self._open = self._high = self._low = price
            self._close = price
            self._ticks = 1
            return candle
        return None

    @property
    def elapsed_minutes(self) -> int:
        if self._start_ts is None:
            return 0
        return int((time.time() - self._start_ts) / 60)

    @property
    def period_minutes(self) -> int:
        return self._period_m


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
            rsi_period      = cfg.strategy.rsi_period,
            rsi_oversold    = cfg.strategy.rsi_oversold,
            rsi_overbought  = cfg.strategy.rsi_overbought,
            fast_ema_period = cfg.strategy.fast_ema_period,
            slow_ema_period = cfg.strategy.slow_ema_period,
            adx_period      = cfg.strategy.adx_period,
            adx_threshold   = cfg.strategy.adx_threshold,
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
    ai = AIEngine(
        model          = cfg.ai.model,
        min_confidence = cfg.ai.min_confidence,
        timeout_s      = cfg.ai.timeout_s,
    ) if cfg.ai.enabled else None

    display.header(
        cfg.exchange.exchange,
        cfg.exchange.symbol,
        cfg.portfolio.starting_cash,
        cfg.strategy.mode,
    )
    if cfg.dashboard.enabled:
        print(f"  Dashboard → file://{_DASHBOARD_PATH}\n")

    is_indicator = isinstance(strategy, IndicatorStrategy)

    # Candle aggregator — live indicator mode only.
    # Simulated mode keeps fake flat candles (building a 4h window in real-time
    # during simulation is impractical; use FEED_MODE=simulated for quick testing).
    candle_agg: CandleAggregator | None = (
        CandleAggregator(cfg.exchange.candle_minutes)
        if is_indicator and cfg.exchange.feed_mode == "live"
        else None
    )
    if candle_agg:
        print(f"  Candle aggregator: {candle_agg.period_minutes}min windows  "
              f"(~{candle_agg.period_minutes * 60 // cfg.exchange.loop_interval} ticks/candle)\n")

    tick     = 0
    tick_log: deque[dict] = deque(maxlen=200)

    while _running:
        tick += 1

        # ── 1. Advance state machine ─────────────────────────────────
        state_machine.tick()

        # ── 2. Fetch price ───────────────────────────────────────────
        try:
            price = feed.get_price()
        except Exception as exc:
            print(f"  TICK {tick:04d} | price fetch failed: {exc}")
            time.sleep(cfg.exchange.loop_interval)
            continue

        # ── 3. Strategy signal ───────────────────────────────────────
        if is_indicator:
            if candle_agg is not None:
                # Live mode: accumulate ticks until a full candle is ready
                candle = candle_agg.add_tick(price)
                if candle is None:
                    display.building_candle(
                        candle_agg.elapsed_minutes,
                        candle_agg.period_minutes,
                        price,
                        tick,
                    )
                    time.sleep(cfg.exchange.loop_interval)
                    continue
                raw_signal = strategy.evaluate(candle)
            else:
                # Simulated mode: flat fake candle (high/low/close == price)
                _tick_candle = _Candle(
                    timestamp=datetime.now(_tz.utc),
                    open=price, high=price, low=price, close=price, volume=0.0,
                )
                raw_signal = strategy.evaluate(_tick_candle)
        else:
            raw_signal = strategy.evaluate(price)

        # ── 4. Warmup guard ──────────────────────────────────────────
        if is_indicator and not strategy.is_warmed_up:
            display.warmup(tick, strategy.tick_count, strategy._warmup, price)
            time.sleep(cfg.exchange.loop_interval)
            continue

        rsi_val   = strategy.last_rsi   if is_indicator else None
        trend_val = strategy.last_trend if is_indicator else None

        # ── 5. Position-aware filter + deduplication ─────────────────
        filtered_signal, filter_reason = state_machine.filter_signal(raw_signal)

        # ── 6. Dynamic position sizing ────────────────────────────────
        # BUY = % of cash; SELL = close full position (AI can't create new SELLs)
        if filtered_signal == Signal.SELL:
            trade_qty = executor.position
        else:
            trade_qty = cfg.calc_trade_qty(executor.cash, price)

        # ── 7. AI advisory (optional, advisory only) ──────────────────
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

        # ── 8. Risk gate ─────────────────────────────────────────────
        approval     = risk.evaluate(final_signal, price, executor.portfolio, trade_qty)
        block_reason = "" if approval else approval.message

        # ── 9. Display tick ──────────────────────────────────────────
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

        # ── 10. Execute ───────────────────────────────────────────────
        if approval:
            order = executor.execute(final_signal, price, quantity=trade_qty)
            if order:
                if order.status == OrderStatus.FILLED:
                    risk.record_fill()
                    state_machine.on_fill(final_signal, order.price)

                    pnl = None
                    if order.side == OrderSide.BUY:
                        position_manager.on_buy(order.price, order.quantity)
                    else:
                        pnl = position_manager.on_sell(order.price, order.quantity)

                    display.fill(
                        order.side.value, order.quantity,
                        cfg.exchange.symbol, order.price, order.total_value, pnl,
                    )
                else:
                    display.reject(order.reject_reason or "")

        # ── 11. Position summary ──────────────────────────────────────
        display.position_line(
            quantity       = position_manager.quantity,
            symbol         = cfg.exchange.symbol,
            avg_entry      = position_manager.avg_entry,
            unrealized_pnl = position_manager.unrealized_pnl(price),
            realized_pnl   = position_manager.realized_pnl,
            cash           = executor.cash,
        )

        # ── 12. Tick log + dashboard ──────────────────────────────────
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

        if cfg.dashboard.enabled:
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
            _dashboard.write(
                path           = _DASHBOARD_PATH,
                exchange       = cfg.exchange.exchange,
                symbol         = cfg.exchange.symbol,
                strategy       = cfg.strategy.mode,
                tick           = tick,
                price          = price,
                signal         = final_signal.value,
                rsi            = rsi_val,
                trend          = trend_val,
                state          = state_machine.state.value,
                cooldown       = state_machine.cooldown_remaining,
                last_trade     = state_machine.last_trade_label,
                cash           = executor.cash,
                position       = position_manager.quantity,
                avg_entry      = position_manager.avg_entry,
                unrealized_pnl = position_manager.unrealized_pnl(price),
                realized_pnl   = position_manager.realized_pnl,
                total_value    = executor.portfolio.total_value(price),
                fills          = fills_data,
                tick_log       = list(tick_log),
                refresh_s      = cfg.dashboard.refresh_s,
            )

        time.sleep(cfg.exchange.loop_interval)

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
