"""
Tests for the USD/CAD sizing fix (2026-07-31).

Bug: stock_bot.execution.{ibkr,paper} treated USD share prices as if they
were already CAD when computing total account value / exposure — the
account's cash (and PAPER_RISK_PCT allocation target in stock_bot/main.py)
is CAD, but US-listed share prices are USD. A USD position's real CAD value
was understated, so exposure/sizing silently ran over the intended target
(found live 2026-07-31 on RY: ~$842 USD spent against a $1,002 CAD target —
actually worth ~$1,150+ CAD, ~23% of the account instead of the intended 20%).

Fix: stock_bot/data/price_feed.py gained get_usd_cad_rate() (cached, yfinance
"CAD=X", falls back to a hardcoded rate on failure) and is_cad_symbol(). Both
executors now convert non-CAD positions through that rate before mixing them
into a CAD total (IBKRExecutor._price_in_cad uses the real contract currency;
StockPaperExecutor._price_in_cad uses the .TO-suffix heuristic, since it has
no broker contract to ask).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import stock_bot.data.price_feed as price_feed
import stock_bot.execution.paper as paper_mod
from stock_bot.data.price_feed import get_usd_cad_rate, is_cad_symbol
from stock_bot.execution.paper import StockPaperExecutor


@pytest.fixture(autouse=True)
def _reset_fx_cache():
    """Isolate the module-level FX cache between tests."""
    price_feed._fx_cache = None
    yield
    price_feed._fx_cache = None


# ── is_cad_symbol ────────────────────────────────────────────────────────────

def test_is_cad_symbol_recognizes_tsx_suffix():
    assert is_cad_symbol("RY.TO") is True
    assert is_cad_symbol("ry.to") is True   # case-insensitive


def test_is_cad_symbol_false_for_us_listings():
    assert is_cad_symbol("RY") is False
    assert is_cad_symbol("AAPL") is False


# ── get_usd_cad_rate ─────────────────────────────────────────────────────────

def _mock_fx_fast_info(rate: float) -> MagicMock:
    fi = MagicMock()
    fi.last_price = rate
    fi.lastPrice = rate
    return fi


def test_get_usd_cad_rate_success():
    with patch("yfinance.Ticker") as mock_ticker, patch("time.sleep"):
        mock_ticker.return_value.fast_info = _mock_fx_fast_info(1.37)
        rate = get_usd_cad_rate()
    assert rate == 1.37


def test_get_usd_cad_rate_falls_back_on_failure(caplog):
    with patch("yfinance.Ticker", side_effect=RuntimeError("no network")), \
         patch("time.sleep"):
        rate = get_usd_cad_rate()
    assert rate == price_feed._FX_FALLBACK_RATE
    assert any("USD/CAD rate unavailable" in r.message for r in caplog.records)


def test_get_usd_cad_rate_caches_within_ttl():
    with patch("yfinance.Ticker") as mock_ticker, patch("time.sleep"):
        mock_ticker.return_value.fast_info = _mock_fx_fast_info(1.40)
        first = get_usd_cad_rate()
        mock_ticker.return_value.fast_info = _mock_fx_fast_info(1.90)  # would differ if re-fetched
        second = get_usd_cad_rate()
    assert first == second == 1.40   # second call served from cache, not re-fetched


# ── StockPaperExecutor: mixed-currency total_value / check_exposure ─────────

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_mod, "_STATE_JSON", str(tmp_path / "state.json"))
    monkeypatch.setattr(paper_mod, "_TRADES_CSV", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(paper_mod, "_RESET_FLAG", str(tmp_path / ".reset"))
    monkeypatch.setattr(paper_mod, "_SETTLEMENT_CSV", str(tmp_path / "settlement.csv"))
    price_feed._sector_cache["RY"] = "financial services"
    price_feed._sector_cache["CM.TO"] = "financial services"
    price_feed._sector_cache["TD"] = "financial services"
    price_feed._sector_cache["KO"] = "consumer defensive"
    return tmp_path


def test_total_value_converts_usd_position_to_cad(sandbox):
    # $1,100 CAD cash (headroom above the 10-share cost + slippage) + 10
    # shares of a USD-listed stock @ $100 USD. At a 1.35 USD->CAD rate that
    # position is worth $1,350 CAD, not $1,000.
    with patch("stock_bot.execution.paper.get_usd_cad_rate", return_value=1.35):
        ex = StockPaperExecutor(starting_cash=1100.0)
        order = ex.buy("RY", 10, 100.0, reason="test")
        assert order.status.value == "FILLED"
        total = ex.total_value({"RY": 100.0})
    # total_value re-prices the position from the explicit price map (100.0),
    # converted through FX — independent of the exact slipped fill/cash math.
    assert total == round(ex.cash + 10 * 100.0 * 1.35, 2)


def test_total_value_leaves_cad_position_unconverted(sandbox):
    with patch("stock_bot.execution.paper.get_usd_cad_rate", return_value=1.35):
        ex = StockPaperExecutor(starting_cash=1000.0)
        order = ex.buy("CM.TO", 5, 100.0, reason="test")
        assert order.status.value == "FILLED"
        total = ex.total_value({"CM.TO": 100.0})
    assert total == round(ex.cash + 5 * 100.0, 2)   # no FX applied to a .TO symbol


def test_check_exposure_accounts_for_fx_on_usd_position(sandbox):
    # A USD position that looks like 25% of account at face value is really
    # ~34% once converted at 1.35 — exposure check must catch that.
    with patch("stock_bot.execution.paper.get_usd_cad_rate", return_value=1.35):
        ex = StockPaperExecutor(starting_cash=1000.0, max_exposure_pct=0.30)
        order = ex.buy("RY", 2, 125.0, reason="test")   # $250 face value = 25% of $1000
        assert order.status.value == "FILLED"
        under_cap = ex.check_exposure({"RY": 125.0})
    # True value: (2*125*1.35) / total_value — must exceed the 30% cap
    assert under_cap is False


# ── StockPaperExecutor: check_exposure projects the PENDING trade too ───────
# (added 2026-08-05, punch-list item #7 — closes the gap where a single
# large BUY could blow past the cap in one shot since the old check only
# looked at current state, not the trade about to happen.)

def test_check_exposure_pending_trade_value_defaults_to_current_state_only(sandbox):
    ex = StockPaperExecutor(starting_cash=1000.0, max_exposure_pct=0.25)
    # No positions open — current exposure is 0%, well under the cap.
    assert ex.check_exposure({}) is True
    # Omitting pending_trade_value preserves the old (pre-2026-08-05) behavior.
    assert ex.check_exposure({}, pending_trade_value=0.0) is True


def test_check_exposure_catches_a_single_oversized_buy():
    ex = StockPaperExecutor(starting_cash=1000.0, max_exposure_pct=0.25)
    # Nothing held yet, so a current-state-only check would pass (0% < 25%) —
    # but a $400 pending BUY would be 40% of the account, over the cap.
    assert ex.check_exposure({}) is True                                    # old behavior: allowed
    assert ex.check_exposure({}, pending_trade_value=400.0) is False        # projected: correctly blocked


def test_check_exposure_allows_pending_trade_that_stays_under_cap():
    ex = StockPaperExecutor(starting_cash=1000.0, max_exposure_pct=0.25)
    assert ex.check_exposure({}, pending_trade_value=200.0) is True   # 20% < 25%


# ── StockPaperExecutor: sector concentration gate (_MAX_PER_SECTOR = 2) ─────
# Previously untested even though the gate is live in the real buy() path
# (found 2026-08-05).

def test_paper_buy_rejected_when_sector_limit_reached(sandbox):
    with patch("stock_bot.execution.paper.get_usd_cad_rate", return_value=1.35):
        ex = StockPaperExecutor(starting_cash=100_000.0)
        assert ex.buy("RY", 10, 100.0, reason="test").status.value == "FILLED"
        assert ex.buy("CM.TO", 10, 100.0, reason="test").status.value == "FILLED"

        order = ex.buy("TD", 10, 80.0, reason="test")
    assert order.status.value == "REJECTED"
    assert "Sector limit" in order.reject_reason
    assert "financial services" in order.reject_reason


def test_paper_buy_allowed_adding_to_already_held_sector_symbol(sandbox):
    # The gate only blocks *new* positions in a full sector — topping up a
    # symbol already held must not be blocked by its own sector count.
    with patch("stock_bot.execution.paper.get_usd_cad_rate", return_value=1.35):
        ex = StockPaperExecutor(starting_cash=100_000.0)
        assert ex.buy("RY", 10, 100.0, reason="test").status.value == "FILLED"
        assert ex.buy("CM.TO", 10, 100.0, reason="test").status.value == "FILLED"

        order = ex.buy("RY", 5, 100.0, reason="test")
    assert order.status.value == "FILLED"


def test_paper_buy_allowed_different_sector_when_limit_reached(sandbox):
    with patch("stock_bot.execution.paper.get_usd_cad_rate", return_value=1.35):
        ex = StockPaperExecutor(starting_cash=100_000.0)
        assert ex.buy("RY", 10, 100.0, reason="test").status.value == "FILLED"
        assert ex.buy("CM.TO", 10, 100.0, reason="test").status.value == "FILLED"

        order = ex.buy("KO", 10, 60.0, reason="test")   # not a bank — different sector
    assert order.status.value == "FILLED"
