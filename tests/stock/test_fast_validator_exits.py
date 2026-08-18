"""
Unit tests for FastValidator.check_exits — the trade-completion engine for
the stock-bot stats book.

Covers the 2026-07-04 fix: MAX_HOLD must fire even when the cycle has no
candles for the position's symbol (yfinance rate limit / market holiday),
falling back to the guarded live-price helper. Before the fix, a feed gap
starved ALL exits and positions could be held indefinitely (observed with
AMZN/HOOD on 2026-07-04).

Run: python -m pytest tests/stock/test_fast_validator_exits.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import stock_bot.fast_validator as fv_mod
from stock_bot.fast_validator import (
    FastCandle,
    FastPosition,
    FastValidator,
    FastValidatorConfig,
    FastValidatorState,
)


def _cfg(**overrides) -> FastValidatorConfig:
    base = dict(
        candle_interval="1h", lookback_hours=168,
        sl_pct=1.5, tp_pct=3.0,
        max_hold_hours=48, max_positions=2, min_confidence=70,
    )
    base.update(overrides)
    return FastValidatorConfig(**base)


def _pos(symbol: str, held_hours: float, entry: float = 100.0) -> FastPosition:
    return FastPosition(
        symbol      = symbol,
        entry_time  = datetime.utcnow() - timedelta(hours=held_hours),
        entry_price = entry,
        sl_price    = entry * 0.985,
        tp_price    = entry * 1.03,
        confidence  = 70,
    )


def _candle(close: float) -> FastCandle:
    return FastCandle(
        timestamp=datetime.utcnow(), open=close, high=close,
        low=close, close=close, volume=1000.0,
    )


def _make_validator(tmp_path, positions: list[FastPosition]) -> FastValidator:
    """Validator with tmp CSV path — never touches the real fast_trades.csv.
    tmp_path is pytest's built-in per-test fixture (auto-cleaned) — pass your
    test's own tmp_path fixture through."""
    tmp_csv = str(tmp_path / "fast_trades.csv")
    with patch.object(fv_mod, "_TRADES_CSV", tmp_csv):
        v = FastValidator(cfg=_cfg(), state=FastValidatorState(positions=positions))
    # keep writes going to the tmp file after construction too
    v._tmp_csv_patch = patch.object(fv_mod, "_TRADES_CSV", tmp_csv)
    return v


def test_max_hold_exits_via_live_price_when_no_candles(tmp_path):
    """Held past max_hold + no candles this cycle → MAX_HOLD exit at live price."""
    v = _make_validator(tmp_path, [_pos("AMZN", held_hours=50.0)])
    with v._tmp_csv_patch, \
         patch.object(fv_mod, "get_live_price", return_value=101.5) as mock_live:
        exits = v.check_exits(v.state, current_candles={}, signals={})

    assert len(exits) == 1
    assert exits[0]["reason"] == "MAX_HOLD"
    assert exits[0]["exit_price"] == 101.5
    assert len(v.state.positions) == 0, "position must be removed from state"
    mock_live.assert_called_once_with("AMZN")


def test_max_hold_no_candles_and_no_live_price_keeps_position(tmp_path):
    """Feed fully down: position kept (never exit at an unknown price)."""
    v = _make_validator(tmp_path, [_pos("AMZN", held_hours=50.0)])
    with v._tmp_csv_patch, \
         patch.object(fv_mod, "get_live_price", return_value=None):
        exits = v.check_exits(v.state, current_candles={}, signals={})

    assert exits == []
    assert len(v.state.positions) == 1


def test_no_candles_under_max_hold_keeps_position_without_price_call(tmp_path):
    """Under max hold with no candles: SL/TP wait for data — no live-price call."""
    v = _make_validator(tmp_path, [_pos("HOOD", held_hours=2.0)])
    with v._tmp_csv_patch, \
         patch.object(fv_mod, "get_live_price", return_value=99.0) as mock_live:
        exits = v.check_exits(v.state, current_candles={}, signals={})

    assert exits == []
    assert len(v.state.positions) == 1
    mock_live.assert_not_called()


def test_stop_loss_fires_with_candles_present(tmp_path):
    """Regression: normal SL path unchanged by the no-candles fallback."""
    v = _make_validator(tmp_path, [_pos("HOOD", held_hours=2.0, entry=100.0)])
    candles = {"HOOD": [_candle(98.0)]}   # below sl_price=98.5
    with v._tmp_csv_patch:
        exits = v.check_exits(v.state, current_candles=candles, signals={})

    assert len(exits) == 1
    assert exits[0]["reason"] == "SL"
    assert len(v.state.positions) == 0


def test_corrupt_close_skips_exit_check(tmp_path):
    """META incident regression: a close deviating >20% from the prior candle
    is suspected corruption — no exit may be written at that price."""
    v = _make_validator(tmp_path, [_pos("META", held_hours=2.0, entry=560.0)])
    candles = {"META": [_candle(564.87), _candle(163.51)]}   # −71% in one candle
    with v._tmp_csv_patch:
        exits = v.check_exits(v.state, current_candles=candles, signals={})

    assert exits == []
    assert len(v.state.positions) == 1, "position must survive a corrupt candle"


def test_sane_gap_within_threshold_still_exits(tmp_path):
    """A real −10% gap (inside the 20% sanity bound) must still trigger SL."""
    v = _make_validator(tmp_path, [_pos("HOOD", held_hours=2.0, entry=100.0)])
    candles = {"HOOD": [_candle(100.0), _candle(90.0)]}
    with v._tmp_csv_patch:
        exits = v.check_exits(v.state, current_candles=candles, signals={})

    assert len(exits) == 1
    assert exits[0]["reason"] == "SL"


if __name__ == "__main__":
    import pathlib
    import shutil
    import sys
    import tempfile

    failures = 0
    for t in [
        test_max_hold_exits_via_live_price_when_no_candles,
        test_max_hold_no_candles_and_no_live_price_keeps_position,
        test_no_candles_under_max_hold_keeps_position_without_price_call,
        test_stop_loss_fires_with_candles_present,
        test_corrupt_close_skips_exit_check,
        test_sane_gap_within_threshold_still_exits,
    ]:
        # Standalone runner has no pytest tmp_path fixture — build an
        # equivalent per-test dir and clean it up manually.
        fake_tmp_path = pathlib.Path(tempfile.mkdtemp())
        try:
            t(fake_tmp_path)
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failures += 1
        finally:
            shutil.rmtree(fake_tmp_path, ignore_errors=True)
    sys.exit(failures)
