"""Exit policy for the position book — asymmetric confidence bars.

Entries and exits are not symmetric decisions: a BUY adds risk (high bar),
a SELL on a held position removes risk (lower bar). Mirrors the crypto bot's
risk-manager philosophy where SELL is always easier than BUY.

Two independent ways an AI SELL verdict exits a held position:
  1. Single verdict at confidence >= min_confidence_sell (default 55).
  2. Streak: `streak_cycles` consecutive scan cycles of SELL verdicts each at
     confidence >= streak_min_conf (default 2 cycles at >= 50) — a repeated
     weak warning is treated as a pattern even if no single reading clears
     the bar. Incident that motivated this: AC.TO SELL 58% then 60% on
     2026-07-10 while held — both ignored under the old single 65% bar.

Streak state is in-memory only: a restart resets counters, which at worst
delays a streak exit by `streak_cycles` scan cycles. The hard SL/TP watcher
is the crash-safe backstop and is unaffected by this policy.

All thresholds come from stock_bot/.env via config.py — never hardcode.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str
    streak: int


class ExitPolicy:
    def __init__(
        self,
        min_confidence_sell: int,
        streak_min_conf: int,
        streak_cycles: int,
    ) -> None:
        self.min_confidence_sell = min_confidence_sell
        self.streak_min_conf = streak_min_conf
        self.streak_cycles = streak_cycles
        self._streaks: dict[str, int] = {}

    def decide(
        self, symbol: str, signal: str, confidence: int, held: bool
    ) -> ExitDecision:
        """Record this cycle's verdict and decide whether a held position exits.

        Must be called exactly once per symbol per scan cycle — for every
        verdict including HOLD/BUY, so that a non-SELL verdict breaks the
        streak. `held` refers to the position book only.
        """
        sym = symbol.upper()
        if signal == "SELL" and confidence >= self.streak_min_conf:
            self._streaks[sym] = self._streaks.get(sym, 0) + 1
        else:
            self._streaks[sym] = 0
        streak = self._streaks[sym]

        if not held or signal != "SELL":
            return ExitDecision(False, "", streak)

        if confidence >= self.min_confidence_sell:
            return ExitDecision(
                True,
                f"SELL {confidence}% >= exit bar {self.min_confidence_sell}%",
                streak,
            )
        if streak >= self.streak_cycles:
            return ExitDecision(
                True,
                f"SELL streak {streak}x >= {self.streak_cycles} cycles "
                f"(each >= {self.streak_min_conf}%)",
                streak,
            )
        return ExitDecision(
            False,
            f"SELL {confidence}% below exit bar {self.min_confidence_sell}% · "
            f"streak {streak}/{self.streak_cycles}",
            streak,
        )

    def clear(self, symbol: str) -> None:
        """Reset streak after the position is closed (AI exit or SL/TP)."""
        self._streaks.pop(symbol.upper(), None)
