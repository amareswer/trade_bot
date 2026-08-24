"""Unit tests for CapitalPool."""
import pytest
from bot.portfolio.capital_pool import CapitalPool


def test_slot_cash_divides_evenly():
    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    assert pool.slot_cash == 100.0


def test_allocate_returns_slot_cash():
    pool = CapitalPool(200.0, 2)
    allocated = pool.allocate("BTC/CAD")
    assert allocated == 100.0
    assert pool.is_allocated("BTC/CAD")


def test_allocate_second_slot():
    pool = CapitalPool(200.0, 2)
    pool.allocate("BTC/CAD")
    allocated = pool.allocate("XRP/CAD")
    assert allocated == 100.0
    assert len(pool.allocated_symbols) == 2


def test_allocate_exhausted_returns_zero():
    pool = CapitalPool(200.0, 2)
    pool.allocate("BTC/CAD")
    pool.allocate("XRP/CAD")
    result = pool.allocate("ETH/CAD")
    assert result == 0.0


def test_can_open_position_blocks_when_full():
    pool = CapitalPool(200.0, 2)
    pool.allocate("BTC/CAD")
    pool.allocate("XRP/CAD")
    assert not pool.can_open_position("ETH/CAD")


def test_can_open_position_allows_existing_symbol():
    pool = CapitalPool(200.0, 2)
    pool.allocate("BTC/CAD")
    pool.allocate("XRP/CAD")
    # Symbol already holds a slot — adding to position is allowed
    assert pool.can_open_position("BTC/CAD")


def test_release_frees_slot():
    pool = CapitalPool(200.0, 2)
    pool.allocate("BTC/CAD")
    pool.allocate("XRP/CAD")
    pool.release("BTC/CAD", cash_returned=100.0)
    assert not pool.is_allocated("BTC/CAD")
    assert pool.free_slots == 1


def test_release_updates_total_on_profit():
    pool = CapitalPool(200.0, 2)
    pool.allocate("BTC/CAD")
    pool.release("BTC/CAD", cash_returned=110.0)   # +10 P&L
    assert pool.total_capital == pytest.approx(210.0)
    assert pool.slot_cash == pytest.approx(105.0)


def test_release_updates_total_on_loss():
    pool = CapitalPool(200.0, 2)
    pool.allocate("BTC/CAD")
    pool.release("BTC/CAD", cash_returned=90.0)    # -10 P&L
    assert pool.total_capital == pytest.approx(190.0)
    assert pool.slot_cash == pytest.approx(95.0)


def test_release_noop_when_not_allocated():
    pool = CapitalPool(200.0, 2)
    pool.release("BTC/CAD", 100.0)   # never allocated — should not raise
    assert pool.total_capital == 200.0


def test_double_allocate_is_idempotent():
    pool = CapitalPool(200.0, 2)
    first  = pool.allocate("BTC/CAD")
    second = pool.allocate("BTC/CAD")   # same symbol again
    assert first == second
    assert len(pool.allocated_symbols) == 1


def test_invalid_total_capital():
    with pytest.raises(ValueError):
        CapitalPool(total_capital=0.0, max_concurrent=2)


def test_invalid_max_concurrent():
    with pytest.raises(ValueError):
        CapitalPool(total_capital=100.0, max_concurrent=0)


def test_available_cash_decreases_on_allocate():
    pool = CapitalPool(200.0, 2)
    pool.allocate("BTC/CAD")
    assert pool.available_cash == pytest.approx(100.0)
    pool.allocate("XRP/CAD")
    assert pool.available_cash == pytest.approx(0.0)


# ── Slot cap tests ─────────────────────────────────────────────────────────

def test_slot_cap_limits_allocation():
    """pool=154, max_concurrent=1, cap=77 → slot=77 (not 154)."""
    pool = CapitalPool(total_capital=154.0, max_concurrent=1, slot_cap=77.0)
    assert pool.slot_cash == pytest.approx(77.0)


def test_slot_cap_zero_means_uncapped():
    pool = CapitalPool(total_capital=200.0, max_concurrent=2, slot_cap=0.0)
    assert pool.slot_cash == pytest.approx(100.0)


def test_slot_cap_larger_than_base_has_no_effect():
    """Cap higher than natural slot: natural slot wins."""
    pool = CapitalPool(total_capital=200.0, max_concurrent=2, slot_cap=200.0)
    assert pool.slot_cash == pytest.approx(100.0)


def test_slot_cap_property_readable():
    pool = CapitalPool(total_capital=200.0, max_concurrent=2, slot_cap=50.0)
    assert pool.slot_cap == pytest.approx(50.0)


def test_slot_cap_invalid_negative():
    with pytest.raises(ValueError):
        CapitalPool(total_capital=100.0, max_concurrent=1, slot_cap=-1.0)


# ── Per-symbol slot cap tests (added 2026-08-24) ────────────────────────────

def test_slot_cash_for_no_override_matches_shared_slot_cash():
    """Backward compat: a symbol with no per-symbol entry falls straight
    through to the original slot_cash computation — numerically identical."""
    pool = CapitalPool(total_capital=154.0, max_concurrent=1, slot_cap=77.0)
    assert pool.slot_cash_for("BTC/CAD") == pytest.approx(pool.slot_cash) == pytest.approx(77.0)


def test_slot_cash_for_single_symbol_dict_matches_old_single_shared_cap():
    """The exact live production shape (BTC/CAD alone, cap=77) expressed via
    slot_caps instead of the shared slot_cap must give the identical number."""
    pool = CapitalPool(total_capital=154.0, max_concurrent=1, slot_caps={"BTC/CAD": 77.0})
    assert pool.slot_cash_for("BTC/CAD") == pytest.approx(77.0)


def test_slot_caps_untouched_symbol_falls_back_to_shared_default():
    """A symbol absent from slot_caps still uses the shared slot_cap fallback,
    not left uncapped, when both are configured together."""
    pool = CapitalPool(
        total_capital=300.0, max_concurrent=2, slot_cap=50.0,
        slot_caps={"SOL/CAD": 100.0},
    )
    assert pool.slot_cash_for("SOL/CAD") == pytest.approx(100.0)
    assert pool.slot_cash_for("BTC/CAD") == pytest.approx(50.0)   # shared fallback, not 100


def test_two_symbols_different_caps_both_fit():
    """Enough total capital to satisfy both caps in full: each symbol gets
    its own configured cap, not an equal split, and the surplus stays idle."""
    pool = CapitalPool(
        total_capital=300.0, max_concurrent=2,
        slot_caps={"BTC/CAD": 77.0, "SOL/CAD": 100.0},
    )
    btc = pool.allocate("BTC/CAD")
    sol = pool.allocate("SOL/CAD")
    assert btc == pytest.approx(77.0)
    assert sol == pytest.approx(100.0)
    assert pool.available_cash == pytest.approx(300.0 - 77.0 - 100.0)  # 123 idle, not force-split


def test_insufficient_total_to_fill_all_caps():
    """Sum of caps (177) exceeds total capital (150): the first-allocated
    symbol gets its full cap, the second gets whatever's left, not its cap."""
    pool = CapitalPool(
        total_capital=150.0, max_concurrent=2,
        slot_caps={"BTC/CAD": 77.0, "SOL/CAD": 100.0},
    )
    btc = pool.allocate("BTC/CAD")
    sol = pool.allocate("SOL/CAD")
    assert btc == pytest.approx(77.0)
    assert sol == pytest.approx(73.0)          # 150 - 77, not the full 100 cap
    assert pool.available_cash == pytest.approx(0.0)


def test_slot_cash_for_pre_allocation_estimate_ignores_other_symbols():
    """Before either symbol has actually allocated, slot_cash_for() for each
    is computed independently (remaining = full total) — an estimate, not a
    reservation. Documents the order-dependence: this is why real commitment
    only happens through allocate() itself, not by calling slot_cash_for()
    for a symbol that hasn't bought yet."""
    pool = CapitalPool(
        total_capital=150.0, max_concurrent=2,
        slot_caps={"BTC/CAD": 77.0, "SOL/CAD": 100.0},
    )
    assert pool.slot_cash_for("BTC/CAD") == pytest.approx(77.0)
    assert pool.slot_cash_for("SOL/CAD") == pytest.approx(100.0)   # not yet reduced by BTC


def test_slot_caps_zero_means_uncapped_for_that_symbol():
    pool = CapitalPool(
        total_capital=200.0, max_concurrent=2,
        slot_caps={"BTC/CAD": 0.0},
    )
    assert pool.slot_cash_for("BTC/CAD") == pytest.approx(200.0)


def test_slot_caps_property_readable():
    pool = CapitalPool(total_capital=200.0, max_concurrent=2, slot_caps={"BTC/CAD": 77.0})
    assert pool.slot_caps == {"BTC/CAD": 77.0}


def test_slot_caps_invalid_negative():
    with pytest.raises(ValueError):
        CapitalPool(total_capital=100.0, max_concurrent=2, slot_caps={"BTC/CAD": -5.0})


def test_release_then_reallocate_with_per_symbol_cap():
    """Releasing a slot frees room for the next allocate() to use the full
    remaining pool again — per-symbol caps don't break the existing
    release()/reallocate() cycle."""
    pool = CapitalPool(
        total_capital=150.0, max_concurrent=2,
        slot_caps={"BTC/CAD": 77.0, "SOL/CAD": 100.0},
    )
    pool.allocate("BTC/CAD")
    sol_first = pool.allocate("SOL/CAD")
    assert sol_first == pytest.approx(73.0)
    pool.release("BTC/CAD", cash_returned=77.0)   # flat P&L
    sol_slot_now = pool.slot_cash_for("SOL/CAD")
    # SOL already holds its slot — allocate() would just return the existing
    # amount, but slot_cash_for() reports what a *fresh* allocation would be
    # worth right now (BTC's slot is free again).
    assert sol_slot_now == pytest.approx(100.0)
