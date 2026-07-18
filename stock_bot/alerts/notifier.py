"""
Alert notifier — Phase 5.

Delivers Alert objects via three channels:
  terminal  — always active (colorama box, HIGH=red, MEDIUM=yellow)
  email     — opt-in via ALERT_EMAIL_ENABLED (Gmail SMTP, stdlib only)
  desktop   — opt-in via ALERT_DESKTOP_ENABLED (plyer, install separately)

Also schedules a weekly summary email every Sunday at 18:00 local time
(gate status, fast validator stats, swing paper stats, regime, TSX warnings).
The timer is self-rearming — it reschedules itself each firing.

Never crashes the scan loop — all delivery errors are caught and logged.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import threading
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from colorama import Fore, Style

from stock_bot.alerts.alert import Alert
from stock_bot.data.yf_client import fetch_with_retry

if TYPE_CHECKING:
    from stock_bot.config import StockConfig

logger = logging.getLogger(__name__)

_STOCK_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Warn once if plyer is absent, then stay silent
_plyer_warned    = False
_plyer_available = False
try:
    from plyer import notification as _plyer_notification
    _plyer_available = True
except ImportError:
    pass

_BOX_WIDTH = 50


def _make_telegram(root_env: dict | None = None):
    """Build the shared TelegramAlerter from the ROOT .env credentials
    (2026-07-17 — one token/chat source for both bots; keys in the process
    environment, e.g. from stock_bot/.env, override the root file).
    Returns None when Telegram is not enabled or the crypto package is
    unavailable — callers must treat None as "channel off"."""
    try:
        from bot.alerts.telegram import TelegramAlerter
    except ImportError:
        return None
    if root_env is None:
        try:
            from dotenv import dotenv_values
            root_env = dotenv_values(
                os.path.join(os.path.dirname(_STOCK_BOT_DIR), ".env")
            )
        except Exception:
            root_env = {}

    def _get(key: str) -> str:
        return (os.getenv(key) or root_env.get(key) or "").strip()

    if _get("TELEGRAM_ENABLED").lower() != "true":
        return None
    token, chat_id = _get("TELEGRAM_BOT_TOKEN"), _get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None
    return TelegramAlerter(token, chat_id, enabled=True)


def _box_line(text: str = "", fill: str = " ") -> str:
    """Pad text to fixed box width."""
    padded = f"  {text}"
    return f"║{padded:<{_BOX_WIDTH - 2}}║"


def _seconds_until_next_sunday_18() -> float:
    """
    Return seconds from now until the next Sunday at 18:00 local time.
    If it is already Sunday and past 18:00, schedules for the following Sunday.
    Minimum 60 s so the timer never fires immediately.
    """
    now  = datetime.now()
    days_ahead = (6 - now.weekday()) % 7   # Sunday = weekday 6
    if days_ahead == 0 and now.hour >= 18:
        days_ahead = 7
    target = now.replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    return max(60.0, (target - now).total_seconds())


class AlertNotifier:
    """
    Delivers alerts to terminal and optionally to email and desktop.
    Instantiated once at startup.
    Call start_weekly_summary() after __init__ to arm the Sunday 18:00 timer.
    """

    def __init__(self, config: StockConfig, telegram_factory=_make_telegram) -> None:
        self._cfg   = config
        self._timer: threading.Timer | None = None
        self._telegram = telegram_factory()
        if self._telegram is not None:
            logger.info("Stock bot Telegram channel active (shared amaresh_tradebot)")

    def start_weekly_summary(self) -> None:
        """
        Arm a self-rearming threading.Timer that fires every Sunday at 18:00
        local time and sends the weekly summary email.

        Safe to call even when email is not configured — the callback will log
        DEBUG and return without sending.  Never raises.
        """
        delay = _seconds_until_next_sunday_18()
        self._timer = threading.Timer(delay, self._weekly_summary_callback)
        self._timer.daemon = True
        self._timer.start()
        next_fire = datetime.now() + timedelta(seconds=delay)
        logger.info(
            "Weekly summary timer armed — next fire: %s (%.0fh away)",
            next_fire.strftime("%Y-%m-%d %H:%M"),
            delay / 3600,
        )

    def _weekly_summary_callback(self) -> None:
        """Timer callback — send email then rearm for next Sunday."""
        try:
            self._tws_relogin_reminder()
        except Exception as exc:
            logger.warning("TWS reminder error: %s", exc)
        try:
            self._send_weekly_summary()
        except Exception as exc:
            logger.warning("Weekly summary callback error: %s", exc)
        finally:
            # Always rearm, even if this firing failed
            self.start_weekly_summary()

    def _tws_relogin_reminder(self) -> None:
        """Sunday 18:00 piggyback: IBKR forces a weekly TWS re-login on
        Sundays — without it, every Monday order fails until someone logs
        in. Only relevant when the IBKR executor is active."""
        if os.getenv("STOCK_EXECUTOR", "paper").strip().lower() != "ibkr":
            return
        self.ops_alert(
            "TWS weekly re-login due",
            "IBKR requires a weekly TWS re-login on Sundays. Check that TWS "
            "is running and logged in before Monday's open, or every stock "
            "bot order will fail.",
        )

    def startup(self, executor_type: str, cash: float, positions: int) -> None:
        """Boot confirmation to Telegram (parity with the crypto bot's
        alerter.startup — a silent boot is indistinguishable from a broken
        channel). No-op when Telegram is off. Never raises."""
        if self._telegram is None:
            return
        try:
            self._telegram.message(
                f"🤖 Stock Bot started\n"
                f"Executor: {executor_type}\n"
                f"Cash: ${cash:,.2f} | Positions: {positions}"
            )
        except Exception as exc:
            logger.warning("Telegram startup relay failed: %s", exc)

    def ops_alert(self, title: str, message: str) -> None:
        """Operational alert: log WARNING + terminal + desktop notification
        + Telegram (channel configured 2026-07-17; healthchecks.io heartbeat
        remains the dead-bot leg). Never raises."""
        logger.warning("OPS ALERT — %s: %s", title, message)
        try:
            print(f"\n{Fore.YELLOW}⚠ {title}: {message}{Style.RESET_ALL}\n", flush=True)
        except Exception:
            pass
        if self._telegram is not None:
            try:
                self._telegram.message(f"⚠️ Stock Bot — {title}\n{message}")
            except Exception as exc:
                logger.warning("Telegram ops relay failed: %s", exc)
        if _plyer_available:
            try:
                _plyer_notification.notify(
                    title=f"Stock Bot — {title}",
                    message=message[:200],
                    timeout=10,
                )
            except Exception as exc:
                logger.warning("Desktop notification failed: %s", exc)

    def _send_weekly_summary(self) -> None:
        from_addr = self._cfg.alert_email_from.strip()
        to_addr   = self._cfg.alert_email_to.strip()
        password  = self._cfg.alert_email_password.strip()

        if not self._cfg.alert_email_enabled or not from_addr or not to_addr or not password:
            logger.debug("Weekly summary: email not configured")
            return

        today = datetime.now().strftime("%Y-%m-%d")

        # ── 1. Gate status ────────────────────────────────────────────────────
        try:
            from stock_bot.analysis.accuracy_tracker import LiveTradingGate
            gate_data  = LiveTradingGate().get_gate_status()
            remaining  = gate_data["remaining"]
            gate_lines = []
            for g in gate_data["gates"]:
                gate_lines.append(
                    f"  Gate {g['gate']} [{g['status']:<7}]: {g.get('detail', '')}"
                )
        except Exception as exc:
            remaining  = -1
            gate_lines = [f"  Gate status unavailable: {exc}"]

        # ── 2. Fast validator stats ───────────────────────────────────────────
        try:
            from stock_bot.fast_validator import FastValidatorReport
            fv_stats = FastValidatorReport().get_stats()
            fv_lines = [
                f"  Completed trades : {fv_stats['completed']}",
                f"  Win rate         : {fv_stats['win_rate']:.1f}%",
                f"  Avg hold (hours) : {fv_stats['avg_hold_hours']:.1f}h",
            ]
        except Exception as exc:
            fv_lines = [f"  Fast validator stats unavailable: {exc}"]

        # ── 3. Position book stats (active executor: sim paper OR IBKR paper) ──
        try:
            from stock_bot.analysis.paper_report import load_active_book_state
            ps = load_active_book_state()
            if not ps:
                raise FileNotFoundError("no executor state file")
            positions   = ps.get("positions", {})
            open_count  = len(positions)
            cash        = float(ps.get("cash", 0.0))
            realized    = float(ps.get("realized_pnl", 0.0))
            # Unrealized: sum of (avg_cost * shares) as a proxy — no live prices in email
            cost_basis  = sum(
                float(v.get("shares", 0)) * float(v.get("avg_cost", 0))
                for v in positions.values()
            )
            acct = ps.get("account", "")
            paper_lines = [
                f"  Open positions   : {open_count} ({', '.join(positions.keys()) or 'none'})",
                f"  Cash             : ${cash:,.2f}",
                f"  Cost basis (open): ${cost_basis:,.2f}",
                f"  Realized P&L     : ${realized:+,.2f}",
            ]
            if ps.get("executor") == "ibkr":
                paper_lines.insert(0, f"  Account          : IBKR paper {acct}")
        except FileNotFoundError:
            paper_lines = ["  no executor state file — no paper trades yet"]
        except Exception as exc:
            paper_lines = [f"  Position book stats unavailable: {exc}"]

        # ── 4. Regime (SPY vs 200-day MA) ─────────────────────────────────────
        try:
            import yfinance as yf
            from stock_bot.indicators.indicators import regime as _regime
            _spy_raw = fetch_with_retry(
                lambda: yf.download(
                    "SPY", interval="1d", period="1y",
                    auto_adjust=True, actions=False, progress=False,
                ),
                label="SPY:weekly_summary",
            )
            if _spy_raw is not None and not _spy_raw.empty:
                if hasattr(_spy_raw.columns, "nlevels") and _spy_raw.columns.nlevels > 1:
                    _spy_raw.columns = [
                        c[0] if isinstance(c, tuple) else c for c in _spy_raw.columns
                    ]
                spy_closes  = [float(v) for v in _spy_raw["Close"].dropna().tolist()]
                regime_str  = _regime(
                    spy_closes,
                    self._cfg.regime_ma_period,
                    self._cfg.regime_fast_ma,
                )
            else:
                regime_str = "UNKNOWN"
        except Exception as exc:
            regime_str = f"UNKNOWN ({exc})"
        regime_icons = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡"}
        regime_icon  = regime_icons.get(regime_str, "⚪")
        regime_line  = f"  Regime: {regime_str} {regime_icon}"

        # ── 5. TSX corruption warnings ────────────────────────────────────────
        try:
            from stock_bot.data.price_feed import get_tsx_warnings
            tsx_warn_line = f"  TSX corruption warnings: {get_tsx_warnings()}"
        except Exception as exc:
            tsx_warn_line = f"  TSX warnings unavailable: {exc}"

        # ── Compose email ─────────────────────────────────────────────────────
        subject = f"Stock Bot Weekly — {today} — {remaining} gates remaining"

        sep  = "=" * 50
        body = "\n".join([
            "Stock Bot — Weekly Summary",
            sep,
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "GATE STATUS",
            sep,
            *gate_lines,
            "",
            "FAST VALIDATOR (1h candles)",
            sep,
            *fv_lines,
            "",
            "SWING PAPER (daily candles)",
            sep,
            *paper_lines,
            "",
            "MARKET REGIME",
            sep,
            regime_line,
            "",
            "PRICE FEED HEALTH",
            sep,
            tsx_warn_line,
            "",
            sep,
            "Not financial advice. This is an automated report from Stock Bot.",
        ])

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = to_addr

        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
                server.login(from_addr, password)
                server.sendmail(from_addr, to_addr, msg.as_string())
            logger.info("Weekly summary email sent to %s", to_addr)
        except Exception as exc:
            logger.warning("Weekly summary email failed: %s", exc)

    def notify(self, alerts: list[Alert]) -> None:
        if not alerts:
            return
        self._terminal(alerts)
        if self._cfg.alert_email_enabled:
            self._email(alerts)
        if self._cfg.alert_desktop_enabled:
            self._desktop(alerts)
        self._telegram_alerts(alerts)

    def _telegram_alerts(self, alerts: list[Alert]) -> None:
        """HIGH-priority alerts only (same filter as desktop) — MEDIUM
        strong-buy chatter every scan cycle would drown the channel."""
        if self._telegram is None:
            return
        try:
            for a in alerts:
                if a.priority != "HIGH":
                    continue
                self._telegram.message(
                    f"📊 Stock Bot — {a.alert_type.value}\n"
                    f"{a.symbol} @ ${a.price:,.2f} {a.currency}\n{a.message}"
                )
        except Exception as exc:
            logger.warning("Telegram alert relay failed: %s", exc)

    def fill(
        self,
        side:   str,
        symbol: str,
        shares: float,
        price:  float,
        total:  float,
        pnl:    float | None = None,
        reason: str = "",
    ) -> None:
        """Trade-fill Telegram notification (stock book). No-op when the
        Telegram channel is off. Never raises."""
        if self._telegram is None:
            return
        try:
            executor = os.getenv("STOCK_EXECUTOR", "paper").strip().lower()
            tag = "IBKR paper" if executor == "ibkr" else "sim paper"
            side_emoji = "🟢 BUY" if side.upper() == "BUY" else "🔴 SELL"
            pnl_line = (
                f"\nP&L: {'🟢' if pnl >= 0 else '🔴'} ${pnl:+.2f}" if pnl is not None else ""
            )
            reason_line = f"\nReason: {reason}" if reason else ""
            self._telegram.message(
                f"{side_emoji}  {symbol} ({tag})\n"
                f"{shares:g} sh @ ${price:,.2f} = ${total:,.2f}"
                f"{pnl_line}{reason_line}"
            )
        except Exception as exc:
            logger.warning("Telegram fill relay failed: %s", exc)

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
