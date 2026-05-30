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

import logging
from dataclasses import dataclass
from datetime import date
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


# ---------------------------------------------------------------------------
# Risk manager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Stateful risk gate. Call evaluate() before every trade; call record_fill()
    after every confirmed fill so daily counters stay accurate.
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config             = config or RiskConfig()
        self._today:            date           = date.today()
        self._fills_today:      int            = 0
        self._day_open_value:   Optional[float] = None
        self._peak_value:       float           = 0.0   # all-time portfolio peak

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
    ) -> ApprovalResult:
        """
        Run all risk checks for *signal* at *price*.
        Returns ApprovalResult — truthy if safe to trade.
        Pass candle_date in backtests so daily counters reset on historical dates
        instead of the real wall-clock date.
        """
        current_value = portfolio.total_value(price)
        self._maybe_reset_day(current_value, candle_date)
        self._update_peak(current_value)

        if signal == Signal.HOLD:
            return APPROVED

        # ── Check 1: manual halt ──────────────────────────────────────
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
        if signal == Signal.BUY and self._fills_today >= self.config.max_trades_per_day:
            return ApprovalResult(
                approved=False,
                message=(
                    f"Daily trade cap reached: {self._fills_today}/"
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

        # ── Check 5: max position size (BUY only) ─────────────────────
        if signal == Signal.BUY:
            new_position_value = (portfolio.position + trade_qty) * price
            new_position_pct   = new_position_value / current_value if current_value else 1.0
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

    def record_fill(self) -> None:
        """Call after every confirmed FILLED order."""
        self._fills_today += 1
        logger.debug("Daily fills: %d/%d", self._fills_today, self.config.max_trades_per_day)

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

    def _maybe_reset_day(self, current_value: float, candle_date: Optional[date] = None) -> None:
        today = candle_date if candle_date is not None else date.today()
        if today != self._today:
            self._today          = today
            self._fills_today    = 0
            self._day_open_value = current_value
            logger.info("New trading day — counters reset | day_open=$%.2f", current_value)
        elif self._day_open_value is None:
            self._day_open_value = current_value
            logger.info("Day-open value set | $%.2f", current_value)
