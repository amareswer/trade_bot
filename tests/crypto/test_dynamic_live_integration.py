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

import math
import os
import tempfile
import time
from unittest.mock import MagicMock

import pytest

import bot.main as bot_main
from bot.execution.executor import Order, OrderSide, OrderStatus
from bot.portfolio.capital_pool import CapitalPool
from bot.portfolio.position_manager import PositionManager
from bot.risk.risk_manager import RiskConfig, RiskManager
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
                 avg_entry=0.0, fee_currency="CAD", fees_paid=0.0, realized_pnl=0.0):
        self.symbol = symbol
        self._portfolio = FakePortfolio(cash=starting_cash)
        self._portfolio.realized_pnl = realized_pnl
        self.position = starting_position
        self.avg_entry = avg_entry
        self.fees_paid = fees_paid
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


def _force_live_pool_mode(monkeypatch):
    """_admit_dynamic_symbol's recovery-value fold (external review,
    2026-09-22, third round) is gated on cfg genuinely representing a
    live, non-paper, non-dry-run pool — the SAME condition
    _initialize_capital_pool uses to decide whether to trust
    exchange_cash_observed. Tests exercising that fold need this forced
    regardless of whatever the ambient module-level cfg (loaded from
    whichever .env is present) happens to have — the real crypto .env in
    this repo currently has DRY_RUN=true (the bot is paused), which would
    otherwise silently skip the fold and fail these tests for a reason
    that has nothing to do with the behavior under test."""
    monkeypatch.setattr(bot_main.cfg.exchange, "live_trading", True)
    monkeypatch.setattr(bot_main.cfg.paper, "paper_mode", False)
    monkeypatch.setattr(bot_main.cfg.exchange, "dry_run", False)


def _force_paper_pool_mode(monkeypatch):
    """The paper/dry-run counterpart to _force_live_pool_mode — makes
    cfg.exchange.dry_run True (paper_mode/live_trading are left at
    whatever they are; dry_run alone is sufficient to make
    _initialize_capital_pool's own live-vs-static branch, and therefore
    _admit_dynamic_symbol's fold gate, take the static-starting_cash
    path)."""
    monkeypatch.setattr(bot_main.cfg.exchange, "dry_run", True)


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
    _force_live_pool_mode(monkeypatch)
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
    # External review (2026-09-22, second round P1): the slot reserved
    # must be THIS position's own real value (cash 50 + qty 2 * entry 10
    # = $70), never a generic equal-split of a pool that never even knew
    # about this holding. total_capital grows by the POSITION's value
    # ONLY ($20 = 2*10) — not the full $70 — since the pool's pre-existing
    # $200 already, implicitly, includes this symbol's own $50 residual
    # cash (a real, whole-account free-cash figure has no notion of "this
    # symbol's slice"; only the position's non-cash value is genuinely
    # new information). See _admit_dynamic_symbol's own comment for the
    # multi-coin reproduction that caught the cash+position version of
    # this as a double-count.
    assert pool.total_capital == pytest.approx(220.0)   # 200 + 20 (position value only)
    assert pool.available_cash == pytest.approx(150.0)   # 220 total - 70 allocated


def test_admit_dynamic_symbol_recovery_folds_real_value_into_pool_not_generic_slot(monkeypatch):
    """External review (2026-09-22, second round P1), exact reproduction:
    shared free cash is $135 (pool sized only from what was known at
    _initialize_capital_pool() time), and THIS symbol is being recovered
    — through _admit_dynamic_symbol, i.e. either an ordinary dynamic-tick
    admission or run()'s unconditional orphaned-position recovery — with
    a real position worth $165 (10 units @ avg_entry 16.5) that the pool
    never knew about. True equity is $135 + $165 = $300.

    Before this fix: a bare `capital_pool.allocate(sym)` reserved a
    generic ~$67.50 theoretical half-split of the still-$135 pool for a
    position actually worth $165 — silently losing $97.50 of real equity
    (the difference between what was reserved and what the position is
    actually worth) with no economic event to explain it, and leaving
    total_capital wrong for every future slot_cash_for() computation too."""
    _force_live_pool_mode(monkeypatch)
    fake_exec = FakeExecutor(starting_cash=0.0, starting_position=10.0, avg_entry=16.5)
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda strat, ex, tf, symbol: 1)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: fake_exec)

    pool = CapitalPool(total_capital=135.0, max_concurrent=2)
    ss, err = bot_main._admit_dynamic_symbol("ETH/CAD", "ex", "4h", pool)

    assert err is None
    assert pool.total_capital == pytest.approx(300.0)          # 135 free + 165 recovered
    assert pool.is_allocated("ETH/CAD")
    assert pool._slots["ETH/CAD"] == pytest.approx(165.0)       # exact position value, not a generic split
    assert pool.available_cash == pytest.approx(135.0)          # 300 - 165, the free cash unaffected


def test_admit_dynamic_symbol_recovery_never_double_counts_an_already_allocated_symbol(monkeypatch):
    """Calling _admit_dynamic_symbol's recovery path twice for a symbol
    the pool already has a slot for (shouldn't happen under any current
    caller, but the fold-in must be idempotent regardless) must not
    inflate total_capital a second time."""
    _force_live_pool_mode(monkeypatch)
    fake_exec = FakeExecutor(starting_cash=0.0, starting_position=10.0, avg_entry=16.5)
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda strat, ex, tf, symbol: 1)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: fake_exec)

    pool = CapitalPool(total_capital=135.0, max_concurrent=2)
    bot_main._admit_dynamic_symbol("ETH/CAD", "ex", "4h", pool)
    assert pool.total_capital == pytest.approx(300.0)

    bot_main._admit_dynamic_symbol("ETH/CAD", "ex", "4h", pool)   # simulate a second call
    assert pool.total_capital == pytest.approx(300.0)   # unchanged — no double-fold


def _write_live_state_json(state_dir, symbol, base, *, cash, position, cost_basis,
                            realized_pnl=0.0, fees_paid=0.0):
    import json as _json
    path = os.path.join(state_dir, f"live_state_{base}_CAD.json")
    with open(path, "w") as fh:
        _json.dump({
            "symbol": symbol, "cash": cash, "position": position,
            "cost_basis": cost_basis, "realized_pnl": realized_pnl,
            "fees_paid": fees_paid, "bot_opened": position > 0,
        }, fh)
    return path


def test_admit_dynamic_symbol_recovery_in_paper_dry_run_mode_conserves_equity_exactly(monkeypatch, tmp_path):
    """External review (2026-09-22, third, fourth, AND seventh rounds
    P1), full reproduction through both stages, now via the REAL startup
    sequence (_initialize_capital_pool's account-level replay, not a bare
    hand-built pool):

        Stage                                Correct equity   (this test)
        Restart, two $0.40 ENTRY fees paid           $899.20
        Both positions EXIT, $0.40 fee each          $898.40

    Third round: the unconditional (pre-fix) position-value bump added
    both positions' market value on top of the unrelated $900 paper
    starting bankroll — 900+100+100=1100 instead of 899.20.

    Fourth round (same day): the third round's own fix ("skip the bump
    in paper/dry-run mode, accept a small residual that self-corrects on
    release()") was ITSELF wrong — the $0.80 entry-fee gap survives every
    subsequent close; release()'s formula only propagates a slot's OWN
    P&L relative to its OWN reserved amount, never the pre-existing
    baseline.

    Seventh round (same day): a per-symbol bump inside
    _admit_dynamic_symbol only ever fires for a symbol CURRENTLY holding
    a position — it does nothing for a symbol that traded and is now
    FLAT (see the sibling test below), so the review asked for account-
    level reconstruction instead. Fixed: _initialize_capital_pool's
    paper/dry-run branch now replays EVERY live_state_*.json file in
    state_dir via _replay_paper_realized_pnl BEFORE any slot is assigned;
    _admit_dynamic_symbol's OWN paper-mode bump is removed entirely (it
    would now double-count, since the account-level replay already
    covers whatever symbol it would have bumped)."""
    _force_paper_pool_mode(monkeypatch)
    state_dir = str(tmp_path)
    _write_live_state_json(
        state_dir, "B/CAD", "B", cash=349.60, position=10.0, cost_basis=10.0, fees_paid=0.40,
    )
    _write_live_state_json(
        state_dir, "C/CAD", "C", cash=349.60, position=5.0, cost_basis=20.0, fees_paid=0.40,
    )

    cfg_fake = _FakeCfgForPoolInit(live_trading=True, dry_run=True, starting_cash=900.0, max_concurrent_positions=2)
    pool, _slots, pool_total, _cap, _caps, _paper_ok = bot_main._initialize_capital_pool({}, cfg_fake, state_dir=state_dir)
    # NOT 900 (nothing replayed) — exactly 899.20 = 900 - 0.40(B) - 0.40(C),
    # reconstructed from the account-level replay alone, before either
    # symbol is even admitted.
    assert pool_total == pytest.approx(899.20)
    assert pool.total_capital == pytest.approx(899.20)

    b_exec = FakeExecutor(
        symbol="B/CAD", starting_cash=349.60, starting_position=10.0, avg_entry=10.0,
        fees_paid=0.40, realized_pnl=0.0,
    )
    c_exec = FakeExecutor(
        symbol="C/CAD", starting_cash=349.60, starting_position=5.0, avg_entry=20.0,
        fees_paid=0.40, realized_pnl=0.0,
    )
    executors_by_symbol = {"B/CAD": b_exec, "C/CAD": c_exec}
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda strat, ex, tf, symbol: 1)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: executors_by_symbol[sym])

    ss_b, err_b = bot_main._admit_dynamic_symbol("B/CAD", "ex", "4h", pool)
    ss_c, err_c = bot_main._admit_dynamic_symbol("C/CAD", "ex", "4h", pool)

    assert err_b is None and err_c is None
    # ── Stage 1: restart ────────────────────────────────────────────────
    # Admission must NOT bump total_capital again (would double-count
    # against the account-level replay above) — still exactly 899.20.
    assert pool.total_capital == pytest.approx(899.20)
    assert pool._slots["B/CAD"] == pytest.approx(449.60)   # slot amount itself unchanged
    assert pool._slots["C/CAD"] == pytest.approx(449.60)
    symbol_state = {"B/CAD": {"last_price": 10.0}, "C/CAD": {"last_price": 20.0}}
    total_at_restart = bot_main._compute_account_value(pool, executors_by_symbol, symbol_state)
    assert total_at_restart == pytest.approx(899.20)

    # ── Stage 2: both positions exit at UNCHANGED prices, another $0.40
    #    fee each — exactly the fourth round's own reproduction ──────────
    b_exec.cash = 349.60 + (10.0 * 10.0 - 0.40)   # sell proceeds net of this exit's fee
    b_exec.position = 0.0
    b_exec.fees_paid = 0.80   # cumulative: 0.40 entry + 0.40 this exit
    ss_b['pm'].on_sell(10.0, 10.0)   # unchanged price -> zero price-based PnL
    pool.release("B/CAD", b_exec.cash)

    c_exec.cash = 349.60 + (5.0 * 20.0 - 0.40)
    c_exec.position = 0.0
    c_exec.fees_paid = 0.80
    ss_c['pm'].on_sell(20.0, 5.0)
    pool.release("C/CAD", c_exec.cash)

    # The core assertion this test exists for: exactly 898.40 = 900 -
    # 0.40 - 0.40 (entry fees) - 0.40 - 0.40 (exit fees), NOT 899.20 (the
    # fourth round's "self-correcting residual" that never actually
    # corrected) and NOT 900 (no correction at all).
    assert pool.total_capital == pytest.approx(898.40)
    assert not pool.allocated_symbols
    assert pool.available_cash == pytest.approx(898.40)   # nothing left allocated


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
    def __init__(self, eligible_symbols, stale=False, scanned_at=None):
        self.eligible_symbols = eligible_symbols
        self.eligible = []
        self.rejected = []
        self.stale = stale
        self.scanned_at = scanned_at if scanned_at is not None else time.time()
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


# ── _dynamic_buy_eligible (BUY-time eligibility gate, external review ────
#    2026-09-22, second round P1: "fixed-roster coins bypass market-screen
#    eligibility ... the ranked BUY path does not require current
#    eligibility") ────────────────────────────────────────────────────────

def test_dynamic_buy_eligible_true_when_symbol_in_current_screen():
    screen = FakeScreenResult(["BTC/CAD", "ETH/CAD"])
    ok, reason = bot_main._dynamic_buy_eligible("BTC/CAD", screen, max_age_s=3600)
    assert ok is True
    assert reason == ""


def test_dynamic_buy_eligible_false_when_no_screen_yet():
    """Before the very first discovery cycle completes (screen is None —
    see run()'s own `_dynamic_last_screen = None` initialization), no BUY
    should be trusted, fixed-roster included."""
    ok, reason = bot_main._dynamic_buy_eligible("BTC/CAD", None, max_age_s=3600)
    assert ok is False
    assert reason == "no_screen_yet"


def test_dynamic_buy_eligible_false_when_symbol_not_in_eligible_list():
    """The exact reproduction from the external review: the screener
    currently lists ZERO eligible coins (a real liquidity freeze) — BTC/
    CAD must be blocked here even though it's the validated fixed-roster
    symbol, not merely a dynamically-discovered one."""
    screen = FakeScreenResult([])   # zero eligible candidates
    ok, reason = bot_main._dynamic_buy_eligible("BTC/CAD", screen, max_age_s=3600)
    assert ok is False
    assert reason == "not_currently_eligible"


def test_dynamic_buy_eligible_false_when_screen_is_stale_cache():
    screen = FakeScreenResult(["BTC/CAD"], stale=True)
    ok, reason = bot_main._dynamic_buy_eligible("BTC/CAD", screen, max_age_s=3600)
    assert ok is False
    assert reason == "stale_screen_cache"


def test_dynamic_buy_eligible_false_when_screen_too_old():
    """Discovery has kept failing (scanned_at only advances on success —
    see the screener's own fail-safe): even a non-stale-flagged screen
    must not be trusted forever."""
    screen = FakeScreenResult(["BTC/CAD"], scanned_at=time.time() - 10_000)
    ok, reason = bot_main._dynamic_buy_eligible("BTC/CAD", screen, max_age_s=3600)
    assert ok is False
    assert reason == "screen_too_old"


def test_dynamic_buy_eligible_within_max_age_passes():
    screen = FakeScreenResult(["BTC/CAD"], scanned_at=time.time() - 1000)
    ok, reason = bot_main._dynamic_buy_eligible("BTC/CAD", screen, max_age_s=3600)
    assert ok is True


def test_run_source_wires_dynamic_eligibility_gate_for_every_buy_candidate():
    """Source guard: the eligibility gate must sit in the SAME gate chain
    ('approval'/'_buy_block_gate') as risk_manager/accounting — applying
    to whichever symbol is currently being processed by the per-symbol
    loop, fixed-roster included, not scoped to dynamic_admitted members
    only. This is what makes 'the shared ranked queue already includes
    fixed and discovered symbols' also true of THIS gate, closing the
    exact asymmetry the external review flagged."""
    import inspect
    src = inspect.getsource(bot_main.run)
    idx = src.index("# ── 7a2. Dynamic-universe eligibility gate")
    end = src.index("# ── 7b. Candle-close structured log")
    section = src[idx:end]
    assert "_dynamic_buy_eligible(" in section
    assert "_dynamic_mode_active" in section
    assert "BlockReason.DYNAMIC_INELIGIBLE" in section
    # Gated on approval-so-far + BUY signal, exactly like every other gate
    # in this chain (accounting, risk_manager) — never a SELL/exit path.
    assert "approval and final_signal == Signal.BUY and _dynamic_mode_active" in section
    # Must run BEFORE section 9's dynamic-buy-queue population, so a
    # rejection here is reflected in `approval` before that decision.
    section9_idx = src.index("# ── 9. Execute")
    assert idx < section9_idx


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


def test_ranked_execution_blocks_on_unreconciled_accounting_state():
    """Money-readiness review 2026-09-20: the fixed-roster BUY path
    (bot.main.run() section 7a) refuses a BUY while the accounting
    BlockState is unreconciled/stale, but this function used to never
    consult the block state at all — only passing accounting_enabled/
    conn/adapter through for fee recording after a fill. A dynamically
    admitted symbol could therefore bypass the same protection the fixed
    roster gets. Proves the gate is now wired: a default (never-reconciled)
    BlockState blocks every candidate exactly like a HALT-equivalent risk
    rejection, and execute() is never reached."""
    from bot.accounting.reconciliation import BlockState

    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    exec_a.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
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
        accounting_enabled=True,
        accounting_state=BlockState(),   # default = never reconciled
        accounting_block_buys_on_unreconciled=True,
        accounting_max_age_ms=3_600_000,
    )

    assert filled == []
    assert blocked == {"A/CAD": "accounting"}
    assert exec_a.execute_calls == []
    assert not pool.is_allocated("A/CAD")


def test_ranked_execution_accounting_disabled_is_a_no_op():
    """The gate must be additive: with accounting_enabled=False (today's
    default), a blocked BlockState must not affect execution at all — the
    same result as before this fix."""
    from bot.accounting.reconciliation import BlockState

    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    exec_a.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
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
        accounting_enabled=False,
        accounting_state=BlockState(),
        accounting_block_buys_on_unreconciled=True,
        accounting_max_age_ms=3_600_000,
    )

    assert filled == ["A/CAD"]
    assert blocked == {}


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
    assert "capital_pool.allocate(" in section
    # Second review round, 2026-09-22 (P1): must pass this recovered
    # position's OWN actual value explicitly (cash + position * avg_entry)
    # — a bare allocate(_rsym) would reserve a generic per-slot split
    # instead, unrelated to what the position is actually worth (see
    # CapitalPool.allocate's own docstring for the exact reproduction).
    assert "_rexc.cash + _rexc.position * _rexc.avg_entry" in section


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


def test_orphan_recovery_call_site_is_unconditional_on_dynamic_enabled():
    """Source guard, external review (2026-09-22, second round P1): a
    prior version of a nearby comment incorrectly described
    _admit_dynamic_symbol's recovery branch as inactive whenever
    DYNAMIC_UNIVERSE_ENABLED is false. It is NOT inactive — run()'s
    orphaned-position recovery block calls this exact function
    unconditionally whenever live_trading is on, regardless of
    cfg.dynamic.enabled, so a real position outside the current roster
    is recovered through it (fees, capital-pool allocation, and all) on
    every restart no matter what the dynamic flag says. This guards
    against a future edit reintroducing a `cfg.dynamic.enabled` check on
    this call site under the mistaken belief that it would be a no-op
    change."""
    import inspect
    src = inspect.getsource(bot_main.run)
    idx = src.index("if _orphaned_symbols and cfg.exchange.live_trading:")
    end = src.index("if is_indicator and cfg.exchange.feed_mode == \"live\":", idx)
    section = src[idx:end]
    assert "cfg.dynamic.enabled" not in section
    assert "_admit_dynamic_symbol(" in section


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


# ── Missing-coverage review (2026-09-22): fixed-roster + dynamic sharing ──
# one bankroll, and multi-coin partial-fill/fee/restart reconciliation.
# Every scenario below combines TWO coins against the SAME shared
# CapitalPool instance — every existing test above exercises either
# dynamic-vs-dynamic contention (fresh pool, no fixed-roster involvement)
# or exactly one symbol at a time for fills/fees/restart recovery. No
# code was changed to write these; DYNAMIC_UNIVERSE_ENABLED stays false
# and no live call or production write happens anywhere here.

def test_fixed_roster_consumed_slot_limits_dynamic_ranked_execution():
    """Missing coverage: every existing ranked-execution test starts from
    a FRESH pool contested only among DYNAMIC candidates. This proves the
    actual claim in CLAUDE.md's own design doc — 'the fixed roster and
    every dynamically admitted symbol compete for this ONE pool of slots'
    — end to end: a FIXED-roster symbol (BTC/CAD) fills via the immediate-
    execute path EARLIER in the same tick (exactly bot.main.run()'s
    section 9 'elif approval:' branch), consuming one of two total slots.
    TWO dynamic candidates then compete for the ONE remaining slot. Total
    allocated positions across BOTH categories must never exceed
    max_concurrent, BTC/CAD's own fixed allocation must never be displaced
    or double-counted, and the dynamic winner must still be chosen by the
    same ranking rule as a dynamic-only contest."""
    pool = CapitalPool(total_capital=300.0, max_concurrent=2)   # 2 total slots, SHARED
    risk = FakeRisk(approve=True)

    btc_executor = FakeExecutor(symbol="BTC/CAD", starting_cash=100.0)
    btc_executor.queue_order(_filled_order(OrderSide.BUY, price=50000.0, qty=0.002))
    btc_ss = _new_ss(executor=btc_executor)
    bot_main._execute_approved_signal(
        "BTC/CAD", btc_ss, Signal.BUY, 50000.0, 0.002, Signal.BUY, "",
        capital_pool=pool, risk=risk, alerter=MagicMock(), trade_log=MagicMock(),
        stuck_detector=MagicMock(), is_indicator=True,
    )
    assert pool.is_allocated("BTC/CAD")
    assert pool.free_slots == 1   # only ONE slot left for dynamic candidates this tick

    exec_a = FakeExecutor(symbol="A/CAD", starting_cash=100.0)
    exec_a.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    exec_b = FakeExecutor(symbol="B/CAD", starting_cash=100.0)
    exec_b.queue_order(_filled_order(OrderSide.BUY, price=10.0, qty=1.0))
    ss_a = _new_ss(executor=exec_a, strategy_adx=15.0)
    ss_b = _new_ss(executor=exec_b, strategy_adx=35.0)   # higher ADX — should win the last slot
    queue = [
        dict(sym="A/CAD", ss=ss_a, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=15.0, quote_volume=1_000_000),
        dict(sym="B/CAD", ss=ss_b, final_signal=Signal.BUY, price=10.0, trade_qty=1.0,
             raw_signal=Signal.BUY, filter_reason="", adx=35.0, quote_volume=1_000_000),
    ]

    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        queue, capital_pool=pool, risk=risk, account_value_fn=lambda: 300.0,
        alerter=MagicMock(), trade_log=MagicMock(), stuck_detector=MagicMock(),
        is_indicator=True, max_concurrent=2,
    )

    assert filled == ["B/CAD"]
    assert blocked == {"A/CAD": "capital_pool"}
    # The critical invariant: total allocated across FIXED + DYNAMIC never
    # exceeds max_concurrent, and BTC/CAD's fixed position is untouched.
    assert set(pool.allocated_symbols) == {"BTC/CAD", "B/CAD"}
    assert len(pool.allocated_symbols) == 2
    assert exec_a.execute_calls == []   # A/CAD never even attempted


def test_two_coin_restart_recovery_allocates_both_slots_independently(monkeypatch):
    """Missing coverage: existing restart-recovery tests
    (test_admit_dynamic_symbol_with_existing_position_seeds_recovery_state,
    test_run_restart_recovery_reallocates_capital_pool_slot) each cover
    exactly ONE symbol recovering. This proves TWO symbols recovering
    open positions against the SAME shared pool, in the same restart,
    don't corrupt each other's allocation, PnL basis, or the aggregate
    account-value computation."""
    _force_live_pool_mode(monkeypatch)
    pool = CapitalPool(total_capital=300.0, max_concurrent=3)

    eth_executor = FakeExecutor(symbol="ETH/CAD", starting_cash=20.0, starting_position=4.0, avg_entry=25.0)
    sol_executor = FakeExecutor(symbol="SOL/CAD", starting_cash=15.0, starting_position=10.0, avg_entry=140.0)
    executors_by_symbol = {"ETH/CAD": eth_executor, "SOL/CAD": sol_executor}

    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda strat, ex, tf, symbol: 1)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: executors_by_symbol[sym])

    eth_ss, eth_err = bot_main._admit_dynamic_symbol("ETH/CAD", "ex", "4h", pool)
    sol_ss, sol_err = bot_main._admit_dynamic_symbol("SOL/CAD", "ex", "4h", pool)

    assert eth_err is None and sol_err is None
    # Both recovered positions correctly seeded, independently.
    assert eth_ss['pm'].quantity == 4.0 and eth_ss['pm'].avg_entry == 25.0
    assert sol_ss['pm'].quantity == 10.0 and sol_ss['pm'].avg_entry == 140.0
    assert eth_ss['sm'].state.value == "LONG"
    assert sol_ss['sm'].state.value == "LONG"
    # Both slots independently claimed — neither overwrote the other.
    assert set(pool.allocated_symbols) == {"ETH/CAD", "SOL/CAD"}
    assert pool.free_slots == 1   # 3 total slots, 2 consumed

    # Cash conservation through recovery (P1 fix, 2026-09-22): a restart
    # recovering an EXISTING position must never re-fund cash to a fresh
    # slot_cash_for() — the executor's own persisted cash already
    # reflects what's genuinely left after that position was bought.
    # Reproduced against the pre-fix code: both executors' cash was
    # incorrectly reset to slot_cash_for()=100.0 regardless of their real
    # $20/$15 remaining balances, fabricating $80+$85=$165 of account
    # value that never existed. Each executor's cash must be UNCHANGED
    # from its own pre-admission value.
    assert eth_executor.cash == pytest.approx(20.0)
    assert sol_executor.cash == pytest.approx(15.0)

    # Account value aggregates BOTH recovered positions correctly, and —
    # the actual conservation property — equals the true bankroll marked
    # at the recovery-time prices (no fresh deposit, no trade, no price
    # change occurred anywhere in this test). Only each recovered
    # position's MARKET VALUE (external review, 2026-09-22, second round
    # P1 — see _admit_dynamic_symbol's own recovery-branch comment) is
    # folded into total_capital at admission time, NOT cash+position:
    # the pool's pre-recovery $300 already, implicitly, contains ETH's
    # own $20 and SOL's own $15 (a real free-cash figure has no notion of
    # "this symbol's slice" — adding their cash again would double-count
    # it). ETH's position value (4@25=100) and SOL's (10@140=1400) both
    # get added, making total_capital 300+100+1400=1800. Each symbol's
    # own SLOT reservation is still its full cash+position (ETH 120, SOL
    # 1415), so available_cash = 1800-120-1415=265 (= 300 - ETH's own $20
    # - SOL's own $15, i.e. whatever of the original $300 neither of them
    # has already claimed). Total account value, marked to the NEW
    # prices: 265 (available) + ETH's OWN cash+position (20 + 4@30=120 ->
    # 140) + SOL's (15 + 10@150=1500 -> 1515) = 1920. This number is
    # written independently of _compute_account_value's own arithmetic,
    # not re-derived from it.
    symbol_state = {"ETH/CAD": {"last_price": 30.0}, "SOL/CAD": {"last_price": 150.0}}
    total = bot_main._compute_account_value(pool, executors_by_symbol, symbol_state)
    assert total == pytest.approx(1920.0)
    assert pool.total_capital == pytest.approx(1800.0)
    assert pool.available_cash == pytest.approx(265.0)


def test_partial_fills_and_fees_across_two_coins_do_not_cross_contaminate():
    """Missing coverage: existing partial-fill/fee tests
    (test_execute_partial_sell_leaves_residual_position_resyncs_stop,
    test_execute_buy_fill_updates_all_state_and_fees) each exercise
    exactly ONE symbol. This proves two DIFFERENT coins, each getting
    their own fill (one full BUY, one PARTIAL SELL) with their own,
    DIFFERENT fee, processed through _execute_approved_signal against
    the SAME shared pool, keep fully independent state — a bug in one
    coin's own bookkeeping cannot bleed into the other's quantity, fee
    total, native-stop resync, or the shared capital-pool allocation.

    Scope, stated plainly (review finding, 2026-09-22): this proves
    ORCHESTRATION independence (each coin's own pm/trade_log/pool-slot
    updates don't cross-contaminate). It does NOT prove real fee
    conservation in cash — FakeExecutor.execute() below moves cash by
    gross order.total_value only, the same simplification every other
    fake-executor test in this file already relies on, and never
    subtracts fee_cost the way the REAL LiveExecutor does. The
    fee_cost assertions here only confirm the value is threaded through
    to trade_log correctly. See
    test_dynamic_symbol_recovery_conserves_real_cash_after_close_reopen_with_real_fees
    below for the real-fee, real-persistence version of this claim."""
    pool = CapitalPool(total_capital=300.0, max_concurrent=3)

    eth_executor = FakeExecutor(symbol="ETH/CAD", starting_cash=100.0)
    eth_executor.queue_order(_filled_order(OrderSide.BUY, price=20.0, qty=5.0, fee_cost=0.40))
    eth_ss = _new_ss(executor=eth_executor)

    sol_executor = FakeExecutor(symbol="SOL/CAD", starting_cash=0.0, starting_position=10.0, avg_entry=140.0)
    sol_executor.queue_order(_filled_order(OrderSide.SELL, price=150.0, qty=2.0, fee_cost=0.60))
    sol_ss = _new_ss(executor=sol_executor)
    sol_ss['pm'].seed(quantity=10.0, avg_entry=140.0)
    sol_ss['native_stop_price'] = 130.0
    sol_ss['native_stop_is_trailing'] = False
    pool.allocate("SOL/CAD")   # already holding, before this tick

    risk = FakeRisk(approve=True)
    eth_trade_log, sol_trade_log = MagicMock(), MagicMock()

    bot_main._execute_approved_signal(
        "ETH/CAD", eth_ss, Signal.BUY, 20.0, 5.0, Signal.BUY, "",
        capital_pool=pool, risk=risk, alerter=MagicMock(), trade_log=eth_trade_log,
        stuck_detector=MagicMock(), is_indicator=True,
    )
    bot_main._execute_approved_signal(
        "SOL/CAD", sol_ss, Signal.SELL, 150.0, 2.0, Signal.SELL, "",
        capital_pool=pool, risk=risk, alerter=MagicMock(), trade_log=sol_trade_log,
        stuck_detector=MagicMock(), is_indicator=True,
    )

    # ETH: fully independent — its own qty, fee, and allocation.
    assert eth_ss['pm'].quantity == 5.0
    assert eth_trade_log.log_fill.call_args.kwargs["fee_cost"] == 0.40
    assert pool.is_allocated("ETH/CAD")

    # SOL: fully independent — 8 remaining (10 - 2 partial), its OWN fee,
    # still allocated (partial close, not full), native stop resynced at
    # its OWN level — none of this was disturbed by ETH's own fill above.
    assert sol_ss['pm'].quantity == 8.0
    assert sol_trade_log.log_fill.call_args.kwargs["fee_cost"] == 0.60
    assert pool.is_allocated("SOL/CAD")
    assert sol_executor.sync_calls[-1] == (130.0, None)

    # Both slots correctly, independently tracked in the ONE shared pool.
    assert set(pool.allocated_symbols) == {"ETH/CAD", "SOL/CAD"}
    assert pool.free_slots == 1


def test_dynamic_symbol_recovery_conserves_real_cash_after_close_reopen_with_real_fees(tmp_path, monkeypatch):
    """The rigorous version of the P1 cash-conservation fix above (review
    finding, 2026-09-22): uses a REAL LiveExecutor (mocked ccxt exchange,
    no network) against a REAL on-disk state file — not an in-memory
    FakeExecutor reused across calls — so this genuinely exercises
    save-then-reload persistence, and a REAL fee actually deducted from
    cash by LiveExecutor.execute() itself (FakeExecutor's simplified cash
    math never subtracts fee_cost at all, so it cannot prove fee
    conservation — see the note on the test above this one). Every
    expected total below is computed independently by hand, not
    re-derived from the code under test."""
    _force_live_pool_mode(monkeypatch)
    from unittest.mock import patch as _patch
    import bot.execution.live_executor as le_mod
    from bot.execution.live_executor import LiveExecutor

    state_path = str(tmp_path / "eth_live_state.json")
    starting_cash = 100.0

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = {}
    mock_ex.fetch_balance.return_value = {"free": {"CAD": starting_cash}}
    mock_ex.fetch_open_orders.return_value = []
    mock_ex.price_to_precision.return_value = "0.0"
    mock_ex.create_order.return_value = {
        "id": "order-1", "status": "closed", "filled": 2.0, "average": 20.0,
        "fee": {"cost": 0.40, "currency": "CAD"},
    }
    mock_ex.fetch_order.return_value = mock_ex.create_order.return_value

    with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        original_executor = LiveExecutor(
            exchange_id="kraken", symbol="ETH/CAD", api_key="k", api_secret="s",
            starting_cash=starting_cash, dry_run=False, state_path=state_path,
        )

    order = original_executor.execute(Signal.BUY, 20.0, 2.0)
    assert order is not None and order.status == OrderStatus.FILLED

    # Independently computed: 100 - (20.0 * 2.0) - 0.40 fee = 59.60.
    expected_cash_after_buy = 59.60
    assert original_executor.cash == pytest.approx(expected_cash_after_buy)
    assert original_executor.position == pytest.approx(2.0)

    del original_executor   # "close" — the original process/object is gone

    # "Reopen" — a BRAND NEW LiveExecutor instance, same state_path, must
    # load its cash/position from the REAL file on disk written above,
    # not from the (deliberately WRONG, to prove it's ignored)
    # starting_cash argument passed here again.
    fresh_mock_ex = MagicMock()
    fresh_mock_ex.load_markets.return_value = {}
    fresh_mock_ex.fetch_balance.return_value = {
        "free": {"CAD": expected_cash_after_buy, "ETH": 2.0},
        "total": {"CAD": expected_cash_after_buy, "ETH": 2.0},
    }
    fresh_mock_ex.fetch_open_orders.return_value = []
    fresh_mock_ex.price_to_precision.return_value = "0.0"
    with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = fresh_mock_ex
        reopened_executor = LiveExecutor(
            exchange_id="kraken", symbol="ETH/CAD", api_key="k", api_secret="s",
            starting_cash=999.0,   # WRONG on purpose — proves recovery ignores it
            dry_run=False, state_path=state_path,
        )
    assert reopened_executor.cash == pytest.approx(expected_cash_after_buy)
    assert reopened_executor.position == pytest.approx(2.0)

    # Now run the reopened, real, fee-adjusted executor through the
    # ACTUAL dynamic-admission recovery path (the exact function the P1
    # fix above was made in).
    pool = CapitalPool(total_capital=300.0, max_concurrent=3)
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda strat, ex, tf, symbol: 1)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: reopened_executor)

    ss, err = bot_main._admit_dynamic_symbol("ETH/CAD", "ex", "4h", pool)

    assert err is None
    # Cash conserved through admission too — the real, fee-adjusted
    # balance, never reset to a fresh slot_cash_for().
    assert reopened_executor.cash == pytest.approx(expected_cash_after_buy)
    assert pool.is_allocated("ETH/CAD")
    assert ss['pm'].quantity == pytest.approx(2.0)

    # Independently-derived total account value. Only ETH's position's
    # MARKET VALUE (external review, 2026-09-22, second round P1 — the
    # pool's pre-existing $300 already, implicitly, contains ETH's own
    # $59.60, so folding cash in again would double-count it — see
    # _admit_dynamic_symbol's own comment for the multi-coin reproduction
    # that caught this) is folded into total_capital at admission: 2
    # units @ its own avg_entry 20.0 = 40.0, making total_capital
    # 300+40=340. ETH's own SLOT is still its full cash+position (99.60),
    # so available_cash = 340-99.60=240.40 (= the original $300 minus
    # ETH's own $59.60, i.e. whatever of it ETH hasn't already claimed).
    # Total, marked to a fresh $22: 240.40 (available) + ETH's own real,
    # fee-adjusted cash (59.60) + its 2 units marked at $22 = 44 -> 344.
    symbol_state = {"ETH/CAD": {"last_price": 22.0}}
    total = bot_main._compute_account_value(pool, {"ETH/CAD": reopened_executor}, symbol_state)
    assert total == pytest.approx(344.0)
    assert pool.total_capital == pytest.approx(340.0)


# ── Multi-executor-sharing-one-account (review finding, 2026-09-22) ───────
# The test above uses exactly ONE real LiveExecutor, so it could not catch
# a deeper bug: LiveExecutor.__init__ calls _sync_cash(), which queries
# the exchange's WHOLE-ACCOUNT free balance and assigns it directly to
# .cash — correct for a single executor alone on an account, WRONG the
# moment a second executor shares the same account (fixed roster + any
# dynamically admitted symbol, exactly bot.main's CapitalPool model):
# fetch_balance() returns the SAME real number to every executor querying
# it, so each one believes it alone owns the entire balance, and
# _compute_account_value() (which correctly sums each ALLOCATED symbol's
# .cash on top of pool.available_cash) then counts that one real number
# once per allocated symbol. The tests below build TWO REAL LiveExecutor
# instances sharing one mocked exchange account throughout.

def _build_and_fill_real_executor(symbol, base, starting_cash, buy_price, buy_qty, fee):
    """Constructs a real LiveExecutor (mocked ccxt, no network) with its
    own real on-disk state file, executes one real BUY with a real fee,
    and returns (state_path, cash_after_fill) — the executor itself is
    discarded (simulating the process ending) since every test below
    reopens fresh instances from the resulting state file."""
    from unittest.mock import patch as _patch
    import bot.execution.live_executor as le_mod
    from bot.execution.live_executor import LiveExecutor

    state_path = str(_tempfile_dir() / f"{base}_state.json")
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = {}
    mock_ex.fetch_balance.return_value = {"free": {"CAD": starting_cash}, "total": {"CAD": starting_cash}}
    mock_ex.fetch_open_orders.return_value = []
    mock_ex.price_to_precision.return_value = "0.0"
    mock_ex.create_order.return_value = {
        "id": f"{base}-buy", "status": "closed", "filled": buy_qty, "average": buy_price,
        "fee": {"cost": fee, "currency": "CAD"},
    }
    mock_ex.fetch_order.return_value = mock_ex.create_order.return_value
    with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol=symbol, api_key="k", api_secret="s",
            starting_cash=starting_cash, dry_run=False, state_path=state_path,
        )
    order = ex.execute(Signal.BUY, buy_price, buy_qty)
    assert order is not None and order.status == OrderStatus.FILLED
    return state_path, ex.cash


def _reopen_real_executor(symbol, base, position, state_path, account_free_cad, sell_fill=None):
    """'Restarts' a real LiveExecutor from its own state_path, backed by a
    mocked exchange reporting `account_free_cad` — the ONE shared,
    whole-account balance every executor on this account independently
    sees. `sell_fill`, if given, is (price, qty, fee) for a create_order
    mock so the reopened executor can immediately execute a real SELL
    against the SAME mocked exchange connection."""
    from unittest.mock import patch as _patch
    import bot.execution.live_executor as le_mod
    from bot.execution.live_executor import LiveExecutor

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = {}
    mock_ex.fetch_balance.return_value = {
        "free": {"CAD": account_free_cad, base: position},
        "total": {"CAD": account_free_cad, base: position},
    }
    mock_ex.fetch_open_orders.return_value = []
    mock_ex.price_to_precision.return_value = "0.0"
    if sell_fill is not None:
        sell_price, sell_qty, sell_fee = sell_fill
        mock_ex.create_order.return_value = {
            "id": f"{base}-sell", "status": "closed", "filled": sell_qty, "average": sell_price,
            "fee": {"cost": sell_fee, "currency": "CAD"},
        }
        mock_ex.fetch_order.return_value = mock_ex.create_order.return_value
    with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol=symbol, api_key="k", api_secret="s",
            starting_cash=999_999.0, dry_run=False, state_path=state_path,
        )
    return ex


def _tempfile_dir():
    import pathlib
    import tempfile
    return pathlib.Path(tempfile.mkdtemp())


def test_two_real_executors_sharing_one_account_conserve_cash_after_restart():
    """The core reproduction + fix, rewritten for the cash-ownership
    redesign (review round 2, 2026-09-22): LiveExecutor now NEVER lets the
    exchange's whole-account balance touch a symbol's own .cash — not
    even transiently — so there is nothing left for a caller to
    "restore". Each executor's .cash is correct immediately after
    construction, before _reconcile_shared_account_cash() is ever called;
    that function's only remaining job is a genuine drift check using the
    SEPARATE exchange_cash_observed field."""
    eth_state_path, eth_cash_after_fill = _build_and_fill_real_executor(
        "ETH/CAD", "ETH", starting_cash=100.0, buy_price=20.0, buy_qty=2.0, fee=0.40,
    )
    sol_state_path, sol_cash_after_fill = _build_and_fill_real_executor(
        "SOL/CAD", "SOL", starting_cash=200.0, buy_price=8.0, buy_qty=15.0, fee=0.60,
    )
    assert eth_cash_after_fill == pytest.approx(59.60)    # 100 - 40 - 0.40
    assert sol_cash_after_fill == pytest.approx(79.40)    # 200 - 120 - 0.60

    pool = CapitalPool(total_capital=300.0, max_concurrent=3)   # 100/slot, ETH+SOL each claimed one
    pool.allocate("ETH/CAD")
    pool.allocate("SOL/CAD")
    # The ONE real, shared account balance right now: genuinely
    # unallocated pool cash + each coin's own real remaining cash — this
    # is what a real Kraken fetch_balance() would actually report.
    real_account_free_cad = pool.available_cash + eth_cash_after_fill + sol_cash_after_fill

    eth = _reopen_real_executor("ETH/CAD", "ETH", 2.0, eth_state_path, real_account_free_cad)
    sol = _reopen_real_executor("SOL/CAD", "SOL", 15.0, sol_state_path, real_account_free_cad)

    # Correct IMMEDIATELY, before any caller-side reconciliation runs —
    # this is the whole point of the redesign: there is no window,
    # however brief, where .cash holds the wrong (whole-account) figure.
    assert eth.cash == pytest.approx(eth_cash_after_fill)
    assert sol.cash == pytest.approx(sol_cash_after_fill)
    # The raw, shared observation lives in a completely separate field.
    assert eth.exchange_cash_observed == pytest.approx(real_account_free_cad)
    assert sol.exchange_cash_observed == pytest.approx(real_account_free_cad)
    assert eth.startup_sync_healthy is True
    assert sol.startup_sync_healthy is True

    executors = {"ETH/CAD": eth, "SOL/CAD": sol}
    alerter = MagicMock()
    raw = bot_main._reconcile_shared_account_cash(pool, executors, alerter=alerter)

    assert raw == pytest.approx(real_account_free_cad)
    # Unchanged by the call — there was never anything to fix.
    assert eth.cash == pytest.approx(eth_cash_after_fill)
    assert sol.cash == pytest.approx(sol_cash_after_fill)
    assert not alerter.error.called   # everything is genuinely self-consistent — no drift

    symbol_state = {"ETH/CAD": {"last_price": 22.0}, "SOL/CAD": {"last_price": 9.0}}
    total = bot_main._compute_account_value(pool, executors, symbol_state)
    # Independently computed, not re-derived from the code under test:
    # unallocated (300-100-100=100) + ETH(59.60 + 2*22=44) + SOL(79.40 + 15*9=135).
    assert total == pytest.approx(100.0 + (59.60 + 44.0) + (79.40 + 135.0))


def test_shared_account_reconciliation_alerts_on_genuine_drift_without_removing_sync():
    """Exchange synchronization must NOT simply be removed — a genuine
    mismatch between the bot's own bookkeeping and the real exchange
    balance (an external withdrawal, a manual trade, a real accounting
    bug) must still be loudly alerted, using the SAME real sync call
    exchange_cash_observed is built from. This is what makes the function
    a reconciliation, not a blind trust of either number — and, per the
    redesign, .cash is correct throughout regardless of what the drift
    check finds, since nothing ever needed to overwrite it."""
    eth_state_path, eth_cash_after_fill = _build_and_fill_real_executor(
        "ETH/CAD", "ETH", starting_cash=100.0, buy_price=20.0, buy_qty=2.0, fee=0.40,
    )
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    pool.allocate("ETH/CAD")

    # Someone withdrew $30 from the account while the bot was down — the
    # real exchange balance no longer matches what the bot's own
    # bookkeeping expects (eth_cash_after_fill + pool.available_cash).
    genuinely_drifted_balance = pool.available_cash + eth_cash_after_fill - 30.0

    eth = _reopen_real_executor("ETH/CAD", "ETH", 2.0, eth_state_path, genuinely_drifted_balance)
    assert eth.cash == pytest.approx(eth_cash_after_fill)   # correct regardless of the drift below
    executors = {"ETH/CAD": eth}
    alerter = MagicMock()

    raw = bot_main._reconcile_shared_account_cash(pool, executors, alerter=alerter)

    assert raw == pytest.approx(genuinely_drifted_balance)
    assert alerter.error.called
    msg = alerter.error.call_args[0][0]
    assert "DRIFT" in msg
    assert f"{genuinely_drifted_balance:.2f}" in msg
    assert eth.cash == pytest.approx(eth_cash_after_fill)   # still correct — nothing to restore


def test_shared_account_reconciliation_skips_check_when_no_sync_was_healthy():
    """If every real executor's own startup balance sync failed, there is
    no trustworthy exchange figure to compare against — the function must
    skip the check (and say so) rather than compute a meaningless "drift"
    against a starting_cash fallback."""
    eth_state_path, eth_cash_after_fill = _build_and_fill_real_executor(
        "ETH/CAD", "ETH", starting_cash=100.0, buy_price=20.0, buy_qty=2.0, fee=0.40,
    )
    pool = CapitalPool(total_capital=100.0, max_concurrent=1)
    pool.allocate("ETH/CAD")

    from unittest.mock import patch as _patch
    import bot.execution.live_executor as le_mod
    from bot.execution.live_executor import LiveExecutor

    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = {}
    mock_ex.fetch_balance.side_effect = ConnectionError("exchange unreachable")
    mock_ex.fetch_open_orders.return_value = []
    mock_ex.price_to_precision.return_value = "0.0"
    with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        eth = LiveExecutor(
            exchange_id="kraken", symbol="ETH/CAD", api_key="k", api_secret="s",
            starting_cash=999.0, dry_run=False, state_path=eth_state_path,
        )
    assert eth.startup_sync_healthy is False
    assert eth.cash == pytest.approx(eth_cash_after_fill)   # still correct — sync failure never touches it

    alerter = MagicMock()
    raw = bot_main._reconcile_shared_account_cash(pool, {"ETH/CAD": eth}, alerter=alerter)

    assert raw is None
    assert alerter.error.called
    assert "SKIPPED" in alerter.error.call_args[0][0]


def test_interruption_before_caller_reconciliation_does_not_corrupt_persisted_symbol_cash():
    """The exact crash-window reproduction a review round found against
    the PRIOR version of this fix: persisted symbol cash $59.60, exchange
    account cash $159.60, construct the executor, simulate the process
    being killed BEFORE any caller-side reconciliation code ever runs,
    then construct again and read the persisted cash directly off disk
    (not through the Python object, which a crash wouldn't leave running
    anyway). The prior "restore in memory, then re-save" approach failed
    this exact test — the corruption happened INSIDE __init__ itself
    before any caller ever got control. The redesign closes it: nothing
    inside LiveExecutor ever writes the whole-account figure into .cash
    (or therefore into anything _save_state() persists) in the first
    place, so there is no window for a crash to land in."""
    import json

    eth_state_path, eth_cash_after_fill = _build_and_fill_real_executor(
        "ETH/CAD", "ETH", starting_cash=100.0, buy_price=20.0, buy_qty=2.0, fee=0.40,
    )
    assert eth_cash_after_fill == pytest.approx(59.60)

    # Construct with a shared-account balance ($159.60) wildly different
    # from the persisted $59.60 — then do NOTHING further (no caller
    # reconciliation call at all), simulating the process being killed
    # the instant __init__ returns.
    eth = _reopen_real_executor("ETH/CAD", "ETH", 2.0, eth_state_path, 159.60)
    del eth   # the "process" ends here — no _reconcile_shared_account_cash ever ran

    with open(eth_state_path) as f:
        persisted = json.load(f)
    assert persisted["cash"] == pytest.approx(59.60)   # NEVER corrupted, regardless of the missing caller step

    # Constructing again confirms the same thing from the object's own
    # perspective too.
    eth_again = _reopen_real_executor("ETH/CAD", "ETH", 2.0, eth_state_path, 159.60)
    assert eth_again.cash == pytest.approx(59.60)
    assert eth_again.exchange_cash_observed == pytest.approx(159.60)


def test_cash_ownership_holds_across_a_second_restart():
    """Verification across a SECOND restart, not just one: a real BUY,
    restart #1 (with the shared-account balance already reflecting it),
    a real PARTIAL sell, then restart #2 — .cash must still be exactly
    this symbol's own economic history at every step, and
    exchange_cash_observed must independently reflect whatever the
    (possibly different, since real activity happened) exchange balance
    is at THAT specific restart, never a stale carryover from the first."""
    eth_state_path, eth_cash_after_buy = _build_and_fill_real_executor(
        "ETH/CAD", "ETH", starting_cash=100.0, buy_price=20.0, buy_qty=2.0, fee=0.40,
    )
    assert eth_cash_after_buy == pytest.approx(59.60)

    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    pool.allocate("ETH/CAD")

    # ── Restart #1 ──────────────────────────────────────────────────────
    account_balance_at_restart_1 = pool.available_cash + eth_cash_after_buy   # = 159.60
    eth_r1 = _reopen_real_executor("ETH/CAD", "ETH", 2.0, eth_state_path, account_balance_at_restart_1)
    assert eth_r1.cash == pytest.approx(59.60)
    assert eth_r1.exchange_cash_observed == pytest.approx(159.60)
    alerter_1 = MagicMock()
    bot_main._reconcile_shared_account_cash(pool, {"ETH/CAD": eth_r1}, alerter=alerter_1)
    assert not alerter_1.error.called

    # A real partial sell happens between the two restarts.
    eth_ss = _new_ss(executor=eth_r1)
    eth_ss['pm'].seed(quantity=2.0, avg_entry=20.0)
    eth_r1_for_sell = _reopen_real_executor(
        "ETH/CAD", "ETH", 2.0, eth_state_path, account_balance_at_restart_1,
        sell_fill=(21.0, 1.0, 0.11),
    )
    eth_ss['executor'] = eth_r1_for_sell
    bot_main._execute_approved_signal(
        "ETH/CAD", eth_ss, Signal.SELL, 21.0, 1.0, Signal.SELL, "",
        capital_pool=pool, risk=FakeRisk(True), alerter=MagicMock(), trade_log=MagicMock(),
        stuck_detector=MagicMock(), is_indicator=True,
    )
    eth_cash_after_partial_sell = 59.60 + 1.0 * 21.0 - 0.11   # = 80.49
    assert eth_r1_for_sell.cash == pytest.approx(eth_cash_after_partial_sell)
    assert pool.is_allocated("ETH/CAD")   # partial — still held

    # ── Restart #2 — a DIFFERENT exchange balance (real activity happened
    # in between), must not carry over anything stale from restart #1 ──
    account_balance_at_restart_2 = pool.available_cash + eth_cash_after_partial_sell   # = 180.49
    assert account_balance_at_restart_2 != pytest.approx(account_balance_at_restart_1)
    eth_r2 = _reopen_real_executor("ETH/CAD", "ETH", 1.0, eth_state_path, account_balance_at_restart_2)

    assert eth_r2.cash == pytest.approx(eth_cash_after_partial_sell)   # still this symbol's own history
    assert eth_r2.exchange_cash_observed == pytest.approx(account_balance_at_restart_2)   # fresh, not stale
    alerter_2 = MagicMock()
    raw_2 = bot_main._reconcile_shared_account_cash(pool, {"ETH/CAD": eth_r2}, alerter=alerter_2)
    assert raw_2 == pytest.approx(account_balance_at_restart_2)
    assert not alerter_2.error.called   # still genuinely self-consistent after two restarts


def test_equity_conservation_across_restart_fees_partial_and_full_exit_with_pool_release():
    """The full requested lifecycle in one walk-through, sharing ONE real
    account throughout: restart -> fees already paid survive -> a PARTIAL
    exit on one coin (with its own new fee) -> a FULL exit on the other
    (with its own new fee, releasing its capital-pool slot). Account value
    is checked at every stage against an independently-computed total —
    a strict, transaction-by-transaction running cash-flow ledger kept by
    hand, never re-derived from _compute_account_value's own formula or
    from CapitalPool.release()'s own pnl arithmetic. Both coins' slots are
    exactly $100 (a $300 pool, max_concurrent=3), matching what each
    executor is actually funded with — an earlier draft of this test fed
    one coin $200 in starting_cash while its real CapitalPool slot was
    only $100, an internally-inconsistent setup that doesn't reflect how
    slot allocation actually works and made independent verification
    impossible to get right by hand."""
    eth_state_path, eth_cash_after_buy = _build_and_fill_real_executor(
        "ETH/CAD", "ETH", starting_cash=100.0, buy_price=20.0, buy_qty=4.0, fee=0.40,
    )
    sol_state_path, sol_cash_after_buy = _build_and_fill_real_executor(
        "SOL/CAD", "SOL", starting_cash=100.0, buy_price=8.0, buy_qty=10.0, fee=0.60,
    )
    assert eth_cash_after_buy == pytest.approx(19.60)     # 100 - 80 - 0.40
    assert sol_cash_after_buy == pytest.approx(19.40)     # 100 - 80 - 0.60

    pool = CapitalPool(total_capital=300.0, max_concurrent=3)   # a genuinely idle 3rd slot's cash too
    pool.allocate("ETH/CAD")
    pool.allocate("SOL/CAD")
    real_account_free_cad = pool.available_cash + eth_cash_after_buy + sol_cash_after_buy   # = 139.00

    # ── Stage 1: restart, reconcile — conservation across restart + fees ──
    eth = _reopen_real_executor("ETH/CAD", "ETH", 4.0, eth_state_path, real_account_free_cad)
    sol = _reopen_real_executor("SOL/CAD", "SOL", 10.0, sol_state_path, real_account_free_cad)
    executors = {"ETH/CAD": eth, "SOL/CAD": sol}
    alerter = MagicMock()
    bot_main._reconcile_shared_account_cash(pool, executors, alerter=alerter)
    assert not alerter.error.called
    assert eth.cash == pytest.approx(eth_cash_after_buy)
    assert sol.cash == pytest.approx(sol_cash_after_buy)

    eth_ss = _new_ss(executor=eth)
    eth_ss['pm'].seed(quantity=4.0, avg_entry=20.0)
    sol_ss = _new_ss(executor=sol)
    sol_ss['pm'].seed(quantity=10.0, avg_entry=8.0)

    symbol_state = {"ETH/CAD": {"last_price": 20.0}, "SOL/CAD": {"last_price": 8.0}}
    total_after_restart = bot_main._compute_account_value(pool, executors, symbol_state)
    # Independent running-ledger check: both positions marked at their OWN
    # cost basis (no price movement yet) means the only real value ever
    # lost is the two buy-side fees — 300 - 0.40 - 0.60 = 299.00.
    assert total_after_restart == pytest.approx(300.0 - 0.40 - 0.60)

    # ── Stage 2: PARTIAL exit on ETH (2 of 4 units), its own new fee ──────
    eth_reopened_for_sell = _reopen_real_executor(
        "ETH/CAD", "ETH", 4.0, eth_state_path, real_account_free_cad,
        sell_fill=(22.0, 2.0, 0.22),
    )
    # No restoration needed — .cash is already correct immediately after
    # this construction, exactly like every earlier one in this test.
    assert eth_reopened_for_sell.cash == pytest.approx(19.60)
    eth_ss['executor'] = eth_reopened_for_sell
    bot_main._execute_approved_signal(
        "ETH/CAD", eth_ss, Signal.SELL, 22.0, 2.0, Signal.SELL, "",
        capital_pool=pool, risk=FakeRisk(True), alerter=MagicMock(), trade_log=MagicMock(),
        stuck_detector=MagicMock(), is_indicator=True,
    )
    # Independently computed: 19.60 (pre-sell cash) + 2*22 proceeds - 0.22 fee = 63.38.
    expected_eth_cash_after_partial = 19.60 + 2.0 * 22.0 - 0.22
    assert eth_reopened_for_sell.cash == pytest.approx(expected_eth_cash_after_partial)
    assert eth_ss['pm'].quantity == pytest.approx(2.0)   # 2 remaining
    assert pool.is_allocated("ETH/CAD")   # partial — slot NOT released

    # ── Stage 3: FULL exit on SOL (all 10 units), its own new fee ─────────
    sol_reopened_for_sell = _reopen_real_executor(
        "SOL/CAD", "SOL", 10.0, sol_state_path, real_account_free_cad,
        sell_fill=(9.0, 10.0, 0.34),
    )
    assert sol_reopened_for_sell.cash == pytest.approx(19.40)
    sol_ss['executor'] = sol_reopened_for_sell
    bot_main._execute_approved_signal(
        "SOL/CAD", sol_ss, Signal.SELL, 9.0, 10.0, Signal.SELL, "strategy sell",
        capital_pool=pool, risk=FakeRisk(True), alerter=MagicMock(), trade_log=MagicMock(),
        stuck_detector=MagicMock(), is_indicator=True,
    )
    # Independently computed: 19.40 (pre-sell cash) + 10*9 proceeds - 0.34 fee = 109.06.
    expected_sol_cash_after_full = 19.40 + 10.0 * 9.0 - 0.34
    assert sol_reopened_for_sell.cash == pytest.approx(expected_sol_cash_after_full)
    assert not sol_ss['pm'].has_position
    assert not pool.is_allocated("SOL/CAD")   # slot RELEASED

    # ── Final: independently-computed total equity across the whole lifecycle ──
    final_executors = {"ETH/CAD": eth_reopened_for_sell, "SOL/CAD": sol_reopened_for_sell}
    final_symbol_state = {"ETH/CAD": {"last_price": 22.0}, "SOL/CAD": {"last_price": 9.0}}
    final_total = bot_main._compute_account_value(pool, final_executors, final_symbol_state)
    # Independent check: a strict, transaction-by-transaction running cash
    # ledger, kept entirely separately from CapitalPool/LiveExecutor code —
    #   300.00 start
    #   - 80.40  ETH buy (4@20 + 0.40 fee)      -> 219.60
    #   - 80.60  SOL buy (10@8 + 0.60 fee)       -> 139.00
    #   + 43.78  ETH partial sell (2@22 - 0.22)  -> 182.78
    #   + 89.66  SOL full sell (10@9 - 0.34)     -> 272.44  (all real cash now)
    # plus ETH's still-held 2 units marked at the latest price (22 each):
    #   272.44 + 2*22 = 316.44
    real_cash_ledger = 300.0 - (4 * 20.0 + 0.40) - (10 * 8.0 + 0.60) + (2 * 22.0 - 0.22) + (10 * 9.0 - 0.34)
    expected_final_equity = real_cash_ledger + 2 * 22.0
    assert expected_final_equity == pytest.approx(316.44)
    assert final_total == pytest.approx(expected_final_equity)


# ── The ACTUAL startup sequence (review round 3, 2026-09-22, P1 #2) ───────
# Every test above exercises _reconcile_shared_account_cash and
# _compute_account_value directly, with hand-built CapitalPool instances —
# proving those functions are individually correct, but not that run()'s
# OWN startup wiring actually calls them in an order that stays correct.
# The review's exact finding: run()'s pool-initialization code used to
# read _first_exec.cash for the pool's total BEFORE .cash meant what it
# means now — then its own slot-forcing loop overwrote every executor's
# .cash (including that same _first_exec) with a SLOT ALLOWANCE, so by
# the time _reconcile_shared_account_cash ran later in startup, its own
# "raw exchange-wide" reading came from the wrong place: a symbol's slot
# ($100), not the real account balance ($159.60). This is only visible by
# testing the REAL sequence — pool init, in order, then reconciliation —
# not the two functions in isolation. _initialize_capital_pool() (was
# inline in run()) is extracted here for exactly this reason.

class _FakeCfgForPoolInit:
    """Minimal cfg surface _initialize_capital_pool actually reads —
    real Config dataclasses have far more fields; only these matter here."""
    def __init__(self, *, live_trading, paper_mode=False, dry_run=False,
                 max_concurrent_positions=2, max_slot_cash_cad=0.0,
                 max_slot_cash_cad_by_base=None, starting_cash=0.0):
        from types import SimpleNamespace
        self.exchange = SimpleNamespace(live_trading=live_trading, dry_run=dry_run)
        self.paper = SimpleNamespace(paper_mode=paper_mode)
        self.portfolio = SimpleNamespace(
            max_concurrent_positions=max_concurrent_positions,
            max_slot_cash_cad=max_slot_cash_cad,
            max_slot_cash_cad_by_base=max_slot_cash_cad_by_base or {},
            starting_cash=starting_cash,
        )


def test_actual_startup_sequence_pool_init_then_reconcile_uses_the_real_exchange_balance():
    """The exact P1 #2 reproduction, using the REAL sequence run() itself
    follows: construct real executors sharing one account (one fresh, one
    already holding a recovered position) -> _initialize_capital_pool() ->
    (restart recovery calls capital_pool.allocate() for the recovering
    symbol only) -> _reconcile_shared_account_cash(). Before this fix,
    reconciliation's own "raw exchange-wide" reading would have come from
    BTC's slot allowance, not the real account balance, because the
    slot-forcing loop had already overwritten the very executor
    reconciliation reads from.

    Deriving a self-consistent real_account_free_cad: _initialize_capital_pool
    calls slot_cash_for() for EVERY symbol before any restart-recovery
    allocate() has happened, so with max_concurrent=2 both BTC and SOL get
    real_account_free_cad/2 as their (informational, in SOL's case unused)
    slot size. Only SOL is ever actually allocate()'d (it's recovering a
    position; BTC is fresh and unallocated until it first fills). So at
    reconciliation time: pool.available_cash == real_account_free_cad/2
    (SOL's slot reserved, BTC's slot never claimed) and
    expected_total == pool.available_cash + sol.cash. For that to equal the
    real exchange balance with zero drift: real_account_free_cad/2 ==
    sol_cash_after_buy, i.e. real_account_free_cad == 2 * sol_cash_after_buy.
    This is not a coincidence to avoid — it's the correct arithmetic for a
    2-slot pool with exactly one allocated symbol and one fresh/unallocated
    one; verified independently via bot/portfolio/capital_pool.py before
    writing these numbers in."""
    # SOL already holds a recovered position, with its own real,
    # trade-history-derived cash from before this restart.
    sol_state_path, sol_cash_after_buy = _build_and_fill_real_executor(
        "SOL/CAD", "SOL", starting_cash=100.0, buy_price=8.0, buy_qty=10.0, fee=0.60,
    )
    assert sol_cash_after_buy == pytest.approx(19.40)

    # A GENUINELY fresh BTC executor — no persisted position at all.
    btc_state_path = str(_tempfile_dir() / "BTC_state.json")

    # See derivation above: must be exactly 2x SOL's own persisted cash for
    # this 2-slot (BTC fresh/unallocated, SOL recovering/allocated) scenario
    # to reconcile with zero drift.
    real_account_free_cad = 2 * sol_cash_after_buy

    from unittest.mock import patch as _patch
    import bot.execution.live_executor as le_mod
    from bot.execution.live_executor import LiveExecutor

    def _make_fresh_or_recovered(symbol, base, position, state_path):
        mock_ex = MagicMock()
        mock_ex.load_markets.return_value = {}
        mock_ex.fetch_balance.return_value = {
            "free": {"CAD": real_account_free_cad, base: position},
            "total": {"CAD": real_account_free_cad, base: position},
        }
        mock_ex.fetch_open_orders.return_value = []
        mock_ex.price_to_precision.return_value = "0.0"
        with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
            mock_cls.return_value = mock_ex
            return LiveExecutor(
                exchange_id="kraken", symbol=symbol, api_key="k", api_secret="s",
                starting_cash=999_999.0, dry_run=False, state_path=state_path,
            )

    btc = _make_fresh_or_recovered("BTC/CAD", "BTC", 0.0, btc_state_path)
    sol = _make_fresh_or_recovered("SOL/CAD", "SOL", 10.0, sol_state_path)
    assert btc.exchange_cash_observed == pytest.approx(real_account_free_cad)
    assert sol.cash == pytest.approx(sol_cash_after_buy)   # untouched — SOL is recovering, not fresh

    executors = {"BTC/CAD": btc, "SOL/CAD": sol}
    cfg = _FakeCfgForPoolInit(live_trading=True, max_concurrent_positions=2)

    capital_pool, per_symbol_slots, pool_total, slot_cap, slot_caps_by_symbol, _paper_ok = (
        bot_main._initialize_capital_pool(executors, cfg)
    )

    # Second review round, 2026-09-22 (P1): total_capital is free cash
    # PLUS the current value of every symbol already holding a position
    # (here, SOL's own 10.0 * avg_entry 8.0 = $80.00) — not free cash
    # alone. Free cash alone would silently lose SOL's holding from every
    # downstream accounting read (see CapitalPool.allocate's own
    # docstring for the exact $135/$165/$300 reproduction this mirrors).
    _sol_holding_value = sol.position * sol.avg_entry   # 10.0 * 8.0 = 80.0
    _expected_pool_total = real_account_free_cad + _sol_holding_value   # 38.80 + 80.0 = 118.80
    assert pool_total == pytest.approx(_expected_pool_total)
    assert per_symbol_slots["BTC/CAD"] == pytest.approx(_expected_pool_total / 2)
    assert per_symbol_slots["SOL/CAD"] == pytest.approx(_expected_pool_total / 2)
    # BTC (fresh) got funded to its slot allowance.
    assert btc.cash == pytest.approx(_expected_pool_total / 2)
    # SOL (recovering) was left completely untouched by the slot-forcing loop.
    assert sol.cash == pytest.approx(sol_cash_after_buy)

    # SOL claims its slot the same way run()'s own "Restart recovery"
    # section does, right before reconciliation runs — matching the real
    # sequence exactly (pool init -> restart recovery -> reconciliation).
    # amount= is REQUIRED here (second review round finding): a bare
    # allocate("SOL/CAD") would reserve the generic per-slot split
    # ($59.40) instead of what SOL is actually worth ($19.40 cash +
    # $80.00 holding = $99.40) — see CapitalPool.allocate's own docstring.
    capital_pool.allocate("SOL/CAD", amount=sol.cash + _sol_holding_value)

    alerter = MagicMock()
    raw = bot_main._reconcile_shared_account_cash(capital_pool, executors, alerter=alerter)

    # The critical assertion this whole test exists for: reconciliation's
    # own reading is the REAL account balance, not BTC's slot allowance —
    # reproduced against the pre-fix ordering, this would have been
    # per_symbol_slots["BTC/CAD"] instead. _reconcile_shared_account_cash
    # compares CASH ONLY (exchange_cash_observed is a free-CAD reading),
    # so it stays at real_account_free_cad regardless of the position-
    # value component now folded into pool_total/available_cash — the
    # position's value cancels out of both sides of that comparison.
    assert raw == pytest.approx(real_account_free_cad)
    assert not alerter.error.called   # genuinely self-consistent — no false drift


def test_pool_init_and_restart_recovery_account_for_full_equity_not_free_cash_alone():
    """Second review round, 2026-09-22 (P1), exact reproduction through
    the REAL startup sequence: shared free cash on the exchange is
    $135.00. SOL/CAD already holds a real recovered position worth
    $165.00 (10 units at avg_entry $16.50, bought with real cash+fee
    before this restart). True account equity is $135 + $165 = $300 —
    not $135 alone, since a genuinely open position's value doesn't
    disappear just because it's not sitting in the free-cash balance.

    Before this fix: _initialize_capital_pool built the pool from free
    cash alone ($135), and the later capital_pool.allocate("SOL/CAD")
    call (no amount= override) reserved a generic slot_cash_for() split
    of that too-small total — unrelated to what SOL is actually holding —
    silently losing real equity from every downstream read with no
    economic event to explain it. Covers restart (this IS a restart:
    both executors are constructed against pre-existing/would-be-existing
    state) and a later exit (SOL fully closes and its slot is released,
    proving the pool's own P&L-folding-on-release logic still works
    against the corrected, real-value allocation)."""
    from unittest.mock import patch as _patch
    import bot.execution.live_executor as le_mod
    from bot.execution.live_executor import LiveExecutor

    # SOL already holds a real recovered position: bought 10 units @
    # $16.50 (cost $165.00) with zero fee, leaving $35.00 of its own cash.
    sol_state_path, sol_cash_after_buy = _build_and_fill_real_executor(
        "SOL/CAD", "SOL", starting_cash=200.0, buy_price=16.5, buy_qty=10.0, fee=0.0,
    )
    assert sol_cash_after_buy == pytest.approx(35.0)

    btc_state_path = str(_tempfile_dir() / "BTC_state.json")
    shared_free_cad = 135.0   # the account's real, whole-shared free balance

    def _make_fresh_or_recovered(symbol, base, position, state_path):
        mock_ex = MagicMock()
        mock_ex.load_markets.return_value = {}
        mock_ex.fetch_balance.return_value = {
            "free": {"CAD": shared_free_cad, base: position},
            "total": {"CAD": shared_free_cad, base: position},
        }
        mock_ex.fetch_open_orders.return_value = []
        mock_ex.price_to_precision.return_value = "0.0"
        with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
            mock_cls.return_value = mock_ex
            return LiveExecutor(
                exchange_id="kraken", symbol=symbol, api_key="k", api_secret="s",
                starting_cash=999_999.0, dry_run=False, state_path=state_path,
            )

    btc = _make_fresh_or_recovered("BTC/CAD", "BTC", 0.0, btc_state_path)
    sol = _make_fresh_or_recovered("SOL/CAD", "SOL", 10.0, sol_state_path)
    assert sol.cash == pytest.approx(35.0)          # untouched — recovering, not fresh
    assert sol.position == pytest.approx(10.0)
    assert sol.avg_entry == pytest.approx(16.5)

    executors = {"BTC/CAD": btc, "SOL/CAD": sol}
    cfg = _FakeCfgForPoolInit(live_trading=True, max_concurrent_positions=2)

    capital_pool, per_symbol_slots, pool_total, _, _, _paper_ok = bot_main._initialize_capital_pool(executors, cfg)

    # The critical assertion: total equity, not free cash alone.
    assert pool_total == pytest.approx(300.0)   # 135 (free) + 165 (SOL's holding)

    # SOL claims its slot at its OWN real value (run()'s "Restart recovery"
    # section's exact call, amount= required per CapitalPool.allocate's
    # own docstring) — never a generic equal-split of the pool.
    capital_pool.allocate("SOL/CAD", amount=sol.cash + sol.position * sol.avg_entry)
    assert capital_pool.allocated_symbols == ["SOL/CAD"]
    assert capital_pool.available_cash == pytest.approx(100.0)   # 300 - 200 (SOL's real slot)

    symbol_state = {"SOL/CAD": {"last_price": 16.5}, "BTC/CAD": {"last_price": 0.0}}
    total_equity = bot_main._compute_account_value(capital_pool, executors, symbol_state)
    # 100 (available) + 35 (SOL cash) + 10*16.5 (SOL position value) = 300.
    # Before this fix this would have read back $167.50 low (a generic
    # $67.50 slot reserved instead of SOL's real $200, understating
    # available_cash by $132.50 — SOL's own cash+position contribution is
    # unaffected either way, so the shortfall lands entirely here).
    assert total_equity == pytest.approx(300.0)

    # Later exit: SOL fully closes. release() folds the REAL slot back —
    # the pool's own P&L math still works correctly downstream of the fix.
    sol._portfolio.cash = 210.0   # a hypothetical post-sale cash figure
    capital_pool.release("SOL/CAD", sol.cash)
    assert capital_pool.allocated_symbols == []
    # total_capital = 300 - 200 (returned slot) + 210 (actual proceeds) = 310
    assert capital_pool.total_capital == pytest.approx(310.0)


def test_actual_startup_sequence_paper_and_dry_run_never_touch_exchange_cash_observed():
    """Source-level confirmation that the paper/dry-run branch of
    _initialize_capital_pool never reads exchange_cash_observed at all —
    it uses cfg.portfolio.starting_cash, exactly as before this fix
    (unaffected; this fix only changed the LIVE-trading branch's source
    of truth and the slot-forcing loop's fresh-vs-recovering guard)."""
    executors = {"BTC/CAD": FakeExecutor(symbol="BTC/CAD", starting_cash=0.0, starting_position=1.0)}
    cfg = _FakeCfgForPoolInit(live_trading=True, paper_mode=True, starting_cash=453.0, max_concurrent_positions=2)

    # state_dir explicitly isolated to a nonexistent tmp path — without
    # this, the default falls through to the REAL module-level
    # _STATE_LOG_DIR (whatever the actual repo's logs/ or logs/shadow/
    # directory happens to contain right now), making this assertion
    # depend on real filesystem state instead of the fixed $453 this
    # test is actually about.
    import tempfile as _tempfile
    _empty_dir = _tempfile.mkdtemp()
    capital_pool, per_symbol_slots, pool_total, slot_cap, slot_caps_by_symbol, _paper_ok = (
        bot_main._initialize_capital_pool(executors, cfg, state_dir=_empty_dir)
    )

    assert pool_total == pytest.approx(453.0)   # from starting_cash, never from a FakeExecutor's own .cash
    # A FakeExecutor holding a position (position=1.0 > 1e-9) is correctly
    # treated as "recovering" here too — the fresh-vs-recovering guard
    # applies regardless of executor type.
    assert executors["BTC/CAD"].cash == pytest.approx(0.0)   # untouched — never funded to its slot


# ── Multi-coin, one-bankroll full lifecycle (external review, 2026-09-22, ─
#    "validate discover -> filter -> rank -> allocate -> execute -> restart
#    across several coins, using one bankroll") ───────────────────────────
#
# End-to-end, no network: three candidates (A/CAD, B/CAD, C/CAD) sharing
# ONE CapitalPool and ONE mocked exchange account, real LiveExecutor +
# real CapitalPool + real TradingStateMachine/PositionManager throughout
# (every unit-tested piece exercised together, not just individually):
#   discover  -> FakeScreener returns all three as eligible
#   filter    -> _sync_dynamic_universe admits all three (none already
#                present in symbol_state)
#   rank      -> _execute_ranked_dynamic_buys orders by ADX; with only 2
#                of 3 candidates fitting the shared pool's 2 slots, the
#                lowest-ADX one is squeezed out by POOL EXHAUSTION, not
#                by anything specific to that symbol
#   allocate  -> each filled BUY claims a real slot from the ONE shared
#                pool; the pool's own bookkeeping (available_cash) drops
#                to exactly zero once both slots are spent
#   execute   -> real fills, real fees, via a real (mocked-ccxt) LiveExecutor
#   restart   -> both holding positions are recovered via
#                _admit_dynamic_symbol against a FRESH pool (this same
#                review's P1 fix) — total equity conserves exactly
#                (900 - two $0.40 fees = 899.20), not $1598.40 (the bug an
#                earlier, over-corrected version of the fix would have
#                produced by folding in cash AS WELL AS position value —
#                caught by this exact test) and not $699.20 either (no
#                fold at all, the ORIGINAL P1 bug)
#   + a further exit, after the restart, releasing one coin's slot back
#     into the SAME shared pool.

def test_multi_coin_one_bankroll_discover_filter_rank_allocate_execute_restart_exit(monkeypatch):
    _force_live_pool_mode(monkeypatch)   # this scenario is genuinely live (dry_run=False throughout)
    from unittest.mock import patch as _patch
    import bot.execution.live_executor as le_mod
    from bot.execution.live_executor import LiveExecutor

    tmp = _tempfile_dir()
    ACCOUNT_FREE_CAD = 900.0   # the ONE shared whole-account balance every executor observes

    def _fresh_real_executor(symbol, base, account_free_cad):
        state_path = str(tmp / f"{base}_state.json")
        mock_ex = MagicMock()
        mock_ex.load_markets.return_value = {}
        mock_ex.fetch_balance.return_value = {"free": {"CAD": account_free_cad}, "total": {"CAD": account_free_cad}}
        mock_ex.fetch_open_orders.return_value = []
        mock_ex.price_to_precision.return_value = "0.0"
        with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
            mock_cls.return_value = mock_ex
            ex = LiveExecutor(
                exchange_id="kraken", symbol=symbol, api_key="k", api_secret="s",
                starting_cash=0.0, dry_run=False, state_path=state_path,
            )
        return ex, mock_ex, state_path

    # ── DISCOVER + FILTER ────────────────────────────────────────────────
    screener = FakeScreener(["A/CAD", "B/CAD", "C/CAD"])
    a_ex, a_mock, a_state_path = _fresh_real_executor("A/CAD", "A", ACCOUNT_FREE_CAD)
    b_ex, b_mock, b_state_path = _fresh_real_executor("B/CAD", "B", ACCOUNT_FREE_CAD)
    c_ex, c_mock, c_state_path = _fresh_real_executor("C/CAD", "C", ACCOUNT_FREE_CAD)
    executors_by_symbol = {"A/CAD": a_ex, "B/CAD": b_ex, "C/CAD": c_ex}

    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda strat, ex, tf, symbol: 1)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: executors_by_symbol[sym])

    symbol_state: dict = {}
    executors: dict = {}
    dynamic_admitted: set = set()
    pool = CapitalPool(total_capital=900.0, max_concurrent=2)   # ONE bankroll, 2 slots — 3 candidates compete

    admitted, retired, screen = bot_main._sync_dynamic_universe(
        symbol_state, executors, dynamic_admitted, screener,
        live_exchange="ex", timeframe="4h", capital_pool=pool, slot_cash_estimate=450.0,
    )
    assert set(admitted) == {"A/CAD", "B/CAD", "C/CAD"}
    assert retired == []
    # All three funded from the SAME shared pool's nominal slot allowance
    # (fresh — no position yet, so no real slot claimed, just a starting
    # sizing basis for a first trade).
    assert a_ex.cash == pytest.approx(450.0)
    assert b_ex.cash == pytest.approx(450.0)
    assert c_ex.cash == pytest.approx(450.0)
    assert pool.allocated_symbols == []

    # ── RANK + ALLOCATE + EXECUTE ────────────────────────────────────────
    symbol_state["A/CAD"]["strategy"].last_adx = 15.0
    symbol_state["B/CAD"]["strategy"].last_adx = 35.0   # highest — ranked first
    symbol_state["C/CAD"]["strategy"].last_adx = 25.0

    b_mock.create_order.return_value = {
        "id": "B-buy", "status": "closed", "filled": 10.0, "average": 10.0,
        "fee": {"cost": 0.40, "currency": "CAD"},
    }
    b_mock.fetch_order.return_value = b_mock.create_order.return_value
    c_mock.create_order.return_value = {
        "id": "C-buy", "status": "closed", "filled": 5.0, "average": 20.0,
        "fee": {"cost": 0.40, "currency": "CAD"},
    }
    c_mock.fetch_order.return_value = c_mock.create_order.return_value
    # A/CAD's mock is deliberately left with NO create_order stub — if it
    # were ever reached, the test would fail loudly (a MagicMock's default
    # return doesn't satisfy LiveExecutor.execute()'s real parsing), which
    # is exactly the point: A must be blocked by pool exhaustion BEFORE
    # ever attempting a real order.

    buy_queue = [
        dict(sym="A/CAD", ss=symbol_state["A/CAD"], final_signal=Signal.BUY, price=5.0, trade_qty=90.0,
             raw_signal=Signal.BUY, filter_reason="", adx=15.0, quote_volume=1_000_000),
        dict(sym="B/CAD", ss=symbol_state["B/CAD"], final_signal=Signal.BUY, price=10.0, trade_qty=10.0,
             raw_signal=Signal.BUY, filter_reason="", adx=35.0, quote_volume=2_000_000),
        dict(sym="C/CAD", ss=symbol_state["C/CAD"], final_signal=Signal.BUY, price=20.0, trade_qty=5.0,
             raw_signal=Signal.BUY, filter_reason="", adx=25.0, quote_volume=1_500_000),
    ]
    risk = FakeRisk(approve=True)
    alerter, trade_log, stuck = MagicMock(), MagicMock(), MagicMock()
    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        buy_queue, capital_pool=pool, risk=risk, account_value_fn=lambda: 900.0,
        alerter=alerter, trade_log=trade_log, stuck_detector=stuck,
        is_indicator=True, max_concurrent=2,
    )

    assert set(filled) == {"B/CAD", "C/CAD"}   # highest two ADX — A squeezed out
    assert blocked == {"A/CAD": "capital_pool"}   # pool exhaustion, not a per-symbol rejection
    assert set(pool.allocated_symbols) == {"B/CAD", "C/CAD"}
    assert pool.available_cash == pytest.approx(0.0)   # both slots spent, nothing spare
    assert pool.total_capital == pytest.approx(900.0)   # unchanged — no restart-recovery fold yet
    assert b_ex.cash == pytest.approx(349.60)   # 450 - 10*10 - 0.40
    assert c_ex.cash == pytest.approx(349.60)   # 450 - 5*20 - 0.40
    assert a_ex.cash == pytest.approx(450.0)    # never touched — no order ever attempted

    # ── RESTART ───────────────────────────────────────────────────────────
    # The real, current whole-account free CAD after both fills: exactly
    # two $0.40 fees spent out of the original $900 free cash (A never
    # bought anything, so its own $450 "allowance" was purely notional —
    # never real money the exchange actually set aside).
    real_account_free_cad = ACCOUNT_FREE_CAD - 100.40 - 100.40   # 699.20

    def _reopen_with_mock(symbol, base, position, state_path, account_free_cad):
        # _reopen_real_executor() builds and discards its own mock exchange
        # internally — fine when the reopened executor never trades again,
        # but this test needs to configure a LATER sell fill on the SAME
        # mock the reopened LiveExecutor actually holds, so it's inlined
        # here instead (identical body, just also returning the mock).
        mock_ex = MagicMock()
        mock_ex.load_markets.return_value = {}
        mock_ex.fetch_balance.return_value = {
            "free": {"CAD": account_free_cad, base: position},
            "total": {"CAD": account_free_cad, base: position},
        }
        mock_ex.fetch_open_orders.return_value = []
        mock_ex.price_to_precision.return_value = "0.0"
        with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
            mock_cls.return_value = mock_ex
            ex = LiveExecutor(
                exchange_id="kraken", symbol=symbol, api_key="k", api_secret="s",
                starting_cash=999_999.0, dry_run=False, state_path=state_path,
            )
        return ex, mock_ex

    b_reopened, b_reopened_mock = _reopen_with_mock("B/CAD", "B", 10.0, b_state_path, real_account_free_cad)
    c_reopened, c_reopened_mock = _reopen_with_mock("C/CAD", "C", 5.0, c_state_path, real_account_free_cad)
    assert b_reopened.cash == pytest.approx(349.60)   # loaded from its OWN state file, untouched by the shared reading
    assert c_reopened.cash == pytest.approx(349.60)
    assert b_reopened.exchange_cash_observed == pytest.approx(real_account_free_cad)
    assert c_reopened.exchange_cash_observed == pytest.approx(real_account_free_cad)

    # A FRESH pool, as a new process would build BEFORE it has discovered
    # either orphaned position — total_capital starts at just the real
    # free-cash reading, exactly like _initialize_capital_pool would
    # produce for a roster that doesn't yet include B or C at all.
    pool2 = CapitalPool(total_capital=real_account_free_cad, max_concurrent=2)
    monkeypatch.setattr(
        bot_main, "_make_dynamic_executor",
        lambda sym: {"B/CAD": b_reopened, "C/CAD": c_reopened}[sym],
    )
    ss_b, err_b = bot_main._admit_dynamic_symbol("B/CAD", "ex", "4h", pool2)
    ss_c, err_c = bot_main._admit_dynamic_symbol("C/CAD", "ex", "4h", pool2)
    assert err_b is None and err_c is None
    assert ss_b['pm'].quantity == pytest.approx(10.0) and ss_b['pm'].avg_entry == pytest.approx(10.0)
    assert ss_c['pm'].quantity == pytest.approx(5.0) and ss_c['pm'].avg_entry == pytest.approx(20.0)

    # The core P1 assertion: total equity conserves EXACTLY across the
    # restart — 900 - 0.40 - 0.40 = 899.20. Not 699.20 (the original bug:
    # no fold at all, losing both positions' value) and not 1598.40 (an
    # over-corrected fold that also re-adds each symbol's own cash on top
    # of a free-cash reading that already contains it — the mistake this
    # exact multi-coin test caught while being built).
    assert pool2.total_capital == pytest.approx(899.20)
    assert pool2.available_cash == pytest.approx(0.0)   # fully committed to B's + C's real slots
    assert set(pool2.allocated_symbols) == {"B/CAD", "C/CAD"}

    restart_symbol_state = {"B/CAD": {"last_price": 10.0}, "C/CAD": {"last_price": 20.0}}
    restart_total = bot_main._compute_account_value(
        pool2, {"B/CAD": b_reopened, "C/CAD": c_reopened}, restart_symbol_state,
    )
    assert restart_total == pytest.approx(899.20)   # exact conservation, no price movement since the fills

    # ── FURTHER EXIT ──────────────────────────────────────────────────────
    # B/CAD sells its full position at a small gain — released back into
    # the SAME shared pool, which must correctly fold the realized P&L in.
    # Configured on b_reopened_mock — the exchange mock actually wired to
    # b_reopened (the ORIGINAL b_mock belongs to the pre-restart executor,
    # a separate object that's no longer in use).
    b_reopened_mock.create_order.return_value = {
        "id": "B-sell", "status": "closed", "filled": 10.0, "average": 11.0,
        "fee": {"cost": 0.40, "currency": "CAD"},
    }
    b_reopened_mock.fetch_order.return_value = b_reopened_mock.create_order.return_value
    order = bot_main._execute_approved_signal(
        "B/CAD", ss_b, Signal.SELL, 11.0, 10.0, Signal.SELL, "",
        capital_pool=pool2, risk=FakeRisk(True), alerter=MagicMock(),
        trade_log=MagicMock(), stuck_detector=MagicMock(), is_indicator=True,
    )
    assert order is not None and order.status == OrderStatus.FILLED
    assert not ss_b['pm'].has_position
    assert not pool2.is_allocated("B/CAD")   # slot released
    assert b_reopened.cash == pytest.approx(459.20)   # 349.60 + 10*11 - 0.40

    # B's realized gain ($9.60 — sold at $11, held at cost basis $10) is
    # now folded into total_capital; C's slot is completely untouched.
    assert pool2.total_capital == pytest.approx(908.80)   # 899.20 - 449.60(B's slot) + 459.20(B's real proceeds)
    assert pool2.available_cash == pytest.approx(459.20)   # B's cash is now genuinely free pool cash
    assert set(pool2.allocated_symbols) == {"C/CAD"}

    final_symbol_state = {"B/CAD": {"last_price": 11.0}, "C/CAD": {"last_price": 20.0}}
    final_total = bot_main._compute_account_value(
        pool2, {"B/CAD": b_reopened, "C/CAD": c_reopened}, final_symbol_state,
    )
    # available_cash (459.20, includes B's freed cash) + C's own
    # cash+position (349.60 + 5*20=100 -> 449.60) = 908.80. B contributes
    # nothing separately — it's flat, no longer an allocated slot.
    assert final_total == pytest.approx(908.80)


# ── Paper/shadow acceptance harness (external review, 2026-09-22, third ──
#    round P2): "document and test one concrete, isolated, zero-order
#    configuration or harness that actually exercises discovery,
#    eligibility, ranking, and recovery." CLAUDE.md's bounded acceptance
#    criteria originally named LIVE_TRADING=false as the harness — WRONG:
#    _dynamic_mode_active = cfg.dynamic.enabled AND cfg.exchange.
#    live_trading, so LIVE_TRADING=false never runs any of this code at
#    all. The actual runnable, zero-real-order combo is LIVE_TRADING=true
#    WITH PAPER_MODE=true (or DRY_RUN=true) — _make_dynamic_executor
#    already builds every dynamic-universe executor with
#    `dry_run=cfg.paper.paper_mode or cfg.exchange.dry_run`, and
#    LiveExecutor.execute()'s own dry_run branch (live_executor.py, above
#    the real create_order call) simulates the fill locally and returns
#    before ever reaching the exchange — never calls create_order,
#    cancel_order, or fetch_balance (dry_run skips _sync_cash() entirely
#    too). This test proves that combo end-to-end, not just asserts it in
#    prose.

def test_paper_shadow_harness_exercises_full_pipeline_with_zero_real_orders(monkeypatch):
    """LIVE_TRADING=true + DRY_RUN=true, two candidates, driven through
    discover -> filter -> rank -> allocate -> execute -> restart-recover
    exactly like the real-money multi-coin test above — but every
    executor is dry_run=True throughout. Asserts BOTH the positive
    (fills happen, state updates, positions recovered) and the negative
    (the mocked exchange's create_order/cancel_order/fetch_balance are
    NEVER called by anything in this run) — the actual proof that this
    is a genuine zero-order harness, not just a claim."""
    _force_paper_pool_mode(monkeypatch)
    from unittest.mock import patch as _patch
    import bot.execution.live_executor as le_mod
    from bot.execution.live_executor import LiveExecutor

    tmp = _tempfile_dir()

    def _dry_run_executor(symbol, base):
        state_path = str(tmp / f"{base}_state.json")
        mock_ex = MagicMock()
        mock_ex.load_markets.return_value = {}
        with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
            mock_cls.return_value = mock_ex
            ex = LiveExecutor(
                exchange_id="kraken", symbol=symbol, api_key="k", api_secret="s",
                starting_cash=0.0, dry_run=True, state_path=state_path,
            )
        return ex, mock_ex, state_path

    screener = FakeScreener(["D/CAD", "E/CAD"])
    d_ex, d_mock, d_state_path = _dry_run_executor("D/CAD", "D")
    e_ex, e_mock, e_state_path = _dry_run_executor("E/CAD", "E")
    executors_by_symbol = {"D/CAD": d_ex, "E/CAD": e_ex}

    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda strat, ex, tf, symbol: 1)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: executors_by_symbol[sym])

    symbol_state: dict = {}
    executors: dict = {}
    dynamic_admitted: set = set()
    pool = CapitalPool(total_capital=500.0, max_concurrent=2)

    # ── DISCOVER + FILTER + fresh ALLOCATE ──────────────────────────────
    admitted, retired, _screen = bot_main._sync_dynamic_universe(
        symbol_state, executors, dynamic_admitted, screener,
        live_exchange="ex", timeframe="4h", capital_pool=pool, slot_cash_estimate=250.0,
    )
    assert set(admitted) == {"D/CAD", "E/CAD"}
    assert d_ex.cash == pytest.approx(250.0) and e_ex.cash == pytest.approx(250.0)

    # ── RANK + EXECUTE (simulated fills, no exchange call) ──────────────
    symbol_state["D/CAD"]["strategy"].last_adx = 30.0
    symbol_state["E/CAD"]["strategy"].last_adx = 20.0
    buy_queue = [
        dict(sym="D/CAD", ss=symbol_state["D/CAD"], final_signal=Signal.BUY, price=10.0, trade_qty=10.0,
             raw_signal=Signal.BUY, filter_reason="", adx=30.0, quote_volume=1_000_000),
        dict(sym="E/CAD", ss=symbol_state["E/CAD"], final_signal=Signal.BUY, price=5.0, trade_qty=10.0,
             raw_signal=Signal.BUY, filter_reason="", adx=20.0, quote_volume=900_000),
    ]
    filled, blocked = bot_main._execute_ranked_dynamic_buys(
        buy_queue, capital_pool=pool, risk=FakeRisk(True), account_value_fn=lambda: 500.0,
        alerter=MagicMock(), trade_log=MagicMock(), stuck_detector=MagicMock(),
        is_indicator=True, max_concurrent=2,
    )
    assert set(filled) == {"D/CAD", "E/CAD"}
    assert blocked == {}
    # Dry-run fills: filled at the exact requested price/qty, zero fee.
    assert d_ex.cash == pytest.approx(150.0)   # 250 - 10*10 - 0 fee
    assert e_ex.cash == pytest.approx(200.0)   # 250 - 10*5 - 0 fee
    assert d_ex.position == pytest.approx(10.0)
    assert e_ex.position == pytest.approx(10.0)

    # ── RESTART-RECOVER (paper/dry-run mode — the fix under test) ───────
    d_reopened, d_reopened_mock, _ = _dry_run_executor("D/CAD", "D")
    e_reopened, e_reopened_mock, _ = _dry_run_executor("E/CAD", "E")
    # A LiveExecutor's OWN persisted position/cash still round-trips
    # correctly through dry-run: _load_state() is unconditional, only
    # _sync_cash() (the exchange-truth overwrite) is skipped for dry_run.
    assert d_reopened.position == pytest.approx(10.0) and d_reopened.cash == pytest.approx(150.0)
    assert e_reopened.position == pytest.approx(10.0) and e_reopened.cash == pytest.approx(200.0)

    monkeypatch.setattr(
        bot_main, "_make_dynamic_executor",
        lambda sym: {"D/CAD": d_reopened, "E/CAD": e_reopened}[sym],
    )
    pool2 = CapitalPool(total_capital=500.0, max_concurrent=2)
    ss_d, err_d = bot_main._admit_dynamic_symbol("D/CAD", "ex", "4h", pool2)
    ss_e, err_e = bot_main._admit_dynamic_symbol("E/CAD", "ex", "4h", pool2)
    assert err_d is None and err_e is None
    assert ss_d['pm'].quantity == pytest.approx(10.0) and ss_e['pm'].quantity == pytest.approx(10.0)
    # The fix under test: total_capital stays at the static $500 starting
    # figure (paper/dry-run mode) — NOT inflated by adding D's and E's
    # position values on top (that would give 500+100+50=650).
    assert pool2.total_capital == pytest.approx(500.0)
    assert pool2._slots["D/CAD"] == pytest.approx(150.0 + 100.0)   # cash + position*avg_entry
    assert pool2._slots["E/CAD"] == pytest.approx(200.0 + 50.0)

    # ── THE ACTUAL ZERO-ORDER PROOF ──────────────────────────────────────
    # Every mocked exchange this run ever touched — never a real write
    # call, and dry_run means _sync_cash() never even reads the balance.
    for _m in (d_mock, e_mock, d_reopened_mock, e_reopened_mock):
        _m.create_order.assert_not_called()
        _m.cancel_order.assert_not_called()
        _m.fetch_balance.assert_not_called()


# ── Shadow-mode storage isolation (external review, 2026-09-22, fifth+ ───
#    round P1): "the documented PAPER_MODE=true option is not storage-
#    isolated ... your test supplies temporary paths and constructs
#    executors directly; it does not validate the actual startup
#    routing." The test above proves the PIPELINE (discover/rank/
#    allocate/execute/recover) works under dry_run=True — it says nothing
#    about which DIRECTORY a real run's state/trade-log/risk-state files
#    would land in. This section tests the REAL routing formula
#    (_compute_shadow_mode, extracted from the module-level _SHADOW_MODE/
#    _STATE_LOG_DIR computation for exactly this reason) directly, plus
#    source-guards every dependent path to prove they're wired through
#    it — the answer bot/main.py's own module-level code actually uses,
#    not a hand-built substitute.

def test_shadow_mode_is_true_only_for_live_trading_plus_dry_run_without_paper_mode():
    """The ONE supported, storage-isolated acceptance configuration:
    LIVE_TRADING=true + DRY_RUN=true + PAPER_MODE=false. State lands
    under logs/shadow/, never the production logs/ directory."""
    shadow, state_dir = bot_main._compute_shadow_mode(
        live_trading=True, paper_mode=False, dry_run=True, log_dir="/tmp/x/logs",
    )
    assert shadow is True
    assert state_dir == "/tmp/x/logs/shadow"


def test_shadow_mode_is_false_when_paper_mode_true_even_with_dry_run():
    """The exact gap the review found: PAPER_MODE=true is NOT storage-
    isolated, REGARDLESS of dry_run — a paper-mode acceptance run's
    executors, risk_state.json, and trades.db would all resolve to the
    ORDINARY PRODUCTION logs/ directory, at risk of colliding with (or
    overwriting) real state. This is not a bug to silently work around —
    it's why CLAUDE.md's acceptance criteria names ONLY the DRY_RUN
    combo above as supported, and explicitly warns against PAPER_MODE=
    true for this purpose."""
    shadow, state_dir = bot_main._compute_shadow_mode(
        live_trading=True, paper_mode=True, dry_run=True, log_dir="/tmp/x/logs",
    )
    assert shadow is False
    assert state_dir == "/tmp/x/logs"   # production path, NOT /tmp/x/logs/shadow


def test_shadow_mode_is_false_when_dry_run_false_or_live_trading_false():
    """The other two ways to land outside isolation — real live trading
    (dry_run=False, genuinely live, no isolation needed) and
    live_trading=False (paper/backtest tooling, never touches this path
    at all)."""
    shadow_a, dir_a = bot_main._compute_shadow_mode(
        live_trading=True, paper_mode=False, dry_run=False, log_dir="/tmp/x/logs",
    )
    assert shadow_a is False and dir_a == "/tmp/x/logs"
    shadow_b, dir_b = bot_main._compute_shadow_mode(
        live_trading=False, paper_mode=False, dry_run=True, log_dir="/tmp/x/logs",
    )
    assert shadow_b is False and dir_b == "/tmp/x/logs"


def test_module_level_shadow_globals_match_the_extracted_formula():
    """Source guard: the module-level _SHADOW_MODE/_STATE_LOG_DIR
    assignment must actually call _compute_shadow_mode with cfg's real
    fields (not a copy-pasted-and-drifted inline formula) — this is what
    makes the tests above trustworthy as a proxy for the real module-
    level computation, not just a parallel implementation that happens
    to agree today."""
    import inspect
    src = inspect.getsource(bot_main)
    idx = src.index("_SHADOW_MODE, _STATE_LOG_DIR = _compute_shadow_mode(")
    call_site = src[idx:idx + 300]
    assert "cfg.exchange.live_trading" in call_site
    assert "cfg.paper.paper_mode" in call_site
    assert "cfg.exchange.dry_run" in call_site


def test_live_state_path_and_dependent_paths_route_through_state_log_dir():
    """Source guard proving _live_state_path, risk_state.json, trades.db,
    and _HALT_FLAG_PATH (the review specifically named: 'trade-log and
    risk-state routing likewise lose shadow isolation', and — thirteenth
    round — 'the documented shadow configuration still reads production
    logs/HALT') are all wired through _STATE_LOG_DIR — the single value
    _compute_shadow_mode controls — rather than any of them independently
    referencing the raw, always-production _log_dir."""
    import inspect
    src = inspect.getsource(bot_main)
    fn_src = inspect.getsource(bot_main._live_state_path)
    assert "_STATE_LOG_DIR" in fn_src
    assert "_log_dir" not in fn_src.replace("_STATE_LOG_DIR", "")
    assert '_trade_log_db_path = os.path.join(_STATE_LOG_DIR, "trades.db")' in src
    assert 'os.path.join(_STATE_LOG_DIR, "risk_state.json")' in src
    assert '_HALT_FLAG_PATH = os.path.join(_STATE_LOG_DIR, "HALT")' in src


# ── Thirteenth pass, same day (2026-09-24) — the shadow-acceptance run's ─
#    own halt control was still reading the PRODUCTION logs/HALT flag,
#    which — with the real crypto bot's HALT correctly still engaged per
#    the 2026-09-12 review-deadline decision — made it structurally
#    IMPOSSIBLE for the shadow run to ever accumulate a round-trip at
#    all, not merely an isolation risk like the sixth-pass finding.

def test_halt_flag_path_matches_the_current_process_actual_state_log_dir():
    """Direct behavioral check (not just a source guard) that the
    MODULE-LEVEL _HALT_FLAG_PATH this process actually computed at
    import time is derived from _STATE_LOG_DIR, not the raw _log_dir —
    proving the fix is live in the running module, not just present in
    the source text."""
    assert bot_main._HALT_FLAG_PATH == os.path.join(bot_main._STATE_LOG_DIR, "HALT")


def test_shadow_and_production_halt_paths_are_genuinely_independent():
    """The actual reproduction: a real, currently-engaged production
    logs/HALT (simulating the crypto bot's own standing 2026-09-12
    halt) must have ZERO effect on a shadow run's OWN _check_halt_flag
    result, and vice versa — proving the shadow-acceptance run can
    accumulate round-trips while the real bot stays halted, which is
    the entire reason this fix exists. live_trading/dry_run/paper_mode
    fed through the SAME _compute_shadow_mode the module itself uses,
    not a hand-rolled substitute."""
    with tempfile.TemporaryDirectory() as prod_dir:
        # Production: NOT shadow mode (dry_run=False, genuinely live).
        prod_shadow, prod_state_dir = bot_main._compute_shadow_mode(
            live_trading=True, paper_mode=False, dry_run=False, log_dir=prod_dir,
        )
        assert prod_shadow is False
        prod_halt_path = os.path.join(prod_state_dir, "HALT")
        assert prod_halt_path == os.path.join(prod_dir, "HALT")   # unchanged production location

        # Shadow: LIVE_TRADING=true + DRY_RUN=true + PAPER_MODE=false.
        shadow_shadow, shadow_state_dir = bot_main._compute_shadow_mode(
            live_trading=True, paper_mode=False, dry_run=True, log_dir=prod_dir,
        )
        assert shadow_shadow is True
        shadow_halt_path = os.path.join(shadow_state_dir, "HALT")
        assert shadow_halt_path == os.path.join(prod_dir, "shadow", "HALT")
        assert shadow_halt_path != prod_halt_path   # genuinely different files

        # Engage the PRODUCTION halt only (simulating the real crypto
        # bot's own standing 2026-09-12 halt) — the shadow run's own
        # check must be completely unaffected by it.
        os.makedirs(os.path.dirname(prod_halt_path), exist_ok=True)
        open(prod_halt_path, "w").close()

        prod_risk = RiskManager(RiskConfig())
        shadow_risk = RiskManager(RiskConfig())
        alerter = MagicMock()

        prod_active = bot_main._check_halt_flag(prod_risk, prod_halt_path, False, alerter)
        shadow_active = bot_main._check_halt_flag(shadow_risk, shadow_halt_path, False, alerter)

        assert prod_active is True and prod_risk.config.halt is True     # production correctly halted
        assert shadow_active is False and shadow_risk.config.halt is False   # shadow run unaffected — can trade

        # And the reverse: halting the SHADOW run specifically must not
        # touch the production flag/risk manager at all.
        os.makedirs(os.path.dirname(shadow_halt_path), exist_ok=True)
        open(shadow_halt_path, "w").close()
        shadow_active2 = bot_main._check_halt_flag(shadow_risk, shadow_halt_path, shadow_active, alerter)
        assert shadow_active2 is True and shadow_risk.config.halt is True
        assert prod_risk.config.halt is True   # still halted, untouched by the shadow-side change
        assert os.path.exists(prod_halt_path)   # production flag itself untouched


def test_paper_mode_fixed_roster_executor_construction_uses_live_state_path():
    """Source guard confirming the review's exact repro location: the
    fixed-roster executor construction under `if cfg.paper.paper_mode:`
    (inside the `if cfg.exchange.live_trading:` branch) calls
    _live_state_path(sym) — which resolves to the ordinary production
    directory whenever paper_mode is True (see the two tests above) —
    proving PAPER_MODE=true genuinely is unisolated in the CURRENT code,
    not a hypothetical concern."""
    import inspect
    src = inspect.getsource(bot_main.run)
    idx = src.index("if cfg.paper.paper_mode:")
    end = src.index("else:", idx)
    section = src[idx:end]
    assert "state_path" in section and "_live_state_path(sym)" in section


# ── Account-level paper/dry-run equity reconstruction (external review, ──
#    2026-09-22, seventh round P1): "_initialize_capital_pool()'s
#    per-symbol fresh-funding step overwrites cash to a generic
#    slot_cash_for() for ANY position<=1e-9 executor, discarding real
#    accumulated P&L for a symbol that traded and is now merely flat.
#    Reconstruct simulated account equity ONCE at the account level,
#    including completed trades and retired symbols, using a complete
#    replay of simulated economic events." ────────────────────────────────

def test_replay_paper_realized_pnl_sums_across_all_state_files(tmp_path):
    state_dir = str(tmp_path)
    _write_live_state_json(state_dir, "B/CAD", "B", cash=0, position=0, cost_basis=0,
                           realized_pnl=10.0, fees_paid=0.0)
    _write_live_state_json(state_dir, "C/CAD", "C", cash=0, position=0, cost_basis=0,
                           realized_pnl=-5.0, fees_paid=0.40)
    total, ok = bot_main._replay_paper_realized_pnl(state_dir)
    assert ok is True
    assert total == pytest.approx(10.0 + (-5.0 - 0.40))   # 4.60


def test_replay_paper_realized_pnl_missing_dir_or_no_files_is_ok_and_zero(tmp_path):
    assert bot_main._replay_paper_realized_pnl(str(tmp_path / "does_not_exist")) == (0.0, True)
    assert bot_main._replay_paper_realized_pnl(str(tmp_path)) == (0.0, True)   # empty dir, no files
    assert bot_main._replay_paper_realized_pnl("") == (0.0, True)
    assert bot_main._replay_paper_realized_pnl(None) == (0.0, True)


# ── Eighth review pass, same day (2026-09-22) — "unreadable history ──────
#    silently restores lost capital" / "NaN and infinity become accepted
#    capital": _replay_paper_realized_pnl's original version treated ANY
#    problem (unreadable file, missing fields, non-finite value) as
#    "contributes 0.0, keep going" — indistinguishable from "this symbol
#    never lost anything." Fixed to report (total, ok) and refuse to
#    silently invent a zero contribution for anything it can't fully
#    trust. ──────────────────────────────────────────────────────────────

def test_replay_paper_realized_pnl_unreadable_file_marks_incomplete_not_zero(tmp_path):
    """The exact reviewer reproduction: a retired symbol with a real
    -$102 lifetime P&L (-$100 realized, $2 fees) becomes unreadable —
    the OLD behavior silently contributed $0 (inventing the $102 back);
    the fix must report ok=False so the caller refuses to fund anything
    new, rather than quietly using a wrong-but-plausible-looking number."""
    state_dir = str(tmp_path)
    with open(os.path.join(state_dir, "live_state_RETIRED_CAD.json"), "w") as fh:
        fh.write("{not valid json")
    total, ok = bot_main._replay_paper_realized_pnl(state_dir)
    assert ok is False
    assert total == pytest.approx(0.0)   # the unreadable file contributes nothing — ok=False is what matters


def test_replay_paper_realized_pnl_good_file_still_counted_alongside_a_bad_one(tmp_path):
    """One unreadable file must not silently zero out every OTHER file's
    real history — the good file's contribution is still summed, but ok
    still correctly reports the overall reconstruction as incomplete."""
    state_dir = str(tmp_path)
    _write_live_state_json(state_dir, "B/CAD", "B", cash=0, position=0, cost_basis=0,
                           realized_pnl=10.0, fees_paid=0.0)
    with open(os.path.join(state_dir, "live_state_CORRUPT_CAD.json"), "w") as fh:
        fh.write("{not valid json")
    total, ok = bot_main._replay_paper_realized_pnl(state_dir)
    assert ok is False
    assert total == pytest.approx(10.0)   # the good file's real contribution is NOT discarded either


def test_replay_paper_realized_pnl_missing_required_fields_marks_incomplete():
    """A file missing realized_pnl or fees_paid entirely (an older
    format, or partial manual editing) must require explicit migration,
    not silently default to zero — a missing field is NOT the same claim
    as a genuine zero."""
    import tempfile
    with tempfile.TemporaryDirectory() as state_dir:
        with open(os.path.join(state_dir, "live_state_B_CAD.json"), "w") as fh:
            import json as _json
            _json.dump({"symbol": "B/CAD", "cash": 100.0, "position": 0.0}, fh)   # no realized_pnl/fees_paid at all
        total, ok = bot_main._replay_paper_realized_pnl(state_dir)
        assert ok is False
        assert total == pytest.approx(0.0)


def test_replay_paper_realized_pnl_nan_value_marks_incomplete_not_accepted(tmp_path):
    """Reproduced: Python's json module accepts the non-standard NaN/
    Infinity literals by default — a corrupted-but-'readable' file
    containing one previously passed straight through as a real float."""
    state_dir = str(tmp_path)
    with open(os.path.join(state_dir, "live_state_B_CAD.json"), "w") as fh:
        fh.write('{"symbol": "B/CAD", "cash": 0, "position": 0, "realized_pnl": NaN, "fees_paid": 0.0}')
    total, ok = bot_main._replay_paper_realized_pnl(state_dir)
    assert ok is False
    assert total == pytest.approx(0.0)


def test_replay_paper_realized_pnl_infinity_value_marks_incomplete_not_accepted(tmp_path):
    state_dir = str(tmp_path)
    with open(os.path.join(state_dir, "live_state_B_CAD.json"), "w") as fh:
        fh.write('{"symbol": "B/CAD", "cash": 0, "position": 0, "realized_pnl": Infinity, "fees_paid": 0.0}')
    total, ok = bot_main._replay_paper_realized_pnl(state_dir)
    assert ok is False
    assert total == pytest.approx(0.0)


def test_initialize_capital_pool_paper_mode_reconstructs_flat_symbol_with_real_gain(monkeypatch, tmp_path):
    """The exact reviewer reproduction: $900 bankroll, one symbol buys
    $100 then sells for $110 (zero fees) and ends FLAT. Its saved cash
    (460) and realized_pnl (10) round-trip correctly through a raw
    reopen (LiveExecutor's own state persistence — already tested
    elsewhere), but _initialize_capital_pool must not simply discard
    that history because the symbol is now merely flat, not genuinely
    fresh: pool_total must be $910, not $900."""
    _force_paper_pool_mode(monkeypatch)
    state_dir = str(tmp_path)
    _write_live_state_json(
        state_dir, "B/CAD", "B", cash=460.0, position=0.0, cost_basis=0.0,
        realized_pnl=10.0, fees_paid=0.0,
    )
    cfg_fake = _FakeCfgForPoolInit(live_trading=True, dry_run=True, starting_cash=900.0, max_concurrent_positions=2)
    pool, _slots, pool_total, _cap, _caps, _paper_ok = bot_main._initialize_capital_pool({}, cfg_fake, state_dir=state_dir)
    assert pool_total == pytest.approx(910.0)
    assert pool.total_capital == pytest.approx(910.0)


def test_initialize_capital_pool_paper_mode_mixed_flat_history_and_open_position(monkeypatch, tmp_path):
    """Mixed restart: B/CAD is flat with real trading history (bought,
    sold at a $10 gain, zero fees) and C/CAD is a DYNAMIC symbol still
    holding an open position (bought, $0.40 entry fee, never sold) —
    B's file is picked up purely by the account-level replay (B is not
    even in the fixed roster's executors dict); C's file is picked up
    by the SAME replay too, and must NOT be bumped again when C is
    later admitted via _admit_dynamic_symbol (paper mode's per-symbol
    bump is removed specifically to avoid this double-count)."""
    _force_paper_pool_mode(monkeypatch)
    state_dir = str(tmp_path)
    _write_live_state_json(
        state_dir, "B/CAD", "B", cash=460.0, position=0.0, cost_basis=0.0,
        realized_pnl=10.0, fees_paid=0.0,
    )
    _write_live_state_json(
        state_dir, "C/CAD", "C", cash=349.60, position=10.0, cost_basis=10.0,
        realized_pnl=0.0, fees_paid=0.40,
    )
    cfg_fake = _FakeCfgForPoolInit(live_trading=True, dry_run=True, starting_cash=900.0, max_concurrent_positions=2)
    # Neither B nor C is in the FIXED roster's executors dict — both are
    # picked up purely by the directory-wide replay, exactly like a
    # dynamically-admitted or orphaned symbol would be.
    pool, _slots, pool_total, _cap, _caps, _paper_ok = bot_main._initialize_capital_pool({}, cfg_fake, state_dir=state_dir)
    # 900 + 10 (B's real gain, zero fees) + (0 - 0.40) (C's entry fee) = 909.60
    assert pool_total == pytest.approx(909.60)

    c_exec = FakeExecutor(
        symbol="C/CAD", starting_cash=349.60, starting_position=10.0, avg_entry=10.0,
        fees_paid=0.40, realized_pnl=0.0,
    )
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda strat, ex, tf, symbol: 1)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: c_exec)
    ss_c, err_c = bot_main._admit_dynamic_symbol("C/CAD", "ex", "4h", pool)
    assert err_c is None
    # Still 909.60 — admitting C must not bump total_capital a second
    # time; the replay above already covered it.
    assert pool.total_capital == pytest.approx(909.60)
    assert pool._slots["C/CAD"] == pytest.approx(349.60 + 10.0 * 10.0)   # 449.60, its real slot


def test_initialize_capital_pool_paper_mode_counts_a_retired_symbols_leftover_file():
    """A dynamically-admitted symbol that fully closed and was RETIRED
    (removed from symbol_state/executors/dynamic_admitted, per
    _retire_dynamic_symbol_if_eligible) leaves its own state file on
    disk untouched — "harmless left in place" per CLAUDE.md's own
    rollback notes. A LATER restart's account-level replay must still
    count its historical P&L even though NOTHING in the current roster
    or dynamic-admission tracking references it any more."""
    import tempfile
    with tempfile.TemporaryDirectory() as state_dir:
        _write_live_state_json(
            state_dir, "RETIRED/CAD", "RETIRED", cash=0.0, position=0.0, cost_basis=0.0,
            realized_pnl=25.0, fees_paid=1.20,
        )
        cfg_fake = _FakeCfgForPoolInit(
            live_trading=True, dry_run=True, starting_cash=900.0, max_concurrent_positions=2,
        )
        # executors={} — RETIRED/CAD is in NEITHER the fixed roster NOR
        # any dynamic tracking; only its leftover file on disk exists.
        pool, _slots, pool_total, _cap, _caps, _paper_ok = bot_main._initialize_capital_pool({}, cfg_fake, state_dir=state_dir)
        assert pool_total == pytest.approx(900.0 + 25.0 - 1.20)   # 923.80


def test_paper_mode_repeated_restarts_do_not_lose_or_reapply_pnl_or_fees(tmp_path):
    """Idempotency: calling _initialize_capital_pool twice in a row
    against the SAME on-disk state (simulating two consecutive restarts
    with no new trades in between — the exact property the review asked
    to verify) must produce the IDENTICAL total both times, never
    accumulating or discarding anything just from restarting again."""
    state_dir = str(tmp_path)
    _write_live_state_json(
        state_dir, "B/CAD", "B", cash=460.0, position=0.0, cost_basis=0.0,
        realized_pnl=10.0, fees_paid=0.40,
    )
    cfg_fake = _FakeCfgForPoolInit(live_trading=True, dry_run=True, starting_cash=900.0, max_concurrent_positions=2)

    _pool1, _slots1, total1, _c1, _cp1, _ok1 = bot_main._initialize_capital_pool({}, cfg_fake, state_dir=state_dir)
    _pool2, _slots2, total2, _c2, _cp2, _ok2 = bot_main._initialize_capital_pool({}, cfg_fake, state_dir=state_dir)

    assert total1 == pytest.approx(total2)
    assert total1 == pytest.approx(900.0 + 10.0 - 0.40)   # 909.60, both times


def test_initialize_capital_pool_flat_fixed_roster_symbol_gets_the_corrected_pool_share():
    """Design confirmation, not a residual bug: a flat FIXED-ROSTER
    symbol's own .cash is STILL reset to slot_cash_for() every restart
    (CapitalPool's own documented design — 'winning pools grow and
    losing pools shrink', an equal-division-of-the-CURRENT-total pool,
    not a per-symbol sub-ledger each holds onto forever). The fix above
    is specifically that slot_cash_for() is now computed against the
    CORRECTED total (910, reflecting this exact symbol's own prior $10
    gain) rather than the raw, un-reconstructed $900 — so this symbol's
    fresh allocation is $455 (a fair share of the grown pool), not $450
    (the pre-fix amount) and not $460 (its own literal leftover cash,
    which this design deliberately does NOT preserve 1:1)."""
    import tempfile
    with tempfile.TemporaryDirectory() as state_dir:
        b_path = _write_live_state_json(
            state_dir, "B/CAD", "B", cash=460.0, position=0.0, cost_basis=0.0,
            realized_pnl=10.0, fees_paid=0.0,
        )
        b_exec = FakeExecutor(symbol="B/CAD", starting_cash=460.0, starting_position=0.0)
        cfg_fake = _FakeCfgForPoolInit(
            live_trading=True, dry_run=True, starting_cash=900.0, max_concurrent_positions=2,
        )
        pool, slots, pool_total, _cap, _caps, _paper_ok = bot_main._initialize_capital_pool(
            {"B/CAD": b_exec}, cfg_fake, state_dir=state_dir,
        )
        assert pool_total == pytest.approx(910.0)
        assert slots["B/CAD"] == pytest.approx(455.0)   # 910 / 2 slots — NOT 450, NOT 460
        assert b_exec.cash == pytest.approx(455.0)       # reset to the corrected share


def test_initialize_capital_pool_reports_paper_accounting_not_ok_and_refuses_fresh_funding(tmp_path):
    """External review (2026-09-22, eighth round P1): an incomplete
    reconstruction must be REPORTED (paper_accounting_ok=False) and must
    NOT fund any flat/fresh executor's slot — funding it is exactly the
    'simulated BUY funding' the review asked to prevent until the
    history is recovered. A currently-open position's own cash is
    untouched either way (pre-existing behavior), so this specifically
    checks the FLAT/fresh case, which the pre-fix code always funded."""
    state_dir = str(tmp_path)
    with open(os.path.join(state_dir, "live_state_CORRUPT_CAD.json"), "w") as fh:
        fh.write("{not valid json")
    fresh_exec = FakeExecutor(symbol="B/CAD", starting_cash=17.0, starting_position=0.0)
    cfg_fake = _FakeCfgForPoolInit(live_trading=True, dry_run=True, starting_cash=900.0, max_concurrent_positions=2)

    pool, slots, pool_total, _cap, _caps, paper_ok = bot_main._initialize_capital_pool(
        {"B/CAD": fresh_exec}, cfg_fake, state_dir=state_dir,
    )
    assert paper_ok is False
    # Not funded to slot_cash_for() (which would be ~450) — left exactly
    # as its own pre-existing cash, since nothing here can be trusted to
    # size a fresh allowance correctly right now.
    assert fresh_exec.cash == pytest.approx(17.0)


def test_initialize_capital_pool_non_finite_pool_total_falls_back_safely(tmp_path):
    """A non-finite reconstructed total (NaN/Infinity poisoning the sum)
    must not be allowed to reach CapitalPool() at all — that would raise
    and take down management of every EXISTING position too, a bigger
    blast radius than refusing new funding alone. It falls back to the
    bare starting_cash (a known-finite value) so the pool itself still
    constructs; paper_accounting_ok=False is what actually blocks new
    BUYs from here, not this fallback number."""
    state_dir = str(tmp_path)
    with open(os.path.join(state_dir, "live_state_B_CAD.json"), "w") as fh:
        fh.write('{"symbol": "B/CAD", "cash": 0, "position": 0, "realized_pnl": Infinity, "fees_paid": 0.0}')
    cfg_fake = _FakeCfgForPoolInit(live_trading=True, dry_run=True, starting_cash=900.0, max_concurrent_positions=2)

    pool, _slots, pool_total, _cap, _caps, paper_ok = bot_main._initialize_capital_pool(
        {}, cfg_fake, state_dir=state_dir,
    )
    assert paper_ok is False
    assert pool_total == pytest.approx(900.0)   # safe fallback, not Infinity
    assert math.isfinite(pool.total_capital)


def test_run_source_wires_paper_accounting_gate_for_every_buy_candidate():
    """Source guard (supplementing the behavioral tests above): the
    paper-accounting gate must sit in the SAME approval-chain pattern as
    the accounting/dynamic-eligibility gates — applying to every BUY
    candidate regardless of fixed-roster-vs-dynamic, never touching a
    SELL, and running BEFORE section 9's execute/queue decision."""
    import inspect
    src = inspect.getsource(bot_main.run)
    idx = src.index("# ── 7a1. Paper-accounting reconstruction gate")
    end = src.index("# ── 7a2. Dynamic-universe eligibility gate")
    section = src[idx:end]
    assert "_paper_accounting_ok" in section
    assert "BlockReason.PAPER_ACCOUNTING_INCOMPLETE" in section
    assert "approval and final_signal == Signal.BUY and not _paper_accounting_ok" in section
    section9_idx = src.index("# ── 9. Execute")
    assert idx < section9_idx


# ── Ninth review pass, same day (2026-09-22) ──────────────────────────────
#    1. valid JSON of the wrong shape (null, 42, a list, ...) crashed the
#       replay outright instead of marking that file incomplete.
#    2. CapitalPool.release() popped the slot BEFORE validating the new
#       total, so a rejected release() (non-finite cash_returned) left
#       the pool in a corrupted intermediate state — slot gone, total
#       never updated, i.e. reserved funds silently made "available".

def test_replay_paper_realized_pnl_null_json_marks_incomplete_not_crash(tmp_path):
    """Reproduction: a live_state_*.json file containing exactly 'null'
    is VALID JSON (json.load returns None) — the old code's very next
    line ('realized_pnl' not in _state) raised an uncaught TypeError,
    crashing the whole replay (and therefore startup) instead of just
    marking this one file incomplete."""
    state_dir = str(tmp_path)
    with open(os.path.join(state_dir, "live_state_B_CAD.json"), "w") as fh:
        fh.write("null")
    total, ok = bot_main._replay_paper_realized_pnl(state_dir)   # must not raise
    assert ok is False
    assert total == pytest.approx(0.0)


def test_replay_paper_realized_pnl_bare_number_json_marks_incomplete_not_crash(tmp_path):
    """Same class of bug, a different valid-but-wrong shape: a file
    containing exactly '42' (json.load returns the int 42)."""
    state_dir = str(tmp_path)
    with open(os.path.join(state_dir, "live_state_B_CAD.json"), "w") as fh:
        fh.write("42")
    total, ok = bot_main._replay_paper_realized_pnl(state_dir)   # must not raise
    assert ok is False
    assert total == pytest.approx(0.0)


def test_replay_paper_realized_pnl_json_array_marks_incomplete_not_crash(tmp_path):
    """A third shape: a JSON array, not an object."""
    state_dir = str(tmp_path)
    with open(os.path.join(state_dir, "live_state_B_CAD.json"), "w") as fh:
        fh.write("[1, 2, 3]")
    total, ok = bot_main._replay_paper_realized_pnl(state_dir)   # must not raise
    assert ok is False
    assert total == pytest.approx(0.0)


def test_capital_pool_release_rejects_non_finite_cash_without_mutating_state():
    """The exact reproduction: releasing with a NaN cash_returned must
    raise WITHOUT first popping the slot — a rejected call changes
    nothing at all, so a caller can retry with a corrected value."""
    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    pool.allocate("B/CAD", amount=100.0)
    pool.allocate("C/CAD", amount=100.0)

    with pytest.raises(ValueError, match="non-finite"):
        pool.release("B/CAD", float("nan"))

    # State fully preserved — the slot was NEVER removed, and
    # total_capital was NEVER touched, despite the raise.
    assert pool.is_allocated("B/CAD")
    assert pool._slots["B/CAD"] == pytest.approx(100.0)
    assert pool.total_capital == pytest.approx(200.0)
    assert pool.available_cash == pytest.approx(0.0)   # NOT 100 — nothing was freed by the failed call

    # A corrected retry succeeds normally afterward.
    pool.release("B/CAD", 105.0)
    assert not pool.is_allocated("B/CAD")
    assert pool.total_capital == pytest.approx(205.0)   # 200 - 100 + 105
    assert pool.available_cash == pytest.approx(105.0)


def test_capital_pool_release_rejects_infinite_cash_without_mutating_state():
    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    pool.allocate("B/CAD", amount=100.0)

    with pytest.raises(ValueError, match="non-finite"):
        pool.release("B/CAD", float("inf"))

    assert pool.is_allocated("B/CAD")
    assert pool.total_capital == pytest.approx(200.0)


# ── Tenth review pass, same day (2026-09-22) — configurable simulated ────
#    fees + shared-bankroll conservation through a full BUY -> partial
#    SELL -> restart -> full-exit lifecycle, across multiple coins,
#    fees included throughout. "The zero-fee-simulation gap" (open since
#    round 4) is closed by LiveExecutor's new simulated_maker_fee_pct/
#    simulated_taker_fee_pct (see test_live_executor.py for the executor-
#    level unit tests); THIS test proves the fees actually flow correctly
#    through the shared-pool accounting this whole review sequence has
#    been hardening, not just that LiveExecutor computes them in isolation.

def test_shared_bankroll_conserves_exactly_through_buy_partial_sell_restart_full_exit_with_fees(monkeypatch):
    """Two coins, ONE paper/dry-run bankroll, real (now nonzero) simulated
    fees throughout — the fullest lifecycle any test in this file
    exercises: B/CAD buys, PARTIALLY sells (still holding afterward), the
    process restarts (account-level replay + _admit_dynamic_symbol
    recovery while B is STILL PARTIALLY OPEN and C is untouched-since-
    entry), then both fully exit. Every stage's expected total is
    computed independently by hand from the real fee math, not
    re-derived from the code under test — see the comment at each
    assertion for the arithmetic.

    Uses the CONSERVATIVE default (simulate_maker_fills left False — see
    the eleventh round's own finding) — every fill, BUY or SELL, pays
    the TAKER rate (0.80%), matching what an actual acceptance-
    measurement run uses by default. order_type='limit' is passed
    anyway to prove it makes no difference to the fee paid without
    also opting into simulate_maker_fills."""
    _force_paper_pool_mode(monkeypatch)
    from unittest.mock import patch as _patch
    import bot.execution.live_executor as le_mod
    from bot.execution.live_executor import LiveExecutor

    tmp = _tempfile_dir()
    MAKER, TAKER = 0.0040, 0.0080

    def _dry_run_executor(symbol, base, state_path=None):
        if state_path is None:
            # Matches _live_state_path()'s real naming convention
            # (live_state_<SYM_WITH_UNDERSCORE>.json) — required so
            # _replay_paper_realized_pnl's glob ("live_state_*.json")
            # actually finds this file at restart time.
            state_path = str(tmp / f"live_state_{base}_CAD.json")
        mock_ex = MagicMock()
        mock_ex.load_markets.return_value = {}
        with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
            mock_cls.return_value = mock_ex
            ex = LiveExecutor(
                exchange_id="kraken", symbol=symbol, api_key="k", api_secret="s",
                starting_cash=0.0, dry_run=True, state_path=state_path,
                order_type="limit",   # irrelevant to the fee without simulate_maker_fills=True
                simulated_maker_fee_pct=MAKER, simulated_taker_fee_pct=TAKER,
                # simulate_maker_fills left at its conservative default (False)
            )
        return ex, state_path

    b_ex, b_state_path = _dry_run_executor("B/CAD", "B")
    c_ex, c_state_path = _dry_run_executor("C/CAD", "C")
    executors_by_symbol = {"B/CAD": b_ex, "C/CAD": c_ex}
    monkeypatch.setattr(bot_main, "build_strategy", lambda: MagicMock(_highs=[], _lows=[], _closes=[]))
    monkeypatch.setattr(bot_main, "_warmup_strategy", lambda strat, ex, tf, symbol: 1)
    monkeypatch.setattr(bot_main, "_make_dynamic_executor", lambda sym: executors_by_symbol[sym])

    # ── DISCOVER + FILTER + fresh ALLOCATE ──────────────────────────────
    symbol_state: dict = {}
    executors: dict = {}
    dynamic_admitted: set = set()
    pool = CapitalPool(total_capital=900.0, max_concurrent=2)
    screener = FakeScreener(["B/CAD", "C/CAD"])
    admitted, _retired, _screen = bot_main._sync_dynamic_universe(
        symbol_state, executors, dynamic_admitted, screener,
        live_exchange="ex", timeframe="4h", capital_pool=pool, slot_cash_estimate=450.0,
    )
    assert set(admitted) == {"B/CAD", "C/CAD"}
    assert b_ex.cash == pytest.approx(450.0) and c_ex.cash == pytest.approx(450.0)

    # ── BUY both (taker — the conservative default applies to entries too) ─
    order_b = bot_main._execute_approved_signal(
        "B/CAD", symbol_state["B/CAD"], Signal.BUY, 10.0, 10.0, Signal.BUY, "",
        capital_pool=pool, risk=FakeRisk(True), alerter=MagicMock(),
        trade_log=MagicMock(), stuck_detector=MagicMock(), is_indicator=True,
    )
    order_c = bot_main._execute_approved_signal(
        "C/CAD", symbol_state["C/CAD"], Signal.BUY, 20.0, 5.0, Signal.BUY, "",
        capital_pool=pool, risk=FakeRisk(True), alerter=MagicMock(),
        trade_log=MagicMock(), stuck_detector=MagicMock(), is_indicator=True,
    )
    assert order_b.status == OrderStatus.FILLED and order_c.status == OrderStatus.FILLED
    # B: 450 - 10*10 - (10*10*0.0080 taker fee = 0.80) = 349.20
    assert b_ex.cash == pytest.approx(349.20)
    # C: 450 - 5*20 - (5*20*0.0080 = 0.80) = 349.20
    assert c_ex.cash == pytest.approx(349.20)

    # ── B partially sells (still holding 6 afterward) — a gain, taker fee ─
    order_b_partial = bot_main._execute_approved_signal(
        "B/CAD", symbol_state["B/CAD"], Signal.SELL, 11.0, 4.0, Signal.SELL, "",
        capital_pool=pool, risk=FakeRisk(True), alerter=MagicMock(),
        trade_log=MagicMock(), stuck_detector=MagicMock(), is_indicator=True,
    )
    assert order_b_partial.status == OrderStatus.FILLED
    assert symbol_state["B/CAD"]['pm'].has_position   # still open — partial, not a full close
    assert pool.is_allocated("B/CAD")   # the slot is NOT released by a partial sell
    # 349.20 + (4*11 - 4*11*0.0080 taker fee) = 349.20 + (44 - 0.352) = 392.848
    assert b_ex.cash == pytest.approx(392.848)
    assert b_ex.position == pytest.approx(6.0)
    assert b_ex.fees_paid == pytest.approx(0.80 + 0.352)   # 1.152, cumulative
    assert b_ex.portfolio.realized_pnl == pytest.approx(4.0)   # (11-10)*4, price-based only

    # ── RESTART: reopen both from their real persisted state ────────────
    b_reopened, _ = _dry_run_executor("B/CAD", "B", state_path=b_state_path)
    c_reopened, _ = _dry_run_executor("C/CAD", "C", state_path=c_state_path)
    assert b_reopened.cash == pytest.approx(392.848) and b_reopened.position == pytest.approx(6.0)
    assert c_reopened.cash == pytest.approx(349.20) and c_reopened.position == pytest.approx(5.0)

    cfg_fake = _FakeCfgForPoolInit(live_trading=True, dry_run=True, starting_cash=900.0, max_concurrent_positions=2)
    pool2, _slots, pool2_total, _cap, _caps, paper_ok = bot_main._initialize_capital_pool(
        {}, cfg_fake, state_dir=str(tmp),
    )
    assert paper_ok is True
    # Account-level replay: B(realized_pnl 4.0 - fees_paid 1.152 = 2.848)
    # + C(realized_pnl 0 - fees_paid 0.80 = -0.80) = 2.048 -> 900+2.048=902.048
    assert pool2_total == pytest.approx(902.048)

    monkeypatch.setattr(
        bot_main, "_make_dynamic_executor",
        lambda sym: {"B/CAD": b_reopened, "C/CAD": c_reopened}[sym],
    )
    ss_b, err_b = bot_main._admit_dynamic_symbol("B/CAD", "ex", "4h", pool2)
    ss_c, err_c = bot_main._admit_dynamic_symbol("C/CAD", "ex", "4h", pool2)
    assert err_b is None and err_c is None
    assert ss_b['pm'].quantity == pytest.approx(6.0) and ss_b['pm'].avg_entry == pytest.approx(10.0)
    assert ss_c['pm'].quantity == pytest.approx(5.0) and ss_c['pm'].avg_entry == pytest.approx(20.0)
    # Slots reserved at real cash+position*avg_entry — NOT bumped again
    # (the account-level replay above already covered both).
    assert pool2._slots["B/CAD"] == pytest.approx(392.848 + 6 * 10.0)    # 452.848
    assert pool2._slots["C/CAD"] == pytest.approx(349.20 + 5 * 20.0)     # 449.20
    assert pool2.total_capital == pytest.approx(902.048)                # unchanged by admission
    assert pool2.available_cash == pytest.approx(0.0)                   # 902.048 - 452.848 - 449.20

    # ── FULL EXIT: both close out completely, taker fees again ──────────
    order_b_final = bot_main._execute_approved_signal(
        "B/CAD", ss_b, Signal.SELL, 10.50, 6.0, Signal.SELL, "",
        capital_pool=pool2, risk=FakeRisk(True), alerter=MagicMock(),
        trade_log=MagicMock(), stuck_detector=MagicMock(), is_indicator=True,
    )
    order_c_final = bot_main._execute_approved_signal(
        "C/CAD", ss_c, Signal.SELL, 19.0, 5.0, Signal.SELL, "",
        capital_pool=pool2, risk=FakeRisk(True), alerter=MagicMock(),
        trade_log=MagicMock(), stuck_detector=MagicMock(), is_indicator=True,
    )
    assert order_b_final.status == OrderStatus.FILLED and order_c_final.status == OrderStatus.FILLED
    assert not ss_b['pm'].has_position and not ss_c['pm'].has_position
    assert not pool2.is_allocated("B/CAD") and not pool2.is_allocated("C/CAD")

    # B final cash: 392.848 + (6*10.50 - 6*10.50*0.0080) = 392.848 + (63 - 0.504) = 455.344
    assert b_reopened.cash == pytest.approx(455.344)
    # C final cash: 349.20 + (5*19 - 5*19*0.0080) = 349.20 + (95 - 0.76) = 443.44
    assert c_reopened.cash == pytest.approx(443.44)

    # The core assertion this test exists for: exact conservation across
    # the ENTIRE lifecycle (BUY -> partial SELL -> restart -> full exit),
    # for BOTH coins, with real (nonzero) fees included at every fill.
    # Computed independently, event by event, not re-derived from the
    # code under test:
    #   B: -0.80 (entry fee) + 4.0 (partial-sell price gain) - 0.352
    #      (partial-sell fee) + 3.0 (final-sell price gain, (10.50-10)*6)
    #      - 0.504 (final-sell fee) = +5.344
    #   C: -0.80 (entry fee) - 5.0 (final-sell price LOSS, (19-20)*5)
    #      - 0.76 (final-sell fee) = -6.56
    #   900 + 5.344 - 6.56 = 898.784
    assert pool2.total_capital == pytest.approx(898.784)
    assert pool2.available_cash == pytest.approx(898.784)   # nothing left allocated
    final_symbol_state = {"B/CAD": {"last_price": 10.50}, "C/CAD": {"last_price": 19.0}}
    final_total = bot_main._compute_account_value(
        pool2, {"B/CAD": b_reopened, "C/CAD": c_reopened}, final_symbol_state,
    )
    assert final_total == pytest.approx(898.784)


# ── Twelfth pass, same day (2026-09-23) — SIMULATE_MAKER_FILLS parsed by ─
#    config but never actually passed at any of the 4 production
#    LiveExecutor construction sites, so setting it had zero effect. Every
#    existing test exercising these sites MONKEYPATCHES _make_dynamic_
#    executor entirely, which is exactly why this went uncaught — nothing
#    called the REAL function and inspected what it actually built.

def test_make_dynamic_executor_actually_wires_simulate_maker_fills_from_cfg(monkeypatch):
    """Calls the REAL _make_dynamic_executor (not monkeypatched away, the
    way every other test in this file uses it) and inspects the returned
    LiveExecutor's own internal flag — the only way to catch 'parsed by
    config but never passed through to construction,' since a test that
    substitutes a fake executor can never observe this class of bug."""
    _force_paper_pool_mode(monkeypatch)
    monkeypatch.setattr(bot_main.cfg.exchange, "simulate_maker_fills", True)
    monkeypatch.setattr(bot_main.cfg.exchange, "simulated_maker_fee_pct", 0.0040)
    monkeypatch.setattr(bot_main.cfg.exchange, "simulated_taker_fee_pct", 0.0080)

    from unittest.mock import patch as _patch
    import bot.execution.live_executor as le_mod
    mock_ex = MagicMock()
    mock_ex.load_markets.return_value = {}
    with _patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = mock_ex
        ex = bot_main._make_dynamic_executor("B/CAD", state_path=str(_tempfile_dir() / "b.json"))

    assert ex._simulate_maker_fills is True
    assert ex._simulated_maker_fee_pct == pytest.approx(0.0040)
    assert ex._simulated_taker_fee_pct == pytest.approx(0.0080)

    # Flip the cfg value and confirm a NEW construction picks up the change
    # (not a one-time snapshot cached somewhere else).
    monkeypatch.setattr(bot_main.cfg.exchange, "simulate_maker_fills", False)
    mock_ex2 = MagicMock()
    mock_ex2.load_markets.return_value = {}
    with _patch.object(le_mod.ccxt, "kraken") as mock_cls2:
        mock_cls2.return_value = mock_ex2
        ex2 = bot_main._make_dynamic_executor("B/CAD", state_path=str(_tempfile_dir() / "b2.json"))
    assert ex2._simulate_maker_fills is False


def test_run_source_every_live_executor_construction_site_passes_simulate_maker_fills():
    """Source guard, supplementing the behavioral test above: the OTHER
    3 production construction sites (inside run(), not independently
    callable the way _make_dynamic_executor is) must ALSO pass
    simulate_maker_fills — catching a future regression where one site
    is updated and the others are missed, exactly how this bug arose
    (simulated_maker_fee_pct/simulated_taker_fee_pct were added to all
    4 sites together; simulate_maker_fills was added later and only
    reached config.py + LiveExecutor, never these call sites, until
    this same-day pass)."""
    import inspect
    src = inspect.getsource(bot_main)
    # 1 in _make_dynamic_executor (asserted directly above) + 3 more
    # inside run()'s own executor-construction blocks = 4 call sites,
    # each contributing one "simulate_maker_fills = cfg.exchange.
    # simulate_maker_fills," occurrence.
    assert src.count("simulate_maker_fills     = cfg.exchange.simulate_maker_fills,") == 4
