"""
Unit tests for IBKRExecutor (stock_bot/execution/ibkr.py) — roadmap item D.

Fully hermetic: a FakeIB stands in for ib_async's IB, so no test touches the
network or a running TWS.  The executor's real threading machinery (private
event loop + run_coroutine_threadsafe) is exercised — only the broker side
is faked.

Covers: live-port guard, paper-account guard, contract mapping both ways,
BUY/SELL fill paths (fill price comes from the broker, not the request),
insufficient cash/position rejections, fill-timeout rejection, and the
cancel-race guard (a fill racing the cancel is recorded, never dropped —
2026-07-15 crypto limit-chase lesson).
"""
import asyncio
import csv
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import stock_bot.execution.ibkr as ibkr_mod
from stock_bot.execution.base import OrderStatus
from stock_bot.execution.ibkr import IBKRExecutor


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeContract:
    def __init__(self, symbol, currency, primaryExchange=""):
        self.symbol = symbol
        self.currency = currency
        self.primaryExchange = primaryExchange


class FakeOrderStatus:
    def __init__(self):
        self.status = "Submitted"
        self.filled = 0.0
        self.avgFillPrice = 0.0


class FakeTrade:
    def __init__(self):
        self.orderStatus = FakeOrderStatus()
        self.fills = []

    def isDone(self):
        return self.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled")


class FakeIB:
    """
    Minimal ib_async.IB stand-in.

    fill_mode:
      "instant"              — orders fill immediately at `fill_price`
      "never"                — orders never fill (executor should time out + cancel)
      "on_cancel"            — order fills the moment cancelOrder is called (race)
      "flicker_cancel_then_fill" — order reads 'Cancelled' with zero fill the
                             instant it's placed (IBKR Error 10349 quirk), then
                             resubmits and fills on its own a moment later —
                             no cancelOrder() call from us at all
      "flicker_cancel_resubmit_then_fill" — same Error 10349 quirk, but the
                             resubmit passes through a live, still-unfilled
                             'Submitted' status before the real fill lands,
                             instead of jumping straight from 'Cancelled' to
                             'Filled'
      "flicker_cancel_then_slow_fill" — same Error 10349 quirk, but the silent
                             resubmit takes ~0.9s to fill (BNS 2026-08-27 took
                             ~8.3s live) — well past a short _fill_timeout_s
      "flicker_cancel_no_resubmit" — order flickers to 'Cancelled' with zero
                             fill and never resubmits or fills (a genuine dead
                             cancel — the executor must give up, not hang)
    """

    def __init__(self, accounts=("DUQ273338",), cash=100_000.0,
                 net_liq=100_000.0, positions=None,
                 fill_mode="instant", fill_price=100.0):
        self._accounts = list(accounts)
        self._cash = cash
        self._net_liq = net_liq
        self._positions = positions or []
        self.fill_mode = fill_mode
        self.fill_price = fill_price
        self._connected = False
        self.placed = []
        self.cancelled = []

    async def connectAsync(self, host, port, clientId, timeout=10):
        self._connected = True

    def isConnected(self):
        return self._connected

    def disconnect(self):
        self._connected = False

    def managedAccounts(self):
        return list(self._accounts)

    def accountValues(self):
        return [
            SimpleNamespace(tag="TotalCashValue", currency="CAD",
                            value=str(self._cash)),
            SimpleNamespace(tag="NetLiquidation", currency="CAD",
                            value=str(self._net_liq)),
        ]

    def positions(self):
        return list(self._positions)

    async def qualifyContractsAsync(self, contract):
        return [contract]

    def placeOrder(self, contract, order):
        trade = FakeTrade()
        self.placed.append((contract, order, trade))
        if self.fill_mode == "instant":
            self._fill(trade, order)
        elif self.fill_mode == "flicker_cancel_then_fill":
            trade.orderStatus.status = "Cancelled"

            async def _delayed_fill():
                await asyncio.sleep(0.4)
                self._fill(trade, order)

            asyncio.ensure_future(_delayed_fill())
        elif self.fill_mode == "flicker_cancel_resubmit_then_fill":
            trade.orderStatus.status = "Cancelled"

            async def _resubmit_then_fill():
                await asyncio.sleep(0.15)
                trade.orderStatus.status = "Submitted"   # alive again, still unfilled
                await asyncio.sleep(0.4)
                self._fill(trade, order)

            asyncio.ensure_future(_resubmit_then_fill())
        elif self.fill_mode == "flicker_cancel_then_slow_fill":
            trade.orderStatus.status = "Cancelled"

            async def _slow_resubmit_then_fill():
                await asyncio.sleep(0.2)
                trade.orderStatus.status = "PreSubmitted"   # alive again, unfilled
                await asyncio.sleep(0.7)
                self._fill(trade, order)

            asyncio.ensure_future(_slow_resubmit_then_fill())
        elif self.fill_mode == "flicker_cancel_no_resubmit":
            trade.orderStatus.status = "Cancelled"   # dead — never resubmits/fills
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order)
        for _, o, trade in self.placed:
            if o is order and not trade.isDone():
                if self.fill_mode == "on_cancel":
                    self._fill(trade, order)   # fill wins the race
                else:
                    trade.orderStatus.status = "Cancelled"

    def _fill(self, trade, order):
        trade.orderStatus.status = "Filled"
        trade.orderStatus.filled = float(order.totalQuantity)
        trade.orderStatus.avgFillPrice = self.fill_price


def make_executor(fake_ib, **kwargs):
    kwargs.setdefault("fill_timeout_s", 1.0)
    kwargs.setdefault("connect_timeout_s", 2.0)
    return IBKRExecutor(ib_factory=lambda: fake_ib, **kwargs)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Redirect state/CSV to tmp and stub sector lookups (no network)."""
    monkeypatch.setattr(ibkr_mod, "_STATE_JSON", str(tmp_path / "ibkr_state.json"))
    monkeypatch.setattr(ibkr_mod, "_TRADES_CSV", str(tmp_path / "ibkr_trades.csv"))
    monkeypatch.setattr(ibkr_mod, "_SETTLEMENT_CSV", str(tmp_path / "ibkr_settlement.csv"))
    monkeypatch.setattr(ibkr_mod, "get_sector", lambda s: "other")
    return tmp_path


@pytest.fixture
def executors():
    """Track executors so their loop threads are torn down after each test."""
    created = []
    yield created
    for ex in created:
        try:
            ex.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_live_port_refused_without_allow_live():
    with pytest.raises(ValueError, match="LIVE"):
        IBKRExecutor(port=7496)


def test_gateway_live_port_refused_without_allow_live():
    with pytest.raises(ValueError, match="LIVE"):
        IBKRExecutor(port=4001)


# ---------------------------------------------------------------------------
# LiveTradingGate enforcement (2026-08-20 — closes the deferred-enforcement
# decision from the same day's gate-repair session)
# ---------------------------------------------------------------------------

def _gate(n, description, status, detail="ok"):
    return {"gate": n, "description": description, "status": status, "detail": detail}


def _all_gates_pass():
    return [
        _gate(1, "Backtest walk-forward (current strategy)", "PASS"),
        _gate(2, "AI confidence-band edge", "PASS"),
        _gate(3, "Position book (live)", "PASS"),
        _gate(4, "Infrastructure", "PASS"),
    ]


def test_live_port_allowed_when_gates_1_to_3_pass(executors):
    fake = FakeIB(accounts=("U26459664",))   # allow_live bypasses the paper-prefix check too
    with patch.object(ibkr_mod.LiveTradingGate, "evaluate", return_value=_all_gates_pass()):
        ex = make_executor(fake, port=7496, allow_live=True)
    executors.append(ex)
    assert ex is not None


def test_live_port_allowed_even_when_gate4_fails(executors):
    """Gate 4 (infrastructure importability) is deliberately not enforced —
    a broken smoke-test import must not block someone otherwise cleared."""
    fake  = FakeIB(accounts=("U26459664",))
    gates = _all_gates_pass()
    gates[3] = _gate(4, "Infrastructure", "FAIL", "AI memory ✗  TSX audit ✓")
    with patch.object(ibkr_mod.LiveTradingGate, "evaluate", return_value=gates):
        ex = make_executor(fake, port=7496, allow_live=True)
    executors.append(ex)
    assert ex is not None


def test_live_port_blocked_when_gate2_fails():
    gates = _all_gates_pass()
    gates[1] = _gate(2, "AI confidence-band edge", "FAIL", "4 round-trips 40.0% win rate (need 55%)")
    with patch.object(ibkr_mod.LiveTradingGate, "evaluate", return_value=gates):
        with pytest.raises(ValueError, match="Gate 2"):
            IBKRExecutor(port=7496, allow_live=True)


def test_live_port_blocked_when_gate_pending():
    """PENDING (not enough data yet) blocks just like FAIL — 'not proven
    ready' is not the same as 'ready'."""
    gates = _all_gates_pass()
    gates[2] = _gate(3, "Position book (live)", "PENDING", "5 / 30 round-trips")
    with patch.object(ibkr_mod.LiveTradingGate, "evaluate", return_value=gates):
        with pytest.raises(ValueError, match="Gate 3"):
            IBKRExecutor(port=7496, allow_live=True)


def test_live_port_blocked_error_names_only_failing_gates():
    gates = _all_gates_pass()
    gates[0] = _gate(1, "Backtest walk-forward (current strategy)", "FAIL", "failing: AMD")
    gates[1] = _gate(2, "AI confidence-band edge", "PENDING", "3 / 10 round-trips")
    with patch.object(ibkr_mod.LiveTradingGate, "evaluate", return_value=gates):
        with pytest.raises(ValueError) as exc_info:
            IBKRExecutor(port=7496, allow_live=True)
    msg = str(exc_info.value)
    assert "Gate 1" in msg
    assert "Gate 2" in msg
    assert "Gate 3" not in msg   # gate 3 passed — must not be named as a problem


def test_live_port_blocked_before_any_connection_attempt():
    """Fail fast: the gate check runs before __init__ ever touches the IB
    connection, so no connectAsync call and no event-loop thread left
    dangling on a blocked start."""
    fake  = FakeIB(accounts=("U26459664",))
    gates = _all_gates_pass()
    gates[1] = _gate(2, "AI confidence-band edge", "FAIL", "not ready")
    with patch.object(ibkr_mod.LiveTradingGate, "evaluate", return_value=gates):
        with pytest.raises(ValueError):
            make_executor(fake, port=7496, allow_live=True)
    assert not fake._connected
    assert fake.placed == []


def test_paper_mode_never_evaluates_gate(executors):
    """allow_live=False (paper, the default) must never even call
    LiveTradingGate — the gate check must have zero effect on the common
    case, even if the gate itself were somehow broken."""
    fake = FakeIB()   # default DU-prefixed paper account
    with patch.object(
        ibkr_mod.LiveTradingGate, "evaluate",
        side_effect=AssertionError("LiveTradingGate must not run in paper mode"),
    ):
        ex = make_executor(fake)   # default port=7497, allow_live=False
    executors.append(ex)
    assert ex is not None


def test_non_paper_account_refused(executors):
    fake = FakeIB(accounts=("U26459664",))   # live account id — no DU prefix
    with pytest.raises(ValueError, match="not a paper account"):
        make_executor(fake)


def test_connect_failure_raises_connection_error():
    class DeadIB(FakeIB):
        async def connectAsync(self, *a, **k):
            raise ConnectionRefusedError("no TWS")
    with pytest.raises(ConnectionError, match="Could not connect"):
        IBKRExecutor(ib_factory=lambda: DeadIB(), connect_timeout_s=1.0)


# ---------------------------------------------------------------------------
# Contract mapping
# ---------------------------------------------------------------------------

def test_contract_mapping_tsx():
    c = IBKRExecutor.to_contract("RY.TO")
    assert (c.symbol, c.currency, c.primaryExchange) == ("RY", "CAD", "TSE")


def test_contract_mapping_tsx_class_shares():
    c = IBKRExecutor.to_contract("TECK-B.TO")
    assert (c.symbol, c.currency) == ("TECK.B", "CAD")


def test_contract_mapping_us():
    c = IBKRExecutor.to_contract("MRNA")
    assert (c.symbol, c.currency, c.exchange) == ("MRNA", "USD", "SMART")
    assert c.primaryExchange == ""


def test_contract_mapping_nyse_cross_listed():
    # Bare Canadian-company tickers (no .TO) must force primaryExchange=NYSE.
    # Without it, IBKR's SMART/USD qualification for symbols like "CM" was
    # resolving to the TSX/CAD primary listing and hitting the CIRO API
    # block (Error 201) even though these are meant to route as US orders.
    for sym in ("RY", "TD", "BNS", "CM", "SU"):
        c = IBKRExecutor.to_contract(sym)
        assert (c.symbol, c.currency, c.primaryExchange) == (sym, "USD", "NYSE")


def test_contract_roundtrip():
    for sym in ("RY.TO", "TECK-B.TO", "MRNA", "BRK-B", "RY", "CM"):
        c = IBKRExecutor.to_contract(sym)
        assert IBKRExecutor.from_contract(c) == sym


# ---------------------------------------------------------------------------
# BUY path
# ---------------------------------------------------------------------------

def test_buy_fills_at_broker_price_not_request_price(executors, tmp_path):
    fake = FakeIB(fill_price=101.25)
    ex = make_executor(fake)
    executors.append(ex)

    order = ex.buy("CM.TO", 3, 100.0, reason="RULE BUY test", confidence=60)
    assert order.status == OrderStatus.FILLED
    assert order.price == 101.25          # broker fill, not the request price
    assert order.quantity == 3
    assert order.total_value == round(3 * 101.25, 2)

    # contract routed as TSX
    contract, placed_order, _ = fake.placed[0]
    assert (contract.symbol, contract.currency) == ("CM", "CAD")

    # tif set explicitly to "DAY" — heads off IBKR Error 10349 ("Order TIF was
    # set to DAY based on order preset"), the cancel/resubmit trigger behind
    # the RY 2026-07-31/08-19 and BNS 2026-08-27 dropped-fill incidents
    assert placed_order.tif == "DAY"

    # CSV row appended with frozen 9-column schema
    with open(tmp_path / "ibkr_trades.csv") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ibkr_mod._CSV_HEADER
    assert rows[1][1] == "CM.TO" and rows[1][2] == "BUY"


def test_buy_rejected_insufficient_cash(executors):
    # Cash kept above the FX/margin-minimum threshold (below tests that
    # specifically) so this exercises the cash check, not the equity guard.
    fake = FakeIB(cash=3_000.0)
    ex = make_executor(fake)
    executors.append(ex)
    order = ex.buy("KO", 100, 60.0, reason="test")   # costs $6,000 > $3,000 cash
    assert order.status == OrderStatus.REJECTED
    assert "Insufficient cash" in order.reject_reason
    assert fake.placed == []              # never reached the broker


def test_buy_rejected_low_equity_fx_trade(executors):
    # IBKR refuses to buy a non-CAD security below $2,500 CAD equity — it
    # treats the purchase as an implicit margin/currency trade (Error 201).
    # Discovered 2026-07-20: CM's first live rule BUY hit this wall at
    # ~$995 CAD equity. Checked proactively so the order never reaches IBKR.
    fake = FakeIB(cash=995.28)
    ex = make_executor(fake)
    executors.append(ex)
    order = ex.buy("KO", 1, 60.0, reason="test")
    assert order.status == OrderStatus.REJECTED
    assert "2,500" in order.reject_reason
    assert fake.placed == []              # never reached the broker


def test_buy_allowed_low_equity_cad_security(executors):
    # The equity floor only applies to non-CAD (foreign-currency) contracts —
    # a CAD-denominated TSX buy must not be blocked by it.
    fake = FakeIB(cash=995.28, fill_price=24.0)
    ex = make_executor(fake)
    executors.append(ex)
    order = ex.buy("CM.TO", 1, 24.0, reason="test")
    assert order.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# Sector concentration gate (_MAX_PER_SECTOR = 2) — previously untested even
# though the gate is live in the real trading path (found 2026-08-05).
# ---------------------------------------------------------------------------

def _bank_sector(sym: str) -> str:
    # CM's held position snapshots back as "CM.TO" (TSE/CAD contract mapping).
    return "financial services" if sym.upper() in ("RY", "CM", "CM.TO", "TD") else "other"


def test_buy_rejected_when_sector_limit_reached(executors, monkeypatch):
    monkeypatch.setattr(ibkr_mod, "get_sector", _bank_sector)
    fake = FakeIB(positions=[_ry_position(), _cm_position()])   # 2 banks already open
    ex = make_executor(fake)
    executors.append(ex)

    order = ex.buy("TD", 1, 80.0, reason="test")
    assert order.status == OrderStatus.REJECTED
    assert "Sector limit" in order.reject_reason
    assert "financial services" in order.reject_reason
    assert fake.placed == []              # never reached the broker


def test_buy_allowed_adding_to_already_held_sector_symbol(executors, monkeypatch):
    # The gate only blocks *new* positions in a full sector — topping up a
    # symbol already held must not be blocked by its own sector count.
    monkeypatch.setattr(ibkr_mod, "get_sector", _bank_sector)
    fake = FakeIB(positions=[_ry_position(), _cm_position()], fill_price=210.0)
    ex = make_executor(fake)
    executors.append(ex)

    order = ex.buy("RY", 1, 210.0, reason="test")
    assert order.status == OrderStatus.FILLED


def test_buy_allowed_different_sector_when_limit_reached(executors, monkeypatch):
    monkeypatch.setattr(ibkr_mod, "get_sector", _bank_sector)
    fake = FakeIB(positions=[_ry_position(), _cm_position()], fill_price=60.0)
    ex = make_executor(fake)
    executors.append(ex)

    order = ex.buy("KO", 1, 60.0, reason="test")   # not a bank — different sector
    assert order.status == OrderStatus.FILLED


def test_buy_rejected_on_fill_timeout(executors):
    fake = FakeIB(fill_mode="never")
    ex = make_executor(fake, fill_timeout_s=0.5)
    executors.append(ex)
    order = ex.buy("KO", 2, 60.0, reason="test")
    assert order.status == OrderStatus.REJECTED
    assert "no fill" in order.reject_reason
    assert len(fake.cancelled) == 1       # timeout triggered a cancel


def test_buy_corrupted_candle_rejected(executors):
    fake = FakeIB()
    ex = make_executor(fake)
    executors.append(ex)
    order = ex.buy("KO", 2, 60.0, reason="test",
                   candle_close=60.0, live_price=100.0)
    assert order.status == OrderStatus.REJECTED
    assert "corrupted data" in order.reject_reason
    assert fake.placed == []


# ---------------------------------------------------------------------------
# Cancel race — the 2026-07-15 lesson
# ---------------------------------------------------------------------------

def test_fill_racing_cancel_is_recorded(executors):
    fake = FakeIB(fill_mode="on_cancel", fill_price=59.9)
    ex = make_executor(fake, fill_timeout_s=0.5)
    executors.append(ex)
    order = ex.buy("KO", 2, 60.0, reason="test")
    assert order.status == OrderStatus.FILLED    # fill won the race → recorded
    assert order.price == 59.9
    assert len(fake.cancelled) == 1


def test_flicker_cancel_then_fill_is_recorded(executors):
    # RY, 2026-07-31: IBKR's Error 10349 ("Order TIF was set to DAY based on
    # order preset") flips the order to 'Cancelled' with zero fill the
    # instant it's placed, then silently resubmits and fills for real ~1s
    # later — with nothing initiated by us. Before this fix, the executor
    # trusted the first 'Cancelled' reading, logged/alerted the order as
    # REJECTED, and the real fill that followed was never recorded anywhere
    # (no CSV row, no order status update) even though the broker genuinely
    # held the position.
    fake = FakeIB(fill_mode="flicker_cancel_then_fill", fill_price=210.55)
    ex = make_executor(fake, fill_timeout_s=2.0)
    executors.append(ex)
    order = ex.buy("RY", 4, 210.0, reason="test")
    assert order.status == OrderStatus.FILLED
    assert order.price == 210.55
    assert order.quantity == 4
    assert fake.cancelled == []    # never initiated a cancel ourselves


def test_flicker_cancel_resubmit_then_fill_is_recorded(executors):
    # RY, 2026-08-19: a second occurrence of the same Error 10349 quirk, but
    # this time the resubmit passed through a live, still-unfilled
    # 'Submitted' status ~350ms after the 'Cancelled' flicker, before filling
    # for real ~2.4s after that. The 2026-07-31 fix's grace loop only kept
    # waiting *while status stayed 'Cancelled'* — the instant it saw
    # 'Submitted' it exited, treating a live-but-unfilled order as resolved.
    # The executor logged/alerted the order as REJECTED ("position remains
    # open") and the real fill that followed was never recorded anywhere,
    # even though the broker genuinely closed the position. This must not
    # regress: the grace window should wait on an actual fill, not on the
    # order staying in one particular status.
    fake = FakeIB(fill_mode="flicker_cancel_resubmit_then_fill", fill_price=212.13)
    ex = make_executor(fake, fill_timeout_s=2.0)
    executors.append(ex)
    order = ex.buy("RY", 4, 212.0, reason="test")
    assert order.status == OrderStatus.FILLED
    assert order.price == 212.13
    assert order.quantity == 4
    assert fake.cancelled == []    # never initiated a cancel ourselves


def test_flicker_cancel_then_slow_resubmit_fill_is_recorded(executors, monkeypatch):
    # BNS, 2026-08-27: the same Error 10349 quirk as the two RY incidents
    # above, but the silent resubmit took ~8.3s to fill — past the old
    # hardcoded 5s grace window. The executor gave up, alerted "order ended
    # 'PreSubmitted' with no fill", and never recorded the fill while the
    # broker held 7 BNS. The resubmit-grace window must be (a) generous enough
    # for a multi-second resubmit and (b) independent of _fill_timeout_s, so a
    # short fill timeout cannot truncate it. fill_timeout_s here (0.3s) is far
    # shorter than the ~0.9s resubmit fill delay.
    monkeypatch.setattr(ibkr_mod, "_CANCEL_RESUBMIT_GRACE_S", 3.0)
    fake = FakeIB(fill_mode="flicker_cancel_then_slow_fill", fill_price=92.96)
    ex = make_executor(fake, fill_timeout_s=0.3)
    executors.append(ex)
    t0 = time.monotonic()
    order = ex.buy("BNS", 7, 93.0, reason="test")
    elapsed = time.monotonic() - t0
    assert order.status == OrderStatus.FILLED
    assert order.price == 92.96
    assert order.quantity == 7
    assert fake.cancelled == []       # never initiated a cancel ourselves
    assert elapsed > 0.3              # waited past _fill_timeout_s for the resubmit


def test_flicker_cancel_no_resubmit_gives_up_after_grace(executors, monkeypatch):
    # The other side of the BNS fix: a genuine dead 'Cancelled' with no
    # resubmit must not hang the scan loop. Once _CANCEL_RESUBMIT_GRACE_S
    # elapses with no fill, give up and report REJECTED.
    monkeypatch.setattr(ibkr_mod, "_CANCEL_RESUBMIT_GRACE_S", 0.4)
    fake = FakeIB(fill_mode="flicker_cancel_no_resubmit", fill_price=93.0)
    ex = make_executor(fake, fill_timeout_s=5.0)   # long — grace must gate, not this
    executors.append(ex)
    t0 = time.monotonic()
    order = ex.buy("BNS", 7, 93.0, reason="test")
    elapsed = time.monotonic() - t0
    assert order.status == OrderStatus.REJECTED
    assert "no fill" in order.reject_reason
    assert fake.cancelled == []       # already 'Cancelled' — nothing to cancel
    assert elapsed < 3.0              # gave up on the grace window, not fill_timeout_s


# ---------------------------------------------------------------------------
# SELL path
# ---------------------------------------------------------------------------

def _cm_position(shares=4, avg_cost=168.35):
    return SimpleNamespace(
        contract=FakeContract("CM", "CAD", "TSE"),
        position=float(shares),
        avgCost=avg_cost,
    )


def test_sell_without_position_rejected(executors):
    fake = FakeIB()
    ex = make_executor(fake)
    executors.append(ex)
    order = ex.sell("CM.TO", 4, 170.0, reason="test")
    assert order.status == OrderStatus.REJECTED
    assert "Insufficient position" in order.reject_reason


def test_sell_fills_and_records_realized_pnl(executors):
    fake = FakeIB(positions=[_cm_position(4, 168.35)], fill_price=170.0)
    ex = make_executor(fake)
    executors.append(ex)
    order = ex.sell("CM.TO", 4, 169.0, reason="STOP_LOSS_HIT")
    assert order.status == OrderStatus.FILLED
    assert order.price == 170.0
    assert ex.realized_pnl() == pytest.approx((170.0 - 168.35) * 4, abs=0.01)


def test_positions_snapshot_maps_back_to_yfinance_symbols(executors):
    fake = FakeIB(positions=[_cm_position()])
    ex = make_executor(fake)
    executors.append(ex)
    snap = ex.positions_snapshot()
    assert "CM.TO" in snap
    shares, cost = snap["CM.TO"]
    assert shares == 4.0 and cost == 168.35


# ---------------------------------------------------------------------------
# TWS-query resilience (2026-08-27): a transient accountValues()/positions()
# failure must serve the last-good cache, not a fabricated 0.0 / {} that
# rejects every BUY and blinds the SL/TP watcher to a real position.
# ---------------------------------------------------------------------------

def test_positions_snapshot_serves_last_good_cache_on_failure(executors):
    fake = FakeIB(positions=[_cm_position(4, 168.35)])
    ex = make_executor(fake)
    executors.append(ex)
    assert ex.positions_snapshot() == {"CM.TO": (4.0, 168.35)}   # primes the cache
    assert ex.sync_healthy is True

    def _boom():
        raise TimeoutError("TWS not responding")
    fake.positions = _boom

    snap = ex.positions_snapshot()
    assert snap == {"CM.TO": (4.0, 168.35)}, "must fall back to the last-good book"
    assert ex.sync_healthy is False

    fake.positions = lambda: [_cm_position(4, 168.35)]
    ex.positions_snapshot()
    assert ex.sync_healthy is True   # recovers


def test_cash_serves_last_good_cache_on_account_values_failure(executors):
    fake = FakeIB(cash=4004.51, net_liq=5003.02)
    ex = make_executor(fake)
    executors.append(ex)
    assert ex.cash == pytest.approx(4004.51)   # primes the cache

    def _boom():
        raise TimeoutError("TWS not responding")
    fake.accountValues = _boom

    assert ex.cash == pytest.approx(4004.51), "stale cash beats a fabricated 0.0"
    assert ex.sync_healthy is False


def test_no_cache_yet_still_falls_back_to_zero_and_empty(executors):
    fake = FakeIB()
    def _boom():
        raise TimeoutError("down from the first call")
    fake.accountValues = _boom
    fake.positions = _boom
    ex = make_executor(fake)
    executors.append(ex)
    assert ex.cash == 0.0
    assert ex.positions_snapshot() == {}
    assert ex.sync_healthy is False


# ---------------------------------------------------------------------------
# ibkr_trades.csv write resilience (2026-08-27): a failed append must buffer
# the real filled-trade row and retry, not silently drop it (the readiness
# gate reads that CSV exactly).
# ---------------------------------------------------------------------------

def test_failed_trade_csv_write_buffers_and_retries(executors, tmp_path, monkeypatch):
    fake = FakeIB(fill_price=100.0)
    ex = make_executor(fake)
    executors.append(ex)

    real_open = open
    def _no_write(path, mode="r", *a, **k):
        if str(path).endswith("ibkr_trades.csv") and "a" in mode:
            raise OSError("disk full")
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr("builtins.open", _no_write)

    ex.buy("CM.TO", 2, 100.0, reason="test-buffered")
    assert ex.csv_write_healthy is False
    assert len(ex._unwritten_csv_rows) == 1

    monkeypatch.setattr("builtins.open", real_open)   # disk recovers
    ex.buy("CM.TO", 1, 100.0, reason="test-recovered")
    assert ex.csv_write_healthy is True
    assert ex._unwritten_csv_rows == []

    import csv as _csv
    with real_open(ibkr_mod._TRADES_CSV, newline="") as f:
        rows = list(_csv.reader(f))
    reasons = [r[7] for r in rows[1:]]   # skip header
    assert "test-buffered" in reasons and "test-recovered" in reasons


def test_realized_pnl_persists_across_instances(executors, tmp_path):
    fake = FakeIB(positions=[_cm_position(4, 168.35)], fill_price=170.0)
    ex = make_executor(fake)
    executors.append(ex)
    ex.sell("CM.TO", 4, 169.0, reason="test")
    pnl = ex.realized_pnl()
    assert pnl > 0

    fake2 = FakeIB()
    ex2 = make_executor(fake2)
    executors.append(ex2)
    assert ex2.realized_pnl() == pytest.approx(pnl, abs=0.01)


# ---------------------------------------------------------------------------
# FX sizing — total_value / check_exposure must convert USD positions to
# the account's CAD base currency (2026-07-31 fix)
# ---------------------------------------------------------------------------

def _ry_position(shares=4, avg_cost=210.55):
    # Bare NYSE cross-listing: USD, no TSE routing — from_contract() maps
    # this back to plain "RY", same as the live RY position that exposed
    # the original bug.
    return SimpleNamespace(
        contract=FakeContract("RY", "USD", "NYSE"),
        position=float(shares),
        avgCost=avg_cost,
    )


def test_total_value_converts_usd_position_via_fx_rate(executors):
    fake = FakeIB(positions=[_ry_position(4, 210.55)], cash=2000.0)
    ex = make_executor(fake)
    executors.append(ex)
    with patch("stock_bot.execution.ibkr.get_usd_cad_rate", return_value=1.35):
        total = ex.total_value({"RY": 210.55})
    assert total == round(2000.0 + 4 * 210.55 * 1.35, 2)


def test_total_value_leaves_cad_position_unconverted(executors):
    fake = FakeIB(positions=[_cm_position(4, 168.35)], cash=2000.0)
    ex = make_executor(fake)
    executors.append(ex)
    with patch("stock_bot.execution.ibkr.get_usd_cad_rate", return_value=1.35):
        total = ex.total_value({"CM.TO": 168.35})
    assert total == round(2000.0 + 4 * 168.35, 2)   # no FX applied — CM.TO is CAD


def test_check_exposure_flags_usd_position_understated_without_fx(executors):
    # $500 USD face value looks like 25% of a $2000 account — but at 1.35
    # it's really ~$675 CAD, ~29% — must trip a 27% cap that the unconverted
    # math would have cleared.
    fake = FakeIB(positions=[_ry_position(shares=2, avg_cost=250.0)], cash=1500.0)
    ex = make_executor(fake, max_exposure_pct=0.27)
    executors.append(ex)
    with patch("stock_bot.execution.ibkr.get_usd_cad_rate", return_value=1.35):
        under_cap = ex.check_exposure({"RY": 250.0})
    assert under_cap is False


# ---------------------------------------------------------------------------
# check_exposure projects the PENDING trade too (added 2026-08-05,
# punch-list item #7 — closes the gap where a single large BUY could blow
# past the cap in one shot since the old check only looked at current state).
# ---------------------------------------------------------------------------

def test_check_exposure_pending_trade_value_defaults_to_current_state_only(executors):
    fake = FakeIB(net_liq=1000.0, cash=1000.0)
    ex = make_executor(fake, max_exposure_pct=0.25)
    executors.append(ex)
    assert ex.check_exposure({}) is True
    assert ex.check_exposure({}, pending_trade_value=0.0) is True


def test_check_exposure_catches_a_single_oversized_buy(executors):
    fake = FakeIB(net_liq=1000.0, cash=1000.0)
    ex = make_executor(fake, max_exposure_pct=0.25)
    executors.append(ex)
    # No positions held, so a current-state-only check would pass — but a
    # $400 pending BUY on a $1000 account is 40%, over the 25% cap.
    assert ex.check_exposure({}) is True
    assert ex.check_exposure({}, pending_trade_value=400.0) is False


def test_check_exposure_allows_pending_trade_that_stays_under_cap(executors):
    fake = FakeIB(net_liq=1000.0, cash=1000.0)
    ex = make_executor(fake, max_exposure_pct=0.25)
    executors.append(ex)
    assert ex.check_exposure({}, pending_trade_value=200.0) is True   # 20% < 25%


# ---------------------------------------------------------------------------
# Reconnect probe (TWS monitor leg — 2026-07-18)
# ---------------------------------------------------------------------------

def test_try_reconnect_redials_after_socket_drop(executors):
    """After TWS drops the socket (nightly logoff), try_reconnect restores it
    — this is what lets the monitor fire "restored" without waiting for the
    next order."""
    fake = FakeIB()
    ex = make_executor(fake)
    executors.append(ex)

    fake.disconnect()                      # TWS logged off
    assert not ex.is_connected
    assert ex.try_reconnect() is True      # TWS is back — probe redials
    assert ex.is_connected


def test_try_reconnect_never_raises_while_tws_still_down(executors):
    class FlakyIB(FakeIB):
        fail_reconnect = False

        async def connectAsync(self, *a, **k):
            if self.fail_reconnect:
                raise ConnectionRefusedError("TWS still down")
            await super().connectAsync(*a, **k)

    fake = FlakyIB()
    ex = make_executor(fake)
    executors.append(ex)

    fake.fail_reconnect = True
    fake.disconnect()
    assert ex.try_reconnect() is False     # swallowed, not raised
    assert not ex.is_connected


def test_try_reconnect_noop_when_already_connected(executors):
    class CountingIB(FakeIB):
        connect_calls = 0

        async def connectAsync(self, *a, **k):
            self.connect_calls += 1
            await super().connectAsync(*a, **k)

    fake = CountingIB()
    ex = make_executor(fake)
    executors.append(ex)

    assert fake.connect_calls == 1         # startup connect only
    assert ex.try_reconnect() is True
    assert fake.connect_calls == 1         # no redial while healthy


# ---------------------------------------------------------------------------
# starting_cash rebaseline (2026-07-20 manual-paper-reset incident)
# ---------------------------------------------------------------------------

def _seed_state(tmp_path, starting_cash, realized_pnl=0.0):
    with open(tmp_path / "ibkr_state.json", "w") as f:
        json.dump({
            "account": "DUQ273338",
            "realized_pnl": realized_pnl,
            "starting_cash": starting_cash,
            "last_updated": "2026-07-17T00:00:00",
        }, f)


def test_starting_cash_seeds_on_first_ever_connect(executors, sandbox):
    # No state file yet — starting_cash must be auto-pulled from the live
    # NetLiquidation feed, not hardcoded.
    fake = FakeIB(net_liq=995.28)
    ex = make_executor(fake)
    executors.append(ex)
    assert ex.starting_cash == 995.28


def test_starting_cash_rebaselines_on_external_reset(executors, sandbox):
    # A manual IBKR paper-account reset changes net_liq with zero trades in
    # between — the executor must detect the unexplained jump and
    # re-baseline automatically instead of keeping the stale figure.
    _seed_state(sandbox, starting_cash=995.30, realized_pnl=0.0)
    fake = FakeIB(net_liq=5000.0, cash=5000.0)
    ex = make_executor(fake)
    executors.append(ex)
    assert ex.starting_cash == 5000.0

    with open(sandbox / "ibkr_state.json") as f:
        saved = json.load(f)
    assert saved["starting_cash"] == 5000.0


def test_starting_cash_not_rebaselined_for_small_drift(executors, sandbox):
    # Ordinary unrealized mark-to-market drift on an open position must NOT
    # be mistaken for an external reset.
    _seed_state(sandbox, starting_cash=1000.0, realized_pnl=0.0)
    fake = FakeIB(net_liq=1010.0)   # $10 drift, under the $50 floor
    ex = make_executor(fake)
    executors.append(ex)
    assert ex.starting_cash == 1000.0


def test_starting_cash_rebaseline_accounts_for_realized_pnl(executors, sandbox):
    # The re-baseline must subtract already-tracked realized P&L so it isn't
    # double-counted as part of the "external" jump.
    _seed_state(sandbox, starting_cash=1000.0, realized_pnl=50.0)
    fake = FakeIB(net_liq=5050.0)   # a $4,000 external deposit on top of the $50 already realized
    ex = make_executor(fake)
    executors.append(ex)
    assert ex.starting_cash == 5000.0   # 5050 - 50, not 5050


# ---------------------------------------------------------------------------
# Live-cash snapshot persisted in ibkr_state.json (for offline report readers)
# ---------------------------------------------------------------------------

def test_save_state_persists_live_cash(executors, sandbox):
    # Offline readers (paper_report / unified_dashboard) show cash from
    # ibkr_state.json — save_state must write the live TWS cash value.
    fake = FakeIB(cash=3337.56, net_liq=5000.0)
    ex = make_executor(fake)
    executors.append(ex)
    ex.save_state()
    with open(sandbox / "ibkr_state.json") as f:
        state = json.load(f)
    assert state["cash"] == 3337.56


def test_save_state_keeps_last_good_cash_when_disconnected(executors, sandbox):
    # A save while TWS is unreachable must not overwrite the snapshot with 0.
    fake = FakeIB(cash=3337.56, net_liq=5000.0)
    ex = make_executor(fake)
    executors.append(ex)
    ex.save_state()                      # good snapshot while connected
    fake._connected = False              # TWS goes away
    ex.save_state()                      # save during the outage
    with open(sandbox / "ibkr_state.json") as f:
        state = json.load(f)
    assert state["cash"] == 3337.56      # previous good value preserved


# ---------------------------------------------------------------------------
# Weekly loss / drawdown-from-peak breaker tiers (added 2026-08-05).
# net_liq=1000 at connect seeds both peak_equity and week_open_equity at
# $1000; mutating fake._net_liq before a call simulates a portfolio move
# (accountValues() re-reads it live on every _net_liquidation() call).
# ---------------------------------------------------------------------------

def _make_breaker_executor(executors, net_liq=1000.0) -> tuple:
    fake = FakeIB(net_liq=net_liq, cash=net_liq)
    ex = make_executor(fake)
    executors.append(ex)
    ex.set_daily_loss_limit(0.99)   # isolate the new tiers from the (tighter) daily breaker
    ex.set_weekly_loss_limit(0.05)
    ex.set_drawdown_limits(0.10, 0.15, 0.20)
    assert ex._peak_equity == pytest.approx(1000.0)
    return ex, fake


def test_ibkr_weekly_loss_blocks_buy_without_tripping_halt_or_kill(executors):
    ex, fake = _make_breaker_executor(executors)
    fake._net_liq = 940.0   # 6% down — between the 5% weekly and 15% halt tiers
    order = ex.buy("KO", 1, 60.0, reason="test")
    assert order.status == OrderStatus.REJECTED
    assert "Weekly loss limit" in order.reject_reason
    assert not ex._is_kill_switch_tripped(940.0)


def test_ibkr_drawdown_halt_blocks_buy_and_auto_lifts_on_recovery(executors):
    ex, fake = _make_breaker_executor(executors)
    fake._net_liq = 830.0   # 17% down — between the 15% halt and 20% kill tiers
    order = ex.buy("KO", 1, 60.0, reason="test")
    assert order.status == OrderStatus.REJECTED
    assert "Drawdown halt" in order.reject_reason

    fake._net_liq = 1000.0   # fully recovers
    assert not ex._is_drawdown_halted()


def test_ibkr_kill_switch_blocks_buy_never_blocks_sell(executors):
    ex, fake = _make_breaker_executor(executors)
    fake._positions = [_cm_position(10, 100.0)]   # position to sell once kill switch trips
    fake._net_liq = 700.0   # 30% down — past the 20% kill-switch threshold

    buy_order = ex.buy("KO", 1, 60.0, reason="test")
    assert buy_order.status == OrderStatus.REJECTED
    assert "KILL SWITCH" in buy_order.reject_reason

    sell_order = ex.sell("CM.TO", 10, 100.0, reason="test")
    assert sell_order.status == OrderStatus.FILLED


def test_ibkr_kill_switch_persists_across_restart(executors, sandbox):
    ex, fake = _make_breaker_executor(executors)
    fake._net_liq = 700.0   # 30% down
    assert ex._is_kill_switch_tripped(700.0)   # evaluates + latches the sticky flag, saves state

    fake2 = FakeIB(net_liq=1000.0, cash=1000.0)   # fresh connection, fully recovered equity
    ex2 = make_executor(fake2)
    executors.append(ex2)
    assert ex2._kill_switch_tripped is True
    order = ex2.buy("KO", 1, 60.0, reason="test")
    assert order.status == OrderStatus.REJECTED
    assert "KILL SWITCH" in order.reject_reason


def test_ibkr_peak_equity_persists_across_restart(executors, sandbox):
    ex, fake = _make_breaker_executor(executors)
    fake._net_liq = 1300.0
    ex._update_breaker_marks(1300.0)
    assert ex._peak_equity == pytest.approx(1300.0)

    fake2 = FakeIB(net_liq=1000.0, cash=1000.0)   # fresh connection at a lower live value
    ex2 = make_executor(fake2)
    executors.append(ex2)
    assert ex2._peak_equity == pytest.approx(1300.0)   # peak survived the restart


def test_ibkr_drawdown_status_warning_flag_tracks_threshold(executors):
    ex, fake = _make_breaker_executor(executors)
    assert ex.drawdown_status()["warning"] is False

    fake._net_liq = 890.0   # 11% down — past the 10% warning tier
    status = ex.drawdown_status()
    assert status["warning"] is True
    assert status["drawdown_pct"] == pytest.approx(0.11, abs=0.001)


# ---------------------------------------------------------------------------
# Daily-loss breaker: calendar-day anchoring (2026-08-28). Was anchored to
# _session_start_value (set once per connect) so a restart mid-drawdown
# forgot the day's loss. Now _day_open_equity / _day_start_iso are persisted
# and roll on a UTC date change, matching the weekly tier + crypto RiskManager.
# ---------------------------------------------------------------------------

def _pin_ibkr_day(monkeypatch, iso: str) -> None:
    monkeypatch.setattr(ibkr_mod.IBKRExecutor, "_current_day_iso",
                        staticmethod(lambda: iso))


def test_ibkr_daily_baseline_persists_across_same_day_restart(executors, sandbox, monkeypatch):
    _pin_ibkr_day(monkeypatch, "2026-08-28")
    fake = FakeIB(net_liq=1000.0, cash=1000.0)
    ex = make_executor(fake)
    executors.append(ex)
    ex.set_daily_loss_limit(0.03)
    assert ex._day_open_equity == pytest.approx(1000.0)

    fake._net_liq = 950.0                       # 5% down
    assert ex._is_daily_loss_tripped()
    ex.save_state()

    # Same-day restart, equity still depressed: the $1000 baseline is restored
    # from disk, NOT re-based to the current $950 net-liq.
    fake2 = FakeIB(net_liq=950.0, cash=950.0)
    ex2 = make_executor(fake2)
    executors.append(ex2)
    ex2.set_daily_loss_limit(0.03)
    assert ex2._day_open_equity == pytest.approx(1000.0)
    assert ex2._is_daily_loss_tripped()
    order = ex2.buy("KO", 1, 60.0, reason="test")
    assert order.status == OrderStatus.REJECTED
    assert "Daily loss limit" in order.reject_reason


def test_ibkr_daily_baseline_rolls_and_unblocks_on_new_day(executors, sandbox, monkeypatch):
    _pin_ibkr_day(monkeypatch, "2026-08-28")
    fake = FakeIB(net_liq=1000.0, cash=1000.0)
    ex = make_executor(fake)
    executors.append(ex)
    ex.set_daily_loss_limit(0.03)
    fake._net_liq = 950.0
    assert ex._is_daily_loss_tripped()          # tripped on the 28th
    ex.save_state()

    # New UTC day: the connect-time _update_breaker_marks rolls the baseline
    # to the live net-liq ($950), so the drawdown from the new open is 0.
    _pin_ibkr_day(monkeypatch, "2026-08-29")
    fake2 = FakeIB(net_liq=950.0, cash=950.0)
    ex2 = make_executor(fake2)
    executors.append(ex2)
    ex2.set_daily_loss_limit(0.03)
    assert ex2._day_open_equity == pytest.approx(950.0)
    assert ex2._day_start_iso == "2026-08-29"
    assert not ex2._is_daily_loss_tripped()


def test_ibkr_daily_baseline_rolls_mid_run_without_restart(executors, sandbox, monkeypatch):
    _pin_ibkr_day(monkeypatch, "2026-08-28")
    fake = FakeIB(net_liq=1000.0, cash=1000.0)
    ex = make_executor(fake)
    executors.append(ex)
    ex.set_daily_loss_limit(0.03)
    fake._net_liq = 940.0
    assert ex._is_daily_loss_tripped()          # 6% down on the 28th

    _pin_ibkr_day(monkeypatch, "2026-08-29")    # date ticks over, process still up
    assert not ex._is_daily_loss_tripped()      # _is_daily_loss_tripped rolls the baseline
    assert ex._day_open_equity == pytest.approx(940.0)
    assert ex._day_start_iso == "2026-08-29"


# ---------------------------------------------------------------------------
# Per-position ATR stop-loss override (opt-in ATR sizing, added 2026-08-05).
# ---------------------------------------------------------------------------

def test_ibkr_position_stop_pct_defaults_to_baseline(executors):
    fake = FakeIB()
    ex = make_executor(fake)
    executors.append(ex)
    assert ex.get_position_stop_pct("KO", 0.05) == 0.05


def test_ibkr_position_stop_pct_override_and_persistence(executors, sandbox):
    fake = FakeIB()
    ex = make_executor(fake)
    executors.append(ex)
    ex.set_position_stop_pct("KO", 0.08)
    assert ex.get_position_stop_pct("KO", 0.05) == 0.08

    fake2 = FakeIB()
    ex2 = make_executor(fake2)
    executors.append(ex2)
    assert ex2.get_position_stop_pct("KO", 0.05) == 0.08


def test_ibkr_position_stop_pct_cleared_on_full_close(executors):
    fake = FakeIB(positions=[_cm_position(10, 100.0)], fill_price=110.0)
    ex = make_executor(fake)
    executors.append(ex)
    ex.set_position_stop_pct("CM.TO", 0.08)
    order = ex.sell("CM.TO", 10, 110.0, reason="test")
    assert order.status == OrderStatus.FILLED
    assert ex.get_position_stop_pct("CM.TO", 0.05) == 0.05
