"""
Shared yfinance rate-limit-aware fetch helper.

All yfinance calls in stock_bot should route through fetch_with_retry().
The module-level circuit breaker trips when any caller exhausts its retries
on YFRateLimitError — all subsequent calls return None immediately until the
cooldown expires, preventing every independent timer from rediscovering the ban.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Callable, Optional

from yfinance.exceptions import YFRateLimitError

logger = logging.getLogger(__name__)

# Unix timestamp after which fetches are allowed again.  0 = not active.
_rate_limit_until: float = 0

# 2026-07-23: non-rate-limit exceptions (e.g. "Fetch failed X:earnings:
# ['Earnings Date']") previously got ZERO retries — one glitch and the whole
# fetch gave up immediately, even though manual retesting showed these are
# usually transient and succeed within seconds. Rate limits get a long,
# escalating backoff (5/15/30s) because the server is actively throttling;
# this is a short, fixed retry for everything else, since it isn't throttling.
_GENERIC_RETRY_DELAY_S = 2


def is_rate_limited() -> bool:
    """Return True while the circuit breaker cooldown is active."""
    return time.time() < _rate_limit_until


def trip_circuit_breaker(cooldown_seconds: int | None = None) -> None:
    """
    Activate the circuit breaker for `cooldown_seconds`.
    All fetch_with_retry calls return None immediately until the cooldown expires.
    Reads RATE_LIMIT_COOLDOWN_SECONDS from env if cooldown_seconds is None (default 120).
    """
    global _rate_limit_until
    if cooldown_seconds is None:
        cooldown_seconds = int(os.getenv("RATE_LIMIT_COOLDOWN_SECONDS", "120"))
    _rate_limit_until = time.time() + cooldown_seconds
    until_str = datetime.fromtimestamp(_rate_limit_until).strftime("%H:%M:%S")
    logger.warning(
        "yfinance circuit breaker tripped — all fetches paused until %s (%ds cooldown)",
        until_str,
        cooldown_seconds,
    )


def fetch_with_retry(
    fetch_fn: Callable[[], Any],
    label:        str,
    max_attempts: int              = 3,
    delays:       list[int] | None = None,
) -> Optional[Any]:
    """
    Call fetch_fn() with retry on YFRateLimitError.

    fetch_fn     : zero-arg callable that performs the actual yf call
    label        : logged on every warning/error (symbol name or description)
    max_attempts : total attempts before giving up (default 3)
    delays       : seconds to wait before each retry [before attempt 1, before attempt 2, ...].
                   Defaults to env RATE_LIMIT_DELAYS ("5,15,30") or [5, 15, 30].

    Circuit breaker: when all attempts are exhausted on YFRateLimitError,
    trip_circuit_breaker() is called and all subsequent calls return None
    immediately for RATE_LIMIT_COOLDOWN_SECONDS (default 120s).

    Non-rate-limit exceptions: logged, returns None immediately — no retry.
    """
    if is_rate_limited():
        remaining = max(0, int(_rate_limit_until - time.time()))
        logger.debug(
            "Skipping %s — circuit breaker active for %ds more", label, remaining
        )
        return None

    if delays is None:
        raw = os.getenv("RATE_LIMIT_DELAYS", "5,15,30")
        try:
            delays = [int(x.strip()) for x in raw.split(",")]
        except ValueError:
            delays = [5, 15, 30]

    for attempt in range(max_attempts):
        try:
            return fetch_fn()
        except YFRateLimitError:
            if attempt < max_attempts - 1:
                delay = delays[min(attempt, len(delays) - 1)]
                logger.warning(
                    "Rate limited fetching %s (attempt %d/%d), waiting %ds",
                    label, attempt + 1, max_attempts, delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Rate limit: giving up on %s after %d attempts", label, max_attempts
                )
                trip_circuit_breaker()
                return None
        except Exception as exc:
            if attempt < max_attempts - 1:
                logger.warning(
                    "Fetch failed %s (attempt %d/%d): %s — retrying in %ds",
                    label, attempt + 1, max_attempts, exc, _GENERIC_RETRY_DELAY_S,
                )
                time.sleep(_GENERIC_RETRY_DELAY_S)
            else:
                logger.warning(
                    "Fetch failed %s after %d attempts: %s", label, max_attempts, exc,
                )
                return None

    return None
