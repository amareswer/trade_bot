"""
Backtest engine.

Runs the full strategy pipeline on historical candles — same layers as main.py,
no live network calls, no real money.

Pipeline per candle:
  candle.close
    → IndicatorStrategy.evaluate()
    → TradingStateMachine.filter_signal()
    → RiskManager.evaluate()
    → PaperExecutor.execute()
    → PositionManager.on_buy / on_sell()
    → equity recorded
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from bot.data.historical_feed import Candle
from bot.strategy.threshold_strategy import ThresholdStrategy, Signal
from bot.strategy.indicator_strategy import IndicatorStrategy, IndicatorConfig
from bot.execution.executor import PaperExecutor, OrderStatus, OrderSide
from bot.indicators.indicators import ema as _ema, trend as _trend
from bot.risk.risk_manager import RiskManager, RiskConfig
from bot.state.trade_state import TradingStateMachine
from bot.portfolio.position_manager import PositionManager

logger = logging.getLogger(__name__)


@dataclass
class FillRecord:
    candle_index: int
    timestamp:    str
    side:         str
    price:        float
    quantity:     float
    total_value:  float
    pnl:          float | None   # None for BUY; realized PnL for SELL
    fee:          float
    reason:       str = "strategy"   # "strategy" | "stop_loss" | "take_profit"


@dataclass
class BacktestResult:
    symbol:           str
    timeframe:        str
    fee_pct:          float
    starting_cash:    float
    final_value:      float
    total_fees:       float
    fills:            list[FillRecord]
    equity_curve:     list[float]
    candles:          list[Candle]
    warmup_ticks:     int
    rejection_stats:  dict[str, int] = field(default_factory=dict)
    entry_snapshots:  list[dict]     = field(default_factory=list)


def run(
    candles:                  list[Candle],
    symbol:                   str,
    timeframe:                str,
    strategy_mode:            str   = "indicator",
    starting_cash:            float = 10_000.0,
    risk_per_trade_pct:       float = 0.01,
    fee_pct:                  float = 0.001,
    cooldown_ticks:           int   = 10,
    # Indicator config
    rsi_period:               int   = 14,
    rsi_oversold:             float = 30.0,
    rsi_overbought:           float = 70.0,
    fast_ema_period:          int   = 9,
    slow_ema_period:          int   = 21,
    adx_period:               int   = 14,
    adx_threshold:            float = 25.0,
    adx_max:                  float = 0.0,
    min_ema_spread_pct:       float = 0.002,
    max_ema_spread_pct:       float = 0.0,
    rsi_filter_enabled:       bool  = True,
    macd_enabled:             bool  = False,
    # Entry mode parameters (Mode A = pullback, Mode B = breakout).
    # Added 2026-07-20: live (bot/main.py build_strategy()) has always sourced
    # these from cfg.strategy, but the backtest engine silently fell back to
    # IndicatorConfig's hardcoded defaults regardless of .env — same drift
    # class as the ATR SL and macd_enabled incidents, just never yet triggered
    # because .env has never overridden these. Defaults below match
    # IndicatorConfig's own dataclass defaults, so this is a no-op until
    # someone actually tunes one of these in .env.
    pullback_rsi_min:         float = 38.0,
    pullback_rsi_max:         float = 58.0,
    breakout_rsi_min:         float = 50.0,
    breakout_rsi_max:         float = 72.0,
    breakout_lookback:        int   = 20,
    max_price_extension_pct:  float = 0.03,
    breakout_adx_threshold:   float = 22.0,
    # Threshold config
    buy_threshold:            float = 0.0,
    sell_threshold:           float = 0.0,
    # Risk config
    max_position_pct:         float = 0.05,
    daily_loss_limit_pct:     float = 0.02,
    max_drawdown_pct:         float = 0.10,
    max_trades_per_day:       int   = 5,
    # Exit rules
    stop_loss_pct:            float = 0.02,
    take_profit_pct:          float = 0.04,
    trail_stop_pct:               float = 0.0,
    trail_stop_activation_pct:    float = 0.0,
    partial_tp_pct:           float = 0.0,
    partial_tp_size:          float = 0.5,
    slippage_pct:             float = 0.0,
    # Regime filter
    regime_ema_period:        int   = 200,
    regime_ema_slope_filter:  bool  = False,
    # Volume filter
    volume_k:                 float = 1.2,
    atr_volatile_multiplier:  float = 1.5,
    # ATR-based SL (0.0 = disabled, uses fixed stop_loss_pct instead)
    atr_sl_mult:              float = 0.0,
    # ATR-aware sizing (2026-07-17): cap BUY qty so an ATR stop-out never
    # risks more $ than the fixed-SL baseline (cash × risk_pct × baseline).
    # Default off — the canonical fingerprint runs use plain notional sizing.
    atr_risk_sizing:          bool  = False,
    atr_sizing_baseline_sl_pct: float = 0.015,
    # ── Live-only BUY overlays (default OFF — NOT part of the validated
    #    fingerprint; every canonical hash run has these unset). When supplied
    #    a BUY signal is additionally vetoed exactly as bot/main.py does live,
    #    letting a research script measure whether the overlay earns its keep:
    #      mtf_daily_closes — list of (completed-day date, daily close). Before
    #        a BUY, the 9/21-EMA trend() of the daily closes strictly PRIOR to
    #        the current candle's date is computed; "BEARISH" vetoes the BUY
    #        (mirrors bot/main.py section 2c, which slices the forming daily
    #        candle off with [:-1] and passes ~29 closes to trend()).
    #      fng_by_date — {date: Fear&Greed value 0-100}. The value on the
    #        candle's date (or the most recent prior date) > fng_bear_max
    #        vetoes the BUY (mirrors ExternalSignalGate.approve_buy()).
    #    None for either = that overlay is inert and results are bit-identical
    #    to a run without the argument.
    mtf_daily_closes: list | None = None,
    mtf_fast_period:  int = 9,
    mtf_slow_period:  int = 21,
    fng_by_date:      dict | None = None,
    fng_bear_max:     float = 75.0,
) -> BacktestResult:
    """Run a full backtest and return the result."""

    # ── Build components (same as main.py) ───────────────────────────
    if strategy_mode == "indicator":
        strategy = IndicatorStrategy(IndicatorConfig(
            rsi_period               = rsi_period,
            rsi_oversold             = rsi_oversold,
            rsi_overbought           = rsi_overbought,
            fast_ema_period          = fast_ema_period,
            slow_ema_period          = slow_ema_period,
            adx_period               = adx_period,
            adx_threshold            = adx_threshold,
            adx_max                  = adx_max,
            min_ema_spread_pct       = min_ema_spread_pct,
            max_ema_spread_pct       = max_ema_spread_pct,
            rsi_filter_enabled       = rsi_filter_enabled,
            macd_enabled             = macd_enabled,
            regime_ema_period        = regime_ema_period,
            regime_ema_slope_filter  = regime_ema_slope_filter,
            volume_k                 = volume_k,
            atr_volatile_multiplier  = atr_volatile_multiplier,
            pullback_rsi_min         = pullback_rsi_min,
            pullback_rsi_max         = pullback_rsi_max,
            breakout_rsi_min         = breakout_rsi_min,
            breakout_rsi_max         = breakout_rsi_max,
            breakout_lookback        = breakout_lookback,
            max_price_extension_pct  = max_price_extension_pct,
            breakout_adx_threshold   = breakout_adx_threshold,
        ))
        is_indicator = True
    else:
        strategy = ThresholdStrategy(
            buy_threshold  = buy_threshold,
            sell_threshold = sell_threshold,
        )
        is_indicator = False

    executor         = PaperExecutor(symbol=symbol, starting_cash=starting_cash)
    risk             = RiskManager(RiskConfig(
        max_position_pct      = max_position_pct,
        daily_loss_limit_pct  = daily_loss_limit_pct,
        max_drawdown_pct      = max_drawdown_pct,
        max_trades_per_day    = max_trades_per_day,
    ))
    state_machine    = TradingStateMachine(cooldown_ticks=cooldown_ticks)
    position_manager = PositionManager()

    equity_curve:    list[float]      = []
    fills:           list[FillRecord] = []
    total_fees       = 0.0
    warmup_ticks     = 0
    entry_price:     float = 0.0
    _trail_peak:     float = 0.0
    _partial_tp_done: bool = False
    _entry_atr:      float = 0.0   # ATR at the time of BUY — used for ATR-based SL
    entry_snapshots: list[dict] = []
    _overlay_rej:    dict[str, int] = {}   # live-only BUY overlays: veto counts

    _overlays_on = mtf_daily_closes is not None or fng_by_date is not None
    _mtf_sorted  = (
        sorted(mtf_daily_closes, key=lambda dc: dc[0]) if mtf_daily_closes is not None else None
    )

    def _fng_asof(d) -> "int | None":
        """Fear&Greed value for date d, else the most recent prior date's value."""
        if not fng_by_date:
            return None
        if d in fng_by_date:
            return fng_by_date[d]
        _prior = [dd for dd in fng_by_date if dd <= d]
        return fng_by_date[max(_prior)] if _prior else None

    # ── Main loop ─────────────────────────────────────────────────────
    for i, candle in enumerate(candles):
        price = candle.close

        state_machine.tick()

        raw_signal = strategy.evaluate(candle) if is_indicator else strategy.evaluate(price)

        if is_indicator and not strategy.is_warmed_up:
            warmup_ticks += 1
            equity_curve.append(executor.portfolio.total_value(price))
            continue

        # ── Live-only BUY overlays (MTF daily trend / Fear&Greed) ────
        # Same veto bot/main.py applies live, replayed on history. Only
        # touches BUY; a forced SL/TP exit below is unaffected.
        if _overlays_on and is_indicator and raw_signal == Signal.BUY:
            _cdate = candle.timestamp.date()
            _veto = ""
            if _mtf_sorted is not None:
                _prior = [cl for (d, cl) in _mtf_sorted if d < _cdate]
                if len(_prior) >= mtf_slow_period and _trend(
                    _prior[-30:], mtf_fast_period, mtf_slow_period
                ) == "BEARISH":
                    _veto = "mtf_trend"
            if not _veto and fng_by_date is not None:
                _f = _fng_asof(_cdate)
                if _f is not None and _f > fng_bear_max:
                    _veto = "external_signal"
            if _veto:
                raw_signal = Signal.HOLD
                _overlay_rej[_veto] = _overlay_rej.get(_veto, 0) + 1

        # ── Trailing peak update ──────────────────────────────────────
        if executor.position > 0 and entry_price > 0 and trail_stop_pct > 0:
            if _trail_peak == 0.0:
                # Activate once candle.high crosses the activation threshold
                if (trail_stop_activation_pct == 0.0
                        or candle.high >= entry_price * (1 + trail_stop_activation_pct)):
                    _trail_peak = candle.high
            else:
                _trail_peak = max(_trail_peak, candle.high)

        # ── Partial TP: sell 50% at partial_tp_pct (only when explicitly set) ─
        if (
            executor.position > 0
            and entry_price > 0
            and partial_tp_pct > 0
            and not _partial_tp_done
            and candle.high >= entry_price * (1 + partial_tp_pct)
        ):
            _p_qty = round(executor.position * partial_tp_size, 6)
            _p_price = entry_price * (1 + partial_tp_pct)
            if _p_qty > 0:
                _p_approval = risk.evaluate(Signal.SELL, _p_price, executor.portfolio, _p_qty, candle.timestamp.date())
                if _p_approval:
                    _p_fill_price = _p_price * (1 - slippage_pct) if slippage_pct > 0 else _p_price
                    _p_order = executor.execute(Signal.SELL, _p_fill_price, quantity=_p_qty)
                    if _p_order and _p_order.status == OrderStatus.FILLED:
                        risk.record_fill()
                        state_machine.on_fill(Signal.SELL, _p_order.price)
                        _p_pnl = position_manager.on_sell(_p_order.price, _p_order.quantity)
                        _p_fee = round(_p_order.total_value * fee_pct, 4)
                        total_fees += _p_fee
                        executor._portfolio.cash -= _p_fee
                        _partial_tp_done = True
                        state_machine.recover_long(entry_price)
                        fills.append(FillRecord(
                            candle_index = i,
                            timestamp    = candle.timestamp.strftime("%Y-%m-%d %H:%M"),
                            side         = _p_order.side.value,
                            price        = _p_order.price,
                            quantity     = _p_order.quantity,
                            total_value  = _p_order.total_value,
                            pnl          = _p_pnl,
                            fee          = _p_fee,
                            reason       = "partial_tp",
                        ))

        # ── Stop-loss / take-profit override ─────────────────────────
        exit_reason = "strategy"
        exit_price  = price
        forced_exit = False
        if executor.position > 0 and entry_price > 0:
            # Trailing stop takes priority over fixed/ATR SL if configured
            if trail_stop_pct > 0 and _trail_peak > 0:
                _trail_sl = _trail_peak * (1 - trail_stop_pct)
                if candle.low <= _trail_sl:
                    raw_signal  = Signal.SELL
                    exit_reason = "trail_stop"
                    exit_price  = _trail_sl
                    forced_exit = True
            elif atr_sl_mult > 0 and _entry_atr > 0:
                sl_level = entry_price - _entry_atr * atr_sl_mult
                if candle.low <= sl_level:
                    raw_signal  = Signal.SELL
                    exit_reason = "stop_loss"
                    exit_price  = max(sl_level, candle.low)
                    forced_exit = True
            elif stop_loss_pct > 0:
                sl_level = entry_price * (1 - stop_loss_pct)
                if candle.low <= sl_level:
                    raw_signal  = Signal.SELL
                    exit_reason = "stop_loss"
                    exit_price  = sl_level
                    forced_exit = True
            if not forced_exit and take_profit_pct > 0:
                tp_level = entry_price * (1 + take_profit_pct)
                if candle.high >= tp_level:
                    raw_signal  = Signal.SELL
                    exit_reason = "take_profit"
                    exit_price  = tp_level
                    forced_exit = True

        filtered_signal, _ = state_machine.filter_signal(raw_signal)
        # SL/TP must never be suppressed by cooldown — always force the exit
        if forced_exit and filtered_signal != Signal.SELL:
            filtered_signal = Signal.SELL

        if filtered_signal == Signal.SELL:
            trade_qty = executor.position
        else:
            trade_qty = round(executor.cash * risk_per_trade_pct / price, 6) if price > 0 else 0.0
            if (
                atr_risk_sizing and atr_sl_mult > 0 and is_indicator
                and atr_sizing_baseline_sl_pct > 0 and price > 0
            ):
                _atr_now = strategy.last_atr or 0.0
                if _atr_now > 0:
                    _risk_qty = (
                        executor.cash * risk_per_trade_pct * atr_sizing_baseline_sl_pct
                        / (_atr_now * atr_sl_mult)
                    )
                    trade_qty = round(min(trade_qty, _risk_qty), 6)

        approval = risk.evaluate(filtered_signal, price, executor.portfolio, trade_qty, candle.timestamp.date())

        if approval:
            if slippage_pct > 0:
                if filtered_signal == Signal.BUY:
                    fill_at = (exit_price if filtered_signal == Signal.SELL else price) * (1 + slippage_pct)
                else:
                    fill_at = (exit_price if filtered_signal == Signal.SELL else price) * (1 - slippage_pct)
            else:
                fill_at = exit_price if filtered_signal == Signal.SELL else price
            order   = executor.execute(filtered_signal, fill_at, quantity=trade_qty)
            if order and order.status == OrderStatus.FILLED:
                risk.record_fill()
                state_machine.on_fill(filtered_signal, order.price)

                fee  = round(order.total_value * fee_pct, 4)
                total_fees += fee
                executor._portfolio.cash -= fee

                pnl = None
                if order.side == OrderSide.BUY:
                    position_manager.on_buy(order.price, order.quantity)
                    entry_price = order.price
                    _trail_peak = 0.0  # activates once activation_pct profit is reached
                    _partial_tp_done = False
                    _entry_atr = strategy.last_atr or 0.0
                    _adx  = strategy.last_adx   if is_indicator else None
                    _rsi  = strategy.last_rsi   if is_indicator else None
                    _trnd = strategy.last_trend if is_indicator else None
                    _closes_snap = list(strategy._closes)
                    _ema_fast = _ema(_closes_snap, strategy.config.fast_ema_period)
                    _ema_slow = _ema(_closes_snap, strategy.config.slow_ema_period)
                    entry_snapshots.append({
                        "candle_index": i,
                        "adx":      _adx,
                        "rsi":      _rsi,
                        "ema_fast": _ema_fast,
                        "ema_slow": _ema_slow,
                        "trend":    _trnd,
                    })
                else:
                    pnl = position_manager.on_sell(order.price, order.quantity)
                    entry_price = 0.0
                    _trail_peak = 0.0
                    _partial_tp_done = False
                    _entry_atr = 0.0

                fills.append(FillRecord(
                    candle_index = i,
                    timestamp    = candle.timestamp.strftime("%Y-%m-%d %H:%M"),
                    side         = order.side.value,
                    price        = order.price,
                    quantity     = order.quantity,
                    total_value  = order.total_value,
                    pnl          = pnl,
                    fee          = fee,
                    reason       = exit_reason if order.side == OrderSide.SELL else "strategy",
                ))

        equity_curve.append(executor.portfolio.total_value(price))

    # ── Close any open position at last price (mark-to-market) ───────
    last_price = candles[-1].close if candles else 0.0
    final_value = executor.portfolio.total_value(last_price)

    logger.info(
        "Backtest complete | candles=%d warmup=%d fills=%d final=$%.2f fees=$%.2f",
        len(candles), warmup_ticks, len(fills), final_value, total_fees,
    )

    rej_stats: dict[str, int] = {}
    if is_indicator and hasattr(strategy, "stats"):
        rej_stats = dict(strategy.stats)
    if _overlays_on:
        rej_stats["overlay_mtf_rejected"] = _overlay_rej.get("mtf_trend", 0)
        rej_stats["overlay_fng_rejected"] = _overlay_rej.get("external_signal", 0)

    return BacktestResult(
        symbol          = symbol,
        timeframe       = timeframe,
        fee_pct         = fee_pct,
        starting_cash   = starting_cash,
        final_value     = final_value,
        total_fees      = total_fees,
        fills           = fills,
        equity_curve    = equity_curve,
        candles         = candles,
        warmup_ticks    = warmup_ticks,
        rejection_stats = rej_stats,
        entry_snapshots = entry_snapshots,
    )