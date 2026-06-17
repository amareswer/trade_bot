"""
Alert notifier — Phase 5.

Delivers Alert objects via three channels:
  terminal  — always active (colorama box, HIGH=red, MEDIUM=yellow)
  email     — opt-in via ALERT_EMAIL_ENABLED (Gmail SMTP, stdlib only)
  desktop   — opt-in via ALERT_DESKTOP_ENABLED (plyer, install separately)

Never crashes the scan loop — all delivery errors are caught and logged.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from colorama import Fore, Style

from stock_bot.alerts.alert import Alert

if TYPE_CHECKING:
    from stock_bot.config import StockConfig

logger = logging.getLogger(__name__)

# Warn once if plyer is absent, then stay silent
_plyer_warned    = False
_plyer_available = False
try:
    from plyer import notification as _plyer_notification
    _plyer_available = True
except ImportError:
    pass

_BOX_WIDTH = 50


def _box_line(text: str = "", fill: str = " ") -> str:
    """Pad text to fixed box width."""
    padded = f"  {text}"
    return f"║{padded:<{_BOX_WIDTH - 2}}║"


class AlertNotifier:
    """
    Delivers alerts to terminal and optionally to email and desktop.
    Instantiated once at startup.
    """

    def __init__(self, config: StockConfig) -> None:
        self._cfg = config

    def notify(self, alerts: list[Alert]) -> None:
        if not alerts:
            return
        self._terminal(alerts)
        if self._cfg.alert_email_enabled:
            self._email(alerts)
        if self._cfg.alert_desktop_enabled:
            self._desktop(alerts)

    # ── Terminal ─────────────────────────────────────────────────────────────

    def _terminal(self, alerts: list[Alert]) -> None:
        high_alerts   = [a for a in alerts if a.priority == "HIGH"]
        medium_alerts = [a for a in alerts if a.priority == "MEDIUM"]

        border_top    = "╔" + "═" * (_BOX_WIDTH - 2) + "╗"
        border_mid    = "╠" + "═" * (_BOX_WIDTH - 2) + "╣"
        border_bottom = "╚" + "═" * (_BOX_WIDTH - 2) + "╝"

        print()
        print(f"  {border_top}")
        print(f"  {_box_line(f'🔔 ALERTS — {len(alerts)} triggered')}")
        print(f"  {border_mid}")

        ordered = high_alerts + medium_alerts
        for i, alert in enumerate(ordered):
            if alert.priority == "HIGH":
                col   = Fore.RED
                icon  = "🔴"
            else:
                col   = Fore.YELLOW
                icon  = "🟡"

            type_str   = alert.alert_type.value
            price_str  = f"${alert.price:,.2f} {alert.currency}"
            source_str = "top mover" if getattr(alert, "source", "watchlist") == "universe" else "watchlist"

            print(f"  {_box_line(f'{col}{icon} {alert.priority}  · {type_str} · {source_str}{Style.RESET_ALL}')}")
            print(f"  {_box_line(f'{alert.symbol} @ {price_str}')}")

            # Wrap message at ~44 chars
            msg = alert.message
            while msg:
                chunk, msg = msg[:44], msg[44:]
                print(f"  {_box_line(f'  {chunk}')}")

            if i < len(ordered) - 1:
                print(f"  {border_mid}")

        print(f"  {border_bottom}")
        print()

    # ── Email ─────────────────────────────────────────────────────────────────

    def _email(self, alerts: list[Alert]) -> None:
        high_alerts = [a for a in alerts if a.priority == "HIGH"]
        if not high_alerts:
            return

        from_addr = self._cfg.alert_email_from.strip()
        to_addr   = self._cfg.alert_email_to.strip()
        password  = self._cfg.alert_email_password.strip()

        if not from_addr or not to_addr or not password:
            logger.warning("Alert email skipped — ALERT_EMAIL_FROM/TO/PASSWORD not configured")
            return

        subject = f"🔔 Stock Bot Alert — {len(high_alerts)} HIGH priority"
        body_lines = ["Stock Bot — HIGH Priority Alerts", "=" * 40, ""]
        for a in high_alerts:
            body_lines.append(f"[{a.alert_type.value}]  {a.symbol} @ ${a.price:,.2f} {a.currency}")
            body_lines.append(a.message)
            body_lines.append(f"Time: {a.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
            body_lines.append("")
        body_lines.append("—\nThis is an automated alert from Stock Bot. Not financial advice.")

        msg = MIMEText("\n".join(body_lines))
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = to_addr

        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
                server.login(from_addr, password)
                server.sendmail(from_addr, to_addr, msg.as_string())
            logger.info("Alert email sent to %s (%d HIGH alerts)", to_addr, len(high_alerts))
        except Exception as exc:
            logger.warning("Alert email failed: %s", exc)

    # ── Desktop ───────────────────────────────────────────────────────────────

    def _desktop(self, alerts: list[Alert]) -> None:
        global _plyer_warned

        if not _plyer_available:
            if not _plyer_warned:
                logger.warning(
                    "Desktop alerts disabled — plyer not installed. "
                    "Run: pip install plyer"
                )
                _plyer_warned = True
            return

        for alert in alerts:
            if alert.priority != "HIGH":
                continue
            try:
                _plyer_notification.notify(
                    title   = f"Stock Bot — {alert.alert_type.value}",
                    message = alert.message[:100],
                    timeout = 8,
                )
            except Exception as exc:
                logger.warning("Desktop notification failed for %s: %s", alert.symbol, exc)
