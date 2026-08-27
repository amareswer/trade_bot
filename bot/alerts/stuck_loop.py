"""
Generic stuck-loop detector — shared by both bots.

2026-08-27 native-stop deadlock: `execute()` rejected ~200 SL/TP exits over
8 minutes with `EOrder:Insufficient funds`, retrying every ~30s. The fix added
a `exit_fail_count` edge-alert for that ONE path — but the codebase has a dozen
operations that could get stuck the same way (order placement, Kraken auth,
candle fetch, native-stop sync, …), each needing its own hand-rolled counter.

This is the generic primitive: `record(key, ok)` per attempt. A key that fails
`threshold` times in a row fires one alert, then re-alerts on an escalating
cadence so a persistent problem keeps nagging without spamming every tick. Any
single success resets that key. Keys with no activity for `ttl_s` are forgotten
so a transient blip doesn't linger in the snapshot forever.

Deliberately error-string-agnostic: it catches the NEXT stuck-loop class we
haven't seen, not just the ones we've already special-cased.
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class StuckLoopDetector:
    """Thread-safe "is the same operation failing over and over" watchdog.

    alert_fn(message: str) is called on escalation edges only. It is wrapped in
    a try/except here — a broken alerter must never take down the caller's loop.
    """

    def __init__(
        self,
        alert_fn: Callable[[str], None],
        *,
        threshold: int = 5,
        re_alert_every: int = 20,
        ttl_s: float = 3600.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        if re_alert_every < 1:
            raise ValueError("re_alert_every must be >= 1")
        self._alert_fn = alert_fn
        self._threshold = threshold
        self._re_alert_every = re_alert_every
        self._ttl_s = ttl_s
        self._time_fn = time_fn
        self._lock = threading.Lock()
        # key -> {"fails": int, "last": float, "last_detail": str}
        self._state: dict[str, dict] = {}

    # ── recording ────────────────────────────────────────────────────────────

    def record(self, key: str, ok: bool, detail: str = "") -> None:
        """One attempt outcome for `key`. Fires alert_fn on an escalation edge."""
        now = self._time_fn()
        fire_msg: str | None = None
        with self._lock:
            self._prune(now)
            if ok:
                self._state.pop(key, None)
                return
            st = self._state.setdefault(key, {"fails": 0, "last": now, "last_detail": ""})
            st["fails"] += 1
            st["last"] = now
            st["last_detail"] = detail or st["last_detail"]
            n = st["fails"]
            if n == self._threshold or (
                n > self._threshold and (n - self._threshold) % self._re_alert_every == 0
            ):
                _d = st["last_detail"]
                _suffix = f" — {_d}" if _d else ""
                fire_msg = (
                    f"STUCK LOOP: '{key}' has failed {n} times in a row{_suffix}. "
                    f"The same operation keeps failing; something needs a manual look."
                )
        if fire_msg is not None:
            try:
                self._alert_fn(fire_msg)
            except Exception:   # noqa: BLE001 — an alerter fault must not propagate
                pass

    # ── inspection ───────────────────────────────────────────────────────────

    def failing_keys(self) -> dict[str, int]:
        """{key: consecutive_failures} for keys AT or past the alert threshold."""
        with self._lock:
            self._prune(self._time_fn())
            return {
                k: v["fails"] for k, v in self._state.items()
                if v["fails"] >= self._threshold
            }

    def snapshot(self) -> dict[str, int]:
        """{key: consecutive_failures} for every key with a live failure streak
        (any count >= 1). For the health digest / dashboard."""
        with self._lock:
            self._prune(self._time_fn())
            return {k: v["fails"] for k, v in self._state.items()}

    # ── internal ─────────────────────────────────────────────────────────────

    def _prune(self, now: float) -> None:
        stale = [k for k, v in self._state.items() if now - v["last"] > self._ttl_s]
        for k in stale:
            del self._state[k]
