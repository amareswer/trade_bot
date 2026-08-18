"""
Tests for stock_bot.main._check_correlation_gate — the stock bot's BUY-path
correlation gate (added 2026-08-05, punch-list item #4, mirrors the crypto
bot's CORRELATION GATE in bot/main.py).

Imports and calls _check_correlation_gate directly from stock_bot.main — the
exact function object the scan loop calls — driven by SimpleNamespace fakes
for the executor and price_data, not real candle fetches (fetch_correlation_
from_closes is already covered in isolation by test_stock_correlation.py).
"""
import inspect
from types import SimpleNamespace

import stock_bot.main as main_mod
from stock_bot.risk.correlation import CORRELATION_THRESHOLD


def _candles(closes: list[float]) -> list[SimpleNamespace]:
    return [SimpleNamespace(close=c) for c in closes]


def _executor(positions: dict[str, tuple[float, float]]) -> SimpleNamespace:
    return SimpleNamespace(positions_snapshot=lambda: positions)


def test_blocks_when_correlated_with_open_position():
    trend = [100.0 + i * 2 for i in range(35)]
    scaled = [10.0 + i * 0.2 for i in range(35)]   # same trend, different scale -> corr ~1.0
    price_data = {
        "NEW": {"candles": _candles(trend)},
        "HELD": {"candles": _candles(scaled)},
    }
    ex = _executor({"HELD": (10.0, 50.0)})

    peer, corr = main_mod._check_correlation_gate("NEW", ex, price_data)
    assert peer == "HELD"
    assert corr > CORRELATION_THRESHOLD


def test_allows_when_uncorrelated_with_open_position():
    import math
    trend = [100.0 + i for i in range(35)]
    oscillating = [50.0 + 5 * math.sin(i * math.pi / 3) for i in range(35)]
    price_data = {
        "NEW": {"candles": _candles(trend)},
        "HELD": {"candles": _candles(oscillating)},
    }
    ex = _executor({"HELD": (10.0, 50.0)})

    peer, corr = main_mod._check_correlation_gate("NEW", ex, price_data)
    assert peer is None
    assert corr is None


def test_allows_when_no_open_positions():
    price_data = {"NEW": {"candles": _candles([100.0 + i for i in range(35)])}}
    ex = _executor({})
    assert main_mod._check_correlation_gate("NEW", ex, price_data) == (None, None)


def test_skips_self_when_symbol_already_held():
    # Adding to an already-open position must not "correlate against itself".
    closes = _candles([100.0 + i for i in range(35)])
    price_data = {"HELD": {"candles": closes}}
    ex = _executor({"HELD": (10.0, 50.0)})
    assert main_mod._check_correlation_gate("HELD", ex, price_data) == (None, None)


def test_fails_open_when_candidate_has_no_candle_data():
    price_data = {"HELD": {"candles": _candles([100.0 + i for i in range(35)])}}
    ex = _executor({"HELD": (10.0, 50.0)})
    assert main_mod._check_correlation_gate("NEW", ex, price_data) == (None, None)


def test_fails_open_when_peer_has_no_candle_data_in_this_cycle():
    # e.g. a held symbol that dropped out of the current watchlist scan.
    trend = _candles([100.0 + i * 2 for i in range(35)])
    price_data = {"NEW": {"candles": trend}, "HELD": {"screened": True}}   # no "candles" key
    ex = _executor({"HELD": (10.0, 50.0)})
    assert main_mod._check_correlation_gate("NEW", ex, price_data) == (None, None)


def test_case_insensitive_symbol_matching():
    trend = [100.0 + i * 2 for i in range(35)]
    scaled = [10.0 + i * 0.2 for i in range(35)]
    price_data = {
        "NEW": {"candles": _candles(trend)},
        "HELD": {"candles": _candles(scaled)},
    }
    ex = _executor({"held": (10.0, 50.0)})   # lowercase key, as some snapshots might return
    peer, corr = main_mod._check_correlation_gate("new", ex, price_data)
    assert peer == "held"
    assert corr > CORRELATION_THRESHOLD


def test_run_wires_up_correlation_gate():
    """
    The tests above prove _check_correlation_gate() works in isolation —
    this proves the BUY path in the scan loop still actually calls it and
    blocks on a hit. Source-inspection rather than executing run() (which
    needs a live IBKR/yfinance/screener/dashboard stack) — a wiring guard,
    not a substitute for the behavioral tests above.
    """
    source = inspect.getsource(main_mod.run)
    assert "_check_correlation_gate(symbol, executor, price_data)" in source
    assert "_corr_peer is not None" in source
