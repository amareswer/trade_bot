"""
ATR-aware position sizing (config.calc_trade_qty_atr_risk, 2026-07-17).

Invariant under test: with ATR stops (ATR_SL_MULT) active, the dollars lost
at a stop-out must never exceed the fixed-SL baseline the validation was run
with (cash × risk_per_trade_pct × baseline_sl_pct) — and a tight ATR stop
must not size UP past the standard notional either (cap, not equality).
Hermetic: builds AppConfig objects directly, no .env reads.
"""
import pytest

from config import AppConfig, RiskConfig


def _make_cfg(risk_pct: float = 0.10) -> AppConfig:
    # Minimal stub — the sizing methods only touch cfg.risk.risk_per_trade_pct.
    cfg = AppConfig.__new__(AppConfig)
    cfg.risk = RiskConfig(risk_per_trade_pct=risk_pct)
    return cfg


def test_wide_atr_stop_shrinks_qty_below_notional():
    cfg = _make_cfg()
    # ATR 3% of price at mult 2.0 → 6% stop distance, 4× the 1.5% baseline
    base = cfg.calc_trade_qty(100.0, 100.0)
    qty  = cfg.calc_trade_qty_atr_risk(100.0, 100.0, atr_value=3.0,
                                       atr_mult=2.0, baseline_sl_pct=0.015)
    assert qty < base


def test_stop_out_dollar_risk_equals_baseline():
    cfg = _make_cfg(risk_pct=0.10)
    cash, price = 100.0, 100.0
    atr, mult, baseline = 3.0, 2.0, 0.015
    qty = cfg.calc_trade_qty_atr_risk(cash, price, atr, mult, baseline)
    loss_at_stop = qty * atr * mult
    assert loss_at_stop == pytest.approx(cash * 0.10 * baseline, rel=1e-4)


def test_tight_atr_stop_never_sizes_up_past_notional():
    cfg = _make_cfg()
    base = cfg.calc_trade_qty(100.0, 100.0)
    # ATR 0.01% of price → tiny stop distance → uncapped risk-qty would be huge
    qty = cfg.calc_trade_qty_atr_risk(100.0, 100.0, atr_value=0.01,
                                      atr_mult=2.0, baseline_sl_pct=0.015)
    assert qty == pytest.approx(base)


def test_zero_atr_falls_back_to_notional():
    cfg = _make_cfg()
    base = cfg.calc_trade_qty(100.0, 100.0)
    assert cfg.calc_trade_qty_atr_risk(100.0, 100.0, 0.0, 2.0, 0.015) == base


def test_zero_baseline_falls_back_to_notional():
    cfg = _make_cfg()
    base = cfg.calc_trade_qty(100.0, 100.0)
    assert cfg.calc_trade_qty_atr_risk(100.0, 100.0, 3.0, 2.0, 0.0) == base


def test_zero_mult_falls_back_to_notional():
    cfg = _make_cfg()
    base = cfg.calc_trade_qty(100.0, 100.0)
    assert cfg.calc_trade_qty_atr_risk(100.0, 100.0, 3.0, 0.0, 0.015) == base


def test_invalid_cash_or_price_returns_zero():
    cfg = _make_cfg()
    assert cfg.calc_trade_qty_atr_risk(0.0, 100.0, 3.0, 2.0, 0.015) == 0.0
    assert cfg.calc_trade_qty_atr_risk(100.0, 0.0, 3.0, 2.0, 0.015) == 0.0
