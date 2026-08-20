"""
Telegram two-way control — inbound command polling (getUpdates), dispatched
to caller-supplied read-only/flag-file handlers only.

Added 2026-08-20, closing the gap flagged during the Freqtrade comparison:
TelegramAlerter (bot/alerts/telegram.py) is outbound-only. This module is
the inbound half — a long-polling getUpdates loop, auth-gated to a single
chat_id, dispatching recognized commands to handler callables the caller
registers. It has ZERO knowledge of trading internals: no import of
LiveExecutor, RiskManager, or anything that could place/modify/cancel an
order. bot/main.py owns building the actual handlers and is the only place
that decides what a command does — this module only owns "receive text
from Telegram, authenticate it, look up a handler by exact command string,
call it, send back whatever string it returns."

── Polling vs webhook ──────────────────────────────────────────────────────
Long-polling (getUpdates), not a webhook: no public endpoint/TLS/reverse-
proxy needed, fits a home/VPS-undecided deployment. Cost is up to
poll_timeout seconds of latency on a command, acceptable for pause/status/
resume.

── SHARED-TOKEN CONSTRAINT — read before adding a second poller ───────────
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are the SAME credentials used by
the stock bot's outbound-only TelegramAlerter (stock_bot/alerts/notifier.py
_make_telegram(), "one token/chat source for both bots" since 2026-07-17).
Telegram's getUpdates `offset` parameter is a SERVER-SIDE, PER-BOT-TOKEN
acknowledgment — passing offset=N tells Telegram to permanently discard
every update with update_id < N for that token, for every caller, not just
whichever process passed it. Two independent processes each tracking their
own local offset against this same token WILL corrupt each other: whichever
one advances its offset further can silently erase updates the other
hasn't processed yet. This is not a corner case to code around — Telegram's
getUpdates model assumes exactly one active consumer per token.

Consequence: exactly ONE process may ever run a TelegramCommandPoller
against this token. Today that's the crypto bot only — the stock bot has
no inbound polling at all (see bot/main.py's handler wiring: /status_stock
is answered by reading stock_bot's state file directly, not by a second
poller). If stock-bot two-way CONTROL (not just read-only status) is ever
added, it must either route through this same crypto-owned poller (adding
more handlers here) or use a second, dedicated Telegram bot token — never
a second independent getUpdates loop against this token.

── Auth ─────────────────────────────────────────────────────────────────
Every inbound message's chat.id is compared against the configured
chat_id. A mismatch is silently ignored (no reply) and logged at INFO —
replying would confirm to a stranger that a live, responsive bot exists at
this token, worth probing further. An authorized chat sending an
unrecognized command (typo, or a namespaced command this process doesn't
own, e.g. a future /pause_stock) is treated the same way: ignored, logged,
no reply.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from bot.exchanges.retry import fetch_with_retry

logger = logging.getLogger(__name__)

_GETUPDATES_TIMEOUT_S = 25   # Telegram long-poll window
_HTTP_TIMEOUT_S       = 35   # client-side socket timeout — must exceed the above


class TelegramCommandPoller:
    """
    Long-polls Telegram's getUpdates for a single bot token, authenticates
    against one chat_id, and dispatches exact-match commands to handler
    callables. Never raises out of its public methods — a broken handler or
    a network hiccup must not kill the polling thread.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id:   str,
        handlers:  dict[str, Callable[[], str]],
        poll_timeout: int = _GETUPDATES_TIMEOUT_S,
        http_timeout: float = _HTTP_TIMEOUT_S,
    ) -> None:
        self._token        = bot_token.strip()
        self._chat_id       = chat_id.strip()
        self._handlers      = dict(handlers)
        self._poll_timeout  = poll_timeout
        self._http_timeout  = http_timeout
        self._offset: Optional[int] = None   # None = not primed yet

    @property
    def enabled(self) -> bool:
        return bool(self._token) and bool(self._chat_id)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def prime_offset(self) -> None:
        """
        One-shot call before the poll loop starts: drain whatever is
        currently queued WITHOUT dispatching any of it, purely to compute
        the offset that marks 'caught up'. Prevents acting on stale
        commands sent while the bot was offline (e.g. a days-old /pause
        firing retroactively on restart). Never raises.
        """
        if not self.enabled:
            return
        try:
            updates = self._get_updates(offset=None, timeout=0)
        except Exception as exc:
            logger.warning("Telegram control: prime_offset failed (%s) — "
                            "starting from an unset offset.", exc)
            return
        if updates:
            self._offset = max(u.get("update_id", 0) for u in updates) + 1
            logger.info("Telegram control: primed offset=%d (%d stale "
                        "update(s) discarded unread)", self._offset, len(updates))

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def poll_once(self) -> None:
        """One getUpdates cycle: fetch, authenticate, dispatch, reply,
        advance the offset. Blocks up to poll_timeout seconds when no
        update is pending (long-poll). Never raises."""
        if not self.enabled:
            return
        try:
            updates = self._get_updates(offset=self._offset, timeout=self._poll_timeout)
        except Exception as exc:
            logger.warning("Telegram control: getUpdates failed: %s", exc)
            return

        max_update_id = None
        for update in updates:
            update_id = update.get("update_id")
            if update_id is not None:
                max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)
            self._handle_update(update)

        if max_update_id is not None:
            self._offset = max_update_id + 1

    def _handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        if not text:
            return
        chat_id = str((message.get("chat") or {}).get("id", ""))
        command = text.split()[0].split("@")[0].lower()   # strip args, strip @botname

        if chat_id != self._chat_id:
            logger.info(
                "Telegram control: unauthorized command attempt from chat %s: %r",
                chat_id, text[:200],
            )
            return

        handler = self._handlers.get(command)
        if handler is None:
            logger.info("Telegram control: unrecognized command ignored: %r", command)
            return

        try:
            reply_text = handler()
        except Exception as exc:
            logger.error("Telegram control: handler for %s raised: %s", command, exc)
            reply_text = f"⚠️ {command} failed: {exc}"

        if reply_text:
            self._send_reply(reply_text)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _get_updates(self, offset: Optional[int], timeout: int) -> list[dict]:
        import requests
        url    = f"https://api.telegram.org/bot{self._token}/getUpdates"
        params = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(url, params=params, timeout=self._http_timeout)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"getUpdates returned ok=false: {body}")
        return body.get("result", [])

    def _send_reply(self, text: str) -> None:
        def _post():
            import requests
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            resp = requests.post(
                url, json={"chat_id": self._chat_id, "text": text},
                timeout=8,
            )
            if not resp.ok:
                logger.warning("Telegram control: reply send failed: %s %s",
                                resp.status_code, resp.text[:100])

        try:
            fetch_with_retry(_post, label="Telegram control reply")
        except Exception as exc:
            logger.warning("Telegram control: reply send error: %s", exc)


def start_telegram_control_thread(
    poller: TelegramCommandPoller,
    name: str = "telegram-control",
) -> Optional[threading.Thread]:
    """Start a daemon thread running the poll loop forever. Returns None
    (feature off) when the poller has no token/chat_id configured."""
    if not poller.enabled:
        logger.info("Telegram control '%s' disabled (no token/chat_id)", name)
        return None

    def _loop() -> None:
        poller.prime_offset()
        while True:
            try:
                poller.poll_once()
            except Exception as exc:
                # poll_once already catches its own errors — this is a last
                # resort so a truly unexpected bug can't kill the thread.
                logger.error("Telegram control: unexpected poll_once error: %s", exc)
                time.sleep(2.0)

    t = threading.Thread(target=_loop, daemon=True, name=name)
    t.start()
    logger.info("Telegram control thread '%s' started", name)
    return t
