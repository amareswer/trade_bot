"""
CNN Fear & Greed Index fetcher.

Fetches the current score (0–100) and label from CNN's public API.
Result is cached for 1 hour — the index updates infrequently.
Returns a safe fallback on any network or parse error.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_URL        = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_HEADERS    = {
    "User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)",
    "Referer":    "https://www.cnn.com/markets/fear-and-greed",
}
_TIMEOUT_S  = 8
_CACHE_TTL  = 3600  # 1 hour

_FALLBACK = None  # populated below

# Module-level cache: (FearGreedData, fetched_at_epoch)
_cache: tuple | None = None


@dataclass
class FearGreedData:
    score:        int
    label:        str
    last_updated: str


_FALLBACK = FearGreedData(score=50, label="Unknown", last_updated="unavailable")


def fetch_fear_greed() -> FearGreedData:
    """
    Return the current CNN Fear & Greed reading.
    Cached for 1 hour. Returns a neutral fallback on failure.
    """
    global _cache

    now = time.time()
    if _cache is not None and (now - _cache[1]) < _CACHE_TTL:
        logger.debug("Fear & Greed: serving from cache (age=%.0fs)", now - _cache[1])
        return _cache[0]

    try:
        resp = requests.get(_URL, headers=_HEADERS, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()

        fg = data.get("fear_and_greed", {})
        score = int(round(float(fg.get("score", 50))))
        label = fg.get("rating", "Unknown")
        ts    = fg.get("timestamp", "")
        # Trim to date portion if timestamp is long
        last_updated = ts[:19] if len(ts) >= 19 else ts or "n/a"

        result = FearGreedData(score=score, label=label, last_updated=last_updated)
        _cache = (result, now)
        logger.info("Fear & Greed: score=%d label=%s", score, label)
        return result

    except Exception as exc:
        logger.warning("Fear & Greed fetch failed: %s — using fallback", exc)
        return _FALLBACK
