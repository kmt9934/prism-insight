"""Tolerant readers for operator-edited environment settings.

`.env` files are hand-edited on the server, so values arrive with stray
whitespace, quotes or trailing ``# comments``. A malformed value must never
crash an import-time knob in a cron loop; it logs a WARNING and keeps the
caller's existing default. Defaults stay owned by each caller.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_COMMENT = re.compile(r"(?:^|\s)#.*$")


def clean_env_value(raw: Optional[str]) -> Optional[str]:
    """Strip whitespace, a trailing ``# comment`` and one pair of outer quotes.

    Returns None for a missing or effectively empty value.
    """
    if raw is None:
        return None
    text = _COMMENT.sub("", str(raw)).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text or None


def _warn(name: str, raw: object, default: object, kind: str) -> None:
    logger.warning(
        "Invalid %s value for %s=%r; using default %r", kind, name or "<setting>", raw, default
    )


def parse_bool(raw: Optional[str], default: bool, name: str = "") -> bool:
    value = clean_env_value(raw)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    _warn(name, raw, default, "boolean")
    return default


def parse_int(raw: Optional[str], default: int, name: str = "") -> int:
    value = clean_env_value(raw)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        try:
            as_float = float(value)
        except ValueError:
            _warn(name, raw, default, "integer")
            return default
        if as_float.is_integer():
            return int(as_float)
        _warn(name, raw, default, "integer")
        return default


def parse_float(raw: Optional[str], default: float, name: str = "") -> float:
    value = clean_env_value(raw)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        _warn(name, raw, default, "number")
        return default


def parse_choice(raw: Optional[str], default: str, choices: Iterable[str], name: str = "") -> str:
    value = clean_env_value(raw)
    if value is None:
        return default
    lowered = value.lower()
    allowed = {str(choice).lower() for choice in choices}
    if lowered in allowed:
        return lowered
    _warn(name, raw, default, "choice")
    return default


def env_bool(name: str, default: bool) -> bool:
    return parse_bool(os.getenv(name), default, name)


def env_int(name: str, default: int) -> int:
    return parse_int(os.getenv(name), default, name)


def env_float(name: str, default: float) -> float:
    return parse_float(os.getenv(name), default, name)


def env_choice(name: str, default: str, choices: Iterable[str]) -> str:
    return parse_choice(os.getenv(name), default, choices, name)


__all__ = [
    "clean_env_value",
    "env_bool",
    "env_choice",
    "env_float",
    "env_int",
    "parse_bool",
    "parse_choice",
    "parse_float",
    "parse_int",
]
