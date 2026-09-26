"""Secret redaction for log output (2026-09-26).

A Telegram bot token lives inside the request URL
(``https://api.telegram.org/bot<token>/...``), and `requests` puts that URL
into its exception text ("502 Server Error ... for url: ..."). Logging the
exception wrote the live token into logs/trade_bot.log 30 times between
2026-08-23 and 2026-09-10.

Two layers:
  - ``RedactingFormatter`` — installed on every root handler by both bots'
    ``_setup_logging()``, so ANY logger's message, args and traceback text
    is scrubbed at the last step before it reaches a file or the console.
  - ``redact()`` — also called directly at the Telegram call sites, for any
    code path that logs without going through those handlers (tests,
    tooling scripts).

Two patterns are scrubbed: anything shaped like a Telegram bot token, and
the literal value of any secret-looking environment variable
(``*_TOKEN``, ``*_SECRET``, ``*_API_KEY``, ``*_PASSWORD``) — so a Kraken
secret or an AI provider key can't leak the same way.
"""
from __future__ import annotations

import logging
import os
import re

_TELEGRAM_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
_SECRET_ENV_SUFFIXES = ("_TOKEN", "_SECRET", "_API_KEY", "_PASSWORD")
_MIN_SECRET_LEN = 12          # shorter values are too likely to match ordinary text
REDACTED = "<redacted>"


def _secret_env_values() -> list[str]:
    vals = {
        v.strip() for k, v in os.environ.items()
        if k.upper().endswith(_SECRET_ENV_SUFFIXES) and v and len(v.strip()) >= _MIN_SECRET_LEN
    }
    return sorted(vals, key=len, reverse=True)   # longest first — no partial overlaps


def redact(text: object) -> str:
    """Return ``str(text)`` with every known secret replaced. Never raises."""
    try:
        s = str(text)
        for secret in _secret_env_values():
            if secret in s:
                s = s.replace(secret, REDACTED)
        return _TELEGRAM_TOKEN_RE.sub(REDACTED, s)
    except Exception:
        return "<unprintable — redaction failed>"


class RedactingFormatter(logging.Formatter):
    """logging.Formatter that scrubs secrets from the fully formatted record
    (message, args and exception/stack text alike)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
