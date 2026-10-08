"""KR sell-monitor loops exit early on KRX holidays (fail-open calendar)."""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prism_core import market_calendar  # noqa: E402

LOOPS = [
    ("tools.hardstop_seller", "HARDSTOP_ENABLED"),
    ("tools.trend_exit_seller", "TREND_EXIT_ENABLED"),
    ("tools.fill_chaser", "FILL_CHASER_ENABLED"),
]


def test_calendar_knows_holidays_and_fails_open(monkeypatch):
    assert market_calendar.kr_market_day_today(date(2026, 10, 9)) is False  # Hangul Day
    assert market_calendar.kr_market_day_today(date(2026, 10, 5)) is False  # substitute holiday
    assert market_calendar.kr_market_day_today(date(2026, 10, 12)) is True

    import check_market_day

    def broken(_day):
        raise RuntimeError("calendar broken")

    monkeypatch.setattr(check_market_day, "is_market_day", broken)
    assert market_calendar.kr_market_day_today(date(2026, 10, 9)) is True


@pytest.mark.parametrize("module_name,flag", LOOPS)
@pytest.mark.parametrize("open_today", [False, True])
def test_loops_skip_kr_only_when_closed(module_name, flag, open_today, monkeypatch):
    module = __import__(module_name, fromlist=["main_async"])
    ran = []

    async def fake_run_market(market, run_id):
        ran.append(market)
        return {}

    monkeypatch.setattr(module, flag, True)
    monkeypatch.setattr(module, "run_market", fake_run_market)
    monkeypatch.setattr(market_calendar, "kr_market_day_today", lambda today=None: open_today)
    assert asyncio.run(module.main_async(["KR"])) == 0
    assert ran == (["KR"] if open_today else [])


def test_us_loop_is_not_affected(monkeypatch):
    import tools.hardstop_seller as module

    ran = []

    async def fake_run_market(market, run_id):
        ran.append(market)
        return {}

    monkeypatch.setattr(module, "HARDSTOP_ENABLED", True)
    monkeypatch.setattr(module, "run_market", fake_run_market)
    monkeypatch.setattr(market_calendar, "kr_market_day_today", lambda today=None: False)
    asyncio.run(module.main_async(["US"]))
    assert ran == ["US"]
