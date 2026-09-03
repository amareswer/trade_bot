"""
Single source of engine.run() kwargs for every validation script.

Why this exists: backtest.py and walkforward.py each hand-listed their
engine.run() arguments, and the lists drifted apart. By 2026-07-17
walkforward.py was missing volume_k (engine default 1.2 vs validated 0),
min_ema_spread_pct (0.002 vs validated 0.004), adx_max, the regime and
partial-TP params, and the live ATR keys (ATR_SL_MULT / ATR_SIZING_ENABLED)
— it was validating a config that no longer matched what trades live.
Same failure class as the 2026-07-02 ATR SL drift incident.

Rule: any script that validates the strategy (backtest.py, walkforward.py,
future sweeps) builds its engine kwargs HERE and only overrides deliberately
(e.g. backtest.py's CLI flags). Never hand-list engine.run() args again.

2026-07-20: macd_enabled added — live (bot/main.py) and shadow_signal.py have
always run with cfg.strategy.macd_enabled (True), but every canonical
fingerprint through 2026-07-18 was produced with the engine default (False),
so the validated numbers described a slightly more permissive strategy than
the one actually trading. Resolved by validating what's live (option a from
the 2026-07-18 CLAUDE.md entry) rather than turning MACD off to match the
old numbers. New canonical fingerprint: see CLAUDE.md.

2026-07-20 (same day, audit follow-up): the seven Mode A/B entry-mode params
(pullback_rsi_min/max, breakout_rsi_min/max, breakout_lookback,
max_price_extension_pct, breakout_adx_threshold) had the identical gap —
live-configurable via cfg.strategy / .env, silently ignored by the backtest
engine (engine.run() didn't even accept them as arguments). No live impact
found (.env has never overridden any of the seven), but it was one .env edit
away from repeating the ATR SL / macd_enabled incidents undetected. Added
here and to engine.run()'s signature. test_engine_params.py now also has a
generic parity test so a NEW shared field can't reopen this gap silently.

Deliberate exclusions — do not add without a validation decision:
  - slippage_pct: engine default 0.0 — fee_pct is the validated cost model.
  - max_drawdown_pct: intentionally decoupled from cfg.risk.max_drawdown_pct
    (live: 0.05 / 5%). Backtests and walk-forward run at a loose 0.25 (25%)
    ceiling so they measure the strategy's raw signal quality — the sequence
    of BUY/SELL decisions the strategy itself would make — rather than being
    truncated or reshaped by where live's 5% capital-protection breaker would
    have halted new BUYs. The breaker is a separate, independently-tested
    safety layer (RiskManager, test_risk_manager.py) that runs for real in
    live/paper trading; it does not need to be re-proven inside every
    backtest. Practical consequence: a live max-drawdown halt event would
    make live's actual trade sequence diverge from a walk-forward's — that
    divergence is expected and acceptable, not a fidelity bug. This was
    flagged during the 2026-07-20 audit as undocumented; now documented
    rather than changed. backtest.py's own --max_drawdown CLI flag (default
    0.25, same number) can still override it per-run.
"""


def engine_kwargs_from_cfg(cfg, symbol: str | None = None) -> dict:
    """Build the full engine.run() kwarg dict from the loaded AppConfig.

    Returns everything except `candles`. Callers may .update() individual
    keys for CLI overrides before passing to engine.run().

    `symbol` overrides `cfg.exchange.symbol` — pass it when building kwargs for
    a symbol other than the configured one (validate_symbol.py, screen_universe.py)
    so the per-symbol EXIT params (TAKE_PROFIT_PCT_<BASE> etc.) resolve for the
    RIGHT base, not the configured symbol's. Omit it for backtest.py /
    walkforward.py, which validate the configured symbol.
    """
    _sym = symbol or cfg.exchange.symbol
    return dict(
        symbol               = _sym,
        timeframe            = cfg.backtest.timeframe,
        strategy_mode        = cfg.strategy.mode,
        starting_cash        = cfg.portfolio.starting_cash,
        risk_per_trade_pct   = cfg.risk.risk_per_trade_pct,
        fee_pct              = cfg.backtest.fee_pct,
        cooldown_ticks       = cfg.risk.cooldown_ticks,
        rsi_period           = cfg.strategy.rsi_period,
        rsi_oversold         = cfg.strategy.rsi_oversold,
        rsi_overbought       = cfg.strategy.rsi_overbought,
        fast_ema_period      = cfg.strategy.fast_ema_period,
        slow_ema_period      = cfg.strategy.slow_ema_period,
        adx_period           = cfg.strategy.adx_period,
        adx_threshold        = cfg.strategy.adx_threshold,
        adx_max              = cfg.strategy.adx_max,
        min_ema_spread_pct   = cfg.strategy.min_ema_spread_pct,
        max_ema_spread_pct   = cfg.strategy.max_ema_spread_pct,
        rsi_filter_enabled   = cfg.strategy.rsi_filter_enabled,
        macd_enabled         = cfg.strategy.macd_enabled,
        pullback_rsi_min         = cfg.strategy.pullback_rsi_min,
        pullback_rsi_max         = cfg.strategy.pullback_rsi_max,
        breakout_rsi_min         = cfg.strategy.breakout_rsi_min,
        breakout_rsi_max         = cfg.strategy.breakout_rsi_max,
        breakout_lookback        = cfg.strategy.breakout_lookback,
        max_price_extension_pct  = cfg.strategy.max_price_extension_pct,
        breakout_adx_threshold   = cfg.strategy.breakout_adx_threshold,
        buy_threshold        = cfg.strategy.buy_threshold,
        sell_threshold       = cfg.strategy.sell_threshold,
        max_position_pct     = cfg.risk.max_position_pct,
        daily_loss_limit_pct = cfg.risk.daily_loss_limit_pct,
        # Deliberately NOT cfg.risk.max_drawdown_pct (live: 0.05) — see the
        # "max_drawdown_pct is intentionally decoupled" note above.
        max_drawdown_pct     = 0.25,
        max_trades_per_day   = cfg.risk.max_trades_per_day,
        stop_loss_pct        = cfg.backtest.stop_loss_pct,
        # Exit params are per-symbol (2026-09-03): BTC rides trends → trailing
        # stop; SOL is choppy → hard TP. exit_params_for() merges any
        # TAKE_PROFIT_PCT_<BASE> / TRAILING_STOP_PCT_<BASE> override over the
        # shared defaults. Keyed off cfg.exchange.symbol — the one symbol this
        # builder is producing kwargs for.
        **cfg.backtest.exit_params_for(_sym),
        partial_tp_pct       = cfg.backtest.partial_tp_pct,
        partial_tp_size      = cfg.backtest.partial_tp_size,
        regime_ema_period       = cfg.strategy.regime_ema_period,
        regime_ema_slope_filter = cfg.strategy.regime_ema_slope_filter,
        volume_k                = cfg.strategy.volume_k,
        atr_volatile_multiplier = cfg.strategy.atr_volatile_multiplier,
        atr_sl_mult             = cfg.backtest.atr_sl_mult,
        atr_risk_sizing         = cfg.backtest.atr_sizing_enabled,
        atr_sizing_baseline_sl_pct = cfg.backtest.stop_loss_pct or 0.015,
    )
