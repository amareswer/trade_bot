"""
Tests for bot/dashboard/renderer.py — first-ever coverage for this module,
added 2026-08-26 alongside the multi-symbol combine (SOL/CAD joining
BTC/CAD live left SOL with zero dashboard visibility; dashboard.html was
hardcoded to render only _active_symbol). Focuses on the new multi-symbol
behavior — the per-symbol HTML fragment logic itself (position-protection
math, fee/PnL formatting, table rendering) was already exercised
end-to-end by every assertion below, just not previously as unit tests.
"""
from __future__ import annotations

from bot.dashboard import renderer


def _sym(symbol: str, **overrides) -> dict:
    """Minimal valid per-symbol dict for write_multi(), with overrides."""
    base = {
        "symbol": symbol, "price": 100.0, "signal": "HOLD", "rsi": 50.0,
        "trend": "BULLISH", "state": "IDLE", "cooldown": 0, "last_trade": "—",
        "cash": 100.0, "position": 0.0, "avg_entry": 0.0, "unrealized_pnl": 0.0,
        "realized_pnl": 0.0, "total_value": 100.0, "fills": [],
        "tick_log": [], "candle_log": [],
        "stop_loss_pct": 0.015, "take_profit_pct": 0.10, "fees_paid": 0.0,
        "rsi_filter_enabled": True, "volume_k": 0.0,
    }
    base.update(overrides)
    return base


def test_write_multi_renders_both_symbols_on_one_page(tmp_path):
    path = tmp_path / "dashboard.html"
    renderer.write_multi(
        path=str(path), exchange="kraken", strategy="indicator", tick=1,
        symbols=[_sym("BTC/CAD"), _sym("SOL/CAD")],
    )
    html = path.read_text()
    assert "BTC/CAD" in html
    assert "SOL/CAD" in html
    # One shared page shell, not two separate documents
    assert html.count("<!DOCTYPE html>") == 1
    assert html.count("<style>") == 1
    assert html.count("</html>") == 1


def test_write_multi_single_symbol_still_works(tmp_path):
    path = tmp_path / "dashboard.html"
    renderer.write_multi(
        path=str(path), exchange="kraken", strategy="indicator", tick=1,
        symbols=[_sym("BTC/CAD")],
    )
    html = path.read_text()
    assert "BTC/CAD" in html
    assert "SOL/CAD" not in html


def test_write_multi_symbol_order_preserved(tmp_path):
    """Callers control ordering (documented in write_multi()'s docstring) —
    first in the list renders first on the page."""
    path = tmp_path / "dashboard.html"
    renderer.write_multi(
        path=str(path), exchange="kraken", strategy="indicator", tick=1,
        symbols=[_sym("SOL/CAD"), _sym("BTC/CAD")],
    )
    html = path.read_text()
    assert html.index("SOL/CAD") < html.index("BTC/CAD")


def test_write_multi_position_panel_only_for_the_symbol_holding_one(tmp_path):
    """Two symbols on one page, only one with a position — the protection
    panel must appear for that symbol without leaking into the flat one's
    section (this was the exact class of cross-contamination risk this
    refactor had to avoid, since it stacks per-symbol HTML fragments)."""
    path = tmp_path / "dashboard.html"
    renderer.write_multi(
        path=str(path), exchange="kraken", strategy="indicator", tick=1,
        symbols=[
            _sym("BTC/CAD"),  # flat
            _sym("SOL/CAD", position=0.08, avg_entry=134.02, price=134.08),
        ],
    )
    html = path.read_text()
    assert html.count("Open Position — Protection Levels") == 1
    # The panel's content must land in SOL's block, not BTC's — check it
    # appears strictly after the SOL/CAD symbol-header marker.
    sol_header_idx = html.index('class="symbol-block"', html.index("SOL/CAD"))
    panel_idx      = html.index("Open Position — Protection Levels")
    assert panel_idx > sol_header_idx


def test_write_multi_no_position_no_protection_panel(tmp_path):
    path = tmp_path / "dashboard.html"
    renderer.write_multi(
        path=str(path), exchange="kraken", strategy="indicator", tick=1,
        symbols=[_sym("BTC/CAD")],  # position=0.0 default
    )
    html = path.read_text()
    assert "Open Position — Protection Levels" not in html


def test_write_multi_fills_and_pnl_render_for_correct_symbol(tmp_path):
    path = tmp_path / "dashboard.html"
    renderer.write_multi(
        path=str(path), exchange="kraken", strategy="indicator", tick=1,
        symbols=[
            _sym("BTC/CAD"),
            _sym("SOL/CAD", fills=[
                {"time": "16:00:36", "side": "BUY", "qty": 0.080808,
                 "price": 134.02, "total": 10.83, "pnl": None},
            ], fees_paid=0.0866),
        ],
    )
    html = path.read_text()
    assert "134.02" in html
    assert "0.0866" in html or "-$0.0866" in html


def test_write_single_symbol_wrapper_matches_write_multi(tmp_path):
    """write() (single-symbol convenience wrapper) must produce equivalent
    content to write_multi() with a one-element list — not a second,
    diverging code path."""
    path_a = tmp_path / "a.html"
    path_b = tmp_path / "b.html"
    kw = _sym("BTC/CAD", price=90000.0, cash=77.0)

    renderer.write(
        path=str(path_a), exchange="kraken", symbol=kw["symbol"],
        strategy="indicator", tick=5, price=kw["price"], signal=kw["signal"],
        rsi=kw["rsi"], trend=kw["trend"], state=kw["state"],
        cooldown=kw["cooldown"], last_trade=kw["last_trade"], cash=kw["cash"],
        position=kw["position"], avg_entry=kw["avg_entry"],
        unrealized_pnl=kw["unrealized_pnl"], realized_pnl=kw["realized_pnl"],
        total_value=kw["total_value"], fills=kw["fills"],
        tick_log=kw["tick_log"], candle_log=kw["candle_log"],
    )
    renderer.write_multi(
        path=str(path_b), exchange="kraken", strategy="indicator", tick=5,
        symbols=[kw],
    )
    html_a = path_a.read_text()
    html_b = path_b.read_text()
    assert "90,000.00" in html_a or "90000.00" in html_a
    assert "$77.00" in html_a
    # Both paths render the same symbol content — same key figures present
    for needle in ("BTC/CAD", "$77.00"):
        assert needle in html_a and needle in html_b


def test_write_multi_creates_parent_directory(tmp_path):
    nested = tmp_path / "nested" / "dir" / "dashboard.html"
    renderer.write_multi(
        path=str(nested), exchange="kraken", strategy="indicator", tick=1,
        symbols=[_sym("BTC/CAD")],
    )
    assert nested.exists()
