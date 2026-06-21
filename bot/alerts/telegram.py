"""
Telegram alerter — sends trade fills and daily P&L to a Telegram chat.

Setup:
  1. Create a bot via @BotFather → get token
  2. Send /start to the bot → get chat_id from https://api.telegram.org/bot<TOKEN>/getUpdates
  3. Set in .env:
       TELEGRAM_ENABLED=true
       TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
       TELEGRAM_CHAT_ID=987654321

Fails silently — never raises. If Telegram is unreachable the bot keeps trading.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class TelegramAlerter:
    """
    Non-blocking Telegram notifier.
    All sends are fire-and-forget in a daemon thread — the trading loop never waits.
    """

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True) -> None:
        self._token   = bot_token.strip()
        self._chat_id = chat_id.strip()
        self._enabled = enabled and bool(self._token) and bool(self._chat_id)

        if enabled and not self._enabled:
            logger.warning(
                "Telegram enabled in config but TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID is empty — alerts disabled"
            )
        if self._enabled:
            logger.info("Telegram alerter ready — chat_id=%s", self._chat_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fill(
        self,
        side:        str,
        symbol:      str,
        quantity:    float,
        price:       float,
        total_value: float,
        pnl:         Optional[float] = None,
        exchange:    str             = "",
    ) -> None:
        side_emoji = "🟢 BUY" if side.upper() == "BUY" else "🔴 SELL"
        pnl_str    = f"\nP&L: {'🟢' if pnl and pnl >= 0 else '🔴'} ${pnl:+.2f}" if pnl is not None else ""
        msg = (
            f"{side_emoji}  {symbol}\n"
            f"Qty:   {quantity:.6f}\n"
            f"Price: ${price:,.2f}\n"
            f"Value: ${total_value:,.2f}"
            f"{pnl_str}\n"
            f"{'Exchange: ' + exchange.upper() + chr(10) if exchange else ''}"
            f"Time:  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._send_async(msg)

    def daily_pnl(
        self,
        symbol:       str,
        realized_pnl: float,
        total_value:  float,
        trade_count:  int,
    ) -> None:
        emoji = "📈" if realized_pnl >= 0 else "📉"
        msg = (
            f"{emoji} Daily Summary — {symbol}\n"
            f"Realized P&L: {'🟢' if realized_pnl >= 0 else '🔴'} ${realized_pnl:+.2f}\n"
            f"Portfolio:    ${total_value:,.2f}\n"
            f"Trades today: {trade_count}\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._send_async(msg)

    def error(self, message: str) -> None:
        msg = f"⚠️ BOT ERROR\n{message}\n{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        self._send_async(msg)

    def startup(self, exchange: str, symbol: str, mode: str) -> None:
        msg = (
            f"🤖 Bot started\n"
            f"Exchange: {exchange.upper()}\n"
            f"Symbol:   {symbol}\n"
            f"Mode:     {mode}\n"
            f"Time:     {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._send_async(msg)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _send_async(self, text: str) -> None:
        if not self._enabled:
            return
        t = threading.Thread(target=self._send, args=(text,), daemon=True)
        t.start()

    def _send(self, text: str) -> None:
        try:
            import requests
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            resp = requests.post(
                url,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": ""},
                timeout=8,
            )
            if not resp.ok:
                logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:100])
        except Exception as exc:
            logger.warning("Telegram send error: %s", exc)
