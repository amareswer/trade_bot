"""
Risk management engine.

Every signal must pass through RiskManager.evaluate() before an order is placed.
If any check fails the trade is BLOCKED — the executor is never called.

Checks (in order, most severe first):
  1. HALT flag          — manual kill-switch, blocks everything
  2. Kill switch         — portfolio down > X% from all-time peak, STICKY (persists
                            across restart, never auto-clears — requires editing the
                            state file). Added 2026-08-07.
  3. Max drawdown (halt) — portfolio down > X% from all-time peak, NOT sticky
                            (auto-lifts on recovery)
  4. Weekly loss limit   — portfolio down > X% from this ISO-week's open. Added 2026-08-07.
  5. Daily trade cap    — no more than N fills per calendar day
  6. Daily loss limit   — portfolio fell > X% from today's open
  7. Max position size  — BUY may not push position above Y% of portfolio

A non-blocking drawdown-WARNING tier also exists (drawdown_status()) — it never
blocks a trade, so it isn't part of evaluate()'s BUY/SELL gate; callers check it
separately to decide whether to fire an alert. Added 2026-08-07, mirroring the
stock bot's four-tier breaker upgrade from 2026-08-05 — crypto had fallen behind
with only a single non-sticky drawdown check even though it trades real money.
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
    max_drawdown_pct:     float = 0.10   # drawdown-HALT tier — halt new BUYs if down >10%
                                          # from all-time peak. Not sticky — auto-lifts on
                                          # recovery.
    max_trades_per_day:   int   = 5      # hard cap on fills per calendar day
    halt:                 bool  = False  # manual kill-switch
    # ── Added 2026-08-07 — mirrors the stock bot's breaker upgrade 2026-08-05 ──
    weekly_loss_limit_pct: float = 0.05  # halt new BUYs if down >X% from this ISO-week's
                                          # UTC-Monday-open value. Resets fresh every week.
    drawdown_warning_pct:  float = 0.03  # non-blocking — informational only, exposed via
                                          # drawdown_status() for the caller to alert on.
    kill_switch_pct:       float = 0.15  # halt new BUYs if down >X% from all-time peak.
                                          # STICKY — persists across restart, never
                                          # auto-clears. Manual state-file edit required.


# ---------------------------------------------------------------------------
# Approval result
# ---------------------------------------------------------------------------

class BlockReason(Enum):
    HALT            = "HALT"
    KILL_SWITCH     = "KILL_SWITCH"
    MAX_DRAWDOWN    = "MAX_DRAWDOWN"
    WEEKLY_LOSS     = "WEEKLY_LOSS"
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
        # Weekly loss / kill-switch tiers (added 2026-08-07). Unlike the daily
        # baseline (session/UTC-day only), peak_value/week_open_value/
        # kill_switch_tripped persist — a crash-restart must not silently
        # reset the all-time peak or un-trip the kill switch.
        self._week_open_value:    Optional[float] = None
        self._week_start_iso:     Optional[str]   = None
        self._kill_switch_tripped: bool           = False

        if state_path:
            self._load_state()

        logger.info(
            "RiskManager ready | max_pos=%.0f%% | daily_loss=%.0f%% | "
            "max_dd=%.0f%% | weekly_loss=%.0f%% | kill_switch=%.0f%% | "
            "max_trades=%d/day | halt=%s",
            self.config.max_position_pct      * 100,
            self.config.daily_loss_limit_pct  * 100,
            self.config.max_drawdown_pct      * 100,
            self.config.weekly_loss_limit_pct * 100,
            self.config.kill_switch_pct       * 100,
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
        self._maybe_reset_week(current_value, candle_date)
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

        # ── Check 2: kill switch (BUY only — SELL must always close). STICKY —
        # checked before the drawdown-halt tier since it's the more severe of
        # the two and, once tripped, stays tripped regardless of what the
        # drawdown-halt check below would independently conclude. ──
        if signal == Signal.BUY and self._is_kill_switch_tripped(current_value):
            return ApprovalResult(
                approved=False,
                message=(
                    f"KILL SWITCH active: {self._drawdown_from_peak_pct(current_value)*100:.2f}% "
                    f"drawdown from peak ${self._peak_value:,.2f} — all new BUYs blocked "
                    f"until manually cleared (edit kill_switch_tripped in the risk state file)"
                ),
                block_reason=BlockReason.KILL_SWITCH,
            )

        # ── Check 3: all-time max drawdown — HALT tier (BUY only — SELL must
        # always close). Not sticky — auto-lifts the moment equity recovers. ──
        if signal == Signal.BUY and self._peak_value > 0:
            drawdown = self._drawdown_from_peak_pct(current_value)
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

        # ── Check 4: weekly loss limit (BUY only — SELL must always close).
        # Not sticky — resets fresh every ISO week regardless of prior trip. ──
        if signal == Signal.BUY and self._week_open_value:
            weekly_loss_pct = (self._week_open_value - current_value) / self._week_open_value
            if weekly_loss_pct >= self.config.weekly_loss_limit_pct:
                return ApprovalResult(
                    approved=False,
                    message=(
                        f"Weekly loss limit: portfolio down {weekly_loss_pct*100:.2f}% "
                        f"(limit={self.config.weekly_loss_limit_pct*100:.0f}%) — "
                        f"week_open=${self._week_open_value:,.2f}, now=${current_value:,.2f}"
                    ),
                    block_reason=BlockReason.WEEKLY_LOSS,
                )

        # ── Check 5: daily trade cap (BUY only — SELL must always be allowed) ──
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

        # ── Check 6: daily loss limit (BUY only — SELL must always close) ──
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

        # ── Check 7: max position size (BUY only, always per-slot) ────
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

    @property
    def week_open_value(self) -> Optional[float]:
        return self._week_open_value

    @property
    def kill_switch_tripped(self) -> bool:
        return self._kill_switch_tripped

    def drawdown_status(self, current_value: float) -> dict:
        """Public snapshot for the non-blocking warning tier. Never blocks a
        trade — the caller (bot/main.py) checks this separately and decides
        whether to fire an alert; this class doesn't own alert delivery."""
        dd = self._drawdown_from_peak_pct(current_value)
        return {
            "peak_value":     self._peak_value,
            "current_value":  current_value,
            "drawdown_pct":   dd,
            "warning":        dd >= self.config.drawdown_warning_pct,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _drawdown_from_peak_pct(self, current_value: float) -> float:
        if self._peak_value <= 0:
            return 0.0
        return max(0.0, (self._peak_value - current_value) / self._peak_value)

    def _is_kill_switch_tripped(self, current_value: float) -> bool:
        """STICKY — once tripped it stays tripped (persisted to disk) until
        someone manually clears kill_switch_tripped in the state file. A
        drawdown this severe should force a human decision, not self-heal —
        same reasoning as the stock bot's identically-named tier."""
        if self._kill_switch_tripped:
            return True
        if self._drawdown_from_peak_pct(current_value) >= self.config.kill_switch_pct:
            self._kill_switch_tripped = True
            logger.error(
                "KILL SWITCH TRIPPED: %.2f%% drawdown from peak $%.2f (current $%.2f) — "
                "all new BUYs blocked until manually cleared in the risk state file",
                self._drawdown_from_peak_pct(current_value) * 100,
                self._peak_value, current_value,
            )
            self._save_state()
            return True
        return False

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

    def _maybe_reset_week(self, current_value: float, candle_date: Optional[date] = None) -> None:
        """ISO-week reset (UTC, Monday-anchored) for the weekly-loss tier.
        Accepts candle_date for the same backtest-determinism reason as
        _maybe_reset_day — a backtest should reset on historical dates, not
        the real wall-clock date."""
        ref_date  = candle_date if candle_date is not None else _utc_today()
        year, week, _ = ref_date.isocalendar()
        this_week = f"{year}-W{week:02d}"
        if self._week_start_iso != this_week:
            self._week_start_iso = this_week
            self._week_open_value = current_value
            logger.info(
                "New trading week — weekly-loss baseline reset | week_open=$%.2f",
                current_value,
            )
            self._save_state()
        elif self._week_open_value is None:
            self._week_open_value = current_value
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
        # Weekly baseline and kill switch are NOT gated on "same UTC day" —
        # they have their own independent reset cadence (weekly / never,
        # respectively) and must survive a restart on any day.
        self._week_start_iso  = data.get("week_start_iso")
        _wov = data.get("week_open_value")
        self._week_open_value = float(_wov) if _wov is not None else None
        self._kill_switch_tripped = bool(data.get("kill_switch_tripped", False))
        logger.info(
            "RiskManager state restored | peak=$%.2f fills_today=%d day_open=%s "
            "week_open=%s kill_switch_tripped=%s",
            self._peak_value, self._fills_today,
            f"${self._day_open_value:.2f}" if self._day_open_value else "unset",
            f"${self._week_open_value:.2f}" if self._week_open_value else "unset",
            self._kill_switch_tripped,
        )

    def _save_state(self) -> None:
        if not self._state_path:
            return
        try:
            tmp_path = self._state_path + ".tmp"
            with open(tmp_path, "w") as fh:
                json.dump({
                    "today":               self._today.isoformat(),
                    "fills_today":         self._fills_today,
                    "fills_by_symbol":     self._fills_by_symbol,
                    "day_open_value":      self._day_open_value,
                    "peak_value":          self._peak_value,
                    "week_start_iso":      self._week_start_iso,
                    "week_open_value":     self._week_open_value,
                    "kill_switch_tripped": self._kill_switch_tripped,
                }, fh)
            os.replace(tmp_path, self._state_path)
        except Exception as exc:
            logger.warning("RiskManager state save failed: %s", exc)
