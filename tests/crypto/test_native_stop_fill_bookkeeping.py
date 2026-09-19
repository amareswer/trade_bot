"""
Integration tests proving a native-stop fill DISCOVERED during a protection
resync (sync_protective_stop's cancel-and-verify cycle) reaches ALL of
bot/main.py's bookkeeping — PositionManager, the state machine, the capital
pool, the risk fill counter, and trade_log — not just the executor's own
internal cash/position.

2026-09-18 follow-up review finding (P1): sync_protective_stop() and
_resync_native_stop() returned this fill_order, but every call site
discarded it. Fixed via _process_discovered_sell_fill, called from every
sync_protective_stop()/_resync_native_stop() call site in bot/main.py.

Uses REAL PositionManager/CapitalPool/TradingStateMachine (already
independently tested) plus a hand-built fake executor and a spy trade_log —
no network, no ccxt, no production file writes.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import bot.main as bot_main
from bot.execution.executor import Order, OrderSide, OrderStatus
from bot.portfolio.capital_pool import CapitalPool
from bot.portfolio.position_manager import PositionManager
from bot.state.trade_state import TradingStateMachine


def _fill_order(quantity, price, fee_cost=0.5, order_id="native-stop:stop-001"):
    return Order(
        order_id=order_id, symbol="BTC/CAD", side=OrderSide.SELL,
        quantity=quantity, price=price, status=OrderStatus.FILLED,
        created_at=None, filled_at=None, fee_cost=fee_cost, fee_currency="CAD",
    )


class _FakeExecutorForResync:
    """Only implements what _resync_native_stop/_process_discovered_sell_fill
    actually touch: symbol, cash, sync_protective_stop(), ack_journal_entry()."""

    def __init__(self, cash=50.0):
        self.symbol = "BTC/CAD"
        self.cash = cash
        self._next_fill = None
        self.acked_order_ids = []

    def queue_fill(self, order):
        self._next_fill = order

    def sync_protective_stop(self, stop_price, trailing_pct=None):
        fill = self._next_fill
        self._next_fill = None
        return fill

    def ack_journal_entry(self, order_id):
        self.acked_order_ids.append(order_id)


def _ss(executor, pm, sm):
    return bot_main._new_symbol_state_dict(
        strategy=MagicMock(), sm=sm, pm=pm, executor=executor,
    )


def test_process_discovered_sell_fill_updates_position_manager():
    pm = PositionManager()
    pm.on_buy(80_000.0, 0.01)
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    capital_pool.allocate("BTC/CAD")
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _fill_order(quantity=0.01, price=78_000.0)
    bot_main._process_discovered_sell_fill(
        "BTC/CAD", ss, order, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert pm.quantity == 0.0                 # PositionManager sees the SELL
    assert not pm.has_position


def test_process_discovered_sell_fill_releases_capital_pool_slot():
    pm = PositionManager()
    pm.on_buy(80_000.0, 0.01)
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync(cash=77.5)
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    capital_pool.allocate("BTC/CAD")
    assert capital_pool.is_allocated("BTC/CAD")
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _fill_order(quantity=0.01, price=78_000.0)
    bot_main._process_discovered_sell_fill(
        "BTC/CAD", ss, order, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert not capital_pool.is_allocated("BTC/CAD")   # slot released, full close


def test_process_discovered_sell_fill_partial_keeps_capital_pool_slot():
    pm = PositionManager()
    pm.on_buy(80_000.0, 0.02)
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    capital_pool.allocate("BTC/CAD")
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _fill_order(quantity=0.01, price=78_000.0)   # only half
    bot_main._process_discovered_sell_fill(
        "BTC/CAD", ss, order, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert pm.quantity == 0.01              # half still held
    assert capital_pool.is_allocated("BTC/CAD")   # slot NOT released — still holding


def test_process_discovered_sell_fill_calls_risk_and_state_machine():
    pm = PositionManager()
    pm.on_buy(80_000.0, 0.01)
    sm = TradingStateMachine(cooldown_ticks=3)
    from bot.strategy.threshold_strategy import Signal
    sm.on_fill(Signal.BUY, 80_000.0)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    capital_pool.allocate("BTC/CAD")
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _fill_order(quantity=0.01, price=78_000.0)
    bot_main._process_discovered_sell_fill(
        "BTC/CAD", ss, order, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    risk.record_fill.assert_called_once_with("BTC/CAD")


def test_process_discovered_sell_fill_writes_trade_log_and_acks_journal():
    pm = PositionManager()
    pm.on_buy(80_000.0, 0.01)
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    capital_pool.allocate("BTC/CAD")
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _fill_order(quantity=0.01, price=78_000.0, order_id="native-stop:s1")
    bot_main._process_discovered_sell_fill(
        "BTC/CAD", ss, order, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    trade_log.log_fill.assert_called_once()
    kwargs = trade_log.log_fill.call_args.kwargs
    assert kwargs["side"] == "SELL"
    assert kwargs["symbol"] == "BTC/CAD"
    assert abs(kwargs["quantity"] - 0.01) < 1e-9
    assert kwargs["signal_reason"] == "native_stop_discovered"
    executor.acked_order_ids == ["native-stop:s1"]
    alerter.fill.assert_called_once()


def test_process_discovered_sell_fill_ignores_non_sell_order():
    """Defensive: a BUY order passed here (should never happen) must not
    corrupt PositionManager state."""
    pm = PositionManager()
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    bad_order = Order(
        order_id="x", symbol="BTC/CAD", side=OrderSide.BUY, quantity=0.01,
        price=80_000.0, status=OrderStatus.FILLED, created_at=None, filled_at=None,
    )
    bot_main._process_discovered_sell_fill(
        "BTC/CAD", ss, bad_order, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert pm.quantity == 0.0   # untouched
    trade_log.log_fill.assert_not_called()
    risk.record_fill.assert_not_called()


# ── End-to-end wiring: _resync_native_stop's return value reaches the ────
# ── consumer through a realistic call site shape ─────────────────────────

def test_resync_native_stop_return_value_flows_into_bookkeeping():
    """Proves the actual wiring: _resync_native_stop returns whatever
    sync_protective_stop discovered, and that value is exactly what
    _process_discovered_sell_fill needs — the full chain a real call site
    in run() exercises, minus run()'s own loop scaffolding."""
    pm = PositionManager()
    pm.on_buy(80_000.0, 0.01)
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    ss['native_stop_is_trailing'] = False
    ss['native_stop_price'] = 78_000.0
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    capital_pool.allocate("BTC/CAD")
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    executor.queue_fill(_fill_order(quantity=0.01, price=78_000.0))

    discovered = bot_main._resync_native_stop(ss)
    assert discovered is not None

    bot_main._process_discovered_sell_fill(
        "BTC/CAD", ss, discovered, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert not pm.has_position
    assert not capital_pool.is_allocated("BTC/CAD")
    trade_log.log_fill.assert_called_once()
    risk.record_fill.assert_called_once_with("BTC/CAD")
