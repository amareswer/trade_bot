"""Regression tests for main.py strategy wiring."""

from bot.main import build_strategy
from bot.strategy.indicator_strategy import IndicatorStrategy


def test_build_strategy_passes_full_indicator_config(monkeypatch):
    import bot.main as main_mod

    monkeypatch.setattr(main_mod.cfg.strategy, "mode", "indicator")
    monkeypatch.setattr(main_mod.cfg.strategy, "rsi_period", 10)
    monkeypatch.setattr(main_mod.cfg.strategy, "rsi_oversold", 25.0)
    monkeypatch.setattr(main_mod.cfg.strategy, "rsi_overbought", 75.0)
    monkeypatch.setattr(main_mod.cfg.strategy, "fast_ema_period", 8)
    monkeypatch.setattr(main_mod.cfg.strategy, "slow_ema_period", 24)
    monkeypatch.setattr(main_mod.cfg.strategy, "adx_period", 12)
    monkeypatch.setattr(main_mod.cfg.strategy, "adx_threshold", 22.0)
    monkeypatch.setattr(main_mod.cfg.strategy, "adx_max", 40.0)
    monkeypatch.setattr(main_mod.cfg.strategy, "max_ema_spread_pct", 0.015)
    monkeypatch.setattr(main_mod.cfg.strategy, "rsi_filter_enabled", False)
    monkeypatch.setattr(main_mod.cfg.strategy, "regime_ema_period", 150)
    monkeypatch.setattr(main_mod.cfg.strategy, "regime_ema_slope_filter", True)

    strategy = build_strategy()

    assert isinstance(strategy, IndicatorStrategy)
    assert strategy.config.rsi_period == 10
    assert strategy.config.rsi_oversold == 25.0
    assert strategy.config.rsi_overbought == 75.0
    assert strategy.config.fast_ema_period == 8
    assert strategy.config.slow_ema_period == 24
    assert strategy.config.adx_period == 12
    assert strategy.config.adx_threshold == 22.0
    assert strategy.config.adx_max == 40.0
    assert strategy.config.max_ema_spread_pct == 0.015
    assert strategy.config.rsi_filter_enabled is False
    assert strategy.config.regime_ema_period == 150
    assert strategy.config.regime_ema_slope_filter is True


def test_indicator_strategy_exposes_tick_count():
    strategy = IndicatorStrategy()

    assert strategy.tick_count == 0
    strategy.evaluate(100.0)
    strategy.evaluate(101.0)
    assert strategy.tick_count == 2
