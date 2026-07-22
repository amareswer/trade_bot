"""
Alert evaluator — Phase 5.

Runs a set of checks over each scan cycle's results and produces Alert objects.
Thresholds are constants at the top of this file — easy to tune without touching logic.

No state persists between cycles — alerts re-fire every scan by design.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

from stock_bot.alerts.alert import Alert, AlertType, _HIGH_TYPES

if TYPE_CHECKING:
    from stock_bot.dashboard.renderer import ScanResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunable thresholds — change here to affect all checks
# ---------------------------------------------------------------------------
_STRONG_BUY_CONF   = 70    # min AI confidence to fire STRONG_BUY
_STRONG_SELL_CONF  = 70    # min AI confidence to fire STRONG_SELL
_PORT_SELL_CONF    = 65    # min AI confidence to fire PORTFOLIO_SELL
_PORT_BUY_CONF     = 70    # min AI confidence to fire PORTFOLIO_BUY_MORE
_EARNINGS_DAYS     = 3     # fire EARNINGS_SOON if within this many days
_RSI_OVERBOUGHT    = 75.0  # RSI above this → RSI_OVERBOUGHT (owned symbols)
_RSI_OVERSOLD      = 25.0  # RSI below this → RSI_OVERSOLD  (owned symbols)


class AlertEvaluator:
    """
    Evaluates scan results each cycle and returns a list of Alert objects.
    "Is this symbol held" is always answered from the live executor's
    positions_snapshot(), passed into evaluate() per cycle — there is no
    static/manual holdings list (the old PORTFOLIO-env PortfolioTracker
    fallback was removed 2026-07-22; it had gone fully dormant once an
    executor became mandatory 2026-07-17 and was never read otherwise).
    """

    def __init__(self) -> None:
        self._live_positions: dict[str, tuple[float, float]] | None = None

    # ── Public ───────────────────────────────────────────────────────────────

    def evaluate(
        self,
        scan_results: list[ScanResult],
        held_positions: dict[str, tuple[float, float]] | None = None,
    ) -> list[Alert]:
        """
        Run all checks over each symbol in the scan cycle.
        Returns a flat list of Alert objects (0 or more per symbol).
        Alerts re-fire every cycle — no deduplication across scans.

        held_positions: the ACTIVE executor's real positions_snapshot()
        ({symbol: (shares, avg_cost)}), when the caller has one — None when
        there is no executor (PAPER_TRADING_ENABLED=false), in which case
        every "is this held" check is False (nothing is held by the bot in
        that mode). 2026-07-21 fix: PORTFOLIO_SELL / RSI_OVERBOUGHT /
        RSI_OVERSOLD / EARNINGS_SOON all gate on "is this symbol held", but
        previously that checked a static, hand-typed PORTFOLIO-env list
        (stock_bot/.env) that the bot's own real trades (e.g. the IBKR CM
        position) were never part of — those protective alerts were
        invisible to the bot's actual holdings. Passing the live snapshot
        here closes that gap. The static-list fallback itself was removed
        2026-07-22 (see stock_bot/portfolio/tracker.py) — it had gone fully
        dormant once an executor became mandatory 2026-07-17.
        """
        self._live_positions = held_positions
        alerts: list[Alert] = []
        today  = date.today()

        for r in scan_results:
            sig   = r.verdict.signal if r.verdict else "HOLD"
            conf  = r.verdict.confidence if r.verdict else 0
            style = r.verdict.trading_style if r.verdict else "SWING"
            in_pf = self._in_portfolio(r.symbol)

            # 1. STRONG_BUY
            if sig == "BUY" and conf >= _STRONG_BUY_CONF:
                alerts.append(self._make(
                    AlertType.STRONG_BUY, r,
                    f"STRONG BUY: {r.symbol} @ ${r.price:,.2f} — "
                    f"{conf}% confidence ({style})",
                    conf, "MEDIUM",
                ))

            # 2. STRONG_SELL
            if sig == "SELL" and conf >= _STRONG_SELL_CONF:
                alerts.append(self._make(
                    AlertType.STRONG_SELL, r,
                    f"STRONG SELL: {r.symbol} @ ${r.price:,.2f} — "
                    f"{conf}% confidence",
                    conf, "MEDIUM",
                ))

            # 3. PORTFOLIO_SELL
            if in_pf and sig == "SELL" and conf >= _PORT_SELL_CONF:
                holding = self._get_holding(r.symbol)
                if holding:
                    shares, avg_cost = holding
                    gl_pct = (r.price - avg_cost) / avg_cost * 100
                    alerts.append(self._make(
                        AlertType.PORTFOLIO_SELL, r,
                        f"SELL SIGNAL on holding: {r.symbol} — "
                        f"you own {shares:g} shares, P&L: {gl_pct:+.1f}%",
                        conf, "HIGH",
                    ))

            # 4. PORTFOLIO_BUY_MORE
            if in_pf and sig == "BUY" and conf >= _PORT_BUY_CONF:
                holding = self._get_holding(r.symbol)
                if holding:
                    _, avg_cost = holding
                    gl_pct = (r.price - avg_cost) / avg_cost * 100
                    alerts.append(self._make(
                        AlertType.PORTFOLIO_BUY_MORE, r,
                        f"ADD MORE? {r.symbol} BUY signal — "
                        f"currently {gl_pct:+.1f}%",
                        conf, "MEDIUM",
                    ))

            # 5. EARNINGS_SOON
            # Priority (and Telegram delivery, HIGH-only) depends on whether the
            # symbol is actually held: earnings risk on a real position is
            # actionable, but a watchlist-only symbol is already auto-blocked
            # from new entries by the earnings blackout (main.py
            # _is_earnings_blackout) — surfacing it as HIGH/Telegram for every
            # scanned symbol was noise with nothing for the user to do.
            e = r.research.earnings if r.research else None
            if e and e.next_earnings_date:
                days_away = (e.next_earnings_date - today).days
                if 0 <= days_away <= _EARNINGS_DAYS:
                    if in_pf:
                        priority = "HIGH" if days_away <= 1 else "MEDIUM"
                        msg = (
                            f"EARNINGS in {days_away} day(s): {r.symbol} "
                            f"reports {e.next_earnings_date} — you hold this position"
                        )
                    else:
                        priority = "MEDIUM"
                        msg = (
                            f"EARNINGS in {days_away} day(s): {r.symbol} "
                            f"reports {e.next_earnings_date} — not held, no action needed "
                            f"(new entries already auto-blocked by earnings blackout)"
                        )
                    alerts.append(self._make(
                        AlertType.EARNINGS_SOON, r, msg, None, priority,
                    ))

            # 6. RSI_OVERBOUGHT  (portfolio only)
            if in_pf and r.rsi is not None and r.rsi > _RSI_OVERBOUGHT:
                alerts.append(self._make(
                    AlertType.RSI_OVERBOUGHT, r,
                    f"RSI OVERBOUGHT: {r.symbol} RSI={r.rsi:.1f} — "
                    f"consider trimming position",
                    None, "HIGH",
                ))

            # 7. RSI_OVERSOLD  (portfolio only)
            if in_pf and r.rsi is not None and r.rsi < _RSI_OVERSOLD:
                alerts.append(self._make(
                    AlertType.RSI_OVERSOLD, r,
                    f"RSI OVERSOLD: {r.symbol} RSI={r.rsi:.1f} — "
                    f"potential buy opportunity",
                    None, "HIGH",
                ))

        logger.info("AlertEvaluator: %d alert(s) triggered this cycle", len(alerts))
        return alerts

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _make(
        alert_type: AlertType,
        r,                         # ScanResult (duck-typed)
        message:    str,
        confidence: Optional[int],
        priority:   str,
    ) -> Alert:
        return Alert(
            alert_type = alert_type,
            symbol     = r.symbol,
            message    = message,
            confidence = confidence,
            price      = r.price,
            currency   = r.currency,
            timestamp  = datetime.now(),
            priority   = priority,
            source     = r.source,
        )

    def _in_portfolio(self, symbol: str) -> bool:
        if self._live_positions is None:
            return False
        return symbol.upper() in self._live_positions

    def _get_holding(self, symbol: str) -> Optional[tuple[float, float]]:
        """Return (shares, avg_cost) or None."""
        if self._live_positions is None:
            return None
        return self._live_positions.get(symbol.upper())
