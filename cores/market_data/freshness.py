"""Freshness check for index series.

FinanceDataReader's KOSPI/KOSDAQ feed stopped at 2026-09-17 while still
returning a non-empty frame, so the chain accepted it and the market regime
froze for three weeks. An index answer is now rejected when its last bar is
more than ``PRISM_INDEX_MAX_STALE_SESSIONS`` (default 2) KRX sessions older
than the requested end date (capped at today, KST). Two sessions tolerate a
provider that publishes after the close plus today's not-yet-finished bar.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from prism_core.env_config import env_int

_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_MAX_STALE_SESSIONS = 2
_MAX_SCAN_DAYS = 60


def _default_is_market_day(day: date) -> bool:
    try:
        from check_market_day import is_market_day
    except Exception:  # noqa: BLE001 - calendar unavailable: weekdays only
        return day.weekday() < 5
    return bool(is_market_day(day))


def _as_date(value) -> Optional[date]:
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    return stamp.date()


def index_staleness(
    frame,
    end_date: str,
    *,
    today: Optional[date] = None,
    max_stale_sessions: Optional[int] = None,
    is_market_day: Callable[[date], bool] = _default_is_market_day,
) -> Optional[str]:
    """Return a reason string when ``frame`` is stale, otherwise None."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None  # emptiness is the chain's existing "no data" path
    last = _as_date(frame.index.max())
    if last is None:
        return "index has no readable dates"
    try:
        requested = datetime.strptime(str(end_date), "%Y%m%d").date()
    except ValueError:
        return None
    today = today or datetime.now(_KST).date()
    anchor = min(requested, today)
    if max_stale_sessions is None:
        max_stale_sessions = env_int("PRISM_INDEX_MAX_STALE_SESSIONS", _DEFAULT_MAX_STALE_SESSIONS)
    if last >= anchor:
        return None
    missed = 0
    day = anchor
    for _ in range(_MAX_SCAN_DAYS):
        if day <= last:
            break
        if is_market_day(day):
            missed += 1
            if missed > max_stale_sessions:
                return f"stale: last bar {last.isoformat()} is {missed}+ sessions before {anchor.isoformat()}"
        day -= timedelta(days=1)
    else:
        return f"stale: last bar {last.isoformat()} is over {_MAX_SCAN_DAYS} days before {anchor.isoformat()}"
    return None
