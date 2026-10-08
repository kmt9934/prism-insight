"""KRX trading-day check for intraday loops (fail-open).

Cron runs the KR sell monitors every few minutes on weekdays, including
exchange holidays (e.g. 2026-10-05, 2026-10-09). ``kr_market_day_today``
lets them exit early on those days. If the calendar cannot be read the loops
keep running, because skipping a real session would leave stops unwatched.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


def kr_market_day_today(today: Optional[date] = None) -> bool:
    day = today or datetime.now(_KST).date()
    try:
        from check_market_day import is_market_day

        return bool(is_market_day(day))
    except Exception as exc:  # noqa: BLE001 - fail open
        logger.warning("KR market calendar unavailable (%s); assuming open", type(exc).__name__)
        return True


def drop_closed_kr(markets: list[str], log: logging.Logger, loop_name: str) -> list[str]:
    """Remove "KR" from ``markets`` when today is not a KRX trading day."""
    if "KR" not in markets or kr_market_day_today():
        return markets
    log.info("%s: KR market closed today (weekend/holiday) -> KR skipped", loop_name)
    return [market for market in markets if market != "KR"]
