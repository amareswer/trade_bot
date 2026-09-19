"""
Stateful fake-exchange / crash-injection harness (PASS-5 review, 2026-09-19).

The reviewer's explicit ask: "test one consistent order-state transition
model across BUY, SELL and protection roles. Use a stateful fake exchange
whose orders, fills and balances agree, then restart the executor at each
persistence boundary. Final cash, inventory and ledger must match
uninterrupted execution." Prior rounds' regression tests injected crashes
using independently-configured per-call MagicMocks — correct for the
specific bug each one reproduced, but not a genuine "does the real economic
outcome converge" check, since nothing enforced that orders/fills/balances
stayed mutually consistent across the crash boundary the way a real
exchange's own state does.

FakeExchange below is a MINIMAL but STATEFUL double: one internal order
book and one internal balance dict, read and written by every method
consistently. simulate_fill() plays the role of "the exchange's own
matching engine" progressing an order to a new cumulative filled amount
and moving the balance accordingly — entirely independent of whether, or
when, the bot's own process has noticed. This is what makes "restart and
recover" meaningful to test: the bot process can crash and restart against
this exact same object (simulating that Kraken itself is unaffected by the
bot's own downtime), and the assertion is genuine economic convergence,
not a hand-computed expected number.

For each role (BUY entry, SELL exit, protective-stop placement/cancel), a
crash is injected immediately after the real economic effect would have
been computed but BEFORE it can be durably saved, then a fresh
LiveExecutor is constructed against the SAME FakeExchange + the same
(crash-preserved) state file, and the test asserts the final cash/
position/fees state exactly matches what an UNINTERRUPTED run of the
identical fill sequence against a separate, otherwise-identical
FakeExchange produces — without ever calling execute() a second time for
the same role (PASS-5 finding 5's own acceptance criterion), instead
relying on reconcile_pending_orders() alone for every fill after the
first, in both the baseline and the crashed run.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import ccxt
import bot.execution.live_executor as le_mod
from bot.execution.executor import OrderSide, OrderStatus
from bot.execution.live_executor import LiveExecutor
from bot.strategy.threshold_strategy import Signal


class FakeExchange:
    """A stateful, minimal ccxt-like double. Orders and balances live in
    ONE internal store shared by every method — unlike per-call
    MagicMocks, calling fetch_order/fetch_open_orders/fetch_balance always
    agrees with whatever create_order/simulate_fill/cancel_order actually
    did, exactly like a real exchange."""

    def __init__(self, cash: float = 1000.0, position: float = 0.0,
                 quote: str = "CAD", base: str = "BTC"):
        self.quote = quote
        self.base = base
        self._balance: dict = {quote: cash, base: position}
        self._orders: dict[str, dict] = {}
        self._next_id = 1
        self._queued_fill = None   # (cumulative_filled, avg_price, cumulative_fee, terminal)
        self._next_cancel_fails = False

    # -- markets / precision ----------------------------------------------
    def load_markets(self):
        return {f"{self.base}/{self.quote}": {}}

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"

    # -- test-driver API ----------------------------------------------------
    def queue_fill_on_create(self, cumulative_filled, avg_price, cumulative_fee=0.0, terminal=False):
        """Real market orders often fill (fully or partially) essentially
        instantly — the NEXT create_order() call applies this fill to the
        order it creates, immediately, before returning. Lets a test fully
        control the exact state execute()'s first read observes."""
        self._queued_fill = (cumulative_filled, avg_price, cumulative_fee, terminal)

    def simulate_fill(
        self, order_id: str, cumulative_filled: float, avg_price: float,
        cumulative_fee: float = 0.0, terminal: bool = True,
    ) -> None:
        """Progress order_id to a new CUMULATIVE filled amount, moving the
        balance by only the DELTA since the last call — a real exchange's
        matching engine does this continuously, independent of whether the
        bot's own process is even running to notice.

        PASS-6 review finding (P2): this used to write cumulative order
        cost as `cumulative_filled * avg_price` but move the quote balance
        by `delta_qty * avg_price` — correct only when avg_price (which is
        the CUMULATIVE average, not the latest execution's own price) is
        unchanged across calls. Reproduced exactly: sell 0.001 BTC at
        cumulative average $90,000 (cost $90), then reach cumulative
        0.002 BTC at average $95,000 (cost correctly $190) — the OLD code
        moved cash by only 0.001*$95,000=$95 for the second call (using
        the NEW average against just the delta quantity), landing at
        $1,185 instead of the correct $1,190 (delta_qty*avg_price
        silently assumes the delta itself executed at the CUMULATIVE
        average, not its own true price of $100,000 implied by
        cost($190)-cost($90) over 0.001). Fixed: move balance by the
        change in cumulative quote COST (this order's own previously
        stored cost vs. its new one) — independent of quantity/fee deltas
        and correct regardless of how the per-fill price varies."""
        o = self._orders[order_id]
        prev_filled = o["filled"]
        prev_cost   = o["cost"]     # cumulative cost as of the LAST simulate_fill call
        prev_fee    = float(o["fee"].get("cost") or 0.0)
        new_cost    = cumulative_filled * avg_price
        delta_qty   = cumulative_filled - prev_filled
        delta_cost  = new_cost - prev_cost
        delta_fee   = cumulative_fee - prev_fee
        o["filled"]  = cumulative_filled
        o["average"] = avg_price
        o["cost"]    = new_cost
        o["fee"]     = {"cost": cumulative_fee, "currency": self.quote}
        if terminal:
            o["status"] = "closed"
        if delta_qty > 0 or delta_cost != 0:
            if o["side"] == "buy":
                self._balance[self.quote] -= delta_cost
                self._balance[self.base]  += delta_qty
            else:
                self._balance[self.quote] += delta_cost
                self._balance[self.base]  -= delta_qty
        if delta_fee > 0:
            self._balance[self.quote] -= delta_fee

    # -- order lifecycle ----------------------------------------------------
    def create_order(self, symbol, type, side, amount, price=None, params=None):
        # Parameter named `type` (shadowing the builtin) to match ccxt's
        # own real keyword name — bot/execution/live_executor.py calls
        # create_order with `type=...` as a keyword in some call sites.
        params = dict(params or {})
        oid = f"O{self._next_id}"
        self._next_id += 1
        is_stop = bool(params.get("stopLossPrice")) or bool(params.get("trailingPercent"))
        self._orders[oid] = {
            "id": oid, "symbol": symbol, "type": type, "side": side,
            "amount": amount, "price": price, "status": "open",
            "filled": 0.0, "average": None, "cost": 0.0,
            "fee": {"cost": 0.0, "currency": self.quote},
            "clientOrderId": params.get("clientOrderId"),
            "info": {"descr": {"ordertype": "stop-loss" if is_stop else type}},
        }
        if self._queued_fill is not None:
            cf, ap, cfee, term = self._queued_fill
            self._queued_fill = None
            self.simulate_fill(oid, cf, ap, cfee, terminal=term)
        return dict(self._orders[oid])

    def fetch_order(self, order_id, symbol=None):
        if order_id not in self._orders:
            raise ccxt.OrderNotFound(f"{order_id} not found")
        return dict(self._orders[order_id])

    def fetch_open_orders(self, symbol=None):
        return [dict(o) for o in self._orders.values() if o["status"] == "open"]

    def fetch_closed_orders(self, symbol=None, limit=10):
        closed = [o for o in self._orders.values() if o["status"] != "open"]
        return [dict(o) for o in closed[-limit:]]

    def queue_cancel_failure(self):
        """Simulate a cancel racing a fill (or a transient network blip):
        the NEXT cancel_order() call raises without changing order state —
        the order remains exactly as it was (still open if it still is)."""
        self._next_cancel_fails = True

    def cancel_order(self, order_id, symbol=None):
        if self._next_cancel_fails:
            self._next_cancel_fails = False
            raise ccxt.NetworkError("simulated cancel race")
        o = self._orders.get(order_id)
        if o is None:
            raise ccxt.OrderNotFound(f"{order_id} not found")
        if o["status"] == "open":
            o["status"] = "canceled"
        return dict(o)

    def fetch_balance(self):
        return {"free": dict(self._balance), "total": dict(self._balance)}


# ---------------------------------------------------------------------------
# PASS-6 review finding (P2), exact reproduction: FakeExchange.simulate_fill
# must move the quote balance by the actual change in cumulative order cost,
# not by delta_qty * (cumulative) avg_price — the two differ whenever the
# per-fill price varies across partial fills.
# ---------------------------------------------------------------------------

def test_fake_exchange_balance_matches_cost_across_varying_average_price():
    fx = FakeExchange(cash=1000.0)
    raw = fx.create_order("BTC/CAD", "market", "sell", 0.002)
    oid = raw["id"]

    fx.simulate_fill(oid, 0.001, 90_000.0, terminal=False)
    assert fx._orders[oid]["cost"] == pytest.approx(90.0)
    assert fx._balance[fx.quote] == pytest.approx(1090.0)

    fx.simulate_fill(oid, 0.002, 95_000.0, terminal=True)
    assert fx._orders[oid]["cost"] == pytest.approx(190.0)
    # The exact reviewer reproduction: correct result is $1,190, not $1,185
    # (the old code would have moved only 0.001*$95,000=$95 on this call).
    assert fx._balance[fx.quote] == pytest.approx(1190.0)


def test_fake_exchange_balance_matches_cost_for_buy_with_varying_price():
    fx = FakeExchange(cash=1000.0)
    raw = fx.create_order("BTC/CAD", "market", "buy", 0.002)
    oid = raw["id"]

    fx.simulate_fill(oid, 0.001, 90_000.0, terminal=False)
    assert fx._balance[fx.quote] == pytest.approx(1000.0 - 90.0)

    fx.simulate_fill(oid, 0.002, 100_000.0, terminal=True)
    # Cumulative cost 0.002*100,000=200; delta cost this call = 200-90=110
    # (the second execution's own true price is $110/0.001=$110,000, not
    # the cumulative average of $100,000).
    assert fx._orders[oid]["cost"] == pytest.approx(200.0)
    assert fx._balance[fx.quote] == pytest.approx(1000.0 - 200.0)
    assert fx._balance[fx.base]  == pytest.approx(0.002)


def _build_executor(fake_ex, *, state_path, starting_cash=1000.0,
                     native_stop_loss_enabled=False, dry_run=False):
    with patch.object(le_mod.ccxt, "kraken") as mock_cls:
        mock_cls.return_value = fake_ex
        ex = LiveExecutor(
            exchange_id="kraken", symbol="BTC/CAD",
            api_key="k", api_secret="s",
            starting_cash=starting_cash, dry_run=dry_run,
            state_path=state_path,
            native_stop_loss_enabled=native_stop_loss_enabled,
        )
    return ex


class _InjectedCrash(BaseException):
    """A deliberately un-caught exception type — mirrors a real process
    death (SIGKILL, OOM, hardware fault), not an application exception
    execute()'s own try/except chain could swallow."""


def _snapshot(ex) -> tuple:
    return (
        round(ex.cash, 8), round(ex.position, 8), round(ex._fees_paid, 8),
        round(ex._portfolio.realized_pnl, 8), len(ex._fills),
    )


# ---------------------------------------------------------------------------
# BUY entry — crash immediately after the delta is computed, before the
# atomic save that would have committed progress + economics + journal
# together.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_buy_crash_at_persistence_boundary_matches_uninterrupted(mock_cfg, mock_sleep, tmp_path):
    mock_cfg.exchange.limit_order_enabled = False

    def _drive_buy_sequence(ex, fake_ex):
        """One BUY decision; first partial fill via execute(); the
        REMAINDER recovered via reconcile_pending_orders() ALONE — no
        second BUY signal, matching PASS-5 finding 5's own acceptance
        criterion (the state machine would suppress it anyway)."""
        fake_ex.queue_fill_on_create(0.001, 90_000.0, 0.18, terminal=False)
        order1 = ex.execute(Signal.BUY, 90_000.0, 0.002)
        assert order1 is not None and order1.quantity == pytest.approx(0.001)
        fake_ex.simulate_fill(order1.order_id, 0.002, 90_000.0, 0.36, terminal=True)
        discovered = ex.reconcile_pending_orders()
        assert len(discovered) == 1
        assert discovered[0].quantity == pytest.approx(0.001)

    # ── Uninterrupted baseline ──────────────────────────────────────────
    baseline_fake = FakeExchange(cash=1000.0)
    baseline = _build_executor(baseline_fake, state_path=str(tmp_path / "baseline.json"))
    _drive_buy_sequence(baseline, baseline_fake)
    baseline_final = _snapshot(baseline)

    # ── Crash-injected run ───────────────────────────────────────────────
    state_path = str(tmp_path / "crashed.json")
    crash_fake = FakeExchange(cash=1000.0)
    ex = _build_executor(crash_fake, state_path=state_path)
    crash_fake.queue_fill_on_create(0.001, 90_000.0, 0.18, terminal=False)

    real_delta = ex._record_order_delta

    def _crash_after_delta(*a, **kw):
        result = real_delta(*a, **kw)
        raise _InjectedCrash("process died right after the delta was computed")

    with patch.object(ex, "_record_order_delta", side_effect=_crash_after_delta):
        with pytest.raises(_InjectedCrash):
            ex.execute(Signal.BUY, 90_000.0, 0.002)

    # "Restart": a FRESH LiveExecutor against the SAME FakeExchange (the
    # order/balance state on the real exchange is untouched by the bot's
    # own crash) and the SAME (crash-preserved) state file. Startup
    # reconciliation recovers the first partial fill at construction time.
    ex2 = _build_executor(crash_fake, state_path=state_path)
    assert ex2.position == pytest.approx(0.001)   # recovered — not lost, not duplicated

    # Remainder fills later; reconciled independently of any new signal.
    open_orders = crash_fake.fetch_open_orders()
    assert len(open_orders) == 1
    crash_fake.simulate_fill(open_orders[0]["id"], 0.002, 90_000.0, 0.36, terminal=True)
    discovered = ex2.reconcile_pending_orders()
    assert len(discovered) == 1
    assert discovered[0].quantity == pytest.approx(0.001)

    crashed_final = _snapshot(ex2)

    assert crashed_final == baseline_final


# ---------------------------------------------------------------------------
# SELL exit — crash immediately after the delta is computed, before the
# atomic save.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_sell_crash_at_persistence_boundary_matches_uninterrupted(mock_cfg, mock_sleep, tmp_path):
    mock_cfg.exchange.limit_order_enabled = False

    def _seed_position(ex, fake_ex, qty=0.002, cost_basis=85_000.0):
        ex._portfolio.position    = qty
        ex._portfolio._cost_basis = cost_basis
        ex._bot_opened_position   = True
        fake_ex._balance[fake_ex.base] = qty
        ex._save_state()

    def _drive_sell_sequence(ex, fake_ex):
        fake_ex.queue_fill_on_create(0.001, 95_000.0, 0.19, terminal=False)
        order1 = ex.execute(Signal.SELL, 95_000.0, 0.002)
        assert order1 is not None and order1.quantity == pytest.approx(0.001)
        fake_ex.simulate_fill(order1.order_id, 0.002, 95_000.0, 0.38, terminal=True)
        discovered = ex.reconcile_pending_orders()
        assert len(discovered) == 1
        assert discovered[0].quantity == pytest.approx(0.001)

    # ── Uninterrupted baseline ──────────────────────────────────────────
    baseline_fake = FakeExchange(cash=1000.0)
    baseline = _build_executor(baseline_fake, state_path=str(tmp_path / "baseline.json"))
    _seed_position(baseline, baseline_fake)
    _drive_sell_sequence(baseline, baseline_fake)
    baseline_final = _snapshot(baseline)
    assert baseline.position == pytest.approx(0.0)

    # ── Crash-injected run ───────────────────────────────────────────────
    state_path = str(tmp_path / "crashed.json")
    crash_fake = FakeExchange(cash=1000.0)
    ex = _build_executor(crash_fake, state_path=state_path)
    _seed_position(ex, crash_fake)
    crash_fake.queue_fill_on_create(0.001, 95_000.0, 0.19, terminal=False)

    real_delta = ex._record_order_delta

    def _crash_after_delta(*a, **kw):
        result = real_delta(*a, **kw)
        raise _InjectedCrash("process died right after the delta was computed")

    with patch.object(ex, "_record_order_delta", side_effect=_crash_after_delta):
        with pytest.raises(_InjectedCrash):
            ex.execute(Signal.SELL, 95_000.0, 0.002)

    ex2 = _build_executor(crash_fake, state_path=state_path)
    assert ex2.position == pytest.approx(0.001)   # half sold, recovered at startup

    open_orders = crash_fake.fetch_open_orders()
    assert len(open_orders) == 1
    crash_fake.simulate_fill(open_orders[0]["id"], 0.002, 95_000.0, 0.38, terminal=True)
    discovered = ex2.reconcile_pending_orders()
    assert len(discovered) == 1

    crashed_final = _snapshot(ex2)

    assert crashed_final == baseline_final
    assert ex2.position == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Protective stop — crash immediately after a cancel-race fill is recorded,
# before the caller advances its own progress baseline and saves.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_protect_crash_between_fill_and_progress_advance_matches_uninterrupted(
    mock_cfg, mock_sleep, tmp_path,
):
    mock_cfg.exchange.limit_order_enabled = False

    def _seed_position_and_stop(ex, fake_ex, qty=0.002, cost_basis=80_000.0):
        ex._portfolio.position    = qty
        ex._portfolio._cost_basis = cost_basis
        ex._bot_opened_position   = True
        fake_ex._balance[fake_ex.base] = qty
        raw = fake_ex.create_order(
            "BTC/CAD", "market", "sell", qty,
            params={"stopLossPrice": "78000.00", "clientOrderId": "seed-stop"},
        )
        ex._native_stop_order_id    = raw["id"]
        ex._native_stop_price       = 78_000.0
        ex._native_stop_is_trailing = False
        ex._save_state()
        return raw["id"]

    def _drive_protect_sequence(ex, fake_ex, stop_id):
        # Cancel attempt races a real fill: the stop fills 0.001 of the
        # 0.002 resting quantity, and the cancel itself is rejected/lost
        # (a genuine race, or a transient network blip) — the order
        # remains open/resting for its remainder.
        fake_ex.simulate_fill(stop_id, 0.001, 78_000.0, 0.16, terminal=False)
        fake_ex.queue_cancel_failure()
        outcome, fill_order = ex._cancel_native_stop()
        assert outcome == "partial"
        assert fill_order.quantity == pytest.approx(0.001)
        # Remainder fills later; recovered without a second cancel call
        # racing anything — reconcile_pending_orders() only covers
        # "buy"/"sell" roles, so the stop's own remaining fill is
        # discovered the SAME way sync_protective_stop's cancel path
        # always finds it: the NEXT cancel-and-verify cycle.
        fake_ex.simulate_fill(stop_id, 0.002, 78_000.0, 0.32, terminal=True)
        outcome2, fill_order2 = ex._cancel_native_stop()
        assert outcome2 == "filled"
        assert fill_order2.quantity == pytest.approx(0.001)

    # ── Uninterrupted baseline ──────────────────────────────────────────
    baseline_fake = FakeExchange(cash=1000.0)
    baseline = _build_executor(
        baseline_fake, state_path=str(tmp_path / "baseline.json"),
        native_stop_loss_enabled=True,
    )
    stop_id_b = _seed_position_and_stop(baseline, baseline_fake)
    _drive_protect_sequence(baseline, baseline_fake, stop_id_b)
    baseline_final = _snapshot(baseline)
    assert baseline.position == pytest.approx(0.0)

    # ── Crash-injected run ───────────────────────────────────────────────
    state_path = str(tmp_path / "crashed.json")
    crash_fake = FakeExchange(cash=1000.0)
    ex = _build_executor(crash_fake, state_path=state_path, native_stop_loss_enabled=True)
    stop_id_c = _seed_position_and_stop(ex, crash_fake)
    crash_fake.simulate_fill(stop_id_c, 0.001, 78_000.0, 0.16, terminal=False)
    crash_fake.queue_cancel_failure()   # same race as the baseline — order stays open

    real_record_fill = ex._record_stop_triggered_fill

    def _crash_after_fill(*a, **kw):
        result = real_record_fill(*a, **kw)
        raise _InjectedCrash("process died right after the fill was recorded")

    with patch.object(ex, "_record_stop_triggered_fill", side_effect=_crash_after_fill):
        with pytest.raises(_InjectedCrash):
            ex._cancel_native_stop()

    # "Restart" against the SAME FakeExchange — the stop is still
    # genuinely resting for its remaining 0.001, exactly as it would be
    # on the real exchange.
    ex2 = _build_executor(crash_fake, state_path=state_path, native_stop_loss_enabled=True)
    assert ex2.position == pytest.approx(0.001)   # first delta recovered, exactly once

    crash_fake.simulate_fill(stop_id_c, 0.002, 78_000.0, 0.32, terminal=True)
    outcome2, fill_order2 = ex2._cancel_native_stop()
    assert outcome2 == "filled"
    assert fill_order2.quantity == pytest.approx(0.001)

    crashed_final = _snapshot(ex2)

    # PASS-6 review finding (P1) fix: cash, inventory, fees_paid, realized
    # P&L and execution count now converge EXACTLY — the startup reseed
    # recovers the missed execution (journal + realized P&L + fees) before
    # ever overwriting the tracked baseline, instead of silently
    # discarding it.
    assert crashed_final == baseline_final
    assert ex2.position == pytest.approx(0.0)
    assert ex2._fees_paid == pytest.approx(0.32)
    assert len(ex2._fills) == 2   # both 0.001 executions journaled, not just one


# ---------------------------------------------------------------------------
# PASS-6 review finding (P2): with the FakeExchange balance bug fixed, a
# crash-recovery sequence where the per-fill price genuinely VARIES between
# the two partial fills must still converge exactly with an uninterrupted
# run — the whole point of fixing the fake is that harness assertions like
# this one are now trustworthy.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_sell_crash_with_varying_fill_price_matches_uninterrupted(mock_cfg, mock_sleep, tmp_path):
    mock_cfg.exchange.limit_order_enabled = False

    def _seed_position(ex, fake_ex, qty=0.002, cost_basis=85_000.0):
        ex._portfolio.position    = qty
        ex._portfolio._cost_basis = cost_basis
        ex._bot_opened_position   = True
        fake_ex._balance[fake_ex.base] = qty
        ex._save_state()

    def _drive_sell_sequence(ex, fake_ex):
        # First partial fills at $90,000; the SECOND partial fills at a
        # genuinely DIFFERENT price ($95,000) — the cumulative average
        # reported at that point is $92,500, not either execution's own
        # true price.
        fake_ex.queue_fill_on_create(0.001, 90_000.0, 0.0, terminal=False)
        order1 = ex.execute(Signal.SELL, 90_000.0, 0.002)
        assert order1 is not None and order1.quantity == pytest.approx(0.001)
        fake_ex.simulate_fill(order1.order_id, 0.002, 92_500.0, 0.0, terminal=True)
        discovered = ex.reconcile_pending_orders()
        assert len(discovered) == 1
        assert discovered[0].quantity == pytest.approx(0.001)
        # The second delta's own true price is $95,000 (cost 0.002*92,500
        # - cost 0.001*90,000 = 185-90=95, over 0.001 qty).
        assert discovered[0].price == pytest.approx(95_000.0)

    # ── Uninterrupted baseline ──────────────────────────────────────────
    baseline_fake = FakeExchange(cash=1000.0)
    baseline = _build_executor(baseline_fake, state_path=str(tmp_path / "baseline.json"))
    _seed_position(baseline, baseline_fake)
    _drive_sell_sequence(baseline, baseline_fake)
    baseline_final = _snapshot(baseline)
    assert baseline.position == pytest.approx(0.0)

    # ── Crash-injected run ───────────────────────────────────────────────
    state_path = str(tmp_path / "crashed.json")
    crash_fake = FakeExchange(cash=1000.0)
    ex = _build_executor(crash_fake, state_path=state_path)
    _seed_position(ex, crash_fake)
    crash_fake.queue_fill_on_create(0.001, 90_000.0, 0.0, terminal=False)

    real_delta = ex._record_order_delta

    def _crash_after_delta(*a, **kw):
        result = real_delta(*a, **kw)
        raise _InjectedCrash("process died right after the delta was computed")

    with patch.object(ex, "_record_order_delta", side_effect=_crash_after_delta):
        with pytest.raises(_InjectedCrash):
            ex.execute(Signal.SELL, 90_000.0, 0.002)

    ex2 = _build_executor(crash_fake, state_path=state_path)
    assert ex2.position == pytest.approx(0.001)

    open_orders = crash_fake.fetch_open_orders()
    assert len(open_orders) == 1
    crash_fake.simulate_fill(open_orders[0]["id"], 0.002, 92_500.0, 0.0, terminal=True)
    discovered = ex2.reconcile_pending_orders()
    assert len(discovered) == 1
    assert discovered[0].price == pytest.approx(95_000.0)

    crashed_final = _snapshot(ex2)

    assert crashed_final == baseline_final
    assert ex2.position == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# PASS-7 review, finding 1 (P0): a native stop that fully fills OFFLINE must
# not have its proceeds credited twice, nor invent profit from a cost basis
# _sync_position() already zeroed before recovery gets a chance to use it.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_stop_fully_fills_offline_does_not_double_credit_or_invent_profit(mock_cfg, mock_sleep, tmp_path):
    """PASS-7 review finding (P0), exact reproduction: 0.002 BTC at an
    $85,000 basis, $1,000 cash, a tracked stop at $78,000. The stop fully
    fills OFFLINE for 0.002 BTC at $78,000 with a $0.32 fee. The old code's
    flat-position startup branch called the LIVE _cancel_native_stop(),
    which re-applied the already-synced proceeds (cash $1,311.36 instead
    of $1,155.68) and computed P&L against the by-then-zeroed cost basis
    (fabricating +$156 "profit" instead of the real -$14 loss)."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    raw = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-stop"},
    )
    ex._native_stop_order_id    = raw["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # Offline: the stop fully fills.
    fake.simulate_fill(raw["id"], 0.002, 78_000.0, 0.32, terminal=True)

    ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    assert ex2.cash == pytest.approx(1_155.68)
    assert ex2.position == pytest.approx(0.0)
    assert ex2._portfolio.realized_pnl == pytest.approx(-14.0)
    assert len(ex2.pending_journal_entries) == 1
    assert ex2.pending_journal_entries[0]["pnl"] == pytest.approx(-14.0)
    assert not ex2.has_resting_stop


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_stop_fully_fills_offline_crash_between_saves_still_converges(mock_cfg, mock_sleep, tmp_path):
    """PASS-7 finding 1's own acceptance criterion: repeat with a crash
    injected between _sync_position() (which zeros cost_basis) and stop
    recovery finishing — the preserved pre-sync basis must survive to the
    NEXT restart, not just the first one."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    raw = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-stop"},
    )
    ex._native_stop_order_id    = raw["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()
    fake.simulate_fill(raw["id"], 0.002, 78_000.0, 0.32, terminal=True)

    with patch.object(LiveExecutor, "_recover_flat_native_stop_at_startup",
                       side_effect=_InjectedCrash("crash after position sync, before recovery")):
        with pytest.raises(_InjectedCrash):
            _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    # Next restart, uninterrupted this time.
    ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    assert ex2.cash == pytest.approx(1_155.68)
    assert ex2._portfolio.realized_pnl == pytest.approx(-14.0)


# ---------------------------------------------------------------------------
# PASS-7 review, finding 3 (P1): a fee finalized during downtime with NO
# change in filled quantity must still reach the reporting journal, not
# just get silently "consumed" by advancing the tracked baseline.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_fee_only_correction_during_downtime_reaches_journal(mock_cfg, mock_sleep, tmp_path):
    """PASS-7 review finding (P1, finding 3), exact reproduction: 0.002 BTC
    position, stop fills 0.001 at $78,000 with a PROVISIONAL zero fee
    (recorded live). While offline, the fee finalizes to $0.36 with NO
    change in filled quantity, remainder stays open. Restart. The old code
    advanced the fee baseline (silently "consuming" the correction)
    without ever journaling it."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 80_000.0
    fake._balance[fake.base] = 0.002
    raw = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-stop"},
    )
    ex._native_stop_order_id    = raw["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    fake.simulate_fill(raw["id"], 0.001, 78_000.0, 0.0, terminal=False)
    fake.queue_cancel_failure()
    outcome, fill_order = ex._cancel_native_stop()
    assert outcome == "partial"
    assert fill_order.fee_cost == pytest.approx(0.0)

    # Offline: fee finalizes to $0.36, filled quantity UNCHANGED.
    fake.simulate_fill(raw["id"], 0.001, 78_000.0, 0.36, terminal=False)

    ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    assert ex2._fees_paid == pytest.approx(0.36)
    kinds = [e.get("kind", "fill") for e in ex2.pending_journal_entries]
    assert "fee_adjustment" in kinds


# ---------------------------------------------------------------------------
# PASS-7 review, finding 4 (P1): a transient final-order lookup failure must
# not permanently discard the stop's recovery identity — a later tick or
# restart must still recover it.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_transient_lookup_failure_preserves_recovery_identity_until_resolved(mock_cfg, mock_sleep, tmp_path):
    """PASS-7 review finding (P1, finding 4), exact reproduction: 0.002 BTC
    at $85,000 basis, a tracked stop. Offline, the stop fills 0.001 BTC at
    $78,000 with a $0.16 fee and becomes TERMINAL (e.g. cancelled with a
    partial fill), leaving 0.001 BTC still held. A transient timeout on
    the final-state lookup during restart must not permanently discard
    the $7 loss — a later restart must still recover it."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    raw = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-stop"},
    )
    ex._native_stop_order_id    = raw["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # Offline: partial fill, then terminal (cancelled), leaving 0.001 BTC.
    fake.simulate_fill(raw["id"], 0.001, 78_000.0, 0.16, terminal=True)
    fake._orders[raw["id"]]["status"] = "canceled"

    # Restart #1: the final-state lookup times out transiently.
    with patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    assert ex2.position == pytest.approx(0.001)          # position sync unaffected
    assert ex2._portfolio.realized_pnl == pytest.approx(0.0)   # not yet recovered
    assert len(ex2._unresolved_stop_recoveries) == 1
    assert ex2._unresolved_stop_recoveries[0]["order_id"] == raw["id"]

    # Restart #2: the lookup succeeds.
    ex3 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    assert ex3._portfolio.realized_pnl == pytest.approx(-7.0)   # (78000-85000)*0.001
    assert ex3._unresolved_stop_recoveries == []
    assert len(ex3.pending_journal_entries) >= 1


# ---------------------------------------------------------------------------
# PASS-8 review, finding 1 (P1): a pending PROTECTIVE PLACEMENT (identity
# lives in pending_submissions['protect'], no _native_stop_order_id ever
# committed) that fully fills offline must also recover using the
# PRESERVED pre-sync cost basis — the same class of bug as PASS-7 finding
# 1, at a separate entry point.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_pending_protect_placement_fully_filled_offline_uses_preserved_basis(mock_cfg, mock_sleep, tmp_path):
    """PASS-8 review finding (P1, finding 1), exact reproduction: a
    protective placement is accepted (persisted in
    _pending_submissions['protect']) before tracking is ever committed —
    no _native_stop_order_id set at all. It fully fills OFFLINE for 0.002
    BTC at $78,000 with a $0.32 fee. The old pending-placement recovery
    path called the cash-free journal helper WITHOUT the preserved basis
    override, fabricating +$156 profit instead of the real -$14 loss
    (cash was already correct)."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    raw = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "protect-cid-1"},
    )
    # Model "placement accepted before tracking was committed": the
    # pending submission exists, but _native_stop_order_id was never set.
    ex._pending_submissions["protect"] = {
        "side": "sell", "client_order_id": "protect-cid-1",
        "quantity": 0.002, "price": 78_000.0, "label": "native stop placement",
        "created_at": "2026-09-19T00:00:00+00:00", "order_id": raw["id"],
    }
    ex._save_state()

    # Offline: the placement fully fills.
    fake.simulate_fill(raw["id"], 0.002, 78_000.0, 0.32, terminal=True)

    ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    assert ex2.cash == pytest.approx(1_155.68)
    assert ex2.position == pytest.approx(0.0)
    assert ex2._portfolio.realized_pnl == pytest.approx(-14.0)
    assert "protect" not in ex2.pending_submissions


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_pending_protect_placement_crash_between_saves_still_converges(mock_cfg, mock_sleep, tmp_path):
    """PASS-8 finding 1's own acceptance criterion: repeat with a crash
    injected between _sync_position() and recovery finishing — the
    preserved basis must survive to the NEXT restart even though the
    outstanding identity lives in pending_submissions['protect'] rather
    than _native_stop_order_id (the capture-guard fix must cover both)."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    raw = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "protect-cid-2"},
    )
    ex._pending_submissions["protect"] = {
        "side": "sell", "client_order_id": "protect-cid-2",
        "quantity": 0.002, "price": 78_000.0, "label": "native stop placement",
        "created_at": "2026-09-19T00:00:00+00:00", "order_id": raw["id"],
    }
    ex._save_state()
    fake.simulate_fill(raw["id"], 0.002, 78_000.0, 0.32, terminal=True)

    with patch.object(LiveExecutor, "_resolve_pending_protect_submission_at_startup",
                       side_effect=_InjectedCrash("crash after position sync, before recovery")):
        with pytest.raises(_InjectedCrash):
            _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    assert ex2.cash == pytest.approx(1_155.68)
    assert ex2._portfolio.realized_pnl == pytest.approx(-14.0)


# ---------------------------------------------------------------------------
# PASS-8 review, finding 2 (P1): a FLAT local position does not establish
# that the stop's own, independent life on the exchange has terminated —
# must never abandon ownership of a still-live order.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_flat_position_does_not_abandon_a_still_open_stop(mock_cfg, mock_sleep, tmp_path):
    """PASS-8 review finding (P1, finding 2), exact reproduction: 0.002 BTC
    and a tracked OPEN stop O1. The BTC balance is externally zeroed (a
    transfer/close unrelated to the stop), leaving O1 genuinely still
    resting on the exchange. The old code unconditionally cleared
    tracking once the local position went flat — abandoning ownership of
    a live order fetch_open_orders() still shows."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    raw = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-stop"},
    )
    ex._native_stop_order_id    = raw["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # External close/transfer: BTC balance zeroed, the stop order itself
    # remains genuinely open (untouched).
    fake._balance[fake.base] = 0.0

    ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    assert ex2.position == pytest.approx(0.0)
    assert ex2.has_resting_stop            # NOT abandoned
    assert ex2._native_stop_order_id == raw["id"]
    open_orders = fake.fetch_open_orders()
    assert any(o["id"] == raw["id"] for o in open_orders)   # still genuinely resting


# ---------------------------------------------------------------------------
# PASS-8 review, finding 3 (P1): a SECOND unresolved historical stop must
# not overwrite the first — both must be tracked and recovered
# independently.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_multiple_unresolved_stops_recovered_independently_not_overwritten(mock_cfg, mock_sleep, tmp_path):
    """PASS-8 review finding (P1, finding 3), exact reproduction: O1 fills
    0.001 BTC at $78,000 (fee $0.16) and becomes terminal while offline; a
    transient lookup failure queues it unresolved, leaving 0.001 BTC
    residual. A replacement O2 is placed for the residual, which ALSO
    fills (0.0005 @ $78,000, fee $0.08) and becomes terminal while
    offline, ALSO hitting a transient lookup failure. The old single-slot
    design silently replaced O1's frozen entry with O2's — recovering
    only -$3.50 instead of the correct -$10.50 across both executions."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    o1 = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-o1"},
    )
    ex._native_stop_order_id    = o1["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # Offline: O1 fills 0.001, becomes terminal, leaving 0.001 residual.
    fake.simulate_fill(o1["id"], 0.001, 78_000.0, 0.16, terminal=True)
    fake._orders[o1["id"]]["status"] = "canceled"

    # Restart #1: O1's final-state lookup times out.
    with patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert len(ex2._unresolved_stop_recoveries) == 1
    assert ex2._unresolved_stop_recoveries[0]["order_id"] == o1["id"]
    assert ex2.position == pytest.approx(0.001)

    # A replacement stop O2 is placed for the residual 0.001.
    o2 = fake.create_order(
        "BTC/CAD", "market", "sell", 0.001,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-o2"},
    )
    ex2._native_stop_order_id    = o2["id"]
    ex2._native_stop_price       = 78_000.0
    ex2._native_stop_is_trailing = False
    ex2._save_state()

    # Offline: O2 ALSO fills (0.0005 of the 0.001 residual) and becomes
    # terminal.
    fake.simulate_fill(o2["id"], 0.0005, 78_000.0, 0.08, terminal=True)
    fake._orders[o2["id"]]["status"] = "canceled"

    # Restart #2: BOTH O1's and O2's final-state lookups still fail.
    with patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex3 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert len(ex3._unresolved_stop_recoveries) == 2   # BOTH retained, not overwritten
    _order_ids = {e["order_id"] for e in ex3._unresolved_stop_recoveries}
    assert _order_ids == {o1["id"], o2["id"]}

    # Restart #3: lookups succeed for both.
    ex4 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    assert ex4._unresolved_stop_recoveries == []
    assert ex4._portfolio.realized_pnl == pytest.approx(-10.50)
    _recovered_qty = sum(e["quantity"] for e in ex4.pending_journal_entries)
    assert _recovered_qty == pytest.approx(0.0015)


# ---------------------------------------------------------------------------
# PASS-9 review, finding 1 (P1): a queued historical stop that stays open
# PAST startup must have any FURTHER execution applied as a live,
# cash-mutating fill and returned to the caller — the executor's own
# startup balance sync ran exactly once and does not cover anything that
# happens while the process keeps running.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_unresolved_stop_new_fill_after_startup_applies_live_and_is_returned(mock_cfg, mock_sleep, tmp_path):
    """PASS-9 review finding (P1, finding 1), reproduction A: O1 is still
    completely unfilled at restart — the open-order listing comes back
    empty and the direct final-state lookup times out, so O1 is queued
    unresolved with balances still showing the pre-fill state. A first
    reconcile_pending_orders() call while O1 remains open/unfilled changes
    nothing. O1 THEN fills 0.001 BTC purely during this live run (nothing
    else re-syncs cash/position) — a second reconcile_pending_orders()
    must apply that delta to cash/position and return it as a discovered
    SELL fill for the normal bookkeeping consumer, instead of silently
    consuming it cash-free as the old code did (leaving cash at $1,000.00
    and position at 0.002 instead of the real $1,077.84 / 0.001, with an
    empty discovered list)."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    o1 = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-o1"},
    )
    ex._native_stop_order_id    = o1["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # Restart: the open-order listing comes back empty (a transient gap)
    # and the direct final-state lookup also times out -> O1 is queued
    # unresolved. Balances still correctly show the pre-fill state.
    with patch.object(fake, "fetch_open_orders", return_value=[]), \
         patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert len(ex2._unresolved_stop_recoveries) == 1
    assert ex2._unresolved_stop_recoveries[0]["order_id"] == o1["id"]
    assert ex2.cash == pytest.approx(1000.0)
    assert ex2.position == pytest.approx(0.002)

    # Healthy queries restored. First live tick: O1 remains open, unfilled
    # -> no delta, nothing changes.
    discovered_1 = ex2.reconcile_pending_orders()
    assert discovered_1 == []
    assert ex2.cash == pytest.approx(1000.0)
    assert ex2.position == pytest.approx(0.002)
    assert len(ex2._unresolved_stop_recoveries) == 1

    # O1 fills 0.001 more, purely during this live run — no restart, no
    # fresh balance sync.
    fake.simulate_fill(o1["id"], 0.001, 78_000.0, 0.16, terminal=False)
    discovered_2 = ex2.reconcile_pending_orders()

    assert ex2.cash     == pytest.approx(1_077.84)
    assert ex2.position == pytest.approx(0.001)
    assert len(discovered_2) == 1
    assert discovered_2[0].side     == OrderSide.SELL
    assert discovered_2[0].quantity == pytest.approx(0.001)
    assert discovered_2[0].pnl      == pytest.approx(-7.0)
    # The order remains genuinely open (0.001 of 0.002 still unfilled) —
    # still queued for its eventual terminal resolution.
    assert len(ex2._unresolved_stop_recoveries) == 1


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_unresolved_stop_fee_finalized_after_startup_deducts_cash(mock_cfg, mock_sleep, tmp_path):
    """PASS-9 review finding (P1, finding 1), reproduction B: O1's full
    0.002 BTC quantity was ALREADY recorded locally before the crash (no
    new quantity ever appears from the recovery queue's point of view) —
    only its FEE was still provisional ($0) at the time of the crash and
    the startup balance sync. The startup sync therefore already reflects
    the quantity's cash effect but NOT the fee, which only finalizes to
    $0.36 strictly AFTER that sync, purely during this live run. The old
    code's cash-free fee-only branch tracked and journaled the correction
    but never actually deducted it from cash — this asserts executor cash
    converges EXACTLY to the real exchange free balance after the fee
    finalizes and is retried live."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    o1 = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-o1"},
    )
    ex._native_stop_order_id    = o1["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False

    # The fill happened BEFORE the crash, with fee still provisional at
    # $0 — and was already recorded locally (this executor's own state
    # already reflects the quantity/cost), but the process died before
    # _cancel_native_stop()'s own terminal-clearing logic ever ran, so
    # the tracked id is still set — exactly the scenario
    # _recover_flat_native_stop_at_startup() exists to handle (position
    # already flat, tracked id still present).
    fake.simulate_fill(o1["id"], 0.002, 78_000.0, 0.0, terminal=True)
    ex._native_stop_last_recorded_filled = 0.002
    ex._native_stop_last_recorded_cost   = 0.002 * 78_000.0
    ex._native_stop_last_recorded_fee    = 0.0
    ex._portfolio.position     = 0.0
    ex._portfolio._cost_basis  = 0.0
    ex._portfolio.realized_pnl = (78_000.0 - 85_000.0) * 0.002
    ex._portfolio.cash         = fake._balance[fake.quote]   # 1156.0
    ex._save_state()

    # Restart: the final-state lookup times out during startup — O1 is
    # queued unresolved. The startup sync reads the exchange's CURRENT
    # free balance, which does not yet reflect the fee (it hasn't
    # finalized on the exchange either, at this point).
    with patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert len(ex2._unresolved_stop_recoveries) == 1
    _synced_cash = ex2.cash
    assert _synced_cash == pytest.approx(1156.0)

    # AFTER startup, purely during this live run, the exchange finalizes
    # the fee — a real, not-yet-applied cash movement.
    fake.simulate_fill(o1["id"], 0.002, 78_000.0, 0.36, terminal=True)
    _real_exchange_cash = fake._balance[fake.quote]
    assert _real_exchange_cash == pytest.approx(_synced_cash - 0.36)

    discovered = ex2.reconcile_pending_orders()

    assert discovered == []   # fee-only — never fabricates a fill Order
    assert ex2.cash == pytest.approx(_real_exchange_cash)   # exactly converges
    assert ex2._unresolved_stop_recoveries == []             # terminal, resolved
    kinds = [e.get("kind", "fill") for e in ex2.pending_journal_entries]
    assert "fee_adjustment" in kinds


# ---------------------------------------------------------------------------
# PASS-9 review, finding 2 (P1): an order queued for historical recovery
# that is LATER independently adopted into active tracking (Gap B) must
# have its recovery entry merged into — not left alongside — the newly
# adopted cursor, or both paths independently discover and journal the
# same future fill.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_adoption_merges_queued_recovery_instead_of_double_booking(mock_cfg, mock_sleep, tmp_path):
    """PASS-9 review finding (P1, finding 2), exact reproduction: O1 is
    queued for historical recovery after a failed lookup (which also
    clears active tracking). A LATER restart's open-orders scan
    independently discovers the same still-resting, still-unfilled O1 and
    adopts it into active tracking. Before this fix, adoption never
    checked the recovery queue: both a queued entry and a freshly adopted
    cursor existed for the same order simultaneously, and when O1 later
    partially filled, BOTH reconcile_pending_orders() (retrying the
    queue) and _cancel_native_stop() (using active tracking) discovered
    and journaled the SAME fill — realized P&L became -$14 for a single
    0.001 BTC execution whose true loss is -$7."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    o1 = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-o1"},
    )
    ex._native_stop_order_id    = o1["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # Restart #1: open-order listing empty, direct lookup times out -> O1
    # queued unresolved, active tracking cleared.
    with patch.object(fake, "fetch_open_orders", return_value=[]), \
         patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert ex2._native_stop_order_id is None
    assert len(ex2._unresolved_stop_recoveries) == 1
    assert ex2._unresolved_stop_recoveries[0]["order_id"] == o1["id"]

    # Restart #2: healthy queries restored. O1 is still genuinely resting,
    # unfilled — the open-orders scan finds it untracked and adopts it.
    ex3 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert ex3._native_stop_order_id == o1["id"]
    # Fixed: adoption merges/removes the queued recovery entry so only
    # ONE cursor survives, instead of both existing side by side.
    assert ex3._unresolved_stop_recoveries == []

    # O1 partially fills 0.001 BTC @ $78,000, fee $0.16 — still open.
    fake.simulate_fill(o1["id"], 0.001, 78_000.0, 0.16, terminal=False)

    # Both consumers run, in the order the review's own reproduction uses.
    discovered = ex3.reconcile_pending_orders()
    assert discovered == []   # the recovery queue is empty — nothing to retry there
    fake.queue_cancel_failure()
    outcome, fill_order = ex3._cancel_native_stop()

    assert outcome == "partial"
    assert fill_order is not None
    assert fill_order.quantity == pytest.approx(0.001)
    assert ex3._portfolio.realized_pnl == pytest.approx(-7.0)   # not -14.0
    _native_stop_entries = [
        e for e in ex3.pending_journal_entries
        if e.get("order_id") == f"native-stop:{o1['id']}"
    ]
    assert len(_native_stop_entries) == 1   # exactly one journal entry, not two


# ---------------------------------------------------------------------------
# PASS-10 review, finding 1 (P0): discovery time is not execution time.
# PASS-9's apply_as_live_fill flag decided cash-free-vs-live by WHICH
# CALLER finally reads a queued order, not by whether the exchange
# movement happened before or after the balance checkpoint — a fill that
# executed entirely OFFLINE, but whose own lookup only starts succeeding
# on a LATER TICK (not the same startup pass that queued it), was
# double-applied on top of a checkpoint that already included it.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_delayed_lookup_of_offline_fill_does_not_double_apply_checkpoint(mock_cfg, mock_sleep, tmp_path):
    """PASS-10 review finding (P0, finding 1), reproduction A, exact
    reproduction: O1 fills 0.001 BTC @ $78,000 (fee $0.16) and becomes
    terminal ENTIRELY WHILE OFFLINE — the checkpoint (_sync_cash()/
    _sync_position() at restart) correctly reads $1,077.84 / 0.001
    BEFORE O1's own lookup ever succeeds. O1's lookup then times out
    during startup (queuing it) and only succeeds LATER, via
    reconcile_pending_orders() on a live tick — with NO further exchange
    activity in between. The old apply_as_live_fill=True (a tick-context
    call) would re-apply this already-checkpointed fill on top of itself:
    $1,155.68 cash / 0 BTC instead of the correct, UNCHANGED
    $1,077.84 / 0.001."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    o1 = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-o1"},
    )
    ex._native_stop_order_id    = o1["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # Entirely offline: O1 fills 0.001 BTC and becomes terminal. The
    # exchange now genuinely holds $1,077.84 / 0.001 BTC.
    fake.simulate_fill(o1["id"], 0.001, 78_000.0, 0.16, terminal=True)
    assert fake._balance[fake.quote] == pytest.approx(1_077.84)
    assert fake._balance[fake.base]  == pytest.approx(0.001)

    # Restart: the checkpoint correctly reads the ALREADY-updated exchange
    # state, but O1's own final-state lookup times out — queued unresolved.
    with patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert ex2.cash     == pytest.approx(1_077.84)   # checkpoint already correct
    assert ex2.position == pytest.approx(0.001)
    assert len(ex2._unresolved_stop_recoveries) == 1

    # Healthy lookup restored. No further exchange activity occurs — this
    # is the SAME historical fill the checkpoint above already reflects.
    discovered = ex2.reconcile_pending_orders()

    assert discovered == []                          # not a new live execution
    assert ex2.cash     == pytest.approx(1_077.84)    # unchanged — not double-applied
    assert ex2.position == pytest.approx(0.001)       # unchanged
    assert ex2._unresolved_stop_recoveries == []       # terminal, resolved
    assert len(ex2.pending_journal_entries) == 1       # still journaled, exactly once


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_adoption_merge_does_not_double_apply_checkpoint_covered_fill(mock_cfg, mock_sleep, tmp_path):
    """PASS-10 review finding (P0, finding 1), reproduction B, exact
    reproduction: same offline partial fill as the test above, but O1
    stays OPEN (not flat). Restart #1 queues it unresolved (empty
    open-orders list + a timed-out direct lookup). Restart #2's
    open-orders discovery succeeds and adopts the same still-resting O1
    while its OWN direct lookup still fails — the PASS-9 fix (seed the
    new cursor from the queue entry's frozen ZERO baseline) consumed the
    entry without recognizing its checkpoint_qty_cap, so a later
    _cancel_native_stop() re-applied the SAME already-checkpointed fill:
    $1,155.68 / 0 BTC instead of the correct, unchanged
    $1,077.84 / 0.001."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    o1 = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-o1"},
    )
    ex._native_stop_order_id    = o1["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # Entirely offline: O1 partially fills 0.001 BTC and stays OPEN (the
    # remaining 0.001 of its original 0.002 size is still unfilled).
    fake.simulate_fill(o1["id"], 0.001, 78_000.0, 0.16, terminal=False)
    assert fake._balance[fake.quote] == pytest.approx(1_077.84)
    assert fake._balance[fake.base]  == pytest.approx(0.001)

    # Restart #1: open-order listing empty, direct lookup times out -> O1
    # queued unresolved, active tracking cleared. The checkpoint already
    # correctly reads the post-fill exchange state.
    with patch.object(fake, "fetch_open_orders", return_value=[]), \
         patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert ex2.cash     == pytest.approx(1_077.84)
    assert ex2.position == pytest.approx(0.001)
    assert len(ex2._unresolved_stop_recoveries) == 1

    # Restart #2: open-order discovery succeeds (O1 still genuinely
    # resting for its unfilled remainder) and adopts it, while O1's own
    # direct lookup (used by the queue's own retry) still times out.
    with patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex3 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert ex3._native_stop_order_id == o1["id"]
    assert ex3._unresolved_stop_recoveries == []   # merged, not left dangling
    assert ex3.cash     == pytest.approx(1_077.84)    # unchanged by the merge itself
    assert ex3.position == pytest.approx(0.001)

    # No further exchange activity. A later cancel attempt must NOT
    # re-discover and re-apply the SAME already-checkpointed 0.001 fill.
    fake.queue_cancel_failure()
    outcome, fill_order = ex3._cancel_native_stop()

    assert fill_order is None          # nothing NEW to report — already accounted for
    assert ex3.cash     == pytest.approx(1_077.84)     # still unchanged
    assert ex3.position == pytest.approx(0.001)        # still unchanged
    assert len(ex3.pending_journal_entries) == 1        # journaled once, by the merge


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_unresolved_stop_delta_straddling_checkpoint_splits_correctly(mock_cfg, mock_sleep, tmp_path):
    """PASS-10 review finding (P0, finding 1), acceptance criterion "one
    order contains both pre-startup and post-startup deltas": O1 fills
    0.001 BTC OFFLINE (checkpoint-covered) and stays open; after being
    queued (lookup times out at startup), it fills a FURTHER 0.0005 BTC
    purely during this live run before its lookup finally succeeds. A
    single reconcile_pending_orders() call must split the combined
    0.0015 delta: 0.001 checkpoint-covered (cash-free) and 0.0005
    genuinely new (live, returned to the caller)."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    o1 = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-o1"},
    )
    ex._native_stop_order_id    = o1["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # Offline: 0.001 fills, order stays open (0.001 of 0.002 remains).
    fake.simulate_fill(o1["id"], 0.001, 78_000.0, 0.16, terminal=False)

    with patch.object(fake, "fetch_open_orders", return_value=[]), \
         patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert ex2.cash     == pytest.approx(1_077.84)
    assert ex2.position == pytest.approx(0.001)
    assert ex2._unresolved_stop_recoveries[0]["checkpoint_qty_cap"] == pytest.approx(0.001)

    # Purely live, a FURTHER 0.0005 fills (cumulative 0.0015 of 0.002),
    # order stays open. The queued entry's own lookup now succeeds for
    # the FIRST time, seeing the COMBINED delta in one read.
    fake.simulate_fill(o1["id"], 0.0015, 78_000.0, 0.24, terminal=False)
    discovered = ex2.reconcile_pending_orders()

    # The entry's OWN baseline never separately captured the offline
    # $0.16 (it was queued with baseline_fee=0.0 — nothing had been
    # locally recorded before the crash) — so the combined $0.24 fee
    # this single read observes is split proportionally by quantity
    # share (0.0005/0.0015), matching this file's existing partial-fill
    # fee-allocation convention (there is no finer-grained, per-event fee
    # to attribute directly from cumulative-only exchange data). Only
    # that live share is actually deducted from cash; the rest is
    # checkpoint-covered, cash-free.
    _combined_fee    = 0.24
    _live_fee_share  = _combined_fee * (0.0005 / 0.0015)
    assert ex2.cash     == pytest.approx(1_077.84 + 0.0005 * 78_000.0 - _live_fee_share)
    assert ex2.position == pytest.approx(0.0005)
    assert len(discovered) == 1
    assert discovered[0].quantity == pytest.approx(0.0005)
    assert ex2._unresolved_stop_recoveries[0]["checkpoint_qty_cap"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# PASS-11 review, finding 1 (P0): a persisted checkpoint_qty_cap goes stale
# the moment a SECOND restart's own fresh balance sync absorbs MORE offline
# activity than the first restart's cap knew about — the stale cap then
# lets that further fill be treated as live on top of an already-updated
# checkpoint.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_stale_checkpoint_cap_is_refreshed_across_a_second_restart(mock_cfg, mock_sleep, tmp_path):
    """PASS-11 review finding (P0, finding 1), exact reproduction: 0.003
    BTC @ $85,000, tracked stop O1. Offline, O1 fills 0.001 BTC @ $78,000
    (zero fee), stays open. Restart #1 (empty open-orders list + a timed-
    out direct lookup) correctly syncs $1,078/0.002 and queues recovery
    with cap 0.001. BEFORE a second restart, O1 fills FURTHER offline —
    cumulative reaches 0.002 BTC @ $78,000, terminal; the exchange now
    holds $1,156/0.001. Restart #2 (healthy lookups, no further
    activity) must converge to the ALREADY-CORRECT $1,156/0.001 — not
    apply the second 0.001 a second time on top of it ($1,234/0)."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.003
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.003
    o1 = fake.create_order(
        "BTC/CAD", "market", "sell", 0.003,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-o1"},
    )
    ex._native_stop_order_id    = o1["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # Offline: O1 fills 0.001 BTC, stays open.
    fake.simulate_fill(o1["id"], 0.001, 78_000.0, 0.0, terminal=False)

    # Restart #1: open-order listing empty, direct lookup times out.
    with patch.object(fake, "fetch_open_orders", return_value=[]), \
         patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert ex2.cash     == pytest.approx(1_078.0)
    assert ex2.position == pytest.approx(0.002)
    assert ex2._unresolved_stop_recoveries[0]["checkpoint_qty_cap"] == pytest.approx(0.001)

    # BEFORE another restart, O1 fills FURTHER offline: cumulative 0.002
    # @ $78,000, now terminal. Exchange truth: $1,156 / 0.001 BTC.
    fake.simulate_fill(o1["id"], 0.002, 78_000.0, 0.0, terminal=True)
    assert fake._balance[fake.quote] == pytest.approx(1_156.0)
    assert fake._balance[fake.base]  == pytest.approx(0.001)

    # Restart #2: healthy lookups, no further activity.
    ex3 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    assert ex3.cash     == pytest.approx(1_156.0)   # unchanged — not double-applied
    assert ex3.position == pytest.approx(0.001)     # unchanged
    assert ex3._unresolved_stop_recoveries == []      # terminal, resolved
    assert len(ex3.pending_journal_entries) == 1


@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_stale_checkpoint_cap_refreshed_across_restart_before_adoption(mock_cfg, mock_sleep, tmp_path):
    """PASS-11 review finding (P0, finding 1) — the same staleness bug,
    but reached through the ADOPTION merge path (Pass-10 finding 1's
    reproduction B) instead of the plain retry loop: the order stays
    OPEN across two restarts, with the queue's OWN direct lookup still
    failing at restart #2 (forcing resolution through
    _adopt_untracked_stop's merge), while MORE fills happen offline in
    between. The refreshed cap must still be visible in time for the
    merge to use it."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.003
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.003
    o1 = fake.create_order(
        "BTC/CAD", "market", "sell", 0.003,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-o1"},
    )
    ex._native_stop_order_id    = o1["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # Offline: O1 fills 0.001 BTC, stays open.
    fake.simulate_fill(o1["id"], 0.001, 78_000.0, 0.0, terminal=False)

    with patch.object(fake, "fetch_open_orders", return_value=[]), \
         patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert ex2._unresolved_stop_recoveries[0]["checkpoint_qty_cap"] == pytest.approx(0.001)

    # Further offline fill: cumulative 0.002 @ $78,000, STILL open (0.001
    # of the original 0.003 remains unfilled). Exchange: $1,156 / 0.001.
    fake.simulate_fill(o1["id"], 0.002, 78_000.0, 0.0, terminal=False)
    assert fake._balance[fake.quote] == pytest.approx(1_156.0)
    assert fake._balance[fake.base]  == pytest.approx(0.001)

    # Restart #2: open-order discovery succeeds (O1 still genuinely
    # resting) and adopts it, while the queue's OWN direct lookup (used
    # by its retry) still times out — forcing resolution through adoption's
    # merge path instead of the plain retry loop.
    with patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex3 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)

    assert ex3._native_stop_order_id == o1["id"]
    assert ex3._unresolved_stop_recoveries == []
    assert ex3.cash     == pytest.approx(1_156.0)   # unchanged — not double-applied
    assert ex3.position == pytest.approx(0.001)


# ---------------------------------------------------------------------------
# PASS-11 review, finding 2 (P1): splitting a checkpoint-straddling delta
# applies the SAME cumulative-average price to both the checkpoint-covered
# and live portions — exact for quantity, an ESTIMATE for cash/fee when the
# true per-segment execution prices actually differ. This asserts the
# estimate itself converges to the KNOWN (documented) approximation and
# that its use is loudly alerted, rather than silently trusted as exact.
# ---------------------------------------------------------------------------

@patch("time.sleep")
@patch("bot.execution.live_executor.cfg")
def test_checkpoint_split_with_varying_prices_is_estimated_and_alerted(mock_cfg, mock_sleep, tmp_path):
    """PASS-11 review finding (P1, finding 2), exact reproduction: the
    first 0.001 BTC executes offline at $78,000 (checkpoint correctly
    syncs $1,078); after startup, a second 0.001 executes live at
    $82,000. A single read reports cumulative 0.002 @ average $80,000,
    cost $160, zero fees. The exact quantity split (0.001 checkpoint-
    covered / 0.001 live) is not in question — but crediting the live
    portion at the blended $80,000 average (giving executor cash
    $1,158) instead of its own true $82,000 (which would give $1,160)
    is a real, bounded estimation error given ccxt exposes no per-fill
    breakdown. This is expected, documented behavior — asserted here
    specifically so a future change to the estimate's formula doesn't
    silently drift — and must be loudly alerted, not silent."""
    state_path = str(tmp_path / "state.json")
    fake = FakeExchange(cash=1000.0)
    ex = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    ex._portfolio.position    = 0.002
    ex._portfolio._cost_basis = 85_000.0
    fake._balance[fake.base] = 0.002
    o1 = fake.create_order(
        "BTC/CAD", "market", "sell", 0.002,
        params={"stopLossPrice": "78000.00", "clientOrderId": "seed-o1"},
    )
    ex._native_stop_order_id    = o1["id"]
    ex._native_stop_price       = 78_000.0
    ex._native_stop_is_trailing = False
    ex._save_state()

    # Offline: 0.001 fills at $78,000, order stays open.
    fake.simulate_fill(o1["id"], 0.001, 78_000.0, 0.0, terminal=False)

    with patch.object(fake, "fetch_open_orders", return_value=[]), \
         patch.object(fake, "fetch_order", side_effect=ccxt.RequestTimeout("timeout")):
        ex2 = _build_executor(fake, state_path=state_path, native_stop_loss_enabled=True)
    assert ex2.cash == pytest.approx(1_078.0)
    assert ex2._unresolved_stop_recoveries[0]["checkpoint_qty_cap"] == pytest.approx(0.001)

    # Live, a further 0.001 fills at a DIFFERENT price ($82,000) — the
    # exchange now reports cumulative 0.002 @ average $80,000 (cost
    # $160), exactly the review's own reproduction numbers.
    fake.simulate_fill(o1["id"], 0.002, 80_000.0, 0.0, terminal=False)
    mock_alerter = MagicMock()
    ex2._alerter = mock_alerter
    discovered = ex2.reconcile_pending_orders()

    # The known, documented estimation result (blended $80,000 applied to
    # the live 0.001) — NOT the true $1,160 a per-fill reconstruction
    # would give. This pins the current approximation's exact behavior.
    assert ex2.cash == pytest.approx(1_078.0 + 0.001 * 80_000.0)   # == 1,158.0
    assert len(discovered) == 1
    assert discovered[0].quantity == pytest.approx(0.001)
    assert discovered[0].price    == pytest.approx(80_000.0)

    # Loudly alerted as an estimate, not silently trusted as exact.
    assert mock_alerter.error.called
    assert "ESTIMATE" in mock_alerter.error.call_args[0][0]
