"""Unit tests for PaperExecutor — no network, no exchange."""

from bot.execution.executor import PaperExecutor, OrderStatus
from bot.strategy.threshold_strategy import Signal

def test_buy_fills_and_deducts_cash():
    ex = PaperExecutor("BTC/USDT", quantity=1.0, starting_cash=100_000)
    order = ex.execute(Signal.BUY, price=74_000)
    assert order.status == OrderStatus.FILLED
    assert ex.position == 1.0
    assert ex.cash == 100_000 - 74_000

def test_sell_fills_and_credits_cash():
    ex = PaperExecutor("BTC/USDT", quantity=1.0, starting_cash=100_000)
    ex.execute(Signal.BUY,  price=70_000)
    order = ex.execute(Signal.SELL, price=74_000)
    assert order.status == OrderStatus.FILLED
    assert ex.position == 0.0
    assert ex.cash == 100_000 + 4_000   # bought 70k, sold 74k
    assert round(ex._portfolio.realized_pnl, 2) == 4_000.0

def test_buy_rejected_when_insufficient_cash():
    ex = PaperExecutor("BTC/USDT", quantity=1.0, starting_cash=1_000)
    order = ex.execute(Signal.BUY, price=74_000)
    assert order.status == OrderStatus.REJECTED
    assert ex.position == 0.0
    assert ex.cash == 1_000             # cash unchanged

def test_sell_rejected_when_no_position():
    ex = PaperExecutor("BTC/USDT", quantity=1.0, starting_cash=100_000)
    order = ex.execute(Signal.SELL, price=74_000)
    assert order.status == OrderStatus.REJECTED
    assert ex.cash == 100_000           # cash unchanged

def test_hold_creates_no_order():
    ex = PaperExecutor("BTC/USDT", quantity=1.0, starting_cash=100_000)
    result = ex.execute(Signal.HOLD, price=74_000)
    assert result is None
    assert len(ex.orders) == 0

def test_order_history_tracked():
    ex = PaperExecutor("BTC/USDT", quantity=0.5, starting_cash=100_000)
    ex.execute(Signal.BUY,  price=70_000)
    ex.execute(Signal.SELL, price=72_000)
    ex.execute(Signal.BUY,  price=74_000)
    assert len(ex.orders) == 3
    assert len(ex.filled_orders()) == 3

if __name__ == "__main__":
    tests = [
        test_buy_fills_and_deducts_cash,
        test_sell_fills_and_credits_cash,
        test_buy_rejected_when_insufficient_cash,
        test_sell_rejected_when_no_position,
        test_hold_creates_no_order,
        test_order_history_tracked,
    ]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
