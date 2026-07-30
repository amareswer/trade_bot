"""
Tests for bot/backtest/params.py — the single source of engine.run() kwargs.

Guards against the validation-script drift class of bug (2026-07-02 ATR SL
incident; 2026-07-17 walkforward.py found missing volume_k / min_ema_spread /
ATR keys): every validation script must build its engine kwargs from
engine_kwargs_from_cfg(), and that builder must stay in sync with the
engine.run() signature and the live .env keys.

Hermetic: uses a fake cfg (SimpleNamespace) — never reads the real .env.
"""
import inspect
from pathlib import Path
from types import SimpleNamespace

from bot.backtest import engine
from bot.backtest.params import engine_kwargs_from_cfg

REPO_ROOT = Path(__file__).resolve().parent


def make_fake_cfg(**overrides):
    """Minimal AppConfig-shaped object covering every attr the builder reads."""
    cfg = SimpleNamespace(
        exchange=SimpleNamespace(symbol="TEST/PAIR"),
        backtest=SimpleNamespace(
            timeframe="4h",
            fee_pct=0.008,
            stop_loss_pct=0.015,
            take_profit_pct=0.10,
            trail_stop_pct=0.0,
            trail_stop_activation_pct=0.03,
            partial_tp_pct=0.0,
            partial_tp_size=0.5,
            atr_sl_mult=2.0,
            atr_sizing_enabled=True,
        ),
        strategy=SimpleNamespace(
            mode="indicator",
            rsi_period=14,
            rsi_oversold=30.0,
            rsi_overbought=70.0,
            fast_ema_period=9,
            slow_ema_period=21,
            adx_period=14,
            adx_threshold=18.0,
            adx_max=0.0,
            min_ema_spread_pct=0.004,
            max_ema_spread_pct=0.0,
            rsi_filter_enabled=True,
            macd_enabled=True,
            buy_threshold=0.0,
            sell_threshold=0.0,
            regime_ema_period=200,
            regime_ema_slope_filter=False,
            volume_k=0.0,
            atr_volatile_multiplier=1.5,
            pullback_rsi_min=38.0,
            pullback_rsi_max=58.0,
            breakout_rsi_min=50.0,
            breakout_rsi_max=72.0,
            breakout_lookback=20,
            max_price_extension_pct=0.03,
            breakout_adx_threshold=22.0,
        ),
        risk=SimpleNamespace(
            risk_per_trade_pct=0.10,
            cooldown_ticks=10,
            max_position_pct=0.75,
            daily_loss_limit_pct=0.02,
            max_trades_per_day=3,
        ),
        portfolio=SimpleNamespace(starting_cash=100.0),
    )
    for dotted, value in overrides.items():
        section, attr = dotted.split(".")
        setattr(getattr(cfg, section), attr, value)
    return cfg


def test_every_key_is_accepted_by_engine_run():
    """A typo'd or removed kwarg must fail here, not at 2am in a live sweep."""
    kwargs = engine_kwargs_from_cfg(make_fake_cfg())
    accepted = set(inspect.signature(engine.run).parameters)
    unknown = set(kwargs) - accepted
    assert not unknown, f"builder emits kwargs engine.run() does not accept: {unknown}"


def test_atr_keys_sourced_from_backtest_config():
    """The live ATR knobs must flow through — the whole point of the builder."""
    cfg = make_fake_cfg(**{"backtest.atr_sl_mult": 2.5,
                           "backtest.atr_sizing_enabled": True,
                           "backtest.stop_loss_pct": 0.02})
    kwargs = engine_kwargs_from_cfg(cfg)
    assert kwargs["atr_sl_mult"] == 2.5
    assert kwargs["atr_risk_sizing"] is True
    assert kwargs["atr_sizing_baseline_sl_pct"] == 0.02


def test_atr_sizing_baseline_falls_back_when_fixed_sl_disabled():
    cfg = make_fake_cfg(**{"backtest.stop_loss_pct": 0.0})
    kwargs = engine_kwargs_from_cfg(cfg)
    assert kwargs["atr_sizing_baseline_sl_pct"] == 0.015


def test_previously_drifted_keys_are_sourced_from_cfg():
    """The exact keys walkforward.py was missing before 2026-07-17."""
    cfg = make_fake_cfg(**{"strategy.volume_k": 1.7,
                           "strategy.min_ema_spread_pct": 0.009,
                           "strategy.max_ema_spread_pct": 0.006,
                           "strategy.adx_max": 33.0,
                           "strategy.regime_ema_period": 150,
                           "strategy.regime_ema_slope_filter": True,
                           "backtest.partial_tp_pct": 0.05})
    kwargs = engine_kwargs_from_cfg(cfg)
    assert kwargs["volume_k"] == 1.7
    assert kwargs["min_ema_spread_pct"] == 0.009
    assert kwargs["max_ema_spread_pct"] == 0.006
    assert kwargs["adx_max"] == 33.0
    assert kwargs["regime_ema_period"] == 150
    assert kwargs["regime_ema_slope_filter"] is True
    assert kwargs["partial_tp_pct"] == 0.05


def test_macd_enabled_is_sourced_from_cfg():
    """2026-07-20: resolved the live/backtest MACD divergence by validating
    what's actually live (cfg.strategy.macd_enabled, True) instead of
    matching the old MACD-off fingerprint. New canonical numbers in CLAUDE.md.
    """
    kwargs = engine_kwargs_from_cfg(make_fake_cfg(**{"strategy.macd_enabled": False}))
    assert kwargs["macd_enabled"] is False
    kwargs = engine_kwargs_from_cfg(make_fake_cfg())
    assert kwargs["macd_enabled"] is True


def test_mode_ab_entry_params_are_sourced_from_cfg():
    """2026-07-20 audit finding: live (bot/main.py build_strategy()) has
    always sourced these seven from cfg.strategy, but engine.run() didn't
    even accept them — the backtest silently used IndicatorConfig's hardcoded
    defaults regardless of .env. No live impact found at the time (.env never
    overrode any of them), but it was one .env edit away from repeating the
    ATR SL / macd_enabled drift incidents undetected.
    """
    cfg = make_fake_cfg(**{
        "strategy.pullback_rsi_min":        40.0,
        "strategy.pullback_rsi_max":        60.0,
        "strategy.breakout_rsi_min":        52.0,
        "strategy.breakout_rsi_max":        74.0,
        "strategy.breakout_lookback":       25,
        "strategy.max_price_extension_pct": 0.05,
        "strategy.breakout_adx_threshold":  24.0,
    })
    kwargs = engine_kwargs_from_cfg(cfg)
    assert kwargs["pullback_rsi_min"] == 40.0
    assert kwargs["pullback_rsi_max"] == 60.0
    assert kwargs["breakout_rsi_min"] == 52.0
    assert kwargs["breakout_rsi_max"] == 74.0
    assert kwargs["breakout_lookback"] == 25
    assert kwargs["max_price_extension_pct"] == 0.05
    assert kwargs["breakout_adx_threshold"] == 24.0


def test_every_shared_strategy_field_reaches_the_backtest():
    """Future-proofing for the whole bug class (ATR SL 2026-07-02, macd_enabled
    and Mode A/B params 2026-07-20): any field that exists on BOTH the real
    StrategyConfig (live, .env-backed) and the real IndicatorConfig (what the
    backtest strategy is built from) is, by construction, something live can
    configure that the backtest must also see. If a future field is added to
    both dataclasses with the same name but never wired into
    engine_kwargs_from_cfg() or engine.run(), this test fails instead of the
    gap sitting silent for months.

    Only introspects field NAMES on the real dataclasses (no .env read) —
    stays hermetic.
    """
    from dataclasses import fields as dc_fields
    from config import StrategyConfig
    from bot.strategy.indicator_strategy import IndicatorConfig

    strategy_fields   = {f.name for f in dc_fields(StrategyConfig)}
    indicator_fields  = {f.name for f in dc_fields(IndicatorConfig)}
    shared            = strategy_fields & indicator_fields

    kwargs   = engine_kwargs_from_cfg(make_fake_cfg())
    accepted = set(inspect.signature(engine.run).parameters)

    not_emitted = {f for f in shared if f not in kwargs}
    not_accepted = {f for f in shared if f not in accepted}
    assert not not_emitted, (
        f"fields configurable live but never sourced by engine_kwargs_from_cfg: {not_emitted}"
    )
    assert not not_accepted, (
        f"fields configurable live but not accepted by engine.run(): {not_accepted}"
    )


def test_validation_scripts_use_the_builder():
    """backtest.py, walkforward.py, and validate_symbol.py must not regrow
    hand-listed arg drift. validate_symbol.py joined this list 2026-07-30
    after being found hand-listing its own config block — missing
    macd_enabled=True (live since 2026-07-20) and the live ATR×2.0 SL (live
    since 2026-07-17), the same drift class documented above for the other
    two scripts. So a fourth script can't reopen this gap silently."""
    for script in ("backtest.py", "walkforward.py", "validate_symbol.py"):
        source = (REPO_ROOT / script).read_text()
        assert "engine_kwargs_from_cfg" in source, (
            f"{script} no longer uses engine_kwargs_from_cfg() — "
            "validation scripts must share one kwargs source"
        )
