"""
Heartbeat pings — dead-man's-switch monitoring via healthchecks.io (or any
URL that expects a periodic GET).

Why this exists (2026-07-17): neither bot has a working outbound alert
channel — TELEGRAM_* was never configured in .env and the stock bot's email
fields are empty — so a dead bot is only discovered by looking at it (it
happened 2026-07-05: crypto bot down for hours, position risk unmonitored).
A heartbeat inverts the direction: the bot pings OUT every interval; the
monitoring service emails when pings STOP. No credentials live in this repo —
the ping URL itself is the only secret, set via env:

  Crypto bot   (.env):            HEARTBEAT_URL=https://hc-ping.com/<uuid>
  Stock bot    (stock_bot/.env):  HEARTBEAT_URL=...   (process alive)
                                  HEARTBEAT_TWS_URL=... (pinged only while
                                  the IBKR connection is up — separates
                                  "bot died" from "TWS logged off")

Unset/empty URL = feature off (no thread started). Pings fail silently —
monitoring must never affect trading.

Used by both bots (same cross-package pattern as stock_bot importing
bot.strategy).
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.request
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def ping(url: str, timeout: float = 10.0) -> bool:
    """Fire one GET at the heartbeat URL. Never raises."""
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        logger.debug("Heartbeat ping failed (%s): %s", url[:40], exc)
        return False


def beat(url: str, healthy_fn: Optional[Callable[[], bool]] = None) -> bool:
    """One heartbeat iteration: ping unless healthy_fn says not to.

    healthy_fn errors count as unhealthy (no ping) — a broken health check
    must not keep the monitor quiet about a real problem.
    """
    if healthy_fn is not None:
        try:
            if not healthy_fn():
                return False
        except Exception as exc:
            logger.debug("Heartbeat healthy_fn error: %s", exc)
            return False
    return ping(url)


def start_heartbeat_thread(
    url: str,
    interval_s: int = 60,
    healthy_fn: Optional[Callable[[], bool]] = None,
    name: str = "heartbeat",
) -> Optional[threading.Thread]:
    """Start a daemon thread pinging url every interval_s. Returns the
    thread, or None (feature off) when url is empty."""
    url = (url or "").strip()
    if not url:
        logger.info("Heartbeat '%s' disabled (no URL configured)", name)
        return None

    def _loop() -> None:
        while True:
            beat(url, healthy_fn)
            time.sleep(interval_s)

    t = threading.Thread(target=_loop, daemon=True, name=name)
    t.start()
    logger.info("Heartbeat '%s' started (interval=%ss)", name, interval_s)
    return t
