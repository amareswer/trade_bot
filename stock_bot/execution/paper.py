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

import sys as _sys
import os as _os
_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _PROJECT_ROOT not in _sys.path:
    _sys.path.insert(0, _PROJECT_ROOT)

import csv
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from stock_bot.data.price_feed import get_sector, get_usd_cad_rate, is_cad_symbol
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

# Separate file, NOT a change to paper_trades.csv — that 9-column schema is
# frozen (ConfidenceBandTracker/accuracy pipeline depend on it exactly; see
# CLAUDE.md hard rules). Settlement date + FX rate at trade time (Canadian
# tax record-keeping — punch-list item #9) are logged here instead, joined
# back to the frozen CSV by (timestamp, symbol, side).
_SETTLEMENT_CSV = os.path.join(_STOCK_BOT_DIR, "paper_trades_settlement.csv")
_SETTLEMENT_CSV_HEADER = ["timestamp", "symbol", "side", "settlement_date", "fx_rate_at_trade"]

_CSV_HEADER = [
    "timestamp", "symbol", "side", "shares",
    "price", "total_value", "cash_remaining", "reason", "confidence",
]


def _next_business_day(d: date) -> date:
    """
    T+1 settlement, skipping weekends. Deliberate simplification: does NOT
    account for market holidays (see stock_bot/main.py's _us_holidays()/
    _ca_holidays() for the real calendar, not wired in here) — this is a
    minimal data-capture field for later tax work, not a guaranteed-exact
    clearing date (punch-list item #9, scoped to "capture the fields, not
    build a tax engine", 2026-08-05).
    """
    next_day = d + timedelta(days=1)
    while next_day.weekday() >= 5:   # Saturday=5, Sunday=6
        next_day += timedelta(days=1)
    return next_day


class StockPaperExecutor(StockExecutorBase):
    """
    In-memory paper trading executor for the stock bot.

    State is held in RAM — resets on process restart.
    All prices come from yfinance data in the scan cycle (no extra API calls).
    """

    def __init__(self, starting_cash: float = 10_000.0, max_exposure_pct: float = 0.25) -> None:
        self._starting_cash: float                          = starting_cash
        self._max_exposure_pct: float                       = max_exposure_pct
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
        self._open_position_value: float = 0.0

        # Weekly loss / drawdown-from-peak breaker tiers (overridden by config
        # via set_weekly_loss_limit / set_drawdown_limits). Unlike the daily
        # breaker (session-lifetime only), peak_equity/week_open_equity/
        # kill_switch_tripped are persisted — a crash-restart must not
        # silently erase the all-time peak or un-trip the kill switch.
        self._weekly_loss_limit_pct: float = 0.05
        self._drawdown_warning_pct: float = 0.10
        self._drawdown_halt_pct: float = 0.15
        self._kill_switch_pct: float = 0.20
        self._peak_equity: float = 0.0
        self._week_open_equity: Optional[float] = None
        self._week_start_iso: Optional[str] = None
        self._kill_switch_tripped: bool = False

        # Per-position stop-loss % override (ATR-based sizing, opt-in via
        # PAPER_ATR_SIZING_ENABLED). Symbols not present here use the flat
        # cfg.paper_stop_loss_pct baseline — see get_position_stop_pct().
        self._position_stop_pct: dict[str, float] = {}

        if os.path.exists(_RESET_FLAG):
            os.remove(_RESET_FLAG)
            self._cash         = self._starting_cash
            self._positions    = {}
            self._realized_pnl = 0.0
            self._position_stop_pct = {}
            print("📄 Paper state RESET — starting fresh")
            logger.info("Paper state reset via flag file | cash=$%.2f", starting_cash)
        else:
            if not self._load_state():
                logger.info("StockPaperExecutor starting fresh | cash=$%.2f", starting_cash)
        # Session baseline = cash + mark value of any restored positions
        # (avg_cost until live prices arrive). Seeding _open_position_value with
        # the same marks keeps baseline and current total consistent — a
        # cash-only baseline disabled the breaker whenever a session started
        # with open positions (current total always far above baseline).
        self._update_position_value({})
        self._session_start_value = self._cash + self._open_position_value

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
        Returns True if total portfolio drawdown from session start exceeds the limit.
        Total = cash + mark-to-market of open positions (cached via _update_position_value).
        Once tripped, stays tripped for the rest of the session.
        """
        if self._daily_loss_tripped:
            return True
        if self._session_start_value <= 0:
            return False
        current_total = self._cash + self._open_position_value
        drawdown = (self._session_start_value - current_total) / self._session_start_value
        if drawdown >= self._daily_loss_limit_pct:
            self._daily_loss_tripped = True
            logger.warning(
                "PAPER daily loss limit hit: %.1f%% drawdown from session start $%.2f → current $%.2f",
                drawdown * 100, self._session_start_value, current_total,
            )
            return True
        return False

    def _update_position_value(self, prices: dict[str, float]) -> None:
        """
        Recalculate cached mark-to-market value of all open positions.
        Called after every fill, and once per scan cycle via
        refresh_position_marks(). Uses provided prices for known symbols;
        falls back to avg_cost for others to avoid stale-price API calls.
        """
        total = 0.0
        for sym, (shares, avg_cost) in self._positions.items():
            px = prices.get(sym, avg_cost)
            total += shares * px
        self._open_position_value = total
        self._update_breaker_marks()

    # ── Weekly loss / drawdown-from-peak breaker tiers ─────────────────────

    def set_weekly_loss_limit(self, pct: float) -> None:
        """Configure the weekly loss circuit breaker (fraction, e.g. 0.05 = 5%)."""
        self._weekly_loss_limit_pct = pct

    def set_drawdown_limits(self, warning_pct: float, halt_pct: float, kill_switch_pct: float) -> None:
        """Configure the three drawdown-from-peak tiers (fractions, increasing severity)."""
        self._drawdown_warning_pct = warning_pct
        self._drawdown_halt_pct = halt_pct
        self._kill_switch_pct = kill_switch_pct

    @staticmethod
    def _current_week_iso() -> str:
        year, week, _ = datetime.now(timezone.utc).isocalendar()
        return f"{year}-W{week:02d}"

    def _update_breaker_marks(self) -> None:
        """Update all-time peak equity and the week-open reference point from
        the current mark-to-market total. Persisted immediately on change so
        a restart can't silently reset either baseline."""
        current_total = self._cash + self._open_position_value
        if current_total > self._peak_equity:
            self._peak_equity = current_total
            self.save_state()
        this_week = self._current_week_iso()
        if self._week_start_iso != this_week:
            self._week_start_iso = this_week
            self._week_open_equity = current_total
            logger.info("New trading week — weekly-loss baseline reset | week_open=$%.2f", current_total)
            self.save_state()
        elif self._week_open_equity is None:
            self._week_open_equity = current_total
            self.save_state()

    def _is_weekly_loss_tripped(self) -> bool:
        """Blocks new BUYs only (mirrors daily loss). Auto-recovers if equity
        climbs back above the week-open threshold — resets fresh next week regardless."""
        if not self._week_open_equity or self._week_open_equity <= 0:
            return False
        current_total = self._cash + self._open_position_value
        loss_pct = (self._week_open_equity - current_total) / self._week_open_equity
        return loss_pct >= self._weekly_loss_limit_pct

    def _drawdown_from_peak_pct(self) -> float:
        if self._peak_equity <= 0:
            return 0.0
        current_total = self._cash + self._open_position_value
        return max(0.0, (self._peak_equity - current_total) / self._peak_equity)

    def _is_drawdown_halted(self) -> bool:
        """Blocks new BUYs only. Not sticky — auto-lifts as soon as the
        drawdown recovers below the halt threshold (unlike the kill switch)."""
        return self._drawdown_from_peak_pct() >= self._drawdown_halt_pct

    def _is_kill_switch_tripped(self) -> bool:
        """Blocks new BUYs only — SELL/exits are never blocked by any breaker
        tier. Sticky: once tripped it stays tripped (persisted to disk) until
        someone manually clears kill_switch_tripped in the state file — a
        20% all-time drawdown should force a human decision, not self-heal."""
        if self._kill_switch_tripped:
            return True
        if self._drawdown_from_peak_pct() >= self._kill_switch_pct:
            self._kill_switch_tripped = True
            logger.error(
                "KILL SWITCH TRIPPED: %.1f%% drawdown from peak $%.2f (current $%.2f) — "
                "all new BUYs blocked until manually cleared in paper_state.json",
                self._drawdown_from_peak_pct() * 100, self._peak_equity,
                self._cash + self._open_position_value,
            )
            self.save_state()
            return True
        return False

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

    def drawdown_status(self) -> dict:
        """Public snapshot for the non-blocking warning-tier alert, which is
        sent from stock_bot/main.py (the executor doesn't own alert delivery)."""
        dd = self._drawdown_from_peak_pct()
        return {
            "peak_equity":    self._peak_equity,
            "current_equity": self._cash + self._open_position_value,
            "drawdown_pct":   dd,
            "warning":        dd >= self._drawdown_warning_pct,
        }

    def refresh_position_marks(self, prices: dict[str, float]) -> None:
        """
        Re-mark open positions to current scan-cycle prices.

        _open_position_value is otherwise only refreshed inside buy()/sell()
        at fill time, so between fills the daily-loss breaker
        (_is_daily_loss_tripped) was checking drawdown against a stale mark —
        a held position that moves significantly with no new fill wouldn't
        be reflected until the next trade. Call this once per scan cycle,
        before any buy()/sell() decisions, so the breaker sees live prices.
        """
        self._update_position_value(prices)

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

        if candle_close is not None and live_price is not None:
            deviation = abs(candle_close - live_price) / max(live_price, 0.01)
            if deviation > 0.10:
                order = self._new_order(sym, OrderSide.BUY, int(shares), price)
                order.status        = OrderStatus.REJECTED
                order.reject_reason = (
                    f"Candle close ${candle_close:.2f} deviates {deviation * 100:.1f}% "
                    f"from live price ${live_price:.2f} — corrupted data"
                )
                logger.warning(
                    "PAPER BUY REJECTED %s — candle close $%.2f deviates "
                    "%.1f%% from live price $%.2f (corrupted data)",
                    sym, candle_close, deviation * 100, live_price,
                )
                self._orders.append(order)
                return order

        if self._is_daily_loss_tripped():
            order = self._new_order(sym, OrderSide.BUY, shares, price)
            order.status        = OrderStatus.REJECTED
            order.reject_reason = f"Daily loss limit ({self._daily_loss_limit_pct:.0%}) reached — no new buys today"
            logger.warning("PAPER BUY BLOCKED  %s — daily loss circuit breaker active", sym)
            self._orders.append(order)
            return order

        if self._is_kill_switch_tripped():
            order = self._new_order(sym, OrderSide.BUY, shares, price)
            order.status        = OrderStatus.REJECTED
            order.reject_reason = (
                f"KILL SWITCH active: {self._drawdown_from_peak_pct():.1%} drawdown from "
                f"peak — all new BUYs blocked until manually cleared"
            )
            logger.error("PAPER BUY BLOCKED  %s — kill switch active", sym)
            self._orders.append(order)
            return order

        if self._is_drawdown_halted():
            order = self._new_order(sym, OrderSide.BUY, shares, price)
            order.status        = OrderStatus.REJECTED
            order.reject_reason = (
                f"Drawdown halt ({self._drawdown_halt_pct:.0%}) reached: "
                f"{self._drawdown_from_peak_pct():.1%} down from peak — no new buys until recovery"
            )
            logger.warning("PAPER BUY BLOCKED  %s — drawdown halt active", sym)
            self._orders.append(order)
            return order

        if self._is_weekly_loss_tripped():
            order = self._new_order(sym, OrderSide.BUY, shares, price)
            order.status        = OrderStatus.REJECTED
            order.reject_reason = f"Weekly loss limit ({self._weekly_loss_limit_pct:.0%}) reached — no new buys this week"
            logger.warning("PAPER BUY BLOCKED  %s — weekly loss circuit breaker active", sym)
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
            self._log_trade_csv(trade, confidence=confidence)
            self._log_settlement_csv(trade, sym)
            self.save_state()
            self._update_position_value({sym: fill_px})

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
                self._position_stop_pct.pop(sym, None)
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
            self._log_settlement_csv(trade, sym)
            self.save_state()
            self._update_position_value({sym: fill_px})

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
            px = prices.get(sym, prices.get(sym.lower(), cost))
            total += (px - cost) * shares
        return round(total, 2)

    @staticmethod
    def _price_in_cad(sym: str, price: float) -> float:
        """self._cash is the CAD-denominated account balance — convert a
        USD-listed symbol's native price before mixing it into a CAD total."""
        return price if is_cad_symbol(sym) else price * get_usd_cad_rate()

    def total_value(self, prices: dict[str, float]) -> float:
        pos_value = sum(
            self._price_in_cad(sym, prices.get(sym, prices.get(sym.lower(), cost))) * shares
            for sym, (shares, cost) in self._positions.items()
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
            self._peak_equity          = float(state.get("peak_equity", 0.0) or 0.0)
            self._week_open_equity     = state.get("week_open_equity")
            if self._week_open_equity is not None:
                self._week_open_equity = float(self._week_open_equity)
            self._week_start_iso       = state.get("week_start_iso")
            self._kill_switch_tripped  = bool(state.get("kill_switch_tripped", False))
            self._position_stop_pct    = {
                sym.upper(): float(pct)
                for sym, pct in (state.get("position_stop_pct") or {}).items()
                if sym.upper() in positions   # drop stale entries for closed positions
            }
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
            "starting_cash": round(self._starting_cash, 6),
            "positions": {
                sym: {"shares": round(shares, 9), "avg_cost": round(cost, 6)}
                for sym, (shares, cost) in self._positions.items()
            },
            "realized_pnl": round(self._realized_pnl, 6),
            "peak_equity": round(self._peak_equity, 6),
            "week_open_equity": self._week_open_equity,
            "week_start_iso": self._week_start_iso,
            "kill_switch_tripped": self._kill_switch_tripped,
            "position_stop_pct": self._position_stop_pct,
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
        if not os.path.exists(_SETTLEMENT_CSV):
            try:
                with open(_SETTLEMENT_CSV, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(_SETTLEMENT_CSV_HEADER)
                logger.info("Created paper_trades_settlement.csv at %s", _SETTLEMENT_CSV)
            except OSError as exc:
                logger.warning("Could not create paper_trades_settlement.csv: %s", exc)

    def _log_settlement_csv(self, trade: PaperTrade, sym: str) -> None:
        """Records T+1 settlement date + the FX rate used for this fill —
        Canadian tax record-keeping (ACB in CAD, FX gain/loss component).
        Separate file from paper_trades.csv on purpose — see its header
        comment. Never blocks or fails a trade — logging-only, best-effort."""
        try:
            trade_date = datetime.strptime(trade.timestamp[:10], "%Y-%m-%d").date()
            settlement = _next_business_day(trade_date)
            fx_rate = 1.0 if is_cad_symbol(sym) else get_usd_cad_rate()
            with open(_SETTLEMENT_CSV, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    trade.timestamp, trade.symbol, trade.side,
                    settlement.isoformat(), f"{fx_rate:.6f}",
                ])
        except (OSError, ValueError) as exc:
            logger.warning("Could not write to paper_trades_settlement.csv: %s", exc)

    def _log_trade_csv(self, trade: PaperTrade, confidence: int = 0) -> None:
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

    def check_exposure(self, price_map: dict[str, float], pending_trade_value: float = 0.0) -> bool:
        """
        Return True if PROJECTED position value — current + pending_trade_value
        (a candidate BUY's approximate dollar size, same currency as total_value,
        i.e. CAD) — stays under the max exposure threshold.

        pending_trade_value defaults to 0.0 (current-state-only check, the old
        behavior) for callers that just want a snapshot. Passing the actual
        pending trade size closes a real gap: a current-state-only check can't
        see a single large BUY that would blow past the cap in one shot — it
        only catches it on the *next* BUY attempt, once already over (found
        2026-08-05 while reviewing the punch-list cash-reserve item).
        """
        total = self.total_value(price_map)
        if total <= 0:
            return True
        snap = self.positions_snapshot()
        pos_val = sum(shares * self._price_in_cad(sym, price_map.get(sym, cost))
                      for sym, (shares, cost) in snap.items())
        return ((pos_val + pending_trade_value) / total) < self._max_exposure_pct

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


if __name__ == "__main__":
    import shutil
    import tempfile

    # Pre-populate sector cache to avoid network calls during the test
    from stock_bot.data.price_feed import _sector_cache
    _sector_cache["TEST"]  = "other"
    _sector_cache["TEST2"] = "other"

    _tmpdir = tempfile.mkdtemp()
    try:
        # Redirect state files so test never touches live paper trading data
        _STATE_JSON = _os.path.join(_tmpdir, "state.json")
        _TRADES_CSV = _os.path.join(_tmpdir, "trades.csv")
        _RESET_FLAG = _os.path.join(_tmpdir, ".reset")

        _results = {"passes": 0, "fails": 0}

        def _chk(label: str, ok: bool) -> None:
            if ok:
                _results["passes"] += 1
                print(f"  PASS  {label}")
            else:
                _results["fails"] += 1
                print(f"  FAIL  {label}")

        # ── Test 1: breaker fires when position value drops 7% ─────────────
        ex = StockPaperExecutor(starting_cash=1000.0)
        order = ex.buy("TEST", 10, 50.0, reason="test")
        _chk("BUY 10×TEST @ $50 filled", order.status == OrderStatus.FILLED)

        # Simulate price drop: 10 shares × $43 = $430 mark value
        ex._update_position_value({"TEST": 43.0})
        _chk("_open_position_value == $430.00",
             abs(ex._open_position_value - 430.0) < 0.01)

        # cash ≈ $499.25 (after slippage), pos = $430 → total ≈ $929.25
        # drawdown ≈ ($1000 − $929.25) / $1000 ≈ 7.1% > 3% → TRIPPED
        _chk("Breaker TRIPPED at ~7% loss (>3% limit)",
             ex._is_daily_loss_tripped())

        # ── Test 2: breaker stays silent when position is only down 2% ─────
        # Clear test-1 state so ex2 starts with a clean $1000 slate
        if _os.path.exists(_STATE_JSON):
            _os.remove(_STATE_JSON)
        ex2 = StockPaperExecutor(starting_cash=1000.0)
        order2 = ex2.buy("TEST2", 10, 50.0, reason="test")
        _chk("BUY 10×TEST2 @ $50 filled", order2.status == OrderStatus.FILLED)

        # Simulate small drop: 10 shares × $48 = $480 mark value
        ex2._update_position_value({"TEST2": 48.0})
        # cash ≈ $499.25, pos = $480 → total ≈ $979.25
        # drawdown ≈ 2.1% < 3% → NOT tripped
        _chk("Breaker NOT tripped at ~2% loss (<3% limit)",
             not ex2._is_daily_loss_tripped())

        p, f = _results["passes"], _results["fails"]
        print(f"\n{'ALL PASS' if f == 0 else 'FAILURES FOUND'} — {p} passed, {f} failed")
        _sys.exit(0 if f == 0 else 1)
    finally:
        shutil.rmtree(_tmpdir, ignore_errors=True)
