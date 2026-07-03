"""
Capital pool — single cash pool shared across all symbols.

Prevents over-commitment when multiple symbols trade simultaneously.
Each symbol gets an equal slice (a "slot") of the total capital.
Capital returns to the pool when a position fully closes, carrying P&L.

Usage:
    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    # pool.slot_cash == 100.0

    # Initialize each executor to its slot before trading:
    for exc in executors.values():
        exc._portfolio.cash = pool.slot_cash

    # BUY fill confirmed:
    pool.allocate("BTC/CAD")

    # Full SELL fill (position closed):
    pool.release("BTC/CAD", executor.cash)   # executor.cash after sell
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class CapitalPool:
    """
    Shared cash pool for multi-symbol trading.

    total_capital is updated on every release() to track compounding P&L.
    slot_cash is recomputed from the current total each time, so winning
    pools grow and losing pools shrink — consistent with fixed-fractional sizing.
    """

    def __init__(self, total_capital: float, max_concurrent: int = 2, slot_cap: float = 0.0) -> None:
        if total_capital <= 0:
            raise ValueError("total_capital must be > 0")
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if slot_cap < 0:
            raise ValueError("slot_cap must be >= 0")
        self._total    = total_capital
        self._max_conc = max_concurrent
        self._slot_cap = slot_cap   # 0 = uncapped
        self._slots: dict[str, float] = {}  # symbol → cash allocated to this slot

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def slot_cash(self) -> float:
        """Cash budget per position slot (total / max_concurrent, capped at slot_cap if > 0)."""
        base = self._total / self._max_conc
        if self._slot_cap > 0:
            return min(base, self._slot_cap)
        return base

    @property
    def slot_cap(self) -> float:
        return self._slot_cap

    @property
    def total_capital(self) -> float:
        return self._total

    @total_capital.setter
    def total_capital(self, value: float) -> None:
        self._total = value

    @property
    def available_cash(self) -> float:
        """Cash not currently allocated to any open position."""
        return self._total - sum(self._slots.values())

    @property
    def allocated_symbols(self) -> list[str]:
        return list(self._slots.keys())

    @property
    def free_slots(self) -> int:
        return self._max_conc - len(self._slots)

    # ── Slot management ──────────────────────────────────────────────────────

    def can_open_position(self, symbol: str) -> bool:
        """
        True if a BUY for symbol is allowed by the pool.
        A symbol that already holds a slot can always add to its position.
        A new symbol needs a free slot.
        """
        if symbol in self._slots:
            return True
        return len(self._slots) < self._max_conc

    def is_allocated(self, symbol: str) -> bool:
        return symbol in self._slots

    def allocate(self, symbol: str) -> float:
        """
        Reserve a slot for symbol on confirmed BUY fill.
        Returns cash allocated (slot_cash). No-op if already allocated.
        Returns 0 if pool is exhausted.
        """
        if symbol in self._slots:
            return self._slots[symbol]
        if len(self._slots) >= self._max_conc:
            logger.warning(
                "CapitalPool: no free slots for %s (%d/%d used)",
                symbol, len(self._slots), self._max_conc,
            )
            return 0.0
        cash = self.slot_cash
        self._slots[symbol] = cash
        logger.info(
            "CapitalPool: allocated %.2f to %s  (%d/%d slots used)",
            cash, symbol, len(self._slots), self._max_conc,
        )
        return cash

    def release(self, symbol: str, cash_returned: float) -> None:
        """
        Return a slot to the pool when a position fully closes.
        Updates total_capital with the P&L embedded in cash_returned.

        cash_returned should be executor.cash immediately after the SELL fill —
        it equals (slot_initial_cash - buy_cost + sell_proceeds).
        """
        if symbol not in self._slots:
            return
        allocated = self._slots.pop(symbol)
        pnl       = cash_returned - allocated
        self._total = self._total - allocated + cash_returned
        logger.info(
            "CapitalPool: released %s  allocated=%.2f  returned=%.2f  pnl=%+.2f"
            "  new_total=%.2f  slots=%d/%d",
            symbol, allocated, cash_returned, pnl,
            self._total, len(self._slots), self._max_conc,
        )
