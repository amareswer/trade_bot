"""
Unit tests for stock_bot/risk/vix_crisis.py — the stock bot's VIX-based
crisis mode gate (added 2026-08-05, punch-list item #8).
"""
from stock_bot.risk.vix_crisis import is_vix_crisis


def test_vix_at_threshold_is_crisis():
    assert is_vix_crisis(35.0, 35.0) is True


def test_vix_above_threshold_is_crisis():
    assert is_vix_crisis(42.5, 35.0) is True


def test_vix_below_threshold_is_not_crisis():
    assert is_vix_crisis(18.0, 35.0) is False


def test_none_vix_fails_open():
    assert is_vix_crisis(None, 35.0) is False


def test_zero_threshold_disables_feature():
    assert is_vix_crisis(90.0, 0.0) is False


def test_negative_threshold_disables_feature():
    assert is_vix_crisis(90.0, -5.0) is False
