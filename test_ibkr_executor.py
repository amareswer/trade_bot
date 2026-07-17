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
from types import SimpleNamespace

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
      "instant"     — orders fill immediately at `fill_price`
      "never"       — orders never fill (executor should time out + cancel)
      "on_cancel"   — order fills the moment cancelOrder is called (race)
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
    contract, _, _ = fake.placed[0]
    assert (contract.symbol, contract.currency) == ("CM", "CAD")

    # CSV row appended with frozen 9-column schema
    with open(tmp_path / "ibkr_trades.csv") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ibkr_mod._CSV_HEADER
    assert rows[1][1] == "CM.TO" and rows[1][2] == "BUY"


def test_buy_rejected_insufficient_cash(executors):
    fake = FakeIB(cash=50.0)
    ex = make_executor(fake)
    executors.append(ex)
    order = ex.buy("KO", 10, 60.0, reason="test")
    assert order.status == OrderStatus.REJECTED
    assert "Insufficient cash" in order.reject_reason
    assert fake.placed == []              # never reached the broker


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
