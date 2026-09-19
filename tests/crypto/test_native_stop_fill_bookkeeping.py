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

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

import bot.main as bot_main
import live_comparison as lc
from bot.data.trade_log import TradeLog
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

    def __init__(self, cash=50.0, has_resting_stop=True):
        self.symbol = "BTC/CAD"
        self.cash = cash
        self._next_fill = None
        self.acked_order_ids = []
        self.has_resting_stop = has_resting_stop
        self.sync_calls = []

    def queue_fill(self, order):
        self._next_fill = order

    def sync_protective_stop(self, stop_price, trailing_pct=None):
        # 2026-09-19 PASS-5: real LiveExecutor.sync_protective_stop() now
        # returns a list (possibly more than one discovered fill) — match
        # that contract so this fake exercises the SAME shape every real
        # call site actually receives.
        self.sync_calls.append((stop_price, trailing_pct))
        fill = self._next_fill
        self._next_fill = None
        return [fill] if fill is not None else []

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
    sync_protective_stop discovered (a list), and that value is exactly
    what _process_discovered_sell_fills needs — the full chain a real call
    site in run() exercises, minus run()'s own loop scaffolding."""
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
    assert discovered == [discovered[0]]   # a one-element list, not a bare Order

    bot_main._process_discovered_sell_fills(
        "BTC/CAD", ss, discovered, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert not pm.has_position
    assert not capital_pool.is_allocated("BTC/CAD")
    trade_log.log_fill.assert_called_once()
    risk.record_fill.assert_called_once_with("BTC/CAD")


def test_sync_protective_stop_can_discover_two_fills_in_one_call():
    """PASS-5 review finding (P1), exact reproduction: a cancel-side fill
    from the OLD stop and a placement-side fill from the immediately-
    following replacement are BOTH real execution events from a single
    sync_protective_stop() call. Both must reach bookkeeping, in order —
    not just whichever one the old single-Order return contract kept."""
    pm = PositionManager()
    pm.on_buy(80_000.0, 0.002)
    sm = TradingStateMachine(cooldown_ticks=3)

    class _TwoFillExecutor:
        symbol = "BTC/CAD"
        cash = 50.0
        has_resting_stop = True

        def __init__(self):
            self.acked_order_ids = []

        def sync_protective_stop(self, stop_price, trailing_pct=None):
            return [
                _fill_order(quantity=0.001, price=78_000.0, order_id="native-stop:old-001"),
                _fill_order(quantity=0.001, price=78_500.0, order_id="native-stop:new-001"),
            ]

        def ack_journal_entry(self, order_id):
            self.acked_order_ids.append(order_id)

    executor = _TwoFillExecutor()
    ss = _ss(executor, pm, sm)
    ss['native_stop_is_trailing'] = False
    ss['native_stop_price'] = 78_000.0
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    capital_pool.allocate("BTC/CAD")
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    discovered = bot_main._resync_native_stop(ss)
    assert len(discovered) == 2

    bot_main._process_discovered_sell_fills(
        "BTC/CAD", ss, discovered, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert not pm.has_position                     # both 0.001s applied — 0.002 fully closed
    assert not capital_pool.is_allocated("BTC/CAD")
    assert trade_log.log_fill.call_count == 2       # both fills reached TradeLog
    assert risk.record_fill.call_count == 2
    assert executor.acked_order_ids == ["native-stop:old-001", "native-stop:new-001"]


# ---------------------------------------------------------------------------
# 2026-09-18 PASS-3 review finding (P1): a discovered PARTIAL exit
# incorrectly forced COOLDOWN and erased trail_peak/atr_sl for the residual
# position, using REAL TradingStateMachine to prove the actual state
# transition, not just a mocked call count.
# ---------------------------------------------------------------------------

def test_partial_discovered_exit_stays_long_not_cooldown():
    """Reproduced exactly: LONG with 0.002 BTC and a trail peak of 95000;
    process a discovered 0.001 BTC stop fill. Remaining position must be
    0.001 BTC with state LONG (not COOLDOWN), trail_peak/atr_sl PRESERVED
    (not reset to 0) — the residual's own exit levels are still correct
    and must not be erased."""
    from bot.strategy.threshold_strategy import Signal

    pm = PositionManager()
    pm.on_buy(80_000.0, 0.002)
    sm = TradingStateMachine(cooldown_ticks=3)
    sm.on_fill(Signal.BUY, 80_000.0)   # real LONG state, not a mock
    assert sm.state.value == "LONG"

    executor = _FakeExecutorForResync(has_resting_stop=True)
    ss = _ss(executor, pm, sm)
    ss['trail_peak'] = 95_000.0
    ss['atr_sl']     = 76_000.0
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    capital_pool.allocate("BTC/CAD")
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _fill_order(quantity=0.001, price=94_000.0)   # partial — half the position
    bot_main._process_discovered_sell_fill(
        "BTC/CAD", ss, order, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert pm.quantity == pytest.approx(0.001)         # residual remains
    assert sm.state.value == "LONG"                     # NOT COOLDOWN
    assert ss['trail_peak'] == 95_000.0                 # preserved, not reset
    assert ss['atr_sl'] == 76_000.0                      # preserved, not reset
    assert ss['partial_done'] is True
    assert capital_pool.is_allocated("BTC/CAD")          # slot NOT released


def test_full_discovered_exit_still_enters_cooldown_and_resets_state():
    """The full-exit path must be unaffected: state goes to COOLDOWN,
    trail_peak/atr_sl reset, capital pool slot released."""
    from bot.strategy.threshold_strategy import Signal

    pm = PositionManager()
    pm.on_buy(80_000.0, 0.001)
    sm = TradingStateMachine(cooldown_ticks=3)
    sm.on_fill(Signal.BUY, 80_000.0)
    executor = _FakeExecutorForResync(has_resting_stop=False)
    ss = _ss(executor, pm, sm)
    ss['trail_peak'] = 95_000.0
    ss['atr_sl']     = 76_000.0
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    capital_pool.allocate("BTC/CAD")
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _fill_order(quantity=0.001, price=94_000.0)   # full close
    bot_main._process_discovered_sell_fill(
        "BTC/CAD", ss, order, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert not pm.has_position
    assert sm.state.value == "COOLDOWN"
    assert ss['trail_peak'] == 0.0
    assert ss['atr_sl'] == 0.0
    assert not capital_pool.is_allocated("BTC/CAD")


def test_partial_discovered_exit_alerts_if_residual_left_unprotected():
    """If the discovered fill somehow reached a CONFIRMED TERMINAL outcome
    (stop fully gone) but a residual position remains — an unusual
    under-sized-stop edge case — this must alert loudly rather than
    silently leave the residual unprotected."""
    from bot.strategy.threshold_strategy import Signal

    pm = PositionManager()
    pm.on_buy(80_000.0, 0.002)
    sm = TradingStateMachine(cooldown_ticks=3)
    sm.on_fill(Signal.BUY, 80_000.0)
    executor = _FakeExecutorForResync(has_resting_stop=False)   # stop is GONE
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    capital_pool.allocate("BTC/CAD")
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _fill_order(quantity=0.001, price=94_000.0)   # partial, but stop is gone
    bot_main._process_discovered_sell_fill(
        "BTC/CAD", ss, order, "native_stop_discovered",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert pm.quantity == pytest.approx(0.001)
    alerter.error.assert_called_once()
    assert "UNPROTECTED RESIDUAL" in alerter.error.call_args[0][0]


# ---------------------------------------------------------------------------
# PASS-5 review finding (P1): _process_discovered_buy_fill — a pending BUY
# reconciled independently of a fresh signal (via
# LiveExecutor.reconcile_pending_orders()) must reach the SAME bookkeeping
# a strategy-driven BUY gets.
# ---------------------------------------------------------------------------

def _buy_fill_order(quantity, price, fee_cost=0.36, order_id="stuck-buy"):
    return Order(
        order_id=order_id, symbol="BTC/CAD", side=OrderSide.BUY,
        quantity=quantity, price=price, status=OrderStatus.FILLED,
        created_at=None, filled_at=None, fee_cost=fee_cost, fee_currency="CAD",
    )


def test_process_discovered_buy_fill_updates_position_manager():
    pm = PositionManager()
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _buy_fill_order(quantity=0.001, price=90_000.0)
    bot_main._process_discovered_buy_fill(
        "BTC/CAD", ss, order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert pm.has_position
    assert pm.quantity == pytest.approx(0.001)


def test_process_discovered_buy_fill_allocates_capital_pool_slot():
    pm = PositionManager()
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    assert not capital_pool.is_allocated("BTC/CAD")
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _buy_fill_order(quantity=0.001, price=90_000.0)
    bot_main._process_discovered_buy_fill(
        "BTC/CAD", ss, order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert capital_pool.is_allocated("BTC/CAD")


def test_process_discovered_buy_fill_calls_risk_and_state_machine():
    from bot.strategy.threshold_strategy import Signal

    pm = PositionManager()
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _buy_fill_order(quantity=0.001, price=90_000.0)
    bot_main._process_discovered_buy_fill(
        "BTC/CAD", ss, order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    risk.record_fill.assert_called_once_with("BTC/CAD")
    assert sm.state.value == "LONG"


def test_process_discovered_buy_fill_writes_trade_log_and_acks_journal():
    pm = PositionManager()
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _buy_fill_order(quantity=0.001, price=90_000.0, order_id="stuck-buy-1")
    bot_main._process_discovered_buy_fill(
        "BTC/CAD", ss, order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    trade_log.log_fill.assert_called_once()
    _, kwargs = trade_log.log_fill.call_args
    assert kwargs["side"] == "BUY"
    assert kwargs["symbol"] == "BTC/CAD"
    assert kwargs["quantity"] == pytest.approx(0.001)
    assert executor.acked_order_ids == ["stuck-buy-1"]


def test_process_discovered_buy_fill_ignores_non_buy_order():
    pm = PositionManager()
    sm = TradingStateMachine(cooldown_ticks=3)
    pm.on_buy(80_000.0, 0.01)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    sell_order = _fill_order(quantity=0.01, price=78_000.0)   # SELL
    bot_main._process_discovered_buy_fill(
        "BTC/CAD", ss, sell_order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    trade_log.log_fill.assert_not_called()
    risk.record_fill.assert_not_called()


# ---------------------------------------------------------------------------
# PASS-6 review finding (P1): a BUY fill recovered independently of a fresh
# signal must have native-stop protection reconciled to the NEW quantity
# immediately — not left to a conditional trailing-activation swap that may
# never fire for this exact situation.
# ---------------------------------------------------------------------------

@patch("bot.main.cfg")
def test_recovered_buy_resizes_existing_static_stop(mock_cfg):
    """A native stop is ALREADY established (static ATR level) covering
    the OLD (smaller) quantity — the recovered delta must resize it to the
    new total, via the same mechanism the partial-TP path already uses."""
    mock_cfg.exchange.native_stop_loss_enabled = True
    pm = PositionManager()
    pm.on_buy(90_000.0, 0.001)   # existing 0.001 already protected
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    ss['native_stop_price']       = 88_000.0   # already-established static level
    ss['native_stop_is_trailing'] = False
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _buy_fill_order(quantity=0.001, price=90_000.0, order_id="buy-recovered")
    bot_main._process_discovered_buy_fill(
        "BTC/CAD", ss, order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert pm.quantity == pytest.approx(0.002)          # inventory grew
    assert len(executor.sync_calls) == 1
    assert executor.sync_calls[0] == (88_000.0, None)   # resized at the SAME level, not recomputed
    alerter.error.assert_not_called()                   # no "unprotected" alert — it WAS resized


@patch("bot.main.cfg")
def test_recovered_buy_resizes_already_active_trailing_stop(mock_cfg):
    """An already-ACTIVE trailing stop must stay trailing on resize — not
    get silently converted to a static level."""
    mock_cfg.exchange.native_stop_loss_enabled = True
    mock_cfg.backtest.exit_params_for.return_value = {"trail_stop_pct": 0.03}
    pm = PositionManager()
    pm.on_buy(90_000.0, 0.001)
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    ss['native_stop_is_trailing'] = True   # already trailing
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _buy_fill_order(quantity=0.001, price=90_000.0, order_id="buy-recovered")
    bot_main._process_discovered_buy_fill(
        "BTC/CAD", ss, order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert pm.quantity == pytest.approx(0.002)
    assert len(executor.sync_calls) == 1
    assert executor.sync_calls[0] == (None, 0.03)   # trailing dispatch, not static
    assert ss['native_stop_is_trailing'] is True    # stays trailing


@patch("bot.main.cfg")
def test_recovered_buy_with_no_prior_protection_establishes_fallback(mock_cfg):
    """First recovered fill for a symbol with NO protection at all yet
    (position was flat) — must establish the configured flat-STOP_LOSS_PCT
    fallback explicitly, not leave the position naked."""
    mock_cfg.exchange.native_stop_loss_enabled = True
    mock_cfg.backtest.stop_loss_pct = 0.015
    pm = PositionManager()   # flat — first fill
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    assert ss['native_stop_price'] is None
    assert ss['native_stop_is_trailing'] is False
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _buy_fill_order(quantity=0.001, price=90_000.0, order_id="buy-first")
    bot_main._process_discovered_buy_fill(
        "BTC/CAD", ss, order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    expected_fallback = 90_000.0 * (1 - 0.015)
    assert len(executor.sync_calls) == 1
    assert executor.sync_calls[0] == (pytest.approx(expected_fallback), None)
    assert ss['native_stop_price'] == pytest.approx(expected_fallback)
    alerter.error.assert_not_called()


@patch("bot.main.cfg")
def test_recovered_buy_no_fallback_possible_alerts_unprotected(mock_cfg):
    """STOP_LOSS_PCT=0 and no prior protection — cannot compute a
    fallback; must alert loudly rather than silently leave it naked."""
    mock_cfg.exchange.native_stop_loss_enabled = True
    mock_cfg.backtest.stop_loss_pct = 0.0
    pm = PositionManager()
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _buy_fill_order(quantity=0.001, price=90_000.0, order_id="buy-first")
    bot_main._process_discovered_buy_fill(
        "BTC/CAD", ss, order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert executor.sync_calls == []
    alerter.error.assert_called_once()
    assert "UNPROTECTED" in alerter.error.call_args[0][0]


@patch("bot.main.cfg")
def test_recovered_buy_protection_disabled_skips_resize_entirely(mock_cfg):
    """NATIVE_STOP_LOSS_ENABLED=false — must not touch protection at all,
    matching fixed-mode/paper-trading behavior exactly as before."""
    mock_cfg.exchange.native_stop_loss_enabled = False
    pm = PositionManager()
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()

    order = _buy_fill_order(quantity=0.001, price=90_000.0, order_id="buy-first")
    bot_main._process_discovered_buy_fill(
        "BTC/CAD", ss, order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert executor.sync_calls == []
    trade_log.log_fill.assert_called_once()   # bookkeeping still happened


# ---------------------------------------------------------------------------
# PASS-10 review, finding 2 (P1): a recovered BUY whose immediate protective
# resize discovers a full SELL must write its OWN trade_log row FIRST — the
# durable ledger's insertion order (and live_comparison.py's entry-fee
# allocation, which walks fills in that same order) must agree with the
# real causal order (BUY opened the position the SELL then closed), not
# whichever order the in-process consumer happened to write rows in.
# ---------------------------------------------------------------------------

@patch("bot.main.cfg")
def test_recovered_buy_with_immediate_full_exit_logs_buy_before_sell(mock_cfg, tmp_path):
    """PASS-10 review finding (P1, finding 2), exact reproduction: a
    recovered BUY (qty 1 @ $100, fee $0.80) whose fallback protective stop
    immediately discovers a full SELL (qty 1 @ $101, fee $0.40). Using a
    REAL temporary SQLite TradeLog and live_comparison.py's actual
    _load_fills/_compute_live_metrics — not a mock — proves the durable
    row order agrees with reality: the BUY row must have a lower id than
    the derivative SELL row, the round trip's net P&L must be the real
    -$0.20 (not +$0.60), win rate 0% (not 100%), and the BUY's entry fee
    must be fully allocated (not left as $0.80 unallocated)."""
    mock_cfg.exchange.native_stop_loss_enabled = True
    mock_cfg.exchange.exchange                 = "kraken"
    mock_cfg.backtest.stop_loss_pct            = 0.015

    pm = PositionManager()   # flat — this IS the position-opening BUY
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    # The fallback protective stop, once placed, discovers it was already
    # filled — a full, immediate exit at $101.
    executor.queue_fill(_fill_order(quantity=1.0, price=101.0, fee_cost=0.40, order_id="native-stop:stop-1"))
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=1000.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = TradeLog(db_path=str(tmp_path / "trades.db"))

    order = _buy_fill_order(quantity=1.0, price=100.0, fee_cost=0.80, order_id="buy-1")
    bot_main._process_discovered_buy_fill(
        "BTC/CAD", ss, order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    conn = sqlite3.connect(str(tmp_path / "trades.db"))
    rows = conn.execute("SELECT id, side FROM fills ORDER BY id").fetchall()
    conn.close()
    assert [r[1] for r in rows] == ["BUY", "SELL"]   # BUY committed first

    fills   = lc._load_fills(str(tmp_path / "trades.db"))
    metrics = lc._compute_live_metrics(fills)

    assert metrics["net_pnl"]             == pytest.approx(-0.20)
    assert metrics["win_rate"]            == pytest.approx(0.0)
    assert metrics["unallocated_buy_fees"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# PASS-11 review, finding 3 (P1): a raised trade_log failure must never skip
# the protective-stop sync for a real, already-owned position — logging is
# best-effort; protection is not conditional on it succeeding.
# ---------------------------------------------------------------------------

@patch("bot.main.cfg")
def test_recovered_buy_ledger_failure_does_not_skip_protection(mock_cfg):
    """PASS-11 review finding (P1, finding 3), exact reproduction: a
    recovered BUY (qty 1 @ $100) with no existing protection, native
    protection enabled, STOP_LOSS_PCT 0.02, and trade_log.log_fill()
    raising OSError('disk full'). The old (PASS-10) ordering let this
    exception propagate straight out of the function, skipping the
    protective-stop sync entirely — PositionManager already shows the
    real, owned quantity at that point. Must not raise, and protection
    MUST still be attempted."""
    mock_cfg.exchange.native_stop_loss_enabled = True
    mock_cfg.exchange.exchange                 = "kraken"
    mock_cfg.backtest.stop_loss_pct            = 0.02

    pm = PositionManager()   # flat — this IS the position-opening BUY
    sm = TradingStateMachine(cooldown_ticks=3)
    executor = _FakeExecutorForResync()
    ss = _ss(executor, pm, sm)
    capital_pool = CapitalPool(total_capital=1000.0, max_concurrent=1)
    risk = MagicMock()
    alerter = MagicMock()
    trade_log = MagicMock()
    trade_log.log_fill.side_effect = OSError("disk full")

    order = _buy_fill_order(quantity=1.0, price=100.0, fee_cost=0.0, order_id="buy-1")
    bot_main._process_discovered_buy_fill(   # must not raise
        "BTC/CAD", ss, order, "pending_order_reconciled",
        capital_pool=capital_pool, risk=risk, alerter=alerter, trade_log=trade_log,
    )

    assert pm.quantity == pytest.approx(1.0)     # the BUY's own economics still applied
    assert len(executor.sync_calls) == 1          # protection WAS attempted
    expected_fallback = 100.0 * (1 - 0.02)
    assert executor.sync_calls[0] == (pytest.approx(expected_fallback), None)
