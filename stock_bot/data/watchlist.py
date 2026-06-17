"""
Watchlist — symbols to monitor each loop tick.

DEFAULT_WATCHLIST covers a mix of TSX blue-chips and high-liquidity US names.
Override at runtime via WATCHLIST env var (comma-separated).
"""
from __future__ import annotations

DEFAULT_WATCHLIST: list[str] = [
    # TSX
    "SHOP.TO",   # Shopify — Canadian tech bellwether
    "RY.TO",     # Royal Bank — largest Canadian bank
    "AC.TO",     # Air Canada — cyclical / macro indicator
    # US
    "AAPL",      # Apple
    "NVDA",      # Nvidia — AI/GPU proxy
]


def get_watchlist(symbols_str: str | None = None) -> list[str]:
    """
    Parse a comma-separated string into a symbol list.
    Falls back to DEFAULT_WATCHLIST when the string is absent or empty.
    """
    if symbols_str:
        parsed = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
        if parsed:
            return parsed
    return DEFAULT_WATCHLIST
