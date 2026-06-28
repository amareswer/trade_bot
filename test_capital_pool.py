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
