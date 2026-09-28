"""Keep Telegram bot tokens out of logs.

httpx logs every request URL at INFO, and Telegram URLs embed the bot token
(``https://api.telegram.org/bot<id>:<secret>/sendMessage``).
"""

from __future__ import annotations

import logging
import re

_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_REPLACEMENT = "bot<REDACTED>"
_QUIET_LOGGERS = ("httpx", "httpcore", "telegram", "telegram.ext")


def redact_text(text: str) -> str:
    return _TOKEN_RE.sub(_REPLACEMENT, text)


class TelegramTokenRedactingFilter(logging.Filter):
    """Rewrites records whose formatted message contains a bot token."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            if _TOKEN_RE.search(message):
                record.msg = redact_text(message)
                record.args = None
            if record.exc_text and _TOKEN_RE.search(record.exc_text):
                record.exc_text = redact_text(record.exc_text)
        except Exception:
            pass
        return True


def _attach(target) -> None:
    if not any(isinstance(f, TelegramTokenRedactingFilter) for f in target.filters):
        target.addFilter(TelegramTokenRedactingFilter())


def install_log_redaction() -> None:
    """Idempotent; never raises. Call after logging handlers are configured."""
    try:
        for name in _QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        root = logging.getLogger()
        _attach(root)
        for handler in list(root.handlers):
            _attach(handler)
    except Exception:
        pass
