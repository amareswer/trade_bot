"""
TWS connection monitor — pure state machine, no I/O (unit-tested directly,
same pattern as ExitPolicy and _audit_due).

Why: with STOCK_EXECUTOR=ibkr the bot is only as alive as the TWS session,
and classic TWS auto-logs-off nightly unless configured otherwise. Order
placement already fails visibly in the log, but nothing tells the USER —
there is no working Telegram/email channel (2026-07-17 audit). This monitor
turns a sustained disconnect into one loud local alert (log ERROR + terminal
+ desktop notification) and one recovery notice, instead of a silent log
trickle. The HEARTBEAT_TWS_URL ping (bot/alerts/heartbeat.py) provides the
remote email leg via healthchecks.io; this provides the immediate local leg.

State rules:
  - connected → no alert, resets any pending outage
  - disconnected < alert_after_s → still quiet (TWS restarts and the
    executor's auto-reconnect routinely cause sub-minute blips)
  - disconnected ≥ alert_after_s → "down" exactly once per outage
  - reconnect after a "down" was fired → "recovered" exactly once
"""
from __future__ import annotations


class TwsConnectionMonitor:
    def __init__(self, alert_after_s: float = 600.0) -> None:
        self.alert_after_s = alert_after_s
        self._down_since: float | None = None
        self._alerted = False

    def update(self, connected: bool, now: float) -> str | None:
        """Feed one connectivity sample; returns "down", "recovered", or None."""
        if connected:
            was_alerted = self._alerted
            self._down_since = None
            self._alerted = False
            return "recovered" if was_alerted else None

        if self._down_since is None:
            self._down_since = now
            return None
        if not self._alerted and (now - self._down_since) >= self.alert_after_s:
            self._alerted = True
            return "down"
        return None

    def down_for(self, now: float) -> float | None:
        """Seconds the current outage has lasted, or None when connected."""
        return None if self._down_since is None else now - self._down_since
