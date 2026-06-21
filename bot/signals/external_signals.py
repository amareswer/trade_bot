"""
External market signals — Fear & Greed Index and BTC Funding Rate.

Both signals are fetched from free public APIs with TTL caching.
They act as BUY gates only — SELL is never blocked.

Usage:
    gate = ExternalSignalGate(cfg.signals)
    approved, reason = gate.approve_buy()
    if not approved:
        raw_signal = Signal.HOLD
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TTL-cached fetch helpers
# ---------------------------------------------------------------------------

def _fetch_fear_greed() -> Optional[dict]:
    """
    Fetch latest Fear & Greed index from alternative.me.
    Returns {"value": int 0-100, "classification": str} or None on failure.
    Updates daily (~00:00 UTC).
    """
    try:
        import requests
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=5,
        )
        r.raise_for_status()
        entry = r.json()["data"][0]
        return {
            "value":          int(entry["value"]),
            "classification": entry.get("value_classification", ""),
        }
    except Exception as exc:
        logger.warning("Fear & Greed fetch failed: %s", exc)
        return None


def _fetch_funding_rate(symbol: str = "BTCUSDT") -> Optional[float]:
    """
    Fetch latest perpetual futures funding rate from Binance (public endpoint).
    Positive = longs pay shorts (crowded long); negative = shorts pay longs.
    Updates every 8 hours.
    """
    try:
        import requests
        r = requests.get(
            f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1",
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            return float(data[0]["fundingRate"])
        return None
    except Exception as exc:
        logger.warning("Funding rate fetch failed (%s): %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

@dataclass
class ExternalSignalsConfig:
    fng_enabled:          bool  = True
    fng_bear_max:         float = 75.0   # block BUY when FNG > 75 (extreme greed)
    fng_bull_min:         float = 0.0    # require FNG >= this (0 = disabled)
    fng_cache_seconds:    int   = 3600   # refresh once per hour (updates daily)
    funding_enabled:      bool  = True
    funding_symbol:       str   = "BTCUSDT"
    funding_max:          float = 0.0005 # block BUY when funding > 0.05% (crowded long)
    funding_cache_seconds: int  = 3600   # refresh once per hour (updates every 8h)


class ExternalSignalGate:
    """
    Stateful gate that caches external signals and approves/rejects BUY signals.

    Thread-safe for single-threaded bot loop (no lock needed).
    Gracefully degrades — if APIs are unreachable, BUY is allowed (fail-open).
    """

    def __init__(self, config: ExternalSignalsConfig) -> None:
        self._cfg = config

        self._fng:              Optional[dict]  = None
        self._fng_fetched_at:   float           = 0.0

        self._funding:          Optional[float] = None
        self._funding_fetched_at: float         = 0.0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def approve_buy(self) -> tuple[bool, str]:
        """
        Return (approved, reason).
        approved=True  → BUY may proceed
        approved=False → BUY should be converted to HOLD
        Fail-open: if data unavailable, return (True, "")
        """
        self._refresh()

        # Fear & Greed gate
        if self._cfg.fng_enabled and self._fng is not None:
            score = self._fng["value"]
            cls   = self._fng["classification"]
            if score > self._cfg.fng_bear_max:
                return False, f"FNG={score} ({cls}) — extreme greed, skip BUY"
            if self._cfg.fng_bull_min > 0 and score < self._cfg.fng_bull_min:
                return False, f"FNG={score} ({cls}) — below required floor {self._cfg.fng_bull_min}"

        # Funding rate gate
        if self._cfg.funding_enabled and self._funding is not None:
            if self._funding > self._cfg.funding_max:
                return False, (
                    f"Funding={self._funding*100:.4f}% > {self._cfg.funding_max*100:.4f}%"
                    " — longs overcrowded, skip BUY"
                )

        return True, ""

    def status(self) -> dict:
        """Return current cached signal values for dashboard/logging."""
        return {
            "fng_value":          self._fng["value"]          if self._fng      else None,
            "fng_classification": self._fng["classification"] if self._fng      else None,
            "funding_rate":       self._funding,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        now = time.monotonic()

        if self._cfg.fng_enabled:
            if now - self._fng_fetched_at >= self._cfg.fng_cache_seconds:
                result = _fetch_fear_greed()
                if result is not None:
                    self._fng = result
                    logger.info(
                        "FNG refreshed: %d (%s)",
                        result["value"], result["classification"],
                    )
                self._fng_fetched_at = now

        if self._cfg.funding_enabled:
            if now - self._funding_fetched_at >= self._cfg.funding_cache_seconds:
                result_f = _fetch_funding_rate(self._cfg.funding_symbol)
                if result_f is not None:
                    self._funding = result_f
                    logger.info(
                        "Funding rate refreshed: %.4f%%", result_f * 100,
                    )
                self._funding_fetched_at = now
