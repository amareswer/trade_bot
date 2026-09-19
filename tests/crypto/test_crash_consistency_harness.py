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

from unittest.mock import patch

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
