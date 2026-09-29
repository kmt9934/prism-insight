"""Broker residual sell retry (prism_core/residual_sells.py + hardstop wiring).

Incident 2026-09-23/28: the ledger closed 161890/005930 after 15:30 but the KIS
demo account rejected the after-hours SELL, leaving shares at the broker.
No network, no real orders: the broker is a fake trader.
"""
import asyncio
import datetime
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import tools.hardstop_seller as hs  # noqa: E402
from prism_core.order_intents import IntentStore  # noqa: E402
from prism_core.residual_sells import (  # noqa: E402
    find_residual_sell_candidates,
    resolve_retry_quantity,
)
from tests.test_hardstop_seller import FakeTrader, _patch  # noqa: E402

NOW = datetime.datetime(2026, 9, 29, 1, 0, tzinfo=datetime.timezone.utc)
ACCOUNT = "vps:50204184"


def _db(tmp_path):
    db = tmp_path / "t.sqlite"
    IntentStore(db).ensure_schema()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE stock_holdings (id INTEGER PRIMARY KEY, ticker TEXT, company_name TEXT, "
            "buy_price REAL, buy_date TEXT, scenario TEXT, target_price REAL, stop_loss REAL, "
            "account_key TEXT, account_name TEXT)"
        )
        conn.execute(
            "CREATE TABLE trading_history (ticker TEXT, account_key TEXT, account_name TEXT, sell_date TEXT)"
        )
    return str(db)


def _intent(db, intent_id, symbol, status, created_at, quantity=1, decision_id=None):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO order_intents (id, idempotency_key, market, account_id, symbol, side, "
            "order_style, quantity, source, source_decision_id, source_position_id, execution_mode, "
            "status, raw_request_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent_id, f"key-{intent_id}", "KR", ACCOUNT, symbol, "SELL", "market", quantity,
             "hardstop", decision_id, f"legacy:KR:{intent_id}", "live", status, "{}",
             created_at, created_at),
        )


def _closed(db, symbol, sell_date="2026-09-28 15:49:13"):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO trading_history VALUES (?,?,?,?)", (symbol, ACCOUNT, "모의-메인", sell_date)
        )


def _candidates(db, **kwargs):
    with sqlite3.connect(db) as conn:
        return find_residual_sell_candidates(conn, now_utc=NOW, **kwargs)


def test_failed_sell_with_closed_ledger_is_a_candidate(tmp_path):
    db = _db(tmp_path)
    _intent(db, "f1", "005930", "FAILED", "2026-09-28T06:49:17+00:00", quantity=1)
    _closed(db, "005930")
    got = _candidates(db)
    assert got == [{"failed_intent_id": "f1", "account_key": ACCOUNT, "account_name": "모의-메인",
                    "symbol": "005930", "quantity": 1}]


def test_skips_when_ledger_holds_the_ticker_again(tmp_path):
    db = _db(tmp_path)
    _intent(db, "f1", "005930", "FAILED", "2026-09-28T06:49:17+00:00")
    _closed(db, "005930")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO stock_holdings (ticker, account_key) VALUES ('005930', ?)", (ACCOUNT,))
    assert _candidates(db) == []


def test_skips_when_a_later_sell_was_submitted(tmp_path):
    db = _db(tmp_path)
    _intent(db, "f1", "005930", "FAILED", "2026-09-28T06:49:17+00:00")
    _intent(db, "s1", "005930", "SUBMITTED", "2026-09-29T00:10:00+00:00")
    _closed(db, "005930")
    assert _candidates(db) == []


def test_skips_old_failures_and_exhausted_retry_budget(tmp_path):
    db = _db(tmp_path)
    _intent(db, "old", "161890", "FAILED", "2026-09-01T06:59:20+00:00")
    _closed(db, "161890")
    _intent(db, "f1", "005930", "FAILED", "2026-09-28T06:49:17+00:00")
    _closed(db, "005930")
    for day in ("2026-09-29", "2026-09-30", "2026-10-01"):
        _intent(db, f"r-{day}", "005930", "FAILED", f"{day}T00:05:00+00:00",
                decision_id=f"residual-retry:f1:{day}")
    assert _candidates(db, lookback_days=10, max_attempts=3) == []


def test_retry_quantity_never_exceeds_intent_or_broker():
    assert resolve_retry_quantity(1, 5) == 1
    assert resolve_retry_quantity(3, 2) == 2
    assert resolve_retry_quantity(None, 3) == 3
    assert resolve_retry_quantity(2, 0) == 0


@pytest.fixture
def live_kr(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(hs, "DB_PATH", db)
    monkeypatch.setattr(hs, "HARDSTOP_LIVE", True)
    monkeypatch.setattr(hs, "HARDSTOP_ENABLED", True)
    monkeypatch.setattr(hs, "RESIDUAL_RETRY", True)
    monkeypatch.setattr(hs, "_kr_regular_session", lambda: True)
    monkeypatch.setenv("POSITION_PENDING_KR_ENABLED", "false")
    recent = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).isoformat()
    _intent(db, "f1", "005930", "FAILED", recent, quantity=1)
    _closed(db, "005930")
    return db


def test_hardstop_resells_residual_once_per_day(live_kr, monkeypatch):
    calls = []
    trader = FakeTrader({}, holding_qty={"005930": 5}, calls=calls)
    _patch(monkeypatch, trader)

    first = asyncio.run(hs.run_market("KR", "run1"))
    second = asyncio.run(hs.run_market("KR", "run2"))

    assert calls == ["kis:005930:1"]
    assert first["residual_sold"] == 1
    assert second["residual_sold"] == 0


@pytest.mark.parametrize("setting", ["shadow", "closed_session", "disabled", "broker_flat"])
def test_hardstop_places_no_residual_order_when_not_allowed(live_kr, monkeypatch, setting):
    calls = []
    holding = {} if setting == "broker_flat" else {"005930": 1}
    trader = FakeTrader({}, holding_qty=holding, calls=calls)
    _patch(monkeypatch, trader)
    if setting == "shadow":
        monkeypatch.setattr(hs, "HARDSTOP_LIVE", False)
    elif setting == "closed_session":
        monkeypatch.setattr(hs, "_kr_regular_session", lambda: False)
    elif setting == "disabled":
        monkeypatch.setattr(hs, "RESIDUAL_RETRY", False)

    summary = asyncio.run(hs.run_market("KR", "run1"))

    assert calls == []
    assert summary["residual_sold"] == 0
