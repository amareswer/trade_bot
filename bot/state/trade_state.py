"""
Trading state machine.

States
──────
  IDLE      no open position, ready to BUY
  LONG      active position exists, ready to SELL
  COOLDOWN  locked after a trade; counts down configured candles before returning to IDLE

Transitions
───────────
  IDLE + BUY filled   → LONG
  LONG + SELL filled  → COOLDOWN (cooldown_ticks candles)
  COOLDOWN exhausted  → IDLE

Position-aware signal filtering (runs BEFORE risk engine)
──────────────────────────────────────────────────────────
  IDLE:     SELL  → HOLD  "no active position to sell"
  LONG:     BUY   → HOLD  "position already open"
  COOLDOWN: any   → HOLD  "cooldown active (N candles remaining)"

Signal deduplication
────────────────────
  If the strategy generates the same actionable signal as the last
  executed action, suppress it — nothing has changed since last time.
  Example: last action was BUY, strategy says BUY again → HOLD.
  (The LONG state filter already handles this; dedup is a safety net.)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from bot.strategy.threshold_strategy import Signal

logger = logging.getLogger(__name__)


class TradingState(Enum):
    IDLE     = "IDLE"
    LONG     = "LONG"
    COOLDOWN = "COOLDOWN"


@dataclass
class TradeEvent:
    timestamp:   datetime
    action:      str
    price:       float
    state_after: str


class TradingStateMachine:
    """
    Single source of truth for trading state.

    Call order each tick:
      1. state_machine.tick()          — advance cooldown countdown
      2. state_machine.filter_signal() — position-aware + dedup filtering
      3. ... risk gate ...
      4. state_machine.on_fill()       — update state after confirmed fill
    """

    def __init__(self, cooldown_ticks: int = 5):
        self.cooldown_ticks      = cooldown_ticks
        self.state               = TradingState.IDLE
        self.cooldown_remaining  = 0
        self.last_action:        Optional[Signal]   = None
        self.last_trade_price:   float              = 0.0
        self.last_trade_at:      Optional[datetime] = None
        self.history:            list[TradeEvent]   = []

    # ── Public interface ─────────────────────────────────────────────

    def tick(self) -> None:
        """Advance cooldown by one candle. Call once at the start of each tick."""
        if self.state == TradingState.COOLDOWN:
            self.cooldown_remaining -= 1
            if self.cooldown_remaining <= 0:
                self.cooldown_remaining = 0
                self.state = TradingState.IDLE
                logger.info("Cooldown expired — state: IDLE")

    def filter_signal(self, raw: Signal) -> tuple[Signal, str]:
        """
        Apply position-aware filtering and deduplication.
        Returns (filtered_signal, human_readable_reason).
        Reason is empty string when the signal passes through unchanged.
        """
        if raw == Signal.HOLD:
            return Signal.HOLD, ""

        # ── Cooldown lock ────────────────────────────────────────────
        if self.state == TradingState.COOLDOWN:
            return Signal.HOLD, f"cooldown active — {self.cooldown_remaining} candles remaining"

        # ── No position: cannot SELL ─────────────────────────────────
        if self.state == TradingState.IDLE and raw == Signal.SELL:
            return Signal.HOLD, "no active position to sell"

        # ── In position: cannot BUY again ────────────────────────────
        if self.state == TradingState.LONG and raw == Signal.BUY:
            return Signal.HOLD, "position already open — ignoring BUY"

        # ── Deduplication: same action as last executed trade ─────────
        if raw == self.last_action:
            return Signal.HOLD, f"duplicate {raw.value} — market state unchanged since last trade"

        return raw, ""

    def on_fill(self, action: Signal, price: float) -> None:
        """Update state after a confirmed FILLED order."""
        prev = self.state
        self.last_action     = action
        self.last_trade_price = price
        self.last_trade_at   = datetime.now(timezone.utc)

        if action == Signal.BUY:
            self.state = TradingState.LONG
        elif action == Signal.SELL:
            self.state              = TradingState.COOLDOWN
            self.cooldown_remaining = self.cooldown_ticks

        self.history.append(TradeEvent(
            timestamp   = self.last_trade_at,
            action      = action.value,
            price       = price,
            state_after = self.state.value,
        ))
        logger.info(
            "State %s → %s | %s @ $%.2f | cooldown=%d",
            prev.value, self.state.value, action.value, price, self.cooldown_remaining,
        )

    # ── Read-only helpers ────────────────────────────────────────────

    @property
    def last_trade_label(self) -> str:
        if self.last_action is None:
            return "—"
        return f"{self.last_action.value} @ ${self.last_trade_price:,.2f}"
