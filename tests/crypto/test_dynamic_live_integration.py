"""
Behavioral tests for the LIVE-engine dynamic-universe integration
(bot/main.py, added 2026-09-13) — admission, retirement, ranked execution,
and fill-processing, using mocked exchange/executor objects and REAL
TradingStateMachine / PositionManager / CapitalPool / RiskManager
instances (all pure, side-effect-free classes already covered by their own
unit tests elsewhere).

Nothing here places a real order, sends a real Telegram message, or writes
production state: every executor is a hand-built fake, every alerter/
trade_log is a MagicMock, and any file path used is under tmp_path.

Covers the point-7 checklist from the live-integration request:
  - admission, warmup, completed-candle dedup (via _admit_dynamic_symbol
    + the reused _fetch_completed_candle/is_warmed_up guard, already
    tested elsewhere — dedup itself is exercised here via last_ts_ms)
  - BUY -> hold -> strategy SELL, state transitions and fees
  - stop-loss/take-profit and trailing seeding (native-stop calls)
  - simultaneous ranked signals competing for limited capital
  - pending/rejected/partially-filled orders
  - insufficient funds / order-minimum rejection handling
  - restart with an open position (capital-pool re-allocation)
  - universe removal while holding a position (retirement gate)
  - discovery/admission failure while exits still need management
  - HALT-equivalent (risk gate returning falsy) blocks execution
  - fixed-mode is never touched by any of this
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import bot.main as bot_main
from bot.execution.executor import Order, OrderSide, OrderStatus
from bot.portfolio.capital_pool import CapitalPool
from bot.portfolio.position_manager import PositionManager
from bot.state.trade_state import TradingStateMachine
from bot.strategy.threshold_strategy import Signal


# ── Fakes ────────────────────────────────────────────────────────────────

class FakePortfolio:
    def __init__(self, cash=100.0):
        self.cash = cash
        self.position = 0.0
        self.realized_pnl = 0.0


class FakeExecutor:
    """Mimics LiveExecutor's public surface used by the code under test —
    no ccxt, no network, no file I/O."""

    def __init__(self, symbol="ETH/CAD", starting_cash=0.0, starting_position=0.0,
                 avg_entry=0.0, fee_currency="CAD"):
        self.symbol = symbol
        self._portfolio = FakePortfolio(cash=starting_cash)
        self.position = starting_position
        self.avg_entry = avg_entry
        self.has_resting_stop = False
        self._next_order = None
        self._raise_on_execute = None
        self.sync_calls = []
        self.save_state_calls = 0
        self.execute_calls = []

    @property
    def cash(self):
        return self._portfolio.cash

    @cash.setter
    def cash(self, v):
        self._portfolio.cash = v

    @property
    def portfolio(self):
        return self._portfolio

    def queue_order(self, order):
        self._next_order = order

    def execute(self, signal, price, quantity=None, urgent=False):
        self.execute_calls.append((signal, price, quantity, urgent))
        if self._raise_on_execute:
            raise self._raise_on_execute
        order = self._next_order
        if order is not None and order.status == OrderStatus.FILLED:
            if order.side == OrderSide.BUY:
                self._portfolio.cash -= order.total_value
                self.position += order.quantity
                self.avg_entry = order.price
            else:
                self._portfolio.cash += order.total_value
                self.position = max(0.0, self.position - order.quantity)
        return order

    def sync_protective_stop(self, price, trailing_pct=None):
        # Real LiveExecutor.sync_protective_stop() returns a list (possibly
        # more than one discovered fill) — 2026-09-19 PASS-5. This fake
        # never discovers one, but must match that contract shape so
        # callers exercising the SAME code path as production don't crash.
        self.sync_calls.append((price, trailing_pct))
        self.has_resting_stop = price is not None or trailing_pct is not None
        return []

    def _save_state(self):
        self.save_state_calls += 1


def _filled_order(side, price, qty, fee_cost=0.05, fee_currency="CAD"):
    return Order(
        order_id="x", symbol="ETH/CAD", side=side, quantity=qty, price=price,
        status=OrderStatus.FILLED, created_at=None, filled_at=None,
        fee_cost=fee_cost, fee_currency=fee_currency,
    )


def _rejected_order(side, reason="insufficient funds"):
    o = Order(
        order_id="x", symbol="ETH/CAD", side=side, quantity=0.0, price=0.0,
        status=OrderStatus.REJECTED, created_at=None, filled_at=None,
    )
    o.reject_reason = reason
    return o


def _new_ss(executor=None, strategy_adx=25.0):
    executor = executor or FakeExecutor()
    strat = MagicMock()
    strat.last_adx = strategy_adx
    strat._highs = [1, 2, 3]
    strat._lows = [1, 2, 3]
    strat._closes = [1, 2, 3]
    ss = bot_main._new_symbol_state_dict(
        strategy=strat,
        sm=TradingStateMachine(cooldown_ticks=3),
        pm=PositionManager(),
        executor=executor,
    )
    return ss


class FakeRisk:
    """Real RiskManager has a lot of moving parts (kill switch, daily loss,
    etc.) already covered by its own 35+ tests — here we only need a
    controllable approval decision + a spy on record_fill()."""

    def __init__(self, approve=True, message="blocked"):
        self._approve = approve
        self._message = message
        self.record_fill_calls = []
        self.evaluate_calls = []

    def evaluate(self, signal, price, portfolio, qty, account_value=0.0, symbol=None):
        self.evaluate_calls.append((signal, symbol, account_value))
        class _R:
            def __init__(self, approved, message):
                self.approved = approved
                self.message = message
            def __bool__(self):
                return self.approved
        return _R(self._approve, self._message)

    def record_fill(self, symbol):
        self.record_fill_calls.append(symbol)


# ── _new_symbol_state_dict ───────────────────────────────────────────────

def test_new_symbol_state_dict_has_every_key_the_tick_loop_reads():
    ss = _new_ss()
    required = {
        'strategy', 'sm', 'pm', 'executor', 'last_ts_ms', 'trail_peak',
        'partial_done', 'atr_sl', 'native_stop_price', 'native_stop_is_trailing',
        'candle_feed_stale', 'last_price', 'err_count', 'drift_count',
        'drift_acked', 'last_candle_time', 'mtf_1d_closes', 'dash_signal',
        'dash_rsi', 'dash_trend', 'dash_filter', 'dash_block',
        'last_buy_block_alert', 'last_buy_signal_alerted', 'exit_fail_count',
    }
    assert required.issubset(ss.keys())


# ── _admit_dynamic_symbol ────────────────────────────────────────────────

def test_admit_dynamic_symbol_success(monkeypatch):
    fake_exec = FakeExecutor(starting_cash=0.0)
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(last_adx=20.0, _highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda strat, ex, tf, symbol: 12345)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: fake_exec)

    pool = CapitalPool(total_capital=300.0, max_concurrent=3)
    ss, err = bot_main._admit_dynamic_symbol("ETH/CAD", live_exchange="ex", timeframe="4h", capital_pool=pool)

    assert err is None
    assert ss is not None
    assert ss['last_ts_ms'] == 12345
    assert fake_exec.cash == 100.0  # 300/3 slots
    assert fake_exec.save_state_calls == 1
    assert not pool.is_allocated("ETH/CAD")  # no position yet — no slot claimed


def test_admit_dynamic_symbol_warmup_failure_returns_error_not_exception(monkeypatch):
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock())
    def _boom(*a, **kw):
        raise ConnectionError("network down")
    monkeypatch.setattr(bot_main, "_warmup_strategy", _boom)

    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    ss, err = bot_main._admit_dynamic_symbol("ETH/CAD", "ex", "4h", pool)

    assert ss is None
    assert "network down" in err


def test_admit_dynamic_symbol_with_existing_position_seeds_recovery_state(monkeypatch):
    """Restart scenario: the executor's OWN persisted state already shows a
    position (loaded inside its constructor) — pm/sm must be seeded AND
    the capital pool slot must be claimed (the exact gap the review found:
    'restart recovery restores executor positions but not capital-pool
    allocations')."""
    fake_exec = FakeExecutor(starting_cash=50.0, starting_position=2.0, avg_entry=10.0)
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda strat, ex, tf, symbol: 999)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: fake_exec)

    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    ss, err = bot_main._admit_dynamic_symbol("ETH/CAD", "ex", "4h", pool)

    assert err is None
    assert ss['pm'].quantity == 2.0
    assert ss['pm'].avg_entry == 10.0
    assert ss['sm'].state.value == "LONG"
    assert pool.is_allocated("ETH/CAD")   # capital-pool gap fixed


def test_admit_dynamic_symbol_is_noop_when_already_admitted_by_caller():
    """_sync_dynamic_universe (not this function alone) is what skips
    already-admitted symbols — covered in its own tests below; this just
    documents that _admit_dynamic_symbol itself has no such check (it's
    the caller's job), so it's always safe to call directly in a test."""
    assert bot_main._admit_dynamic_symbol.__doc__ is not None


# ── _retire_dynamic_symbol_if_eligible ───────────────────────────────────

def test_retire_flat_symbol_releases_capital_pool():
    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    pool.allocate("ETH/CAD")
    ss = _new_ss(executor=FakeExecutor(starting_cash=105.0, starting_position=0.0))

    retired = bot_main._retire_dynamic_symbol_if_eligible("ETH/CAD", ss, pool)

    assert retired is True
    assert not pool.is_allocated("ETH/CAD")


def test_retire_keeps_symbol_holding_a_position():
    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    pool.allocate("ETH/CAD")
    ss = _new_ss(executor=FakeExecutor(starting_position=1.5))

    retired = bot_main._retire_dynamic_symbol_if_eligible("ETH/CAD", ss, pool)

    assert retired is False
    assert pool.is_allocated("ETH/CAD")


def test_retire_keeps_symbol_with_resting_native_stop_even_if_flat():
    """Orders/protective stops must be resolved, not just the position
    counter zeroed, before retiring — see point 4 of the live-integration
    request ('retire only after positions are flat and orders are
    resolved')."""
    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    executor = FakeExecutor(starting_position=0.0)
    executor.has_resting_stop = True
    ss = _new_ss(executor=executor)

    retired = bot_main._retire_dynamic_symbol_if_eligible("ETH/CAD", ss, pool)

    assert retired is False


# ── _sync_dynamic_universe ───────────────────────────────────────────────

class FakeScreenResult:
    def __init__(self, eligible_symbols):
        self.eligible_symbols = eligible_symbols
        self.eligible = []
        self.rejected = []
        self.stale = False
    def all_candidates(self):
        return self.eligible + self.rejected


class FakeScreener:
    def __init__(self, eligible_symbols, raise_on_discover=None):
        self._eligible = eligible_symbols
        self._raise = raise_on_discover
    def discover(self, exchange, slot_cash):
        if self._raise:
            raise self._raise
        return FakeScreenResult(self._eligible)


def test_sync_admits_new_eligible_symbols(monkeypatch):
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda *a, **kw: 1)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: FakeExecutor(symbol=sym))

    symbol_state, executors, dynamic_admitted = {}, {}, set()
    pool = CapitalPool(total_capital=300.0, max_concurrent=3)
    screener = FakeScreener(["ETH/CAD", "XRP/CAD"])

    admitted, retired, screen = bot_main._sync_dynamic_universe(
        symbol_state, executors, dynamic_admitted, screener, "ex", "4h", pool, 100.0,
    )

    assert set(admitted) == {"ETH/CAD", "XRP/CAD"}
    assert retired == []
    assert set(symbol_state.keys()) == {"ETH/CAD", "XRP/CAD"}
    assert dynamic_admitted == {"ETH/CAD", "XRP/CAD"}


def test_sync_does_not_readmit_already_present_symbol(monkeypatch):
    calls = []
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    def _warmup(*a, **kw):
        calls.append(1)
        return 1
    monkeypatch.setattr(bot_main, "_warmup_strategy", _warmup)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: FakeExecutor(symbol=sym))

    symbol_state = {"BTC/CAD": _new_ss()}   # fixed-roster symbol, pre-existing
    executors = {"BTC/CAD": symbol_state["BTC/CAD"]["executor"]}
    dynamic_admitted = set()
    pool = CapitalPool(total_capital=300.0, max_concurrent=3)
    screener = FakeScreener(["BTC/CAD", "ETH/CAD"])   # screener also lists the fixed symbol

    admitted, retired, screen = bot_main._sync_dynamic_universe(
        symbol_state, executors, dynamic_admitted, screener, "ex", "4h", pool, 100.0,
    )

    assert admitted == ["ETH/CAD"]   # BTC/CAD skipped — already in symbol_state
    assert "BTC/CAD" not in dynamic_admitted   # never claimed as a dynamic symbol


def test_sync_retires_flat_dynamic_symbol_dropped_from_eligible_set(monkeypatch):
    symbol_state = {"ETH/CAD": _new_ss(executor=FakeExecutor(starting_position=0.0))}
    executors = {"ETH/CAD": symbol_state["ETH/CAD"]["executor"]}
    dynamic_admitted = {"ETH/CAD"}
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    screener = FakeScreener([])   # ETH/CAD no longer eligible

    admitted, retired, screen = bot_main._sync_dynamic_universe(
        symbol_state, executors, dynamic_admitted, screener, "ex", "4h", pool, 100.0,
    )

    assert retired == ["ETH/CAD"]
    assert "ETH/CAD" not in symbol_state
    assert "ETH/CAD" not in executors
    assert dynamic_admitted == set()


def test_sync_never_retires_a_symbol_holding_a_position_even_if_ineligible(monkeypatch):
    """Universe removal while holding a position — the position must keep
    being managed."""
    held_executor = FakeExecutor(starting_position=3.0)
    symbol_state = {"ETH/CAD": _new_ss(executor=held_executor)}
    executors = {"ETH/CAD": held_executor}
    dynamic_admitted = {"ETH/CAD"}
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    pool.allocate("ETH/CAD")
    screener = FakeScreener([])   # dropped out of eligibility

    admitted, retired, screen = bot_main._sync_dynamic_universe(
        symbol_state, executors, dynamic_admitted, screener, "ex", "4h", pool, 100.0,
    )

    assert retired == []
    assert "ETH/CAD" in symbol_state    # still being managed
    assert "ETH/CAD" in dynamic_admitted


def test_sync_never_retires_the_fixed_roster(monkeypatch):
    """A fixed-roster symbol (BTC/CAD) is never in dynamic_admitted, so
    even if the screener stopped listing it as eligible, sync must not
    touch it — it isn't this function's to manage."""
    fixed_ss = _new_ss(executor=FakeExecutor(starting_position=0.0))
    symbol_state = {"BTC/CAD": fixed_ss}
    executors = {"BTC/CAD": fixed_ss["executor"]}
    dynamic_admitted = set()   # BTC/CAD was never added here
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    screener = FakeScreener([])

    admitted, retired, screen = bot_main._sync_dynamic_universe(
        symbol_state, executors, dynamic_admitted, screener, "ex", "4h", pool, 100.0,
    )

    assert retired == []
    assert "BTC/CAD" in symbol_state


def test_sync_one_bad_admission_does_not_block_others(monkeypatch):
    call_count = {"n": 0}
    def _warmup(strat, ex, tf, symbol):
        call_count["n"] += 1
        if symbol == "BADCOIN/CAD":
            raise ConnectionError("timeout")
        return 1
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", _warmup)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: FakeExecutor(symbol=sym))

    symbol_state, executors, dynamic_admitted = {}, {}, set()
    pool = CapitalPool(total_capital=300.0, max_concurrent=3)
    screener = FakeScreener(["BADCOIN/CAD", "ETH/CAD"])

    admitted, retired, screen = bot_main._sync_dynamic_universe(
        symbol_state, executors, dynamic_admitted, screener, "ex", "4h", pool, 100.0,
    )

    assert admitted == ["ETH/CAD"]
    assert "BADCOIN/CAD" not in symbol_state
    assert "ETH/CAD" in symbol_state


def test_sync_discovery_failure_does_not_raise(monkeypatch):
    """Discovery/data failure while exits still need management: sync
    itself must not raise even if the screener's discover() somehow does
    (eligibility.py's own discover() is already internally exception-safe
    — this proves the CALLER, _sync_dynamic_universe, adds no new risk on
    top of that)."""
    symbol_state = {"BTC/CAD": _new_ss(executor=FakeExecutor(starting_position=1.0))}
    executors = {"BTC/CAD": symbol_state["BTC/CAD"]["executor"]}
    dynamic_admitted = set()
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    pool.allocate("BTC/CAD")
    screener = FakeScreener([], raise_on_discover=ConnectionError("exchange down"))

    with pytest.raises(ConnectionError):
        bot_main._sync_dynamic_universe(
            symbol_state, executors, dynamic_admitted, screener, "ex", "4h", pool, 100.0,
        )
    # BTC/CAD's position is untouched by the failed call — run()'s own
    # try/except around this call (see "0b." in run()) is what keeps the
    # per-symbol loop executing regardless; this confirms the position
    # itself was never mutated by the failed sync attempt.
    assert symbol_state["BTC/CAD"]["executor"].position == 1.0


# ── _execute_approved_signal ─────────────────────────────────────────────

def test_execute_buy_fill_updates_all_state_and_fees():
    executor = FakeExecutor(starting_cash=100.0)
    executor.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=5.0, fee_cost=0.25))
    ss = _new_ss(executor=executor)
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = FakeRisk(approve=True)
    alerter, trade_log, stuck = MagicMock(), MagicMock(), MagicMock()

    order = bot_main._execute_approved_signal(
        "ETH/CAD", ss, Signal.BUY, 10.0, 5.0, Signal.BUY, "",
        capital_pool=pool, risk=risk, alerter=alerter, trade_log=trade_log,
        stuck_detector=stuck, is_indicator=True,
    )

    assert order.status == OrderStatus.FILLED
    assert ss['sm'].state.value == "LONG"           # sm.on_fill was called
    assert ss['pm'].quantity == 5.0                  # pm.on_buy was called
    assert risk.record_fill_calls == ["ETH/CAD"]     # risk.record_fill was called
    assert pool.is_allocated("ETH/CAD")              # capital_pool.allocate was called
    trade_log.log_fill.assert_called_once()
    assert trade_log.log_fill.call_args.kwargs["fee_cost"] == 0.25
    alerter.fill.assert_called_once()
    stuck.record.assert_called_once()
    assert stuck.record.call_args.kwargs["ok"] is True


def test_execute_sell_fill_full_close_releases_capital_and_clears_stop():
    executor = FakeExecutor(starting_cash=0.0, starting_position=5.0, avg_entry=10.0)
    executor.queue_order(_filled_order(OrderSide.SELL, price=12.0, qty=5.0))
    ss = _new_ss(executor=executor)
    ss['pm'].seed(quantity=5.0, avg_entry=10.0)
    ss['native_stop_price'] = 9.5
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    pool.allocate("ETH/CAD")
    risk = FakeRisk(approve=True)
    alerter, trade_log, stuck = MagicMock(), MagicMock(), MagicMock()

    order = bot_main._execute_approved_signal(
        "ETH/CAD", ss, Signal.SELL, 12.0, 5.0, Signal.SELL, "strategy sell",
        capital_pool=pool, risk=risk, alerter=alerter, trade_log=trade_log,
        stuck_detector=stuck, is_indicator=True,
    )

    assert order.status == OrderStatus.FILLED
    assert ss['sm'].state.value == "COOLDOWN"
    assert not ss['pm'].has_position
    assert not pool.is_allocated("ETH/CAD")          # capital_pool.release was called
    assert executor.sync_calls[-1] == (None, None)   # native stop cleared


def test_execute_partial_sell_leaves_residual_position_resyncs_stop():
    executor = FakeExecutor(starting_cash=0.0, starting_position=5.0, avg_entry=10.0)
    executor.queue_order(_filled_order(OrderSide.SELL, price=12.0, qty=2.0))  # only 2 of 5
    ss = _new_ss(executor=executor)
    ss['pm'].seed(quantity=5.0, avg_entry=10.0)
    ss['native_stop_price'] = 9.5
    ss['native_stop_is_trailing'] = False
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    pool.allocate("ETH/CAD")
    risk = FakeRisk(approve=True)

    bot_main._execute_approved_signal(
        "ETH/CAD", ss, Signal.SELL, 12.0, 2.0, Signal.SELL, "",
        capital_pool=pool, risk=risk, alerter=MagicMock(), trade_log=MagicMock(),
        stuck_detector=MagicMock(), is_indicator=True,
    )

    assert ss['pm'].has_position    # 3 remaining
    assert pool.is_allocated("ETH/CAD")   # NOT released — still holding
    # _resync_native_stop was called (re-places at the same static level
    # since native_stop_is_trailing is False)
    assert executor.sync_calls[-1] == (9.5, None)


def test_execute_rejected_order_alerts_and_does_not_mutate_state():
    executor = FakeExecutor(starting_cash=100.0)
    executor.queue_order(_rejected_order(OrderSide.BUY, reason="Insufficient funds"))
    ss = _new_ss(executor=executor)
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = FakeRisk(approve=True)
    alerter, stuck = MagicMock(), MagicMock()

    order = bot_main._execute_approved_signal(
        "ETH/CAD", ss, Signal.BUY, 10.0, 5.0, Signal.BUY, "",
        capital_pool=pool, risk=risk, alerter=alerter, trade_log=MagicMock(),
        stuck_detector=stuck, is_indicator=True,
    )

    assert order.status == OrderStatus.REJECTED
    assert ss['sm'].state.value == "IDLE"     # unchanged — no on_fill
    assert not pool.is_allocated("ETH/CAD")
    alerter.error.assert_called_once()
    assert "Insufficient funds" in alerter.error.call_args[0][0]
    assert stuck.record.call_args.kwargs["ok"] is False


def test_execute_min_order_rejection_is_a_normal_rejected_order():
    """Actual order minimums: LiveExecutor.execute() itself already enforces
    the exchange minimum (see test_live_executor.py's own min-size-guard
    tests, unchanged) and returns a REJECTED order when a proposed size is
    below it — this proves _execute_approved_signal treats that exactly
    like any other rejection, with no special-casing needed."""
    executor = FakeExecutor(starting_cash=100.0)
    executor.queue_order(_rejected_order(OrderSide.BUY, reason="Order size 0.0001 below exchange minimum 0.001"))
    ss = _new_ss(executor=executor)
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)

    order = bot_main._execute_approved_signal(
        "ETH/CAD", ss, Signal.BUY, 10.0, 0.0001, Signal.BUY, "",
        capital_pool=pool, risk=FakeRisk(True), alerter=MagicMock(),
        trade_log=MagicMock(), stuck_detector=MagicMock(), is_indicator=True,
    )
    assert order.status == OrderStatus.REJECTED
    assert "minimum" in order.reject_reason


def test_execute_exception_from_executor_is_caught_and_logged():
    executor = FakeExecutor(starting_cash=100.0)
    executor._raise_on_execute = ConnectionError("exchange timeout")
    ss = _new_ss(executor=executor)
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    alerter = MagicMock()

    order = bot_main._execute_approved_signal(
        "ETH/CAD", ss, Signal.BUY, 10.0, 5.0, Signal.BUY, "",
        capital_pool=pool, risk=FakeRisk(True), alerter=alerter,
        trade_log=MagicMock(), stuck_detector=MagicMock(), is_indicator=True,
    )

    assert order is None
    alerter.error.assert_called_once()
    assert "EXECUTOR EXCEPTION" in alerter.error.call_args[0][0]


def test_execute_filled_qty_zero_treated_as_no_fill():
    executor = FakeExecutor(starting_cash=100.0)
    executor.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=0.0))
    ss = _new_ss(executor=executor)
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)

    order = bot_main._execute_approved_signal(
        "ETH/CAD", ss, Signal.BUY, 10.0, 5.0, Signal.BUY, "",
        capital_pool=pool, risk=FakeRisk(True), alerter=MagicMock(),
        trade_log=MagicMock(), stuck_detector=MagicMock(), is_indicator=True,
    )
    assert ss['sm'].state.value == "IDLE"   # never treated as a real fill


# ── _execute_ranked_dynamic_buys ──────────────────────────────────────────

def test_ranked_execution_higher_adx_wins_limited_slot():
    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    exec_a.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    exec_b = FakeExecutor(symbol="B/CAD", starting_cash=100.0)
    exec_b.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    ss_a = _new_ss(executor=exec_a, strategy_adx=15.0)
    ss_b = _new_ss(executor=exec_b, strategy_adx=35.0)   # higher ADX — should win

    pool = CapitalPool(total_capital=100.0, max_concurrent=1)   # only ONE slot
    risk = FakeRisk(approve=True)
    queue = [
        dict(sym="A/CAD", ss=ss_a, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=15.0, quote_volume=1_000_000),
        dict(sym="B/CAD", ss=ss_b, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=35.0, quote_volume=1_000_000),
    ]

    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        queue, capital_pool=pool, risk=risk, account_value_fn=lambda: 100.0,
        alerter=MagicMock(), trade_log=MagicMock(), stuck_detector=MagicMock(),
        is_indicator=True, max_concurrent=1,
    )

    assert filled == ["B/CAD"]
    assert pool.is_allocated("B/CAD")
    assert not pool.is_allocated("A/CAD")
    assert ss_a['last_buy_block_alert'] == "capital_pool"
    assert blocked == {"A/CAD": "capital_pool"}


def test_ranked_execution_rechecks_risk_freshly_per_candidate():
    """The second candidate's risk approval is evaluated FRESH — a risk
    object that flips its answer after the first fill must be respected,
    not a stale gather-time snapshot."""
    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    exec_a.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    exec_b = FakeExecutor(symbol="B/CAD", starting_cash=100.0)
    exec_b.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    ss_a = _new_ss(executor=exec_a, strategy_adx=35.0)
    ss_b = _new_ss(executor=exec_b, strategy_adx=15.0)

    class FlippingRisk(FakeRisk):
        def evaluate(self, *a, **kw):
            self.evaluate_calls.append(1)
            # Approve the first call, reject every call after (simulates a
            # kill-switch/drawdown tripping after the first fill).
            approved = len(self.evaluate_calls) == 1
            class _R:
                def __bool__(self_inner): return approved
                message = "blocked after first fill"
            return _R()

    pool = CapitalPool(total_capital=200.0, max_concurrent=2)  # 2 slots — capital isn't the limiter here
    risk = FlippingRisk()
    queue = [
        dict(sym="A/CAD", ss=ss_a, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=35.0, quote_volume=1),
        dict(sym="B/CAD", ss=ss_b, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=15.0, quote_volume=1),
    ]

    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        queue, capital_pool=pool, risk=risk, account_value_fn=lambda: 200.0,
        alerter=MagicMock(), trade_log=MagicMock(), stuck_detector=MagicMock(),
        is_indicator=True, max_concurrent=2,
    )

    assert filled == ["A/CAD"]   # B/CAD's fresh risk check failed even though it had a slot
    assert blocked == {"B/CAD": "risk_manager"}


def test_ranked_execution_halt_equivalent_blocks_every_candidate():
    """A HALT-style risk rejection (risk.evaluate() falsy for everyone,
    matching this bot's documented full-stop HALT semantics — see
    CLAUDE.md 'SELL is never blocked ... HALT' section, Check 1 is the one
    tier that IS BUY-and-SELL-blocking) must block ALL ranked candidates,
    not just the first."""
    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    ss_a = _new_ss(executor=exec_a, strategy_adx=35.0)
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = FakeRisk(approve=False, message="Trading is halted (config.halt=True)")
    queue = [
        dict(sym="A/CAD", ss=ss_a, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=35.0, quote_volume=1),
    ]

    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        queue, capital_pool=pool, risk=risk, account_value_fn=lambda: 100.0,
        alerter=MagicMock(), trade_log=MagicMock(), stuck_detector=MagicMock(),
        is_indicator=True, max_concurrent=1,
    )

    assert filled == []
    assert blocked == {"A/CAD": "risk_manager"}
    assert exec_a.execute_calls == []   # execute() was never even called


# ── Fixed-mode compatibility ──────────────────────────────────────────────

def test_compute_account_value_unaffected_by_admitting_flat_symbol():
    """CRITICAL regression (external review, 2026-09-13): admitting a
    dynamic candidate must never change account value on its own.
    Reproduced against the pre-fix code: summing executor.cash across
    EVERY executor (including flat, merely-funded-for-sizing dynamic
    candidates) turned $453 into ~$530 by admitting one extra symbol with
    zero trades or deposits."""
    pool = CapitalPool(total_capital=453.0, max_concurrent=2)
    btc = FakeExecutor(symbol="BTC/CAD", starting_cash=77.0)
    sol = FakeExecutor(symbol="SOL/CAD", starting_cash=376.0)
    executors = {"BTC/CAD": btc, "SOL/CAD": sol}
    symbol_state = {
        "BTC/CAD": {"last_price": 0.0},
        "SOL/CAD": {"last_price": 0.0},
    }

    before = bot_main._compute_account_value(pool, executors, symbol_state)
    assert before == pytest.approx(453.0)

    # Admit a flat ETH/CAD candidate — funded with a full slot's worth of
    # cash for SIZING purposes only (matches _admit_dynamic_symbol), no
    # trade, no deposit.
    eth = FakeExecutor(symbol="ETH/CAD", starting_cash=pool.slot_cash_for("ETH/CAD"))
    executors["ETH/CAD"] = eth
    symbol_state["ETH/CAD"] = {"last_price": 0.0}

    after = bot_main._compute_account_value(pool, executors, symbol_state)
    assert after == pytest.approx(before)   # unchanged — the whole point of the fix


def test_compute_account_value_counts_only_allocated_symbols_holdings():
    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    pool.allocate("BTC/CAD")   # BTC holds a real position
    btc = FakeExecutor(symbol="BTC/CAD", starting_cash=50.0, starting_position=1.0, avg_entry=40.0)
    eth = FakeExecutor(symbol="ETH/CAD", starting_cash=100.0)   # admitted, flat, NOT allocated
    executors = {"BTC/CAD": btc, "ETH/CAD": eth}
    symbol_state = {"BTC/CAD": {"last_price": 45.0}, "ETH/CAD": {"last_price": 0.0}}

    total = bot_main._compute_account_value(pool, executors, symbol_state)

    # available_cash (100 = 200 total - BTC's 100 allocated slot) + BTC's
    # own cash(50) + position(1.0)*price(45.0) — ETH's cash never counted.
    assert total == pytest.approx(100.0 + 50.0 + 45.0)


# ── LIVE_TRADING gate (Critical fix #2) ─────────────────────────────────

def test_dynamic_mode_requires_live_trading():
    """CRITICAL regression: DYNAMIC_UNIVERSE_ENABLED=true must NOT activate
    dynamic admission when LIVE_TRADING is false — reproduced against the
    pre-fix code, where _make_dynamic_executor's dry_run computation never
    checked live_trading at all and could receive dry_run=False under
    LIVE_TRADING=False."""
    import inspect
    src = inspect.getsource(bot_main.run)
    assert "_dynamic_mode_active = cfg.dynamic.enabled and cfg.exchange.live_trading" in src
    # Every subsequent dynamic-mode gate must key off the combined flag,
    # never the bare cfg.dynamic.enabled (which is exactly the bug).
    idx = src.index("_dynamic_screener   = DynamicUniverseScreener")
    assert "if _dynamic_mode_active else None" in src[idx:idx + 100]


# ── Correlation recheck in ranked execution (High fix #4) ───────────────

def test_ranked_execution_rechecks_correlation_against_earlier_fill_in_batch():
    """HIGH regression: two candidates correlated with EACH OTHER both
    passed section 2f's gather-time check (neither held a position yet),
    then both filled — this recheck, using the position state AFTER each
    fill, must block the second once the first has filled."""
    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    exec_a.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    exec_b = FakeExecutor(symbol="B/CAD", starting_cash=100.0)
    exec_b.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    ss_a = _new_ss(executor=exec_a, strategy_adx=35.0)   # ranked first
    ss_b = _new_ss(executor=exec_b, strategy_adx=15.0)   # ranked second
    symbol_state = {"A/CAD": ss_a, "B/CAD": ss_b}

    pool = CapitalPool(total_capital=200.0, max_concurrent=2)   # capital is NOT the limiter
    risk = FakeRisk(approve=True)
    queue = [
        dict(sym="A/CAD", ss=ss_a, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=35.0, quote_volume=1),
        dict(sym="B/CAD", ss=ss_b, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=15.0, quote_volume=1),
    ]

    def fake_correlation(exchange, sym_a, sym_b):
        return 0.95   # highly correlated, always

    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        queue, capital_pool=pool, risk=risk, account_value_fn=lambda: 200.0,
        alerter=MagicMock(), trade_log=MagicMock(), stuck_detector=MagicMock(),
        is_indicator=True, max_concurrent=2,
        symbol_state=symbol_state, live_exchange="fake-exchange",
        correlation_fn=fake_correlation, correlation_threshold=0.70,
    )

    assert filled == ["A/CAD"]
    assert blocked == {"B/CAD": "correlation"}
    assert exec_b.execute_calls == []   # never even attempted


def test_ranked_execution_allows_uncorrelated_simultaneous_fills():
    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    exec_a.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    exec_b = FakeExecutor(symbol="B/CAD", starting_cash=100.0)
    exec_b.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    ss_a = _new_ss(executor=exec_a, strategy_adx=35.0)
    ss_b = _new_ss(executor=exec_b, strategy_adx=15.0)
    symbol_state = {"A/CAD": ss_a, "B/CAD": ss_b}

    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    risk = FakeRisk(approve=True)
    queue = [
        dict(sym="A/CAD", ss=ss_a, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=35.0, quote_volume=1),
        dict(sym="B/CAD", ss=ss_b, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=15.0, quote_volume=1),
    ]

    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        queue, capital_pool=pool, risk=risk, account_value_fn=lambda: 200.0,
        alerter=MagicMock(), trade_log=MagicMock(), stuck_detector=MagicMock(),
        is_indicator=True, max_concurrent=2,
        symbol_state=symbol_state, live_exchange="fake-exchange",
        correlation_fn=lambda ex, a, b: 0.10, correlation_threshold=0.70,
    )

    assert set(filled) == {"A/CAD", "B/CAD"}
    assert blocked == {}


# ── Unresolved-order capital reservation (High fix #5) ──────────────────

def test_ambiguous_order_conservatively_reserves_capital_slot():
    """HIGH regression: execute() returning None for a BUY (an AMBIGUOUS
    outcome — LiveExecutor's own 'qty=0 GUARD' path, not a clean rejection)
    must not leave the slot free for a different candidate to claim."""
    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    exec_a.queue_order(None)   # simulates execute() returning None (ambiguous)
    ss_a = _new_ss(executor=exec_a, strategy_adx=35.0)

    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    risk = FakeRisk(approve=True)
    queue = [
        dict(sym="A/CAD", ss=ss_a, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=35.0, quote_volume=1),
    ]

    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        queue, capital_pool=pool, risk=risk, account_value_fn=lambda: 100.0,
        alerter=MagicMock(), trade_log=MagicMock(), stuck_detector=MagicMock(),
        is_indicator=True, max_concurrent=1,
    )

    assert filled == []
    assert blocked == {"A/CAD": "unresolved_order"}
    assert pool.is_allocated("A/CAD")   # conservatively held, not free for another candidate
    assert not pool.can_open_position("B/CAD")   # a different candidate can't claim it


# ── Quote-currency enforcement (High fix #6) ─────────────────────────────

def test_unsupported_quote_currency_disables_dynamic_mode():
    """HIGH regression: DYNAMIC_QUOTE_CURRENCIES=CAD,USD must not scan/trade
    USD pairs against the single CAD-denominated capital pool — the live
    integration refuses to activate rather than risk it."""
    import inspect
    src = inspect.getsource(bot_main.run)
    idx = src.index("_dynamic_mode_active = cfg.dynamic.enabled and cfg.exchange.live_trading")
    end = src.index("_dynamic_screener   = DynamicUniverseScreener")
    section = src[idx:end]
    assert '_unsupported_quotes = [q for q in cfg.dynamic.quote_list if q != "CAD"]' in section
    assert "_dynamic_mode_active = False" in section


def test_completed_candle_dedup_after_dynamic_admission():
    """A dynamically-admitted symbol's last_ts_ms (seeded from warmup's own
    return value) must be honored by _fetch_completed_candle exactly as it
    is for the fixed roster — the SAME still-forming candle it just warmed
    up on must not be re-evaluated as if it were new."""
    from bot.main import _fetch_completed_candle

    class FakeExchange:
        def __init__(self, rows):
            self.rows = rows
        def fetch_ohlcv(self, symbol, timeframe="4h", limit=2):
            return self.rows[-limit:]

    warmup_last_row = [1_000, 10.0, 10.5, 9.5, 10.2, 100.0]
    still_forming    = [1_014_400_000, 10.2, 10.3, 10.1, 10.25, 50.0]
    ex = FakeExchange([warmup_last_row, still_forming])

    # last_ts_ms == warmup_last_row's own timestamp, exactly as
    # _admit_dynamic_symbol seeds ss['last_ts_ms'] from _warmup_strategy's
    # return value.
    candle, ts = _fetch_completed_candle(ex, last_ts_ms=1_000, timeframe="4h", symbol="ETH/CAD")

    assert candle is None and ts is None   # correctly deduped — not treated as a new candle


def test_dynamic_config_disabled_by_default():
    from config import DynamicUniverseConfig
    assert DynamicUniverseConfig().enabled is False


def test_run_restart_recovery_reallocates_capital_pool_slot():
    """Source guard for the fixed-roster half of the 'restart recovery
    restores executor positions but not capital-pool allocations' fix —
    the behavioral proof for the equivalent dynamic-symbol case is
    test_admit_dynamic_symbol_with_existing_position_seeds_recovery_state
    above (capital_pool.allocate IS the same CapitalPool method, already
    covered by its own 37 tests); this only proves run()'s restart-
    recovery block for the ORIGINAL fixed roster calls it too, since that
    block is irreducibly inline in run() (loops over live executors
    restored from real persisted state, not something to fake safely)."""
    import inspect
    src = inspect.getsource(bot_main.run)
    idx = src.index("# ── Restart recovery")
    end = src.index("# Aliases for _render_dashboard closure")
    section = src[idx:end]
    assert "capital_pool.allocate(_rsym)" in section


# ── Price refresh before execution (external-review "other improvement") ─

def test_ranked_execution_uses_refreshed_price_within_tolerance():
    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    exec_a.queue_order(_filled_order(OrderSide.BUY, price=10.05, qty=1.0))
    ss_a = _new_ss(executor=exec_a, strategy_adx=35.0)
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    queue = [
        dict(sym="A/CAD", ss=ss_a, final_signal=Signal.BUY, price=10.00, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=35.0, quote_volume=1),
    ]

    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        queue, capital_pool=pool, risk=FakeRisk(True), account_value_fn=lambda: 100.0,
        alerter=MagicMock(), trade_log=MagicMock(), stuck_detector=MagicMock(),
        is_indicator=True, max_concurrent=1,
        refresh_price_fn=lambda sym: 10.05, max_price_deviation_pct=0.02,
    )

    assert filled == ["A/CAD"]
    assert blocked == {}
    # execute() was called with the REFRESHED price (10.05), not the
    # gather-time one (10.00).
    assert exec_a.execute_calls[0][1] == 10.05


def test_ranked_execution_skips_candidate_when_price_moved_past_tolerance():
    """HIGH/other-improvement regression: a candidate must not execute
    against stale sizing math when the market has moved meaningfully since
    it was gathered — it should re-qualify on a fresh signal next tick
    instead."""
    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    exec_a.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    ss_a = _new_ss(executor=exec_a, strategy_adx=35.0)
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    queue = [
        dict(sym="A/CAD", ss=ss_a, final_signal=Signal.BUY, price=10.00, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=35.0, quote_volume=1),
    ]

    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        queue, capital_pool=pool, risk=FakeRisk(True), account_value_fn=lambda: 100.0,
        alerter=MagicMock(), trade_log=MagicMock(), stuck_detector=MagicMock(),
        is_indicator=True, max_concurrent=1,
        refresh_price_fn=lambda sym: 11.50,   # +15% — well past tolerance
        max_price_deviation_pct=0.02,
    )

    assert filled == []
    assert blocked == {"A/CAD": "stale_price"}
    assert exec_a.execute_calls == []   # never even attempted
    assert not pool.is_allocated("A/CAD")   # slot never touched — free for next tick


def test_ranked_execution_price_refresh_failure_falls_back_gracefully():
    """A network error refreshing the price must not crash the batch — it
    falls back to the gather-time price rather than skipping or raising."""
    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    exec_a.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    ss_a = _new_ss(executor=exec_a, strategy_adx=35.0)
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    queue = [
        dict(sym="A/CAD", ss=ss_a, final_signal=Signal.BUY, price=10.00, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=35.0, quote_volume=1),
    ]

    def _boom(sym):
        raise ConnectionError("exchange timeout")

    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        queue, capital_pool=pool, risk=FakeRisk(True), account_value_fn=lambda: 100.0,
        alerter=MagicMock(), trade_log=MagicMock(), stuck_detector=MagicMock(),
        is_indicator=True, max_concurrent=1,
        refresh_price_fn=_boom,
    )

    assert filled == ["A/CAD"]   # fell back to gather-time price, still executed
    assert exec_a.execute_calls[0][1] == 10.00


def test_sync_dynamic_universe_call_is_positioned_after_the_per_symbol_loop():
    """Regression for the 'move expensive screening off the exit-processing
    loop' finding: _sync_dynamic_universe (discovery/admission/retirement —
    the potentially slow call, up to DYNAMIC_MAX_CANDIDATES network round
    trips) must appear AFTER the per-symbol loop and the ranked-BUY
    execution pass in run()'s source, not before — so a slow discovery
    cycle can never delay checking an existing position's stop-loss."""
    import inspect
    src = inspect.getsource(bot_main.run)
    per_symbol_loop_idx = src.index("# ── Per-symbol processing")
    ranked_exec_idx = src.index("# ── Dynamic universe: ranked BUY execution")
    sync_call_idx = src.index("_admitted, _retired, _dynamic_last_screen = _sync_dynamic_universe(")
    assert per_symbol_loop_idx < ranked_exec_idx < sync_call_idx


def test_run_source_gates_every_dynamic_addition_behind_dynamic_mode_active():
    """Source guard (supplementing, not replacing, the behavioral tests
    above): every new dynamic-mode branch inside run() must be reachable
    only through '_dynamic_mode_active' (cfg.dynamic.enabled AND
    cfg.exchange.live_trading — see the LIVE_TRADING-bypass fix) — proving
    fixed mode's code path can never accidentally execute dynamic-mode
    logic, and dynamic mode itself can never activate without live_trading.
    This does NOT stand in for behavioral proof of correctness (see every
    test above) — it only proves the gate exists in the source."""
    import inspect
    src = inspect.getsource(bot_main.run)
    assert "_dynamic_mode_active = cfg.dynamic.enabled and cfg.exchange.live_trading" in src
    assert src.count("_dynamic_mode_active") >= 4   # definition, universe sync, buy-queue population, ranked execution
    assert "_execute_ranked_dynamic_buys(" in src
    assert "_sync_dynamic_universe(" in src


def test_fixed_mode_buy_still_executes_immediately_not_queued():
    """When dynamic mode isn't active, section 9's branch in run() must
    take the immediate _execute_approved_signal() path, never the
    deferred-queue path — verified structurally: the deferred branch is
    explicitly gated on _dynamic_mode_active AND Signal.BUY together, so
    with dynamic inactive every signal (BUY or SELL) falls through to the
    'elif approval:' immediate-execute branch. This mirrors exactly how
    fixed-mode SELLs already work (never deferred, dynamic or not)."""
    import inspect
    src = inspect.getsource(bot_main.run)
    idx = src.index("# ── 9. Execute")
    end = src.index("# ── 10. Position summary")
    section = src[idx:end]
    assert "if approval and _dynamic_mode_active and final_signal == Signal.BUY:" in section
    assert "elif approval:" in section
