"""
Capital pool — single cash pool shared across all symbols.

Prevents over-commitment when multiple symbols trade simultaneously.
By default each symbol gets an equal slice (a "slot") of the total capital,
optionally capped by a single shared slot_cap. Capital returns to the pool
when a position fully closes, carrying P&L.

Usage (shared cap — original behavior, unchanged):
    pool = CapitalPool(total_capital=200.0, max_concurrent=2)
    # pool.slot_cash == 100.0

    # Initialize each executor to its slot before trading:
    for exc in executors.values():
        exc._portfolio.cash = pool.slot_cash

    # BUY fill confirmed:
    pool.allocate("BTC/CAD")

    # Full SELL fill (position closed):
    pool.release("BTC/CAD", executor.cash)   # executor.cash after sell

Usage (per-symbol caps — added 2026-08-24, for symbols with different-sized
slots sharing one pool, e.g. BTC/CAD at $77 alongside a smaller SOL/CAD slot):
    pool = CapitalPool(
        total_capital=153.39, max_concurrent=2,
        slot_caps={"BTC/CAD": 77.0, "SOL/CAD": 45.0},
    )
    for sym, exc in executors.items():
        exc._portfolio.cash = pool.slot_cash_for(sym)

A symbol not present in slot_caps falls back to the shared slot_cap (0 =
uncapped) — numerically identical to the pre-existing single-cap behavior,
so an unmodified single-symbol setup (BTC/CAD alone, MAX_SLOT_CASH_CAD=77,
no per-symbol overrides) is untouched by this feature's existence.
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


class CapitalPool:
    """
    Shared cash pool for multi-symbol trading.

    total_capital is updated on every release() to track compounding P&L.
    slot_cash is recomputed from the current total each time, so winning
    pools grow and losing pools shrink — consistent with fixed-fractional sizing.

    slot_caps (optional) lets individual symbols carry a different cap than
    the shared slot_cap default — see slot_cash_for() for the allocation
    semantics when caps differ per symbol.
    """

    def __init__(
        self,
        total_capital: float,
        max_concurrent: int = 2,
        slot_cap: float = 0.0,
        slot_caps: dict[str, float] | None = None,
    ) -> None:
        # External review, 2026-09-22, eighth round P1: "NaN and infinity
        # become accepted capital — CapitalPool's positive-value check
        # does not reject them." Both compare False against <= 0
        # (float('nan') <= 0 is False; float('inf') <= 0 is False too),
        # so either sailed straight past the check above and got
        # accepted as a real total. Reproduced via a corrupted
        # live_state_*.json feeding a NaN/Infinity realized_pnl all the
        # way through _replay_paper_realized_pnl into here. Checked
        # BEFORE the <= 0 comparison, which is itself meaningless against
        # NaN.
        if not math.isfinite(total_capital):
            raise ValueError(f"total_capital must be finite, got {total_capital!r}")
        if total_capital <= 0:
            raise ValueError("total_capital must be > 0")
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if slot_cap < 0:
            raise ValueError("slot_cap must be >= 0")
        if slot_caps:
            for sym, cap in slot_caps.items():
                if cap < 0:
                    raise ValueError(f"slot_caps[{sym!r}] must be >= 0")
        self._total     = total_capital
        self._max_conc  = max_concurrent
        self._slot_cap  = slot_cap   # 0 = uncapped (shared fallback)
        self._slot_caps: dict[str, float] = dict(slot_caps) if slot_caps else {}  # per-symbol overrides, 0 = uncapped
        self._slots: dict[str, float] = {}  # symbol → cash allocated to this slot

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def slot_cash(self) -> float:
        """Cash budget per position slot (total / max_concurrent, capped at slot_cap if > 0).

        This is the original, shared-cap-only computation — unaffected by
        slot_caps, so existing callers that never pass a symbol keep getting
        exactly the pre-2026-08-24 number. Use slot_cash_for(symbol) to get
        per-symbol-aware sizing.
        """
        base = self._total / self._max_conc
        if self._slot_cap > 0:
            return min(base, self._slot_cap)
        return base

    @property
    def slot_cap(self) -> float:
        return self._slot_cap

    @property
    def slot_caps(self) -> dict[str, float]:
        return dict(self._slot_caps)

    @property
    def total_capital(self) -> float:
        return self._total

    @total_capital.setter
    def total_capital(self, value: float) -> None:
        # Same finite-value guard as __init__ (2026-09-22, eighth round
        # P1) — every mutation path (the fold-in bumps in bot/main.py,
        # release()'s own reassignment below) goes through this setter,
        # so validating here catches a bad value at its point of entry
        # regardless of which caller produced it.
        if not math.isfinite(value):
            raise ValueError(f"total_capital must be finite, got {value!r}")
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

    # ── Slot sizing ──────────────────────────────────────────────────────────

    def slot_cash_for(self, symbol: str) -> float:
        """
        Per-symbol-aware target slot size.

        No per-symbol cap configured for `symbol`: falls straight through to
        `slot_cash` (equal division of the pool, capped by the shared
        slot_cap) — numerically identical to the original single-cap
        behavior. This is what makes the feature additive: a config that
        only ever set MAX_SLOT_CASH_CAD (no per-symbol overrides) produces
        the exact same slot_cash_for() result as slot_cash for every symbol.

        A per-symbol cap IS configured: that symbol's target is its OWN cap
        (0 = uncapped for that symbol), not an equal division of the pool —
        further capped by whatever cash isn't already committed to OTHER
        currently-open slots (total_capital minus every other symbol's
        allocated amount). This makes an under-capitalized pool degrade
        gracefully instead of over-committing: if the sum of configured caps
        exceeds total capital, a symbol allocated after the others already
        hold their slots gets whatever remains rather than its full cap
        (order of allocate() calls therefore matters when caps overcommit
        the pool — first allocated gets priority). If the sum of caps is
        LESS than total capital, the surplus is simply never claimed by
        anyone and stays idle in the pool (see available_cash) instead of
        being force-split across symbols the way equal division would.
        """
        already_committed = sum(v for k, v in self._slots.items() if k != symbol)
        remaining = max(0.0, self._total - already_committed)
        cap = self._slot_caps.get(symbol)
        if cap is None:
            # 2026-09-18 review finding: this branch used to return
            # self.slot_cash (an equal division of the FULL pool) with no
            # bound against what other symbols already hold — reproduced:
            # total=100, 2 slots, one symbol capped at 80 and allocated
            # first, the other (uncapped) still got the full 50 equal-share
            # via this branch, for a combined 130 out of a 100 pool.
            return min(self.slot_cash, remaining)
        if cap <= 0:
            return remaining   # 0 = uncapped for this symbol — bounded only by what's left
        return min(cap, remaining)

    # ── Slot management ──────────────────────────────────────────────────────

    def can_open_position(self, symbol: str) -> bool:
        """
        True if a BUY for symbol is allowed by the pool.
        A symbol that already holds a slot can always add to its position.
        A new symbol needs both a free slot AND actual remaining cash — a
        free slot count alone (2026-09-18 review finding) let a fully
        committed or zero-cash pool admit a new entry just because a
        nominal slot was open, before slot_cash_for() would have handed it
        $0 anyway.
        """
        if symbol in self._slots:
            return True
        if len(self._slots) >= self._max_conc:
            return False
        return self.slot_cash_for(symbol) > 0

    def is_allocated(self, symbol: str) -> bool:
        return symbol in self._slots

    def allocate(self, symbol: str, amount: "float | None" = None) -> float:
        """
        Reserve a slot for symbol on confirmed BUY fill.
        Returns cash allocated. No-op if already allocated.
        Returns 0 if pool is exhausted.

        amount (2026-09-22 review finding, P1): the ordinary caller (a
        FRESH BUY fill) omits this — the slot is sized by slot_cash_for(),
        an equal (or capped) division of the pool, exactly as before this
        parameter existed. A RESTART recovering a position that was
        already open before this process started must pass its OWN
        actual current value explicitly (typically executor.cash +
        executor.position * a price) instead: slot_cash_for() computes a
        THEORETICAL fresh-slot size from the pool's current total_capital,
        which has no relationship to what this specific position is
        actually worth right now (it may have been sized against a
        different total_capital at a different time, or grown/shrunk via
        price movement or partial fills since). Reproduced: shared free
        cash $135, one symbol already holding a position worth $165
        (equity $300 total). Passing no amount reserved a generic ~$67
        theoretical slot for it — unrelated to the $165 it's actually
        holding — silently losing $55 of real equity from every
        downstream accounting read (available_cash, _compute_account_
        value) with no economic event to explain it. Passing amount=165
        (or the executor's own cash+position*price) reserves exactly what
        this position is actually worth, keeping the pool's own books
        internally consistent from the moment it's constructed.
        """
        if symbol in self._slots:
            return self._slots[symbol]
        if len(self._slots) >= self._max_conc:
            logger.warning(
                "CapitalPool: no free slots for %s (%d/%d used)",
                symbol, len(self._slots), self._max_conc,
            )
            return 0.0
        cash = self.slot_cash_for(symbol) if amount is None else amount
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

        Ninth-round review finding, 2026-09-22, P1: the previous version
        popped the slot from self._slots FIRST, then computed/assigned
        the new total_capital — so a non-finite cash_returned raised
        (via the setter's own guard, added the round before) only AFTER
        the slot was already gone. The raise then propagated to the
        caller with the pool left in a corrupted intermediate state:
        the symbol's reservation vanished from self._slots (so
        available_cash silently counted it as free) while total_capital
        was NEVER actually updated to reflect that release — reserved
        funds became available with no real economic event, from a
        call that supposedly failed. Fixed: validate the WOULD-BE new
        total first; only mutate self._slots (and self._total) once
        that value is confirmed finite. A rejected call therefore
        changes nothing at all — the slot stays allocated exactly as
        before, so a caller can fix the bad value and simply call
        release() again.
        """
        if symbol not in self._slots:
            return
        allocated = self._slots[symbol]
        new_total = self._total - allocated + cash_returned
        if not math.isfinite(new_total):
            raise ValueError(
                f"release({symbol!r}, cash_returned={cash_returned!r}) would "
                f"produce a non-finite total_capital ({new_total!r}) — rejected "
                f"before any state changed; {symbol!r}'s slot remains allocated."
            )
        pnl = cash_returned - allocated
        self._slots.pop(symbol)
        self.total_capital = new_total   # through the setter — already known-finite, but stays the single source of truth
        logger.info(
            "CapitalPool: released %s  allocated=%.2f  returned=%.2f  pnl=%+.2f"
            "  new_total=%.2f  slots=%d/%d",
            symbol, allocated, cash_returned, pnl,
            self._total, len(self._slots), self._max_conc,
        )
