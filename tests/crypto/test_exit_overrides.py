"""
Per-symbol EXIT overrides — TAKE_PROFIT_PCT_<BASE> / TRAILING_STOP_PCT_<BASE> /
TRAILING_STOP_ACTIVATION_PCT_<BASE> (added 2026-09-03 after the exit-logic
research found BTC's flat 10% take-profit was capping its trend winners; SOL is
choppier and keeps the 10% TP).

Hermetic — builds config objects directly / patches os.environ, no live .env.

Run: python -m pytest tests/crypto/test_exit_overrides.py -q
"""
import inspect
import os

import pytest

from config import BacktestConfig, _exit_overrides_by_base

_PER_BASE_PREFIXES = (
    "TAKE_PROFIT_PCT_", "TRAILING_STOP_PCT_", "TRAILING_STOP_ACTIVATION_PCT_",
)


@pytest.fixture(autouse=True)
def _clear_real_env_overrides(monkeypatch):
    """The real .env carries TAKE_PROFIT_PCT_BTC — clear every per-base key so
    each test controls the scanner input entirely."""
    for k in list(os.environ):
        if any(k.startswith(p) for p in _PER_BASE_PREFIXES):
            monkeypatch.delenv(k, raising=False)


# ── _exit_overrides_by_base scanner ──────────────────────────────────────

def test_scanner_empty_when_nothing_set():
    assert _exit_overrides_by_base() == {}


def test_scanner_picks_up_per_base_keys(monkeypatch):
    monkeypatch.setenv("TAKE_PROFIT_PCT_BTC", "0.20")
    monkeypatch.setenv("TRAILING_STOP_PCT_ETH", "0.08")
    monkeypatch.setenv("TRAILING_STOP_ACTIVATION_PCT_ETH", "0.05")
    out = _exit_overrides_by_base()
    assert out["BTC"] == {"take_profit_pct": 0.20}
    assert out["ETH"] == {"trail_stop_pct": 0.08, "trail_stop_activation_pct": 0.05}


def test_scanner_rejects_non_numeric(monkeypatch):
    monkeypatch.setenv("TAKE_PROFIT_PCT_BTC", "wide")
    with pytest.raises(ValueError):
        _exit_overrides_by_base()


def test_scanner_ignores_bare_keys(monkeypatch):
    # the flat keys (no _<BASE>) must NOT be swallowed by the scanner
    monkeypatch.setenv("TAKE_PROFIT_PCT", "0.10")
    monkeypatch.setenv("TRAILING_STOP_PCT", "0.0")
    assert _exit_overrides_by_base() == {}


# ── BacktestConfig.exit_params_for ──────────────────────────────────────

def _cfg(**over):
    return BacktestConfig(
        take_profit_pct=0.10, trail_stop_pct=0.0, trail_stop_activation_pct=0.03,
        atr_sl_mult=2.0, **over,
    )


def test_exit_params_for_falls_back_to_shared_when_no_override():
    c = _cfg()
    assert c.exit_params_for("SOL/CAD") == {
        "take_profit_pct": 0.10, "trail_stop_pct": 0.0, "trail_stop_activation_pct": 0.03,
    }


def test_exit_params_for_applies_base_override_and_merges():
    c = _cfg(exit_overrides_by_base={"BTC": {"take_profit_pct": 0.20}})
    btc = c.exit_params_for("BTC/USDT")
    assert btc["take_profit_pct"] == 0.20            # overridden
    assert btc["trail_stop_pct"] == 0.0              # inherited
    assert btc["trail_stop_activation_pct"] == 0.03  # inherited
    # a different base is untouched
    assert c.exit_params_for("SOL/CAD")["take_profit_pct"] == 0.10


def test_exit_params_for_is_base_not_quote_specific():
    c = _cfg(exit_overrides_by_base={"BTC": {"take_profit_pct": 0.20}})
    # same base, different quote — both get the override (validate on /USDT,
    # trade on /CAD)
    assert c.exit_params_for("BTC/USDT")["take_profit_pct"] == 0.20
    assert c.exit_params_for("BTC/CAD")["take_profit_pct"] == 0.20


def test_exit_params_for_handles_missing_symbol():
    c = _cfg(exit_overrides_by_base={"BTC": {"take_profit_pct": 0.20}})
    assert c.exit_params_for("")["take_profit_pct"] == 0.10
    assert c.exit_params_for(None)["take_profit_pct"] == 0.10


# ── validation ─────────────────────────────────────────────────────────

def test_out_of_range_override_is_rejected():
    with pytest.raises(ValueError):
        _cfg(exit_overrides_by_base={"BTC": {"take_profit_pct": 1.5}})
    with pytest.raises(ValueError):
        _cfg(exit_overrides_by_base={"BTC": {"trail_stop_pct": 0.9}})


# ── engine_kwargs_from_cfg routes through exit_params_for ───────────────

def test_engine_kwargs_uses_per_symbol_exit_params():
    from bot.backtest.params import engine_kwargs_from_cfg
    from tests.crypto.test_engine_params import make_fake_cfg

    cfg = make_fake_cfg()
    cfg.exchange.symbol = "BTC/USDT"
    cfg.backtest.exit_overrides_by_base = {"BTC": {"take_profit_pct": 0.20}}
    kw = engine_kwargs_from_cfg(cfg)
    assert kw["take_profit_pct"] == 0.20

    # the symbol= override resolves for a DIFFERENT base (validate_symbol.py path)
    kw_sol = engine_kwargs_from_cfg(cfg, symbol="SOL/USDT")
    assert kw_sol["symbol"] == "SOL/USDT"
    assert kw_sol["take_profit_pct"] == 0.10


# ── live wiring: bot/main.py exit block reads the per-symbol dict ───────

def test_main_exit_block_uses_per_symbol_params():
    import bot.main as bot_main
    src = inspect.getsource(bot_main.run)
    assert "_ep = cfg.backtest.exit_params_for(sym)" in src
    assert '_ep["take_profit_pct"]' in src, "the intra-candle TP check must use the per-symbol value"
    assert "cfg.backtest.take_profit_pct\n" not in src.replace(" ", ""), \
        "no bare cfg.backtest.take_profit_pct should remain in the exit path"
