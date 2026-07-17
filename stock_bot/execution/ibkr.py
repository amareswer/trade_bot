"""
IBKR executor — roadmap item D (paper mode first, live gate-blocked).

Implements StockExecutorBase against Interactive Brokers TWS / IB Gateway
via ib_async (maintained successor of ib_insync — same API).  Drop-in for
StockPaperExecutor: implements the same extra methods main.py calls
(check_exposure, build_paper_summary, log_state, save_state,
set_daily_loss_limit, set_slippage_bps, starting_cash) and the same
pre-trade sanity rejections, so risk behavior is identical — only the
fill source changes.

Threading model
---------------
The stock bot is synchronous and calls the executor from TWO threads (the
scan loop and the SL/TP watcher); ib_async is asyncio-based and not
thread-safe.  The executor therefore owns a dedicated daemon thread running
a private event loop where the single IB connection lives.  Every public
method submits a coroutine to that loop via run_coroutine_threadsafe and
blocks on the result with a timeout — safe from any caller thread.

Safety guards
-------------
- Port 7496 (TWS live) refuses to start unless allow_live=True is passed
  explicitly (env IBKR_ALLOW_LIVE=true).  Default port is 7497 (paper).
- The connected account must start with "DU" (IBKR paper prefix) unless
  allow_live — belt and suspenders against a mis-toggled TWS login.
- Market orders wait for a fill deadline; on timeout the order is
  cancelled and THEN re-checked for a fill that raced the cancel — the
  fill is recorded, never lost (2026-07-15 crypto limit-chase lesson).

State
-----
IBKR is the source of truth for cash / positions / avg_cost.  Realized
P&L and the trade log are tracked locally (ibkr_state.json +
ibkr_trades.csv, same frozen 9-column schema as paper_trades.csv).
starting_cash = first NetLiquidation ever seen, persisted.
"""
from __future__ import annotations

import sys as _sys
import os as _os
_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _PROJECT_ROOT not in _sys.path:
    _sys.path.insert(0, _PROJECT_ROOT)

import asyncio
import csv
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from stock_bot.data.price_feed import get_sector
from stock_bot.execution.base import (
    OrderSide, OrderStatus, StockExecutorBase, StockOrder,
)
from stock_bot.portfolio.tracker import (
    PaperSummary, PaperTrade, PortfolioPosition,
)

logger = logging.getLogger(__name__)

_STOCK_BOT_DIR = os.path.dirname(os.path.dirname(__file__))  # stock_bot/
_MAX_PER_SECTOR = 2   # max open positions in any single sector (matches paper)
_TRADES_CSV = os.path.join(_STOCK_BOT_DIR, "ibkr_trades.csv")
_STATE_JSON = os.path.join(_STOCK_BOT_DIR, "ibkr_state.json")

_CSV_HEADER = [
    "timestamp", "symbol", "side", "shares",
    "price", "total_value", "cash_remaining", "reason", "confidence",
]

_LIVE_PORTS = {7496, 4001}   # TWS live, IB Gateway live
_PAPER_ACCOUNT_PREFIX = "DU"


def _default_ib_factory():
    from ib_async import IB
    return IB()


class IBKRExecutor(StockExecutorBase):
    """
    Interactive Brokers executor (paper by default, port 7497).

    Requires a running, logged-in TWS or IB Gateway on `host:port`.
    Raises ConnectionError from __init__ if the connection fails — the
    bot must not start half-connected.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 7,
        allow_live: bool = False,
        max_exposure_pct: float = 0.25,
        fill_timeout_s: float = 60.0,
        connect_timeout_s: float = 15.0,
        ib_factory: Callable[[], Any] | None = None,
    ) -> None:
        if port in _LIVE_PORTS and not allow_live:
            raise ValueError(
                f"Port {port} is a LIVE trading port. IBKRExecutor refuses to "
                f"start without allow_live=True (env IBKR_ALLOW_LIVE=true)."
            )

        self._host = host
        self._port = port
        self._client_id = client_id
        self._allow_live = allow_live
        self._max_exposure_pct = max_exposure_pct
        self._fill_timeout_s = fill_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._ib_factory = ib_factory or _default_ib_factory

        self._orders: list[StockOrder] = []
        self._trade_log: list[PaperTrade] = []
        self._realized_pnl: float = 0.0
        self._starting_cash: float = 0.0
        self._daily_loss_limit_pct: float = 0.03
        self._daily_loss_tripped: bool = False
        self._slippage_bps: int = 0        # real broker — kept only for interface parity
        self._state_lock = threading.Lock()

        # ── dedicated event-loop thread ──────────────────────────────────────
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="ibkr-loop",
        )
        self._thread.start()

        self._ib = None
        try:
            self._ib = self._call(self._connect_async(), timeout=connect_timeout_s + 10)
        except Exception as exc:
            self._shutdown_loop()
            raise ConnectionError(
                f"Could not connect to TWS/Gateway at {host}:{port} — is it "
                f"running and logged in with API enabled? ({exc})"
            ) from exc

        accounts = self._call(self._managed_accounts_async(), timeout=10)
        self._account = accounts[0] if accounts else ""
        if not self._allow_live and not self._account.startswith(_PAPER_ACCOUNT_PREFIX):
            self.disconnect()
            raise ValueError(
                f"Connected account '{self._account}' is not a paper account "
                f"(expected '{_PAPER_ACCOUNT_PREFIX}*'). TWS may be logged into "
                f"LIVE mode — refusing to trade."
            )

        self._load_state()
        net_liq = self._net_liquidation()
        if self._starting_cash <= 0 and net_liq > 0:
            self._starting_cash = net_liq
            self.save_state()
        self._session_start_value = net_liq

        self._ensure_csv_header()
        logger.info(
            "IBKRExecutor connected | account=%s (%s) | net_liq=$%.2f | "
            "cash=$%.2f | positions=%s",
            self._account,
            "PAPER" if self._account.startswith(_PAPER_ACCOUNT_PREFIX) else "LIVE",
            net_liq, self.cash, list(self.positions_snapshot().keys()),
        )
        print(f"  IBKR executor: account {self._account} "
              f"({'PAPER' if self._account.startswith(_PAPER_ACCOUNT_PREFIX) else 'LIVE'}) "
              f"net_liq=${net_liq:,.2f}")

    # ── event-loop plumbing ──────────────────────────────────────────────────

    def _call(self, coro, timeout: float):
        """Run a coroutine on the executor's private loop from any thread."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    def _shutdown_loop(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    async def _connect_async(self):
        ib = self._ib_factory()
        await ib.connectAsync(
            self._host, self._port, clientId=self._client_id,
            timeout=self._connect_timeout_s,
        )
        return ib

    async def _managed_accounts_async(self) -> list[str]:
        return list(self._ib.managedAccounts())

    async def _ensure_connected_async(self) -> None:
        if self._ib.isConnected():
            return
        logger.warning("IBKR connection lost — attempting reconnect")
        await self._ib.connectAsync(
            self._host, self._port, clientId=self._client_id,
            timeout=self._connect_timeout_s,
        )
        logger.info("IBKR reconnected")

    def disconnect(self) -> None:
        """Disconnect from TWS and stop the private event loop."""
        try:
            if self._ib is not None:
                async def _dc():
                    self._ib.disconnect()
                self._call(_dc(), timeout=10)
        except Exception as exc:
            logger.warning("IBKR disconnect error: %s", exc)
        finally:
            self._shutdown_loop()

    @property
    def is_connected(self) -> bool:
        try:
            async def _chk():
                return self._ib.isConnected()
            return bool(self._call(_chk(), timeout=5))
        except Exception:
            return False

    # ── contract mapping ─────────────────────────────────────────────────────

    # Canadian companies that also trade on NYSE under the SAME bare ticker
    # as their TSX primary listing (RY.TO/RY, TD.TO/TD, etc). Without an
    # explicit primaryExchange, IBKR's SMART/USD qualification resolves the
    # ambiguous symbol back to the TSX/CAD primary contract — exactly the
    # listing our API access is blocked from (CIRO DMR 3200 A.1.(b)(i), see
    # 2026-07-17 Error 201 incident). Force NYSE so the USD contract wins.
    _NYSE_CROSS_LISTED = {"RY", "TD", "BNS", "CM", "SU"}

    @staticmethod
    def to_contract(symbol: str):
        """
        yfinance symbol → IBKR Stock contract.
        'RY.TO'  → Stock('RY',  SMART, CAD, primaryExchange=TSE)
        'TECK-B.TO' → Stock('TECK.B', SMART, CAD, primaryExchange=TSE)
        'BRK-B'  → Stock('BRK B', SMART, USD)
        'RY'     → Stock('RY', SMART, USD, primaryExchange=NYSE)  (cross-listed)
        """
        from ib_async import Stock
        sym = symbol.upper()
        if sym.endswith(".TO"):
            return Stock(sym[:-3].replace("-", "."), "SMART", "CAD",
                         primaryExchange="TSE")
        base = sym.replace("-", " ")
        if base in IBKRExecutor._NYSE_CROSS_LISTED:
            return Stock(base, "SMART", "USD", primaryExchange="NYSE")
        return Stock(base, "SMART", "USD")

    @staticmethod
    def from_contract(contract) -> str:
        """IBKR contract → yfinance symbol (inverse of to_contract)."""
        if contract.currency == "CAD":
            return contract.symbol.replace(".", "-") + ".TO"
        return contract.symbol.replace(" ", "-")

    # ── order execution ──────────────────────────────────────────────────────

    async def _place_market_async(
        self, symbol: str, action: str, qty: int,
    ) -> tuple[str, float, float]:
        """
        Place a market order and wait for a terminal state.
        Returns (status, filled_qty, avg_fill_price).
        """
        from ib_async import MarketOrder

        await self._ensure_connected_async()

        contract = self.to_contract(symbol)
        qualified = await self._ib.qualifyContractsAsync(contract)
        if not qualified:
            raise RuntimeError(f"IBKR could not qualify contract for {symbol}")

        order = MarketOrder(action, qty)
        trade = self._ib.placeOrder(qualified[0], order)

        deadline = self._loop.time() + self._fill_timeout_s
        while not trade.isDone() and self._loop.time() < deadline:
            await asyncio.sleep(0.25)

        if not trade.isDone():
            # Timeout: cancel, then wait for the order's actual fate — a fill
            # can race the cancel and must be recorded, never dropped.
            logger.warning(
                "IBKR %s %s ×%d not done after %.0fs — cancelling",
                action, symbol, qty, self._fill_timeout_s,
            )
            self._ib.cancelOrder(order)
            cancel_deadline = self._loop.time() + 15.0
            while not trade.isDone() and self._loop.time() < cancel_deadline:
                await asyncio.sleep(0.25)

        status = trade.orderStatus.status
        filled_qty = float(trade.orderStatus.filled or 0.0)
        avg_px = float(trade.orderStatus.avgFillPrice or 0.0)
        if filled_qty <= 0 and trade.fills:
            filled_qty = sum(f.execution.shares for f in trade.fills)
            total = sum(f.execution.shares * f.execution.price for f in trade.fills)
            avg_px = total / filled_qty if filled_qty else 0.0
        return status, filled_qty, avg_px

    def _execute(self, symbol: str, side: OrderSide, shares: int) -> tuple[float, float]:
        """
        Blocking wrapper: place market order, return (filled_qty, avg_price).
        Raises RuntimeError when nothing filled.
        """
        status, filled_qty, avg_px = self._call(
            self._place_market_async(symbol, side.value, shares),
            timeout=self._fill_timeout_s + self._connect_timeout_s + 30,
        )
        if filled_qty <= 0 or avg_px <= 0:
            raise RuntimeError(f"order ended '{status}' with no fill")
        if filled_qty < shares:
            logger.warning(
                "IBKR PARTIAL FILL %s %s: %d of %d shares — recording actual",
                side.value, symbol, int(filled_qty), shares,
            )
        return filled_qty, avg_px

    # ── core trading operations (StockExecutorBase) ──────────────────────────

    def buy(
        self,
        symbol: str,
        shares: float,
        price: float,
        reason: str = "",
        confidence: int = 0,
        candle_close: float | None = None,
        live_price: float | None = None,
    ) -> StockOrder:
        sym = symbol.upper()

        # Same pre-trade sanity gates as StockPaperExecutor — risk behavior
        # must not loosen just because the broker is real.
        if candle_close is not None and live_price is not None:
            deviation = abs(candle_close - live_price) / max(live_price, 0.01)
            if deviation > 0.10:
                return self._reject(
                    sym, OrderSide.BUY, int(shares), price,
                    f"Candle close ${candle_close:.2f} deviates {deviation * 100:.1f}% "
                    f"from live price ${live_price:.2f} — corrupted data",
                )

        if self._is_daily_loss_tripped():
            return self._reject(
                sym, OrderSide.BUY, int(shares) if isinstance(shares, (int, float)) else 0, price,
                f"Daily loss limit ({self._daily_loss_limit_pct:.0%}) reached — no new buys today",
            )

        if not isinstance(price, (int, float)):
            return self._reject(sym, OrderSide.BUY, 0, 0.0,
                                f"Invalid price type: {type(price)}")
        if price <= 0 or price > 500_000:
            return self._reject(sym, OrderSide.BUY, int(shares), price,
                                f"Invalid price: {price}")
        if price < 1.00:
            return self._reject(
                sym, OrderSide.BUY, int(shares), price,
                f"Price ${price:.4f} below $1.00 minimum — possible corrupted data",
            )

        shares = int(shares)
        if shares > 100_000:
            return self._reject(
                sym, OrderSide.BUY, shares, price,
                f"Share count unrealistic: {shares} — price data may be corrupted",
            )
        if shares < 1:
            return self._reject(
                sym, OrderSide.BUY, shares, price,
                f"Insufficient cash for 1 share @ ${price:.2f}",
            )

        if sym not in self.positions_snapshot():
            sector = get_sector(sym)
            counts: dict[str, int] = {}
            for held in self.positions_snapshot():
                s = get_sector(held)
                counts[s] = counts.get(s, 0) + 1
            if counts.get(sector, 0) >= _MAX_PER_SECTOR:
                return self._reject(
                    sym, OrderSide.BUY, shares, price,
                    f"Sector limit: already {_MAX_PER_SECTOR} open positions in '{sector}'",
                )

        est_cost = shares * price
        if est_cost > self.cash + 1e-9:
            return self._reject(
                sym, OrderSide.BUY, shares, price,
                f"Insufficient cash: have ${self.cash:,.2f}, need ${est_cost:,.2f}",
            )

        order = self._new_order(sym, OrderSide.BUY, shares, price)
        try:
            filled_qty, fill_px = self._execute(sym, OrderSide.BUY, shares)
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = f"IBKR order failed: {exc}"
            logger.error("IBKR BUY REJECTED  %s × %d — %s", sym, shares, exc)
            self._orders.append(order)
            return order

        order.quantity = filled_qty
        order.price = fill_px
        order.total_value = round(abs(fill_px * filled_qty), 2)
        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now(timezone.utc)
        self._orders.append(order)

        self._record_trade("BUY", sym, filled_qty, fill_px, reason, confidence)
        logger.info(
            "IBKR BUY FILLED    %s  %d shares @ $%.2f  cash=$%.2f",
            sym, int(filled_qty), fill_px, self.cash,
        )
        return order

    def sell(self, symbol: str, shares: float, price: float, reason: str = "") -> StockOrder:
        sym = symbol.upper()
        held_shares, held_cost = self.positions_snapshot().get(sym, (0.0, 0.0))

        if shares > held_shares + 1e-9:
            return self._reject(
                sym, OrderSide.SELL, shares, price,
                f"Insufficient position: have {held_shares:.4f} shares, need {shares:.4f}",
            )

        shares = int(shares)
        order = self._new_order(sym, OrderSide.SELL, shares, price)
        try:
            filled_qty, fill_px = self._execute(sym, OrderSide.SELL, shares)
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = f"IBKR order failed: {exc}"
            logger.error("IBKR SELL REJECTED %s × %d — %s", sym, shares, exc)
            self._orders.append(order)
            return order

        pnl = round((fill_px - held_cost) * filled_qty, 2)
        with self._state_lock:
            self._realized_pnl += pnl

        order.quantity = filled_qty
        order.price = fill_px
        order.total_value = round(abs(fill_px * filled_qty), 2)
        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now(timezone.utc)
        self._orders.append(order)

        self._record_trade("SELL", sym, filled_qty, fill_px, reason)
        logger.info(
            "IBKR SELL FILLED   %s  %d shares @ $%.2f  trade_pnl=$%.2f  "
            "total_realized=$%.2f  cash=$%.2f",
            sym, int(filled_qty), fill_px, pnl, self._realized_pnl, self.cash,
        )
        return order

    # ── portfolio state (queried from IBKR) ──────────────────────────────────

    def _account_value(self, *tags: str) -> float:
        """First matching account value (base currency preferred)."""
        try:
            async def _vals():
                return list(self._ib.accountValues())
            rows = self._call(_vals(), timeout=10)
        except Exception as exc:
            logger.warning("IBKR account values unavailable: %s", exc)
            return 0.0
        for tag in tags:
            base_row = None
            for r in rows:
                if r.tag == tag and r.currency in ("BASE", "CAD", ""):
                    base_row = r
                    break
            if base_row is None:
                for r in rows:
                    if r.tag == tag:
                        base_row = r
                        break
            if base_row is not None:
                try:
                    return float(base_row.value)
                except (TypeError, ValueError):
                    continue
        return 0.0

    def _net_liquidation(self) -> float:
        return self._account_value("NetLiquidation", "NetLiquidationByCurrency")

    @property
    def cash(self) -> float:
        return self._account_value("TotalCashValue", "TotalCashBalance")

    @property
    def starting_cash(self) -> float:
        return self._starting_cash

    def position(self, symbol: str) -> float:
        return self.positions_snapshot().get(symbol.upper(), (0.0, 0.0))[0]

    def avg_cost(self, symbol: str) -> float:
        return self.positions_snapshot().get(symbol.upper(), (0.0, 0.0))[1]

    def realized_pnl(self) -> float:
        return self._realized_pnl

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, (shares, cost) in self.positions_snapshot().items():
            px = prices.get(sym, prices.get(sym.lower(), cost))
            total += (px - cost) * shares
        return round(total, 2)

    def total_value(self, prices: dict[str, float]) -> float:
        pos_value = sum(
            prices.get(sym, prices.get(sym.lower(), cost)) * shares
            for sym, (shares, cost) in self.positions_snapshot().items()
        )
        return round(self.cash + pos_value, 2)

    def positions_snapshot(self) -> dict[str, tuple[float, float]]:
        try:
            async def _pos():
                return list(self._ib.positions())
            rows = self._call(_pos(), timeout=10)
        except Exception as exc:
            logger.warning("IBKR positions unavailable: %s", exc)
            return {}
        snapshot: dict[str, tuple[float, float]] = {}
        for p in rows:
            if p.position == 0:
                continue
            sym = self.from_contract(p.contract)
            # IBKR avgCost is per share and includes commission
            snapshot[sym] = (float(p.position), round(float(p.avgCost), 6))
        return snapshot

    # ── order history ────────────────────────────────────────────────────────

    def all_orders(self) -> list[StockOrder]:
        return list(self._orders)

    def filled_orders(self) -> list[StockOrder]:
        return [o for o in self._orders if o.status == OrderStatus.FILLED]

    # ── interface parity with StockPaperExecutor ─────────────────────────────

    def set_slippage_bps(self, bps: int) -> None:
        """Real broker — slippage is real, not simulated. Kept for parity."""
        self._slippage_bps = max(0, bps)

    def set_daily_loss_limit(self, pct: float) -> None:
        self._daily_loss_limit_pct = pct

    def _is_daily_loss_tripped(self) -> bool:
        if self._daily_loss_tripped:
            return True
        if self._session_start_value <= 0:
            return False
        current = self._net_liquidation()
        if current <= 0:
            return False
        drawdown = (self._session_start_value - current) / self._session_start_value
        if drawdown >= self._daily_loss_limit_pct:
            self._daily_loss_tripped = True
            logger.warning(
                "IBKR daily loss limit hit: %.1f%% drawdown from session start "
                "$%.2f → current $%.2f",
                drawdown * 100, self._session_start_value, current,
            )
            return True
        return False

    def get_sector_exposure(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sym in self.positions_snapshot():
            sector = get_sector(sym)
            counts[sector] = counts.get(sector, 0) + 1
        return counts

    def check_exposure(self, price_map: dict[str, float]) -> bool:
        total = self.total_value(price_map)
        if total <= 0:
            return True
        snap = self.positions_snapshot()
        pos_val = sum(shares * price_map.get(sym, cost)
                      for sym, (shares, cost) in snap.items())
        return (pos_val / total) < self._max_exposure_pct

    def build_paper_summary(self, scan_results: list) -> PaperSummary:
        price_map    = {r.symbol.upper(): r.price    for r in scan_results}
        verdict_map  = {r.symbol.upper(): r.verdict  for r in scan_results}
        currency_map = {r.symbol.upper(): r.currency for r in scan_results}

        positions: list[PortfolioPosition] = []
        for sym, (shares, avg_cost) in self.positions_snapshot().items():
            current_price = price_map.get(sym, avg_cost)
            current_value = round(shares * current_price, 2)
            total_cost    = round(shares * avg_cost, 2)
            gain_loss     = round(current_value - total_cost, 2)
            gain_loss_pct = round((gain_loss / total_cost * 100) if total_cost else 0.0, 2)
            currency      = currency_map.get(sym, "CAD" if sym.endswith(".TO") else "USD")
            positions.append(PortfolioPosition(
                symbol        = sym,
                shares        = shares,
                avg_cost      = avg_cost,
                current_price = current_price,
                current_value = current_value,
                total_cost    = total_cost,
                gain_loss     = gain_loss,
                gain_loss_pct = gain_loss_pct,
                currency      = currency,
                verdict       = verdict_map.get(sym),
            ))

        unrealized  = round(sum(p.gain_loss for p in positions), 2)
        pos_mkt_val = sum(p.current_value for p in positions)
        cash        = self.cash
        total_val   = round(cash + pos_mkt_val, 2)
        recent      = list(reversed(self._trade_log[-10:]))

        return PaperSummary(
            cash           = cash,
            starting_cash  = self._starting_cash,
            positions      = positions,
            realized_pnl   = self._realized_pnl,
            unrealized_pnl = unrealized,
            total_value    = total_val,
            recent_trades  = recent,
        )

    def log_state(self, prices: dict[str, float] | None = None) -> None:
        prices = prices or {}
        logger.info(
            "IBKR PORTFOLIO | account=%s | cash=$%.2f | realized_pnl=$%.2f | "
            "unrealized_pnl=$%.2f | total_fills=%d | open_positions=%d",
            self._account,
            self.cash,
            self._realized_pnl,
            self.unrealized_pnl(prices),
            len(self.filled_orders()),
            len(self.positions_snapshot()),
        )

    # ── state persistence (local realized P&L only) ──────────────────────────

    def _load_state(self) -> bool:
        if not os.path.exists(_STATE_JSON):
            return False
        try:
            with open(_STATE_JSON, "r", encoding="utf-8") as f:
                state = json.load(f)
            realized = float(state.get("realized_pnl", 0.0))
            starting = float(state.get("starting_cash", 0.0))
            if abs(realized) > 1_000_000 or starting < 0 or starting > 10_000_000:
                logger.warning("ibkr_state.json looks corrupted — starting fresh")
                return False
            self._realized_pnl = realized
            self._starting_cash = starting
            logger.info(
                "IBKR state restored | realized_pnl=$%.2f | starting_cash=$%.2f",
                realized, starting,
            )
            return True
        except (KeyError, ValueError, json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load ibkr_state.json (%s) — starting fresh", exc)
            return False

    def save_state(self) -> None:
        state = {
            "account": getattr(self, "_account", ""),
            "realized_pnl": round(self._realized_pnl, 6),
            "starting_cash": round(self._starting_cash, 6),
            "last_updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            with open(_STATE_JSON, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except OSError as exc:
            logger.warning("Could not save ibkr_state.json: %s", exc)

    # ── CSV persistence (frozen 9-column schema) ─────────────────────────────

    def _ensure_csv_header(self) -> None:
        if not os.path.exists(_TRADES_CSV):
            try:
                with open(_TRADES_CSV, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(_CSV_HEADER)
                logger.info("Created ibkr_trades.csv at %s", _TRADES_CSV)
            except OSError as exc:
                logger.warning("Could not create ibkr_trades.csv: %s", exc)

    def _record_trade(
        self, side: str, sym: str, shares: float, fill_px: float,
        reason: str, confidence: int = 0,
    ) -> None:
        trade = PaperTrade(
            timestamp      = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            symbol         = sym,
            side           = side,
            shares         = shares,
            price          = fill_px,
            total_value    = round(shares * fill_px, 2),
            cash_remaining = self.cash,
            reason         = reason,
        )
        self._trade_log.append(trade)
        try:
            with open(_TRADES_CSV, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    trade.timestamp,
                    trade.symbol,
                    trade.side,
                    f"{trade.shares:.4f}",
                    f"{trade.price:.4f}",
                    f"{trade.total_value:.2f}",
                    f"{trade.cash_remaining:.2f}",
                    trade.reason,
                    confidence,
                ])
        except OSError as exc:
            logger.warning("Could not write to ibkr_trades.csv: %s", exc)
        self.save_state()

    # ── internal ─────────────────────────────────────────────────────────────

    def _reject(
        self, sym: str, side: OrderSide, shares: float, price: float, reason: str,
    ) -> StockOrder:
        order = self._new_order(sym, side, shares, price)
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        logger.warning("IBKR %s REJECTED  %s — %s", side.value, sym, reason)
        self._orders.append(order)
        return order

    @staticmethod
    def _new_order(
        symbol: str, side: OrderSide, shares: float, price: float,
    ) -> StockOrder:
        return StockOrder(
            order_id   = str(uuid.uuid4()),
            symbol     = symbol,
            side       = side,
            quantity   = shares,
            price      = price,
            status     = OrderStatus.PENDING,
            created_at = datetime.now(timezone.utc),
        )
