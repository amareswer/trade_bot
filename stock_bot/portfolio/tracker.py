"""
Portfolio tracker — Phase 4.5.

Parses the PORTFOLIO env var and matches positions against live scan results
to compute current values and unrealized P&L.

PORTFOLIO format: "SYMBOL:SHARES:AVG_COST,..."
Example:          "SHOP.TO:10:85.20,AAPL:5:195.00,NVDA:3:420.00"

No live broker connection — all prices come from the scan cycle's yfinance data.
Empty or missing PORTFOLIO → all methods return None/empty, no crash.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from stock_bot.dashboard.renderer import ScanResult

from stock_bot.ai.verdict import AIVerdict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PortfolioPosition:
    symbol:         str
    shares:         float
    avg_cost:       float
    current_price:  float
    current_value:  float         # shares × current_price
    total_cost:     float         # shares × avg_cost
    gain_loss:      float         # current_value - total_cost
    gain_loss_pct:  float         # (gain_loss / total_cost) × 100
    currency:       str           # "CAD" (.TO) | "USD"
    verdict:        Optional[AIVerdict]


@dataclass
class PortfolioSummary:
    positions:          list[PortfolioPosition]
    total_invested:     float
    total_value:        float
    total_gain_loss:    float
    total_gain_loss_pct: float


@dataclass
class PaperTrade:
    """One filled paper-trade record — written to paper_trades.csv and shown in dashboard."""
    timestamp:      str    # "YYYY-MM-DD HH:MM:SS"
    symbol:         str
    side:           str    # "BUY" | "SELL"
    shares:         float
    price:          float
    total_value:    float
    cash_remaining: float
    reason:         str    # e.g. "BUY 72% SWING"


@dataclass
class PaperSummary:
    """Full paper-portfolio state for one scan cycle, passed to the dashboard renderer."""
    cash:           float
    starting_cash:  float
    positions:      list[PortfolioPosition]  # open positions with live P&L
    realized_pnl:   float
    unrealized_pnl: float
    total_value:    float             # cash + open-position market value
    recent_trades:  list[PaperTrade]  # last 10 filled, newest first


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class PortfolioTracker:
    """
    Parses PORTFOLIO config once at startup, builds a PortfolioSummary
    each scan cycle by matching against live scan results.
    """

    def __init__(self, portfolio_str: str) -> None:
        # List of (symbol, shares, avg_cost) tuples
        self._holdings: list[tuple[str, float, float]] = []

        raw = (portfolio_str or "").strip()
        if not raw:
            return

        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) != 3:
                logger.warning("Portfolio: skipping malformed entry %r (expected SYMBOL:SHARES:COST)", entry)
                continue
            try:
                sym      = parts[0].strip().upper()
                shares   = float(parts[1].strip())
                avg_cost = float(parts[2].strip())
            except ValueError as exc:
                logger.warning("Portfolio: skipping entry %r — %s", entry, exc)
                continue

            if shares <= 0 or shares > 100_000:
                logger.warning("Portfolio: invalid shares for %s: %s — skipping", sym, shares)
                continue
            if avg_cost <= 0 or avg_cost > 100_000:
                logger.warning("Portfolio: invalid avg_cost for %s: %s — skipping", sym, avg_cost)
                continue

            self._holdings.append((sym, int(shares), avg_cost))

        if self._holdings:
            logger.info("Portfolio loaded: %d position(s): %s",
                        len(self._holdings),
                        ", ".join(f"{s}×{n}" for s, n, _ in self._holdings))

    @property
    def has_positions(self) -> bool:
        return len(self._holdings) > 0

    def build_summary(self, scan_results: list[ScanResult]) -> PortfolioSummary | None:
        """
        Match holdings against current scan results to compute P&L.
        Symbols not in scan_results are silently skipped (watchlist may differ).
        Returns None when no positions are configured or none matched.
        """
        if not self.has_positions:
            return None

        # Build lookup maps from scan results
        price_map:    dict[str, float]    = {}
        verdict_map:  dict[str, AIVerdict]= {}
        currency_map: dict[str, str]      = {}
        for r in scan_results:
            key = r.symbol.upper()
            price_map[key]    = r.price
            verdict_map[key]  = r.verdict
            currency_map[key] = r.currency

        positions: list[PortfolioPosition] = []
        for sym, shares, avg_cost in self._holdings:
            if sym not in price_map:
                logger.debug("Portfolio: %s not in this scan cycle — skipped", sym)
                continue

            current_price = price_map[sym]
            current_value = shares * current_price
            total_cost    = shares * avg_cost
            gain_loss     = current_value - total_cost
            gain_loss_pct = (gain_loss / total_cost * 100) if total_cost != 0 else 0.0
            currency      = currency_map.get(sym, "CAD" if sym.endswith(".TO") else "USD")

            positions.append(PortfolioPosition(
                symbol        = sym,
                shares        = shares,
                avg_cost      = avg_cost,
                current_price = current_price,
                current_value = current_value,
                total_cost    = total_cost,
                gain_loss     = gain_loss,
                gain_loss_pct = round(gain_loss_pct, 2),
                currency      = currency,
                verdict       = verdict_map.get(sym),
            ))

        if not positions:
            return None

        total_invested     = sum(p.total_cost    for p in positions)
        total_value        = sum(p.current_value for p in positions)
        total_gain_loss    = total_value - total_invested
        total_gain_loss_pct = (total_gain_loss / total_invested * 100) if total_invested != 0 else 0.0

        return PortfolioSummary(
            positions           = positions,
            total_invested      = total_invested,
            total_value         = total_value,
            total_gain_loss     = total_gain_loss,
            total_gain_loss_pct = round(total_gain_loss_pct, 2),
        )
