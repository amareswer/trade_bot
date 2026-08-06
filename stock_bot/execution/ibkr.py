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

from stock_bot.data.price_feed import get_sector, get_usd_cad_rate
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

# A manual IBKR paper-account reset (or any other external deposit/withdrawal)
# changes NetLiquidation outside of anything this executor traded — at cost
# basis, a BUY just moves cash into inventory at no gain/loss, so absent an
# external change, net_liq should always equal starting_cash + realized_pnl
# to within normal unrealized mark-to-market drift. A gap bigger than this
# means something external happened; see _rebaseline_if_external_change().
_REBASELINE_ABS_MIN_CAD = 50.0
_REBASELINE_PCT_OF_STARTING = 0.02


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
        self._last_cash: float = 0.0   # last good live-cash reading, persisted for offline readers
        self._daily_loss_limit_pct: float = 0.03
        self._daily_loss_tripped: bool = False
        self._slippage_bps: int = 0        # real broker — kept only for interface parity

        # Weekly loss / drawdown-from-peak breaker tiers (overridden by config
        # via set_weekly_loss_limit / set_drawdown_limits). Unlike the daily
        # breaker (connection-lifetime only), peak_equity/week_open_equity/
        # kill_switch_tripped are persisted — a restart must not silently
        # reset the all-time peak or un-trip the kill switch.
        self._weekly_loss_limit_pct: float = 0.05
        self._drawdown_warning_pct: float = 0.10
        self._drawdown_halt_pct: float = 0.15
        self._kill_switch_pct: float = 0.20
        self._peak_equity: float = 0.0
        self._week_open_equity: float | None = None
        self._week_start_iso: str | None = None
        self._kill_switch_tripped: bool = False

        # Per-position stop-loss % override (ATR-based sizing, opt-in via
        # PAPER_ATR_SIZING_ENABLED). Symbols not present here use the flat
        # cfg.paper_stop_loss_pct baseline — see get_position_stop_pct().
        self._position_stop_pct: dict[str, float] = {}
        self._state_lock = threading.Lock()
        self._reconnect_lock = threading.Lock()

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
            # First-ever connection with this state file: seed the permanent
            # baseline from the live account, exactly once.
            self._starting_cash = net_liq
            self.save_state()
        elif net_liq > 0:
            self._rebaseline_if_external_change(net_liq)
        self._session_start_value = net_liq
        if net_liq > 0:
            self._update_breaker_marks(net_liq)

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

    def try_reconnect(self) -> bool:
        """Re-establish the TWS API socket if it is down. Never raises.

        ib_async does not redial on its own after TWS drops the socket
        (nightly auto-logoff, weekend maintenance), and _ensure_connected_async
        otherwise runs only at order placement — so a TWS that came back
        would go undetected (no "restored" notice, TWS heartbeat stays red)
        until the next order. The TWS monitor thread calls this while the
        connection is down. Returns the resulting connection state; a probe
        already in flight from another thread returns False immediately.
        """
        if not self._reconnect_lock.acquire(blocking=False):
            return False
        try:
            self._call(self._ensure_connected_async(),
                       timeout=self._connect_timeout_s + 5)
            return True
        except Exception as exc:
            logger.debug("TWS reconnect probe failed: %s", exc)
            return False
        finally:
            self._reconnect_lock.release()

    # ── contract mapping ─────────────────────────────────────────────────────

    # Canadian companies that also trade on NYSE under the SAME bare ticker
    # as their TSX primary listing (RY.TO/RY, TD.TO/TD, etc). Without an
    # explicit primaryExchange, IBKR's SMART/USD qualification resolves the
    # ambiguous symbol back to the TSX/CAD primary contract — exactly the
    # listing our API access is blocked from (CIRO DMR 3200 A.1.(b)(i), see
    # 2026-07-17 Error 201 incident). Force NYSE so the USD contract wins.
    _NYSE_CROSS_LISTED = {"RY", "TD", "BNS", "CM", "SU"}

    # IBKR refuses to buy a non-base-currency security below this account
    # equity (CAD) — it treats the purchase as an implicit margin/currency
    # trade (Error 201: "MINIMUM OF 2500 CAD ... REQUIRED ... TO ... TRADE
    # CURRENCY"). Discovered 2026-07-20: CM's first live rule BUY (account
    # equity ~$995 CAD, all ten RULE_WHITELIST symbols USD-denominated) hit
    # this wall — every future USD BUY would have repeated the same rejection
    # cycle. Checked proactively so a doomed order never reaches IBKR.
    _MIN_EQUITY_FOR_FX_TRADE_CAD = 2500.0

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
        elif (trade.orderStatus.status in ("Cancelled", "ApiCancelled")
                and not trade.fills and float(trade.orderStatus.filled or 0.0) <= 0):
            # IBKR can report an unprompted, transient 'Cancelled' status (e.g.
            # Error 10349 "Order TIF was set to DAY based on order preset")
            # before silently resubmitting the same order, which then fills
            # normally a moment later (RY, 2026-07-31: order read 'Cancelled'
            # with zero fill, logged/alerted as rejected, then filled 4sh
            # @ $210.55 ~700ms afterward with nothing left recording it).
            # Don't trust an unfilled 'Cancelled' at face value — give it a
            # short grace window to reveal whether it's actually still alive.
            grace_deadline = self._loop.time() + 5.0
            while (trade.orderStatus.status in ("Cancelled", "ApiCancelled")
                    and not trade.fills
                    and float(trade.orderStatus.filled or 0.0) <= 0
                    and self._loop.time() < grace_deadline):
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

        # One live net-liq fetch, reused across all three drawdown-based tiers
        # below (and to refresh the peak/week-open marks) instead of one
        # IB round-trip per tier.
        _net_liq_now = self._net_liquidation()
        if _net_liq_now > 0:
            self._update_breaker_marks(_net_liq_now)

        if self._is_kill_switch_tripped(_net_liq_now):
            return self._reject(
                sym, OrderSide.BUY, int(shares) if isinstance(shares, (int, float)) else 0, price,
                f"KILL SWITCH active: {self._drawdown_from_peak_pct(_net_liq_now):.1%} drawdown "
                f"from peak — all new BUYs blocked until manually cleared",
            )

        if self._is_drawdown_halted(_net_liq_now):
            return self._reject(
                sym, OrderSide.BUY, int(shares) if isinstance(shares, (int, float)) else 0, price,
                f"Drawdown halt ({self._drawdown_halt_pct:.0%}) reached: "
                f"{self._drawdown_from_peak_pct(_net_liq_now):.1%} down from peak — no new buys until recovery",
            )

        if self._is_weekly_loss_tripped(_net_liq_now):
            return self._reject(
                sym, OrderSide.BUY, int(shares) if isinstance(shares, (int, float)) else 0, price,
                f"Weekly loss limit ({self._weekly_loss_limit_pct:.0%}) reached — no new buys this week",
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

        contract_currency = self.to_contract(sym).currency
        if contract_currency != "CAD" and self.cash < self._MIN_EQUITY_FOR_FX_TRADE_CAD:
            return self._reject(
                sym, OrderSide.BUY, shares, price,
                f"Account equity ${self.cash:,.2f} CAD is below IBKR's "
                f"${self._MIN_EQUITY_FOR_FX_TRADE_CAD:,.0f} CAD minimum required to buy "
                f"a {contract_currency}-denominated security (IBKR Error 201 — "
                "margin/currency-trade minimum)",
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

        if filled_qty >= held_shares - 1e-9 and sym in self._position_stop_pct:   # full close
            self._position_stop_pct.pop(sym, None)
            self.save_state()

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

    def _price_in_cad(self, sym: str, price: float) -> float:
        """self.cash is CAD (base currency) — convert a USD-listed symbol's
        native price before mixing it into a CAD total. Non-CAD contracts
        route as USD in this codebase (see to_contract / _NYSE_CROSS_LISTED)."""
        if self.to_contract(sym).currency == "CAD":
            return price
        return price * get_usd_cad_rate()

    def total_value(self, prices: dict[str, float]) -> float:
        pos_value = sum(
            self._price_in_cad(sym, prices.get(sym, prices.get(sym.lower(), cost))) * shares
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

    def set_weekly_loss_limit(self, pct: float) -> None:
        """Configure the weekly loss circuit breaker (fraction, e.g. 0.05 = 5%)."""
        self._weekly_loss_limit_pct = pct

    def set_drawdown_limits(self, warning_pct: float, halt_pct: float, kill_switch_pct: float) -> None:
        """Configure the three drawdown-from-peak tiers (fractions, increasing severity)."""
        self._drawdown_warning_pct = warning_pct
        self._drawdown_halt_pct = halt_pct
        self._kill_switch_pct = kill_switch_pct

    def refresh_position_marks(self, prices: dict[str, float]) -> None:
        """No-op — _is_daily_loss_tripped() always marks live via
        _net_liquidation(). Kept for interface parity with StockPaperExecutor."""
        pass

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

    # ── Weekly loss / drawdown-from-peak breaker tiers ─────────────────────
    # current_value is optional so callers that already fetched net_liq this
    # cycle (buy() does, once) can reuse it instead of paying another IB
    # round-trip per tier; omit it to fetch fresh (e.g. from tests).

    @staticmethod
    def _current_week_iso() -> str:
        year, week, _ = datetime.now(timezone.utc).isocalendar()
        return f"{year}-W{week:02d}"

    def _update_breaker_marks(self, current_value: float) -> None:
        """Update all-time peak equity and the week-open reference point from
        a live net-liquidation value. Persisted immediately on change so a
        restart can't silently reset either baseline."""
        if current_value <= 0:
            return
        if current_value > self._peak_equity:
            self._peak_equity = current_value
            self.save_state()
        this_week = self._current_week_iso()
        if self._week_start_iso != this_week:
            self._week_start_iso = this_week
            self._week_open_equity = current_value
            logger.info("New trading week — weekly-loss baseline reset | week_open=$%.2f", current_value)
            self.save_state()
        elif self._week_open_equity is None:
            self._week_open_equity = current_value
            self.save_state()

    def _is_weekly_loss_tripped(self, current_value: float | None = None) -> bool:
        """Blocks new BUYs only (mirrors daily loss). Auto-recovers if equity
        climbs back above the week-open threshold — resets fresh next week regardless."""
        if not self._week_open_equity or self._week_open_equity <= 0:
            return False
        current = current_value if current_value is not None else self._net_liquidation()
        if current <= 0:
            return False
        loss_pct = (self._week_open_equity - current) / self._week_open_equity
        return loss_pct >= self._weekly_loss_limit_pct

    def _drawdown_from_peak_pct(self, current_value: float | None = None) -> float:
        if self._peak_equity <= 0:
            return 0.0
        current = current_value if current_value is not None else self._net_liquidation()
        if current <= 0:
            return 0.0
        return max(0.0, (self._peak_equity - current) / self._peak_equity)

    def _is_drawdown_halted(self, current_value: float | None = None) -> bool:
        """Blocks new BUYs only. Not sticky — auto-lifts as soon as the
        drawdown recovers below the halt threshold (unlike the kill switch)."""
        return self._drawdown_from_peak_pct(current_value) >= self._drawdown_halt_pct

    def _is_kill_switch_tripped(self, current_value: float | None = None) -> bool:
        """Blocks new BUYs only — SELL/exits are never blocked by any breaker
        tier. Sticky: once tripped it stays tripped (persisted to disk) until
        someone manually clears kill_switch_tripped in ibkr_state.json — a
        20% all-time drawdown should force a human decision, not self-heal."""
        if self._kill_switch_tripped:
            return True
        dd = self._drawdown_from_peak_pct(current_value)
        if dd >= self._kill_switch_pct:
            self._kill_switch_tripped = True
            logger.error(
                "KILL SWITCH TRIPPED: %.1f%% drawdown from peak $%.2f — "
                "all new BUYs blocked until manually cleared in ibkr_state.json",
                dd * 100, self._peak_equity,
            )
            self.save_state()
            return True
        return False

    def drawdown_status(self) -> dict:
        """Public snapshot for the non-blocking warning-tier alert, which is
        sent from stock_bot/main.py (the executor doesn't own alert delivery)."""
        current = self._net_liquidation()
        dd = self._drawdown_from_peak_pct(current)
        return {
            "peak_equity":    self._peak_equity,
            "current_equity": current,
            "drawdown_pct":   dd,
            "warning":        dd >= self._drawdown_warning_pct,
        }

    # ── Per-position ATR stop-loss override (opt-in ATR sizing) ────────────

    def set_position_stop_pct(self, symbol: str, pct: float) -> None:
        """Record a per-position stop-loss % that overrides the flat
        cfg.paper_stop_loss_pct baseline for this symbol's open position.
        Call once, right after a BUY fill, when PAPER_ATR_SIZING_ENABLED."""
        self._position_stop_pct[symbol.upper()] = pct
        self.save_state()

    def get_position_stop_pct(self, symbol: str, default: float) -> float:
        """Effective stop-loss % for this symbol's open position — the ATR
        override if one was set at entry, else the flat baseline."""
        return self._position_stop_pct.get(symbol.upper(), default)

    def get_sector_exposure(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sym in self.positions_snapshot():
            sector = get_sector(sym)
            counts[sector] = counts.get(sector, 0) + 1
        return counts

    def check_exposure(self, price_map: dict[str, float], pending_trade_value: float = 0.0) -> bool:
        """
        Return True if PROJECTED position value — current + pending_trade_value
        (a candidate BUY's approximate dollar size, CAD) — stays under the max
        exposure threshold. pending_trade_value defaults to 0.0 (current-state-
        only, the old behavior) — see StockPaperExecutor.check_exposure for the
        full rationale (both executors implement this identically).
        """
        total = self.total_value(price_map)
        if total <= 0:
            return True
        snap = self.positions_snapshot()
        pos_val = sum(shares * self._price_in_cad(sym, price_map.get(sym, cost))
                      for sym, (shares, cost) in snap.items())
        return ((pos_val + pending_trade_value) / total) < self._max_exposure_pct

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

    def _rebaseline_if_external_change(self, net_liq: float) -> None:
        """
        Detect an external cash change (a manual paper-account reset, or any
        deposit/withdrawal) and re-baseline starting_cash to absorb it.

        At cost basis, a BUY just moves cash into inventory at no gain or
        loss — so with no external change, net_liq should track
        starting_cash + realized_pnl to within small unrealized
        mark-to-market drift on any open position. A gap larger than that
        can only come from something outside this executor's own trading,
        most commonly a manual "Paper Trading Account Reset" in the IBKR
        portal (discovered 2026-07-20: a reset landed while the bot kept
        running, and the frozen starting_cash required a manual JSON edit
        to reflect it — this makes that automatic).
        """
        expected = self._starting_cash + self._realized_pnl
        drift = net_liq - expected
        threshold = max(_REBASELINE_ABS_MIN_CAD,
                         _REBASELINE_PCT_OF_STARTING * self._starting_cash)
        if abs(drift) <= threshold:
            return
        old_starting = self._starting_cash
        self._starting_cash = net_liq - self._realized_pnl
        logger.warning(
            "IBKR net_liq $%.2f is $%.2f away from tracked P&L (expected ~$%.2f) "
            "— treating as an external deposit/reset and re-baselining "
            "starting_cash $%.2f → $%.2f",
            net_liq, drift, expected, old_starting, self._starting_cash,
        )
        self.save_state()

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
            self._last_cash = float(state.get("cash", 0.0) or 0.0)
            self._peak_equity          = float(state.get("peak_equity", 0.0) or 0.0)
            _week_open                 = state.get("week_open_equity")
            self._week_open_equity     = float(_week_open) if _week_open is not None else None
            self._week_start_iso       = state.get("week_start_iso")
            self._kill_switch_tripped  = bool(state.get("kill_switch_tripped", False))
            # Not filtered against live positions here (unlike paper.py) —
            # IBKR has no local position cache to check against at load time;
            # sell()'s full-close cleanup keeps this from accumulating stale
            # entries in practice, and a stale entry only affects sizing for
            # a symbol that would need to be re-bought from flat anyway.
            self._position_stop_pct    = {
                sym.upper(): float(pct)
                for sym, pct in (state.get("position_stop_pct") or {}).items()
            }
            logger.info(
                "IBKR state restored | realized_pnl=$%.2f | starting_cash=$%.2f",
                realized, starting,
            )
            return True
        except (KeyError, ValueError, json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load ibkr_state.json (%s) — starting fresh", exc)
            return False

    def save_state(self) -> None:
        # Refresh the persisted live-cash snapshot when TWS is reachable; keep
        # the last good value otherwise so a disconnected save can't write 0.
        try:
            if self.is_connected:
                live = self.cash
                if live > 0:
                    self._last_cash = live
        except Exception:
            pass
        state = {
            "account": getattr(self, "_account", ""),
            "cash": round(self._last_cash, 2),
            "realized_pnl": round(self._realized_pnl, 6),
            "starting_cash": round(self._starting_cash, 6),
            "peak_equity": round(self._peak_equity, 6),
            "week_open_equity": self._week_open_equity,
            "week_start_iso": self._week_start_iso,
            "kill_switch_tripped": self._kill_switch_tripped,
            "position_stop_pct": self._position_stop_pct,
            "last_updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            from bot.atomic_json import atomic_write_json
            atomic_write_json(_STATE_JSON, state)
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
