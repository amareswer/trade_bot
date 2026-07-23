"""
Loop-liveness tracker — shared by both bots' heartbeat wiring.

2026-07-22 incident: the stock bot's swing-book worker thread hung silently
for 5+ hours (no exception, no crash) while the heartbeat kept reporting
"healthy" the whole time — heartbeat only ever checked process-alive (crypto)
or IBKR-socket-alive (stock), never whether the actual work loop was still
making progress. A thread can be technically running while permanently stuck.

Call touch() from inside the loop being monitored (once per full outer-loop
tick for the crypto bot; once per symbol processed for the stock bot, since
a single AI call has been observed taking up to ~800s and a full multi-symbol
scan cycle can legitimately run many minutes) — then wire
healthy_fn=lambda: tracker.is_alive(max_stale_s) into start_heartbeat_thread.
A stale tracker means the loop stopped progressing; the heartbeat stops
pinging, and healthchecks.io's existing grace-period alerting takes it from
there (same dead-man's-switch pattern as every other heartbeat check here).
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class LivenessTracker:
    """Thread-safe "when did the monitored loop last make progress" clock."""

    def __init__(self, time_fn: Callable[[], float] = time.monotonic) -> None:
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._last_touch = time_fn()

    def touch(self) -> None:
        """Call from inside the monitored loop to record a progress mark."""
        with self._lock:
            self._last_touch = self._time_fn()

    def seconds_since_touch(self) -> float:
        with self._lock:
            return self._time_fn() - self._last_touch

    def is_alive(self, max_stale_s: float) -> bool:
        """True if touch() was called within the last max_stale_s seconds."""
        return self.seconds_since_touch() < max_stale_s
