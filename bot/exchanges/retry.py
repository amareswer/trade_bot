"""
Retry helper for transient Kraken/ccxt network hiccups.

Wraps a zero-arg callable, retrying on any exception up to `attempts` times
with a fixed delay between tries. Raises the last exception if every attempt
fails — callers keep their existing except/fallback logic unchanged, they
just see fewer transient blips reach it.
"""

import logging
import time

logger = logging.getLogger(__name__)

_DEFAULT_ATTEMPTS = 3
_DEFAULT_DELAY_S = 2.0


def fetch_with_retry(
    fn,
    *,
    attempts: int = _DEFAULT_ATTEMPTS,
    delay_s: float = _DEFAULT_DELAY_S,
    label: str = "exchange call",
):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                    label, attempt, attempts, exc, delay_s,
                )
                time.sleep(delay_s)
    raise last_exc
