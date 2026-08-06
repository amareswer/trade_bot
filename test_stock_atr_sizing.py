"""
ATR-aware stock position sizing (stock_bot.config.StockConfig.calc_shares_atr_risk,
2026-08-05). Mirrors the crypto bot's calc_trade_qty_atr_risk (test_atr_sizing.py) —
same invariant, adapted for whole-share sizing: with ATR stops active, the dollars
lost at a stop-out must never exceed the fixed-SL baseline (cash * risk_pct *
baseline_sl_pct), and a tight ATR stop must not size UP past the flat notional
baseline either (cap, not equality).

Opt-in feature (PAPER_ATR_SIZING_ENABLED, default false) — see punch-list item #2
in conversation history / CLAUDE.md "Risk-gate config (stock bot)".

Hermetic: builds StockConfig objects directly via __new__, no .env reads.
"""
import pytest

from stock_bot.config import StockConfig


def _make_cfg(risk_pct: float = 0.10) -> StockConfig:
    # Minimal stub — calc_shares_atr_risk only touches self.paper_risk_pct.
    cfg = StockConfig.__new__(StockConfig)
    cfg.paper_risk_pct = risk_pct
    return cfg


def test_wide_atr_stop_shrinks_shares_below_notional():
    cfg = _make_cfg()
    # ATR $3 at mult 2.0 -> $6 stop distance, 4x the 1.5% ($1.50) baseline on a $100 stock
    base  = int((10_000.0 * 0.10) / 100.0)
    shares = cfg.calc_shares_atr_risk(10_000.0, 100.0, atr_value=3.0,
                                       atr_mult=2.0, baseline_sl_pct=0.015)
    assert shares < base


def test_stop_out_dollar_risk_does_not_exceed_baseline():
    cfg = _make_cfg(risk_pct=0.10)
    cash, price = 10_000.0, 100.0
    atr, mult, baseline = 3.0, 2.0, 0.015
    shares = cfg.calc_shares_atr_risk(cash, price, atr, mult, baseline)
    loss_at_stop = shares * atr * mult
    # int() truncation means the realized risk is <= the baseline, not exactly equal
    # (whole shares can't hit the continuous crypto-qty equality exactly).
    assert loss_at_stop <= cash * 0.10 * baseline + 1e-9


def test_tight_atr_stop_never_sizes_up_past_notional():
    cfg = _make_cfg()
    base = int((100.0 * 0.10) / 100.0)
    # ATR $0.01 -> tiny stop distance -> uncapped risk-shares would be huge
    shares = cfg.calc_shares_atr_risk(100.0, 100.0, atr_value=0.01,
                                       atr_mult=2.0, baseline_sl_pct=0.015)
    assert shares == base


def test_zero_atr_falls_back_to_notional():
    cfg = _make_cfg()
    base = int((100.0 * 0.10) / 100.0)
    assert cfg.calc_shares_atr_risk(100.0, 100.0, 0.0, 2.0, 0.015) == base


def test_zero_baseline_falls_back_to_notional():
    cfg = _make_cfg()
    base = int((100.0 * 0.10) / 100.0)
    assert cfg.calc_shares_atr_risk(100.0, 100.0, 3.0, 2.0, 0.0) == base


def test_zero_mult_falls_back_to_notional():
    cfg = _make_cfg()
    base = int((100.0 * 0.10) / 100.0)
    assert cfg.calc_shares_atr_risk(100.0, 100.0, 3.0, 0.0, 0.015) == base


def test_invalid_cash_or_price_returns_zero():
    cfg = _make_cfg()
    assert cfg.calc_shares_atr_risk(0.0, 100.0, 3.0, 2.0, 0.015) == 0.0
    assert cfg.calc_shares_atr_risk(100.0, 0.0, 3.0, 2.0, 0.015) == 0.0
