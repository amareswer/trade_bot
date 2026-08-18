"""
Wiring test for the VIX crisis-mode gate in stock_bot.main.run() (added
2026-08-05, punch-list item #8). The pure threshold logic itself is already
covered by test_stock_vix_crisis.py — this only proves the scan loop still
fetches VIX, computes crisis mode, and gates new BUYs on it (source
inspection, same pattern as the correlation/macro-blackout wiring guards —
run() needs a live yfinance/IBKR/screener/dashboard stack to execute
directly).
"""
import inspect

import stock_bot.main as main_mod


def test_run_fetches_vix_and_computes_crisis_mode():
    source = inspect.getsource(main_mod.run)
    assert '"^VIX"' in source
    assert "is_vix_crisis(_vix_now, cfg.vix_crisis_threshold)" in source


def test_run_gates_buys_on_crisis_mode():
    source = inspect.getsource(main_mod.run)
    assert "if _cycle_crisis_mode:" in source
    assert "_regime_ok = False" in source   # crisis mode reuses the same BUY gate as the regime filter
