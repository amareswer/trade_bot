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
            buy_threshold=0.0,
            sell_threshold=0.0,
            regime_ema_period=200,
            regime_ema_slope_filter=False,
            volume_k=0.0,
            atr_volatile_multiplier=1.5,
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


def test_macd_enabled_is_deliberately_excluded():
    """Canonical-fingerprint parity: every validated result (35 trades /
    PF 1.90 as of 2026-07-17) was produced with the engine's macd_enabled
    default (False), while live runs True. Including it here would silently
    change the fingerprint. If that divergence is ever resolved by decision,
    flip this test alongside a full walk-forward re-run + CLAUDE.md update.
    """
    kwargs = engine_kwargs_from_cfg(make_fake_cfg())
    assert "macd_enabled" not in kwargs


def test_validation_scripts_use_the_builder():
    """backtest.py and walkforward.py must not regrow hand-listed arg drift."""
    for script in ("backtest.py", "walkforward.py"):
        source = (REPO_ROOT / script).read_text()
        assert "engine_kwargs_from_cfg" in source, (
            f"{script} no longer uses engine_kwargs_from_cfg() — "
            "validation scripts must share one kwargs source"
        )
