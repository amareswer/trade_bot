"""
Stock paper trading executor — Phase 6.

Simulates stock orders against a virtual cash balance.
Fills are instantaneous at the price provided by the scan cycle (market-order semantics).
Tracks multiple symbols, weighted average cost basis, realized + unrealized P&L.

Every fill is appended to stock_bot/paper_trades.csv (created on first trade, never overwritten).

No slippage, no commissions, no partial fills modelled — add those when
moving to a real broker by implementing StockExecutorBase.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from stock_bot.data.price_feed import get_sector
from stock_bot.execution.base import (
    OrderSide, OrderStatus, StockExecutorBase, StockOrder,
)
from stock_bot.portfolio.tracker import (
    PaperSummary, PaperTrade, PortfolioPosition,
)

logger = logging.getLogger(__name__)

_STOCK_BOT_DIR   = os.path.dirname(os.path.dirname(__file__))  # stock_bot/
_MAX_PER_SECTOR  = 2   # max open positions in any single sector
_TRADES_CSV    = os.path.join(_STOCK_BOT_DIR, "paper_trades.csv")
_STATE_JSON    = os.path.join(_STOCK_BOT_DIR, "paper_state.json")
_RESET_FLAG    = os.path.join(_STOCK_BOT_DIR, ".paper_reset")

_CSV_HEADER = [
    "timestamp", "symbol", "side", "shares",
    "price", "total_value", "cash_remaining", "reason",
]


class StockPaperExecutor(StockExecutorBase):
    """
    In-memory paper trading executor for the stock bot.

    State is held in RAM — resets on process restart.
    All prices come from yfinance data in the scan cycle (no extra API calls).
    """

    def __init__(self, starting_cash: float = 10_000.0) -> None:
        self._starting_cash: float                          = starting_cash
        self._orders:        list[StockOrder]               = []
        self._trade_log:     list[PaperTrade]               = []

        # Defaults — overwritten by _load_state() if a saved state exists
        self._cash:         float                           = starting_cash
        self._positions:    dict[str, tuple[float, float]]  = {}
        self._realized_pnl: float                           = 0.0
        self._session_start_value: float = starting_cash
        self._daily_loss_limit_pct: float = 0.03   # overridden by config
        self._daily_loss_tripped: bool = False
        self._slippage_bps: int = 15   # 0.15% — override via set_slippage_bps()

        if os.path.exists(_RESET_FLAG):
            os.remove(_RESET_FLAG)
            self._cash         = self._starting_cash
            self._positions    = {}
            self._realized_pnl = 0.0
            print("📄 Paper state RESET — starting fresh")
            logger.info("Paper state reset via flag file | cash=$%.2f", starting_cash)
        else:
            if not self._load_state():
                logger.info("StockPaperExecutor starting fresh | cash=$%.2f", starting_cash)
        # Session start value reflects the actual cash after any state restore,
        # so the daily loss circuit breaker measures drawdown from this session's
        # opening balance, not the original starting_cash constructor arg.
        self._session_start_value = self._cash

        self._ensure_csv_header()
        print("  Paper trading: whole shares only (no fractions)")

    # ── Core operations ───────────────────────────────────────────────────────

    def set_slippage_bps(self, bps: int) -> None:
        """Set simulated slippage in basis points (e.g. 15 = 0.15%)."""
        self._slippage_bps = max(0, bps)

    def _fill_price(self, price: float, side: str) -> float:
        """
        Apply one-way slippage to simulate realistic fills.
        BUY pays slightly more, SELL receives slightly less.
        """
        factor = self._slippage_bps / 10_000
        if side == "BUY":
            return round(price * (1.0 + factor), 4)
        return round(price * (1.0 - factor), 4)

    def set_daily_loss_limit(self, pct: float) -> None:
        """Configure the daily loss circuit breaker (fraction, e.g. 0.03 = 3%)."""
        self._daily_loss_limit_pct = pct

    def _is_daily_loss_tripped(self) -> bool:
        """
        Returns True if current cash drawdown from session start exceeds the limit.
        Once tripped, stays tripped for the rest of the session.
        """
        if self._daily_loss_tripped:
            return True
        if self._session_start_value <= 0:
            return False
        drawdown = (self._session_start_value - self._cash) / self._session_start_value
        if drawdown >= self._daily_loss_limit_pct:
            self._daily_loss_tripped = True
            logger.warning(
                "PAPER daily loss limit hit: %.1f%% drawdown from session start $%.2f → current $%.2f",
                drawdown * 100, self._session_start_value, self._cash,
            )
            return True
        return False

    def buy(self, symbol: str, shares: float, price: float, reason: str = "") -> StockOrder:
        sym = symbol.upper()

        if self._is_daily_loss_tripped():
            order = self._new_order(sym, OrderSide.BUY, shares, price)
            order.status        = OrderStatus.REJECTED
            order.reject_reason = f"Daily loss limit ({self._daily_loss_limit_pct:.0%}) reached — no new buys today"
            logger.warning("PAPER BUY BLOCKED  %s — daily loss circuit breaker active", sym)
            self._orders.append(order)
            return order

        if not isinstance(price, (int, float)):
            order = self._new_order(sym, OrderSide.BUY, 0, price)
            order.status        = OrderStatus.REJECTED
            order.reject_reason = f"Invalid price type: {type(price)}"
            logger.error("PAPER BUY REJECTED  %s — invalid price type %s", sym, type(price))
            self._orders.append(order)
            return order

        if price <= 0 or price > 500_000:
            order = self._new_order(sym, OrderSide.BUY, int(shares), price)
            order.status        = OrderStatus.REJECTED
            order.reject_reason = f"Invalid price: {price}"
            logger.error("PAPER BUY REJECTED  %s — invalid price %.8f", sym, price)
            self._orders.append(order)
            return order

        if price < 1.00:
            order = self._new_order(sym, OrderSide.BUY, int(shares), price)
            order.status        = OrderStatus.REJECTED
            order.reject_reason = f"Price ${price:.4f} below $1.00 minimum — possible corrupted data"
            logger.warning("PAPER BUY REJECTED  %s — price $%.4f below $1.00 minimum", sym, price)
            self._orders.append(order)
            return order

        shares = int(shares)  # stocks trade in whole shares only
        order  = self._new_order(sym, OrderSide.BUY, shares, price)

        # Sector concentration check — don't pile into the same industry
        if sym not in self._positions:   # only gate new positions, not add-ons
            sector = self._sector_of(sym)
            if self._sector_count().get(sector, 0) >= _MAX_PER_SECTOR:
                order.status        = OrderStatus.REJECTED
                order.reject_reason = (
                    f"Sector limit: already {_MAX_PER_SECTOR} open positions "
                    f"in '{sector}'"
                )
                logger.warning(
                    "PAPER BUY BLOCKED  %s — sector '%s' at limit %d",
                    sym, sector, _MAX_PER_SECTOR,
                )
                self._orders.append(order)
                return order

        if shares > 100_000:
            order.status        = OrderStatus.REJECTED
            order.reject_reason = f"Share count unrealistic: {shares} — price data may be corrupted"
            logger.error("PAPER BUY REJECTED  %s — share count %d unrealistic", sym, shares)
            self._orders.append(order)
            return order

        if shares < 1:
            order.status        = OrderStatus.REJECTED
            order.reject_reason = (
                f"Insufficient cash for 1 share "
                f"@ ${price:.2f} — need ${price:.2f} "
                f"have ${self._cash:.2f}"
            )
            logger.warning("PAPER BUY REJECTED  %s × %d @ $%.2f — %s",
                           sym, shares, price, order.reject_reason)
            self._orders.append(order)
            return order

        fill_px = self._fill_price(price, "BUY")
        cost  = round(shares * fill_px, 2)

        if cost > self._cash + 1e-9:
            order.status        = OrderStatus.REJECTED
            order.reject_reason = (
                f"Insufficient cash: have ${self._cash:,.2f}, need ${cost:,.2f}"
            )
            logger.warning("PAPER BUY REJECTED  %s × %d @ $%.2f — %s",
                           sym, shares, fill_px, order.reject_reason)
        else:
            held_shares, held_cost = self._positions.get(sym, (0.0, 0.0))
            new_shares = held_shares + shares
            new_cost   = (
                (held_shares * held_cost + shares * fill_px) / new_shares
                if new_shares > 0 else fill_px
            )
            self._positions[sym] = (new_shares, round(new_cost, 6))
            self._cash          -= cost
            order.status    = OrderStatus.FILLED
            order.filled_at = datetime.now(timezone.utc)

            trade = PaperTrade(
                timestamp      = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                symbol         = sym,
                side           = "BUY",
                shares         = shares,
                price          = fill_px,
                total_value    = cost,
                cash_remaining = self._cash,
                reason         = reason,
            )
            self._trade_log.append(trade)
            self._log_trade_csv(trade)
            self.save_state()

            logger.info(
                "PAPER BUY FILLED   %s  %d shares @ $%.2f  "
                "new_pos=%d  avg_cost=$%.2f  cash=$%.2f",
                sym, shares, fill_px, int(new_shares), new_cost, self._cash,
            )

        self._orders.append(order)
        return order

    def sell(self, symbol: str, shares: float, price: float, reason: str = "") -> StockOrder:
        sym   = symbol.upper()
        order = self._new_order(sym, OrderSide.SELL, shares, price)

        fill_px = self._fill_price(price, "SELL")
        held_shares, held_cost = self._positions.get(sym, (0.0, 0.0))
        if shares > held_shares + 1e-9:
            order.status        = OrderStatus.REJECTED
            order.reject_reason = (
                f"Insufficient position: have {held_shares:.4f} shares, need {shares:.4f}"
            )
            logger.warning("PAPER SELL REJECTED  %s × %.4f — %s",
                           sym, shares, order.reject_reason)
        else:
            pnl              = round((fill_px - held_cost) * shares, 2)
            proceeds         = round(shares * fill_px, 2)
            self._cash      += proceeds
            self._realized_pnl += pnl
            new_shares       = round(held_shares - shares, 9)
            if new_shares < 1e-9:
                del self._positions[sym]
            else:
                self._positions[sym] = (new_shares, held_cost)
            order.status    = OrderStatus.FILLED
            order.filled_at = datetime.now(timezone.utc)

            trade = PaperTrade(
                timestamp      = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                symbol         = sym,
                side           = "SELL",
                shares         = shares,
                price          = fill_px,
                total_value    = proceeds,
                cash_remaining = self._cash,
                reason         = reason,
            )
            self._trade_log.append(trade)
            self._log_trade_csv(trade)
            self.save_state()

            logger.info(
                "PAPER SELL FILLED  %s  %.4f shares @ $%.2f  "
                "trade_pnl=$%.2f  total_realized=$%.2f  cash=$%.2f",
                sym, shares, fill_px, pnl, self._realized_pnl, self._cash,
            )

        self._orders.append(order)
        return order

    # ── Portfolio state ───────────────────────────────────────────────────────

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def starting_cash(self) -> float:
        return self._starting_cash

    def position(self, symbol: str) -> float:
        return self._positions.get(symbol.upper(), (0.0, 0.0))[0]

    def avg_cost(self, symbol: str) -> float:
        return self._positions.get(symbol.upper(), (0.0, 0.0))[1]

    def realized_pnl(self) -> float:
        return self._realized_pnl

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, (shares, cost) in self._positions.items():
            px = prices.get(sym, prices.get(sym.lower(), 0.0))
            total += (px - cost) * shares
        return round(total, 2)

    def total_value(self, prices: dict[str, float]) -> float:
        pos_value = sum(
            prices.get(sym, prices.get(sym.lower(), 0.0)) * shares
            for sym, (shares, _) in self._positions.items()
        )
        return round(self._cash + pos_value, 2)

    def positions_snapshot(self) -> dict[str, tuple[float, float]]:
        return {sym: (shares, cost) for sym, (shares, cost) in self._positions.items()}

    # ── Order history ─────────────────────────────────────────────────────────

    def all_orders(self) -> list[StockOrder]:
        return list(self._orders)

    def filled_orders(self) -> list[StockOrder]:
        return [o for o in self._orders if o.status == OrderStatus.FILLED]

    # ── Paper summary for dashboard ───────────────────────────────────────────

    def build_paper_summary(self, scan_results: list) -> PaperSummary:
        """Build a full PaperSummary from current state + live scan prices."""
        price_map   = {r.symbol.upper(): r.price    for r in scan_results}
        verdict_map = {r.symbol.upper(): r.verdict  for r in scan_results}
        currency_map= {r.symbol.upper(): r.currency for r in scan_results}

        positions: list[PortfolioPosition] = []
        for sym, (shares, avg_cost) in self._positions.items():
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

        unrealized   = round(sum(p.gain_loss for p in positions), 2)
        pos_mkt_val  = sum(p.current_value for p in positions)
        total_val    = round(self._cash + pos_mkt_val, 2)
        recent       = list(reversed(self._trade_log[-10:]))

        return PaperSummary(
            cash           = self._cash,
            starting_cash  = self._starting_cash,
            positions      = positions,
            realized_pnl   = self._realized_pnl,
            unrealized_pnl = unrealized,
            total_value    = total_val,
            recent_trades  = recent,
        )

    # ── Stats helper ──────────────────────────────────────────────────────────

    def log_state(self, prices: dict[str, float] | None = None) -> None:
        prices = prices or {}
        logger.info(
            "PAPER PORTFOLIO | cash=$%.2f | realized_pnl=$%.2f | "
            "unrealized_pnl=$%.2f | total_fills=%d | open_positions=%d",
            self._cash,
            self._realized_pnl,
            self.unrealized_pnl(prices),
            len(self.filled_orders()),
            len(self._positions),
        )

    # ── State persistence (JSON) ──────────────────────────────────────────────

    def _load_state(self) -> bool:
        """Load cash/positions/realized_pnl from paper_state.json. Returns True on success."""
        if not os.path.exists(_STATE_JSON):
            return False
        try:
            with open(_STATE_JSON, "r", encoding="utf-8") as f:
                state = json.load(f)
            cash = float(state["cash"])
            realized_pnl = float(state.get("realized_pnl", 0.0))

            # Sanity check — reject obviously corrupted state
            if cash > 1_000_000 or cash < 0:
                logger.warning(
                    "paper_state.json has corrupted cash=%.2f — deleting and starting fresh", cash
                )
                os.remove(_STATE_JSON)
                return False
            if abs(realized_pnl) > 1_000_000:
                logger.warning(
                    "paper_state.json has corrupted realized_pnl=%.2f — deleting and starting fresh",
                    realized_pnl,
                )
                os.remove(_STATE_JSON)
                return False

            positions: dict[str, tuple[float, float]] = {}
            for sym, v in state.get("positions", {}).items():
                shares   = float(v["shares"])
                avg_cost = float(v["avg_cost"])
                if shares <= 0 or shares > 100_000 or avg_cost <= 0 or avg_cost > 500_000:
                    logger.warning("Skipping corrupted position %s: shares=%s avg_cost=%s", sym, shares, avg_cost)
                    continue
                positions[sym.upper()] = (shares, avg_cost)

            self._cash         = cash
            self._realized_pnl = realized_pnl
            self._positions    = positions
            print("📄 Paper state restored:")
            print(f"   Cash: ${self._cash:,.2f}")
            print(f"   Positions: {list(self._positions.keys())}")
            print(f"   Realized P&L: ${self._realized_pnl:+.2f}")
            logger.info(
                "Paper state restored | cash=$%.2f | positions=%s | realized_pnl=$%.2f",
                self._cash, list(self._positions.keys()), self._realized_pnl,
            )
            return True
        except (KeyError, ValueError, json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load paper_state.json (%s) — starting fresh", exc)
            return False

    def save_state(self) -> None:
        """Persist current cash/positions/realized_pnl to paper_state.json."""
        state = {
            "cash": round(self._cash, 6),
            "positions": {
                sym: {"shares": round(shares, 9), "avg_cost": round(cost, 6)}
                for sym, (shares, cost) in self._positions.items()
            },
            "realized_pnl": round(self._realized_pnl, 6),
            "last_updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            with open(_STATE_JSON, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except OSError as exc:
            logger.warning("Could not save paper_state.json: %s", exc)

    # ── CSV persistence ───────────────────────────────────────────────────────

    def _ensure_csv_header(self) -> None:
        if not os.path.exists(_TRADES_CSV):
            try:
                with open(_TRADES_CSV, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(_CSV_HEADER)
                logger.info("Created paper_trades.csv at %s", _TRADES_CSV)
            except OSError as exc:
                logger.warning("Could not create paper_trades.csv: %s", exc)

    def _log_trade_csv(self, trade: PaperTrade) -> None:
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
                ])
        except OSError as exc:
            logger.warning("Could not write to paper_trades.csv: %s", exc)

    # ── Sector helpers ────────────────────────────────────────────────────────

    def _sector_of(self, symbol: str) -> str:
        return get_sector(symbol)

    def _sector_count(self) -> dict[str, int]:
        """Return {sector: open_position_count} for current portfolio."""
        counts: dict[str, int] = {}
        for sym in self._positions:
            sector = self._sector_of(sym)
            counts[sector] = counts.get(sector, 0) + 1
        return counts

    def get_sector_exposure(self) -> dict[str, int]:
        """Public view of sector concentration in open positions."""
        return self._sector_count()

    # ── Internal ─────────────────────────────────────────────────────────────

    @staticmethod
    def _new_order(
        symbol: str, side: OrderSide, shares: float, price: float
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
