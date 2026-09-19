"""
Tests for bot/strategy/fingerprint.py.

2026-09-18 review finding (P1-9): the strategy fingerprint hashes only
entry/exit signal logic — two runs sharing that hash can still trade
differently if execution assumptions (order type, ATR sizing, risk-gate
thresholds) differ. compute_execution_hash()/compute_full_run_fingerprint()
are new, additive fingerprints; compute_strategy_hash() itself is untouched
(same return value, format, and callers as before this addition).
"""
from __future__ import annotations

from bot.strategy.fingerprint import (
    compute_execution_hash,
    compute_full_run_fingerprint,
    compute_strategy_hash,
    execution_hashed_files,
    hashed_files,
)


def test_strategy_hash_unaffected_by_this_change():
    """compute_strategy_hash() must remain exactly as before — same file
    list, same 16-hex-char format, deterministic."""
    h1 = compute_strategy_hash()
    h2 = compute_strategy_hash()
    assert h1 == h2
    assert len(h1) == 16
    assert hashed_files() == (
        "bot/strategy/indicator_strategy.py",
        "bot/strategy/threshold_strategy.py",
        "bot/indicators/indicators.py",
    )


def test_execution_hash_is_deterministic():
    h1 = compute_execution_hash()
    h2 = compute_execution_hash()
    assert h1 == h2
    assert len(h1) == 16


def test_execution_hash_differs_from_strategy_hash():
    """These fingerprint two different layers — they must not collide."""
    assert compute_execution_hash() != compute_strategy_hash()


def test_execution_hashed_files_list():
    assert execution_hashed_files() == (
        "bot/execution/live_executor.py",
        "bot/portfolio/capital_pool.py",
        "bot/risk/risk_manager.py",
    )


def test_execution_hash_changes_with_config_snapshot():
    """The whole point: two runs with identical execution CODE but
    different config (order type, sizing mode, risk thresholds) must
    fingerprint differently."""
    h_a = compute_execution_hash({"order_type": "limit", "atr_sizing_enabled": True})
    h_b = compute_execution_hash({"order_type": "market", "atr_sizing_enabled": True})
    assert h_a != h_b


def test_execution_hash_config_snapshot_order_independent():
    h_a = compute_execution_hash({"a": 1, "b": 2})
    h_b = compute_execution_hash({"b": 2, "a": 1})
    assert h_a == h_b


def test_execution_hash_no_snapshot_vs_empty_snapshot_equivalent():
    assert compute_execution_hash() == compute_execution_hash({})


def test_full_run_fingerprint_combines_both_layers():
    full = compute_full_run_fingerprint()
    assert len(full) == 16
    # Changing the config snapshot must change the full fingerprint even
    # though the strategy hash itself is untouched.
    full_a = compute_full_run_fingerprint({"order_type": "limit"})
    full_b = compute_full_run_fingerprint({"order_type": "market"})
    assert full_a != full_b


def test_full_run_fingerprint_deterministic():
    assert compute_full_run_fingerprint({"x": 1}) == compute_full_run_fingerprint({"x": 1})
