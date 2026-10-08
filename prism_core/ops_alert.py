"""One-line operator alerts over Telegram (best effort, never raises).

Target chat: ``PRISM_OPS_ALERT_CHAT_ID`` if set, otherwise the first id in
``TELEGRAM_ADMIN_IDS`` (a direct message to the operator). The public channel
is never used implicitly. Without a bot token or target the alert is logged
only. The token is never logged: failures report the exception type only.
Each ``key`` alerts at most once per KST day per process family, tracked in
``runtime/ops_alert_state.json``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from prism_core.env_config import clean_env_value

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_STATE = Path(__file__).resolve().parents[1] / "runtime" / "ops_alert_state.json"


def _target_chat() -> Optional[str]:
    explicit = clean_env_value(os.getenv("PRISM_OPS_ALERT_CHAT_ID"))
    if explicit:
        return explicit
    admins = clean_env_value(os.getenv("TELEGRAM_ADMIN_IDS")) or ""
    for token in admins.replace(";", ",").split(","):
        token = token.strip()
        if token:
            return token
    return None


def _post(token: str, chat_id: str, text: str) -> bool:
    import requests

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=10,
    )
    return response.status_code == 200


def _already_sent(key: str, day: str, state_path: Path) -> bool:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return state.get(key) == day


def _remember(key: str, day: str, state_path: Path) -> None:
    try:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state[key] = day
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def send_ops_alert(
    key: str,
    text: str,
    *,
    poster: Callable[[str, str, str], bool] = _post,
    state_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Send ``text`` once per KST day for ``key``. Returns True if delivered."""
    logger.error("[OPS-ALERT] %s", text)
    state_path = state_path or _STATE
    day = (now or datetime.now(_KST)).strftime("%Y-%m-%d")
    if _already_sent(key, day, state_path):
        return False
    token = clean_env_value(os.getenv("TELEGRAM_BOT_TOKEN"))
    chat_id = _target_chat()
    if not token or not chat_id:
        logger.warning("[OPS-ALERT] no Telegram target configured; alert logged only")
        return False
    try:
        delivered = bool(poster(token, chat_id, text))
    except Exception as exc:  # noqa: BLE001 - never surface the token-bearing URL
        logger.warning("[OPS-ALERT] Telegram send failed (%s)", type(exc).__name__)
        return False
    if delivered:
        _remember(key, day, state_path)
    else:
        logger.warning("[OPS-ALERT] Telegram send was not accepted")
    return delivered
