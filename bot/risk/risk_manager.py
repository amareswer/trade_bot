"""
Risk management engine.

Every signal must pass through RiskManager.evaluate() before an order is placed.
If any check fails the trade is BLOCKED — the executor is never called.

Checks (in order):
  1. HALT flag          — manual kill-switch, blocks everything
  2. Max drawdown       — portfolio down > X% from all-time peak (never resets)
  3. Daily trade cap    — no more than N fills per calendar day
  4. Daily loss limit   — portfolio fell > X% from today's open
  5. Max position size  — BUY may not push position above Y% of portfolio
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from bot.strategy.threshold_strategy import Signal
from bot.execution.executor import Portfolio

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class RiskConfig:
    max_position_pct:     float = 0.05   # max position value as % of portfolio
    daily_loss_limit_pct: float = 0.02   # halt if portfolio down >2% from day-open
    max_drawdown_pct:     float = 0.10   # halt if portfolio down >10% from all-time peak
    max_trades_per_day:   int   = 5      # hard cap on fills per calendar day
    halt:                 bool  = False  # manual kill-switch


# ---------------------------------------------------------------------------
# Approval result
# ---------------------------------------------------------------------------

class BlockReason(Enum):
    HALT            = "HALT"
    MAX_DRAWDOWN    = "MAX_DRAWDOWN"
    DAILY_TRADE_CAP = "DAILY_TRADE_CAP"
    DAILY_LOSS      = "DAILY_LOSS"
    POSITION_SIZE   = "POSITION_SIZE"


@dataclass
class ApprovalResult:
    approved:     bool
    message:      str
    block_reason: Optional[BlockReason] = None

    def __bool__(self) -> bool:
        return self.approved

    def __str__(self) -> str:
        tag = "APPROVED" if self.approved else f"BLOCKED [{self.block_reason.value}]"
        return f"{tag} — {self.message}"


APPROVED = ApprovalResult(approved=True, message="All checks passed")


def _utc_today() -> date:
    """Calendar date in UTC — all daily counters reset at UTC midnight,
    matching candle timestamps and the daily P&L alert."""
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Risk manager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Stateful risk gate. Call evaluate() before every trade; call record_fill()
    after every confirmed fill so daily counters stay accurate.
    """

    def __init__(self, config: Optional[RiskConfig] = None, state_path: Optional[str] = None):
        """
        state_path: optional JSON file for breaker state (peak value, day-open
        value, daily fill count). Pass a path in live mode so circuit breakers
        survive restarts; leave None for backtests and unit tests.
        """
        self.config             = config or RiskConfig()
        self._state_path        = state_path
        self._today:            date           = _utc_today()
        self._fills_today:      int            = 0
        self._fills_by_symbol:  dict           = {}    # per-symbol daily fill counts
        self._day_open_value:   Optional[float] = None
        self._peak_value:       float           = 0.0   # all-time portfolio peak

        if state_path:
            self._load_state()

        logger.info(
            "RiskManager ready | max_pos=%.0f%% | daily_loss=%.0f%% | "
            "max_dd=%.0f%% | max_trades=%d/day | halt=%s",
            self.config.max_position_pct     * 100,
            self.config.daily_loss_limit_pct * 100,
            self.config.max_drawdown_pct     * 100,
            self.config.max_trades_per_day,
            self.config.halt,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def evaluate(
        self,
        signal:      Signal,
        price:       float,
        portfolio:   Portfolio,
        trade_qty:   float,
        candle_date: Optional[date] = None,
        *,
        account_value: Optional[float] = None,
        symbol:        Optional[str]   = None,
    ) -> ApprovalResult:
        """
        Run all risk checks for *signal* at *price*.
        Returns ApprovalResult — truthy if safe to trade.
        Pass candle_date in backtests so daily counters reset on historical dates
        instead of the real wall-clock date.

        Multi-symbol mode (both optional, single-symbol behavior unchanged when omitted):
          account_value — aggregate value across ALL symbol slots. Used for the
            daily-loss and max-drawdown breakers so they measure the whole account,
            not whichever slot happens to be evaluating this tick. The position-size
            check always uses the slot portfolio (per-slot sizing semantics).
          symbol — enables the per-symbol daily trade cap instead of the global one.
        """
        slot_value    = portfolio.total_value(price)
        current_value = account_value if account_value is not None else slot_value
        self._maybe_reset_day(current_value, candle_date)
        self._update_peak(current_value)

        if signal == Signal.HOLD:
            return APPROVED

        # ── Check 1: manual halt — blocks BUY and SELL (HOLD returns early above) ──
        if self.config.halt:
            return ApprovalResult(
                approved=False,
                message="Trading is halted (config.halt=True)",
                block_reason=BlockReason.HALT,
            )

        # ── Check 2: all-time max drawdown (BUY only — SELL must always close) ──
        if signal == Signal.BUY and self._peak_value > 0:
            drawdown = (self._peak_value - current_value) / self._peak_value
            if drawdown >= self.config.max_drawdown_pct:
                return ApprovalResult(
                    approved=False,
                    message=(
                        f"Max drawdown circuit breaker: portfolio down {drawdown*100:.2f}% "
                        f"from peak ${self._peak_value:,.2f} "
                        f"(limit={self.config.max_drawdown_pct*100:.0f}%)"
                    ),
                    block_reason=BlockReason.MAX_DRAWDOWN,
                )

        # ── Check 3: daily trade cap (BUY only — SELL must always be allowed) ──
        # Per-symbol when symbol is passed (each symbol gets its own budget);
        # global count otherwise.
        _cap_fills = (
            self._fills_by_symbol.get(symbol, 0) if symbol is not None
            else self._fills_today
        )
        if signal == Signal.BUY and _cap_fills >= self.config.max_trades_per_day:
            _cap_label = f" [{symbol}]" if symbol is not None else ""
            return ApprovalResult(
                approved=False,
                message=(
                    f"Daily trade cap reached{_cap_label}: {_cap_fills}/"
                    f"{self.config.max_trades_per_day} fills today"
                ),
                block_reason=BlockReason.DAILY_TRADE_CAP,
            )

        # ── Check 4: daily loss limit (BUY only — SELL must always close) ──
        if signal == Signal.BUY and self._day_open_value:
            loss_pct = (self._day_open_value - current_value) / self._day_open_value
            if loss_pct >= self.config.daily_loss_limit_pct:
                return ApprovalResult(
                    approved=False,
                    message=(
                        f"Daily loss limit: portfolio down {loss_pct*100:.2f}% "
                        f"(limit={self.config.daily_loss_limit_pct*100:.0f}%) — "
                        f"open=${self._day_open_value:,.2f}, now=${current_value:,.2f}"
                    ),
                    block_reason=BlockReason.DAILY_LOSS,
                )

        # ── Check 5: max position size (BUY only, always per-slot) ────
        if signal == Signal.BUY:
            new_position_value = (portfolio.position + trade_qty) * price
            new_position_pct   = new_position_value / slot_value if slot_value else 1.0
            if new_position_pct > self.config.max_position_pct:
                return ApprovalResult(
                    approved=False,
                    message=(
                        f"Position size limit: {trade_qty} units would put "
                        f"{new_position_pct*100:.2f}% in position "
                        f"(limit={self.config.max_position_pct*100:.0f}%)"
                    ),
                    block_reason=BlockReason.POSITION_SIZE,
                )

        return APPROVED

    def record_fill(self, symbol: Optional[str] = None) -> None:
        """Call after every confirmed FILLED order. Pass symbol in multi-symbol
        mode so the per-symbol daily trade cap stays accurate."""
        self._fills_today += 1
        if symbol is not None:
            self._fills_by_symbol[symbol] = self._fills_by_symbol.get(symbol, 0) + 1
        logger.debug("Daily fills: %d/%d", self._fills_today, self.config.max_trades_per_day)
        self._save_state()

    def fills_today_for(self, symbol: str) -> int:
        """Daily fill count for one symbol (0 if none recorded)."""
        return self._fills_by_symbol.get(symbol, 0)

    def halt(self) -> None:
        self.config.halt = True
        logger.warning("RiskManager: HALT activated.")

    def resume(self) -> None:
        self.config.halt = False
        logger.info("RiskManager: HALT lifted.")

    @property
    def fills_today(self) -> int:
        return self._fills_today

    @property
    def peak_value(self) -> float:
        return self._peak_value

    @property
    def day_open_value(self) -> Optional[float]:
        return self._day_open_value

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _update_peak(self, current_value: float) -> None:
        if current_value > self._peak_value:
            self._peak_value = current_value
            self._save_state()

    def _maybe_reset_day(self, current_value: float, candle_date: Optional[date] = None) -> None:
        today = candle_date if candle_date is not None else _utc_today()
        if today != self._today:
            self._today           = today
            self._fills_today     = 0
            self._fills_by_symbol = {}
            self._day_open_value  = current_value
            logger.info("New trading day — counters reset | day_open=$%.2f", current_value)
            self._save_state()
        elif self._day_open_value is None:
            self._day_open_value = current_value
            logger.info("Day-open value set | $%.2f", current_value)
            self._save_state()

    # ------------------------------------------------------------------
    # State persistence — circuit breakers must survive restarts.
    # systemd auto-restarts the bot on crash; without this, a crash loop
    # would silently reset the max-drawdown peak and the daily trade cap.
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            with open(self._state_path) as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("RiskManager state load failed (%s) — starting fresh", exc)
            return

        self._peak_value = float(data.get("peak_value", 0.0))
        # Daily counters only apply if the saved state is from the same UTC day
        if data.get("today") == self._today.isoformat():
            self._fills_today = int(data.get("fills_today", 0))
            self._fills_by_symbol = dict(data.get("fills_by_symbol") or {})
            _dov = data.get("day_open_value")
            self._day_open_value = float(_dov) if _dov is not None else None
        logger.info(
            "RiskManager state restored | peak=$%.2f fills_today=%d day_open=%s",
            self._peak_value, self._fills_today,
            f"${self._day_open_value:.2f}" if self._day_open_value else "unset",
        )

    def _save_state(self) -> None:
        if not self._state_path:
            return
        try:
            tmp_path = self._state_path + ".tmp"
            with open(tmp_path, "w") as fh:
                json.dump({
                    "today":           self._today.isoformat(),
                    "fills_today":     self._fills_today,
                    "fills_by_symbol": self._fills_by_symbol,
                    "day_open_value":  self._day_open_value,
                    "peak_value":      self._peak_value,
                }, fh)
            os.replace(tmp_path, self._state_path)
        except Exception as exc:
            logger.warning("RiskManager state save failed: %s", exc)
