"""
Fake exchange adapter for execution-accounting tests — implements
bot.accounting.engine.ExchangeAdapter without any network access or ccxt
import. Modeled on execution_accounting_reference_model.py's
SyntheticExchange (which proved this exact reasoning across four review
passes against a synthetic exchange) but speaks the REAL production types
(ObservedTrade, LedgerMovement, TradePage, ISO timestamps) that
bot/accounting/engine.py and bot/accounting/reconciliation.py actually use,
so these tests exercise the real production code paths, not a parallel
toy implementation.

Every balance-affecting event (trade, deposit, withdrawal) takes effect on
the actual balance immediately — exactly like a real exchange, where the
balance endpoint is authoritative and immediate. History VISIBILITY is a
separate, independently controlled flag, modeling real propagation lag
between "the fill executed" and "fetch_my_trades reports it" — this is
what makes the checkpoint-race scenario reproducible on purpose.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from bot.accounting.engine import LedgerMovement, ObservedTrade, TradePage


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def ms(iso_str: str) -> int:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


@dataclass
class _Deposit:
    entry_id: str
    asset: str
    amount: float
    timestamp_ms: int
    visible: bool


class FakeExchangeAdapter:
    def __init__(self, *, amount_tol: float = 1e-8, cash_tol: float = 0.005) -> None:
        self._balances: "dict[str, float]" = {}
        self._trades: "list[tuple[ObservedTrade, bool]]" = []  # (trade, visible)
        self._deposits: "list[_Deposit]" = []
        self._withdrawals: "list[_Deposit]" = []
        self._next_id = 1
        self._amount_tol = amount_tol
        self._cash_tol = cash_tol

    def _new_id(self, prefix: str) -> str:
        i = self._next_id
        self._next_id += 1
        return f"{prefix}{i}"

    def _apply(self, asset: str, delta: float) -> None:
        self._balances[asset] = self._balances.get(asset, 0.0) + delta

    # -- mutation (test-driver only) --------------------------------------

    def execute_trade(
        self, *, symbol: str, side: str, price: float, amount: float, timestamp_ms: int,
        fee_cost: float = 0.0, fee_currency: "str | None" = None, order_id: "str | None" = None,
        trade_id: "str | None" = None, visible: bool = True,
    ) -> ObservedTrade:
        base, quote = symbol.split("/")
        cost = price * amount
        oid = order_id or self._new_id("O")
        tid = trade_id or self._new_id("T")
        fee_currency = fee_currency or quote
        trade = ObservedTrade(
            trade_id=tid, order_id=oid, symbol=symbol, side=side, price=price, amount=amount,
            cost=cost, fee_cost=fee_cost, fee_currency=fee_currency,
            exchange_timestamp=iso(timestamp_ms), source="live",
        )
        self._trades.append((trade, visible))
        if side == "buy":
            self._apply(quote, -cost - (fee_cost if fee_currency == quote else 0.0))
            self._apply(base, amount)
        else:
            self._apply(quote, cost - (fee_cost if fee_currency == quote else 0.0))
            self._apply(base, -amount)
        return trade

    def deposit(self, asset: str, amount: float, *, timestamp_ms: int, visible: bool = True) -> None:
        self._apply(asset, amount)
        self._deposits.append(_Deposit(self._new_id("D"), asset, amount, timestamp_ms, visible))

    def withdraw(self, asset: str, amount: float, *, timestamp_ms: int, visible: bool = True) -> None:
        self._apply(asset, -amount)
        self._withdrawals.append(_Deposit(self._new_id("W"), asset, -amount, timestamp_ms, visible))

    def reveal_all(self) -> None:
        self._trades = [(t, True) for t, _ in self._trades]
        self._deposits = [_Deposit(d.entry_id, d.asset, d.amount, d.timestamp_ms, True) for d in self._deposits]
        self._withdrawals = [_Deposit(d.entry_id, d.asset, d.amount, d.timestamp_ms, True) for d in self._withdrawals]

    def revise_trade_fee(self, trade_id: str, new_fee: float) -> float:
        """Also applies the real delta to the actual quote balance (a fee
        revision is a genuine cash movement on a real exchange)."""
        for i, (t, vis) in enumerate(self._trades):
            if t.trade_id == trade_id:
                delta = new_fee - t.fee_cost
                revised = ObservedTrade(
                    trade_id=t.trade_id, order_id=t.order_id, symbol=t.symbol, side=t.side,
                    price=t.price, amount=t.amount, cost=t.cost, fee_cost=new_fee,
                    fee_currency=t.fee_currency, exchange_timestamp=t.exchange_timestamp, source=t.source,
                )
                self._trades[i] = (revised, vis)
                if delta != 0.0:
                    self._apply(t.fee_currency, -delta)
                return delta
        raise KeyError(trade_id)

    # -- ExchangeAdapter protocol ------------------------------------------

    def fetch_my_trades_page(self, symbol, *, since, offset: int, limit: int) -> TradePage:
        since_ms = ms(since) if since else None
        matching = [t for t, vis in self._trades if vis and (since_ms is None or ms(t.exchange_timestamp) > since_ms)]
        matching.sort(key=lambda t: (ms(t.exchange_timestamp), t.trade_id))
        total = len(matching)
        page = matching[offset:offset + limit]
        next_offset = offset + limit if offset + limit < total else None
        if symbol is not None:
            page = [t for t in page if t.symbol == symbol]
        return TradePage(trades=page, reported_total=total, next_offset=next_offset)

    def fetch_balance_total(self, asset: str) -> float:
        return self._balances.get(asset, 0.0)

    def fetch_deposits(self, asset: str, *, since) -> "list[LedgerMovement]":
        since_ms = ms(since) if since else None
        return [
            LedgerMovement(d.entry_id, "deposit", d.asset, d.amount, iso(d.timestamp_ms))
            for d in self._deposits
            if d.visible and d.asset == asset and (since_ms is None or d.timestamp_ms > since_ms)
        ]

    def fetch_withdrawals(self, asset: str, *, since) -> "list[LedgerMovement]":
        since_ms = ms(since) if since else None
        return [
            LedgerMovement(d.entry_id, "withdrawal", d.asset, d.amount, iso(d.timestamp_ms))
            for d in self._withdrawals
            if d.visible and d.asset == asset and (since_ms is None or d.timestamp_ms > since_ms)
        ]

    def amount_tolerance(self, symbol: str) -> float:
        return self._amount_tol

    def cash_tolerance(self, quote: str) -> float:
        return self._cash_tol
