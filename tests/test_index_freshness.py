"""Stale index series are rejected so the next source answers (or regime=unknown)."""

from __future__ import annotations

from datetime import date

import pytest

pd = pytest.importorskip("pandas")

import cores.market_data as md  # noqa: E402
from cores.market_data.freshness import index_staleness  # noqa: E402
from cores.market_data.source import SourceChain, Unavailable  # noqa: E402


def _weekdays(day):
    return day.weekday() < 5


def _frame(last, days=30):
    index = pd.bdate_range(end=last, periods=days)
    return pd.DataFrame({"Close": range(days)}, index=index)


def test_fdr_frozen_on_0917_is_stale_on_1008():
    reason = index_staleness(_frame("2026-09-17"), "20261008", today=date(2026, 10, 8), is_market_day=_weekdays)
    assert reason and "2026-09-17" in reason


def test_one_or_two_sessions_behind_is_accepted():
    today = date(2026, 10, 8)
    assert index_staleness(_frame("2026-10-08"), "20261008", today=today, is_market_day=_weekdays) is None
    assert index_staleness(_frame("2026-10-07"), "20261008", today=today, is_market_day=_weekdays) is None
    assert index_staleness(_frame("2026-10-06"), "20261008", today=today, is_market_day=_weekdays) is None
    assert index_staleness(_frame("2026-10-05"), "20261008", today=today, is_market_day=_weekdays)


def test_holidays_do_not_count_as_missed_sessions():
    # 10/9 (Hangul Day) and the weekend are not sessions: Friday's bar is current on Monday.
    closed = {date(2026, 10, 9)}
    cal = lambda d: d.weekday() < 5 and d not in closed  # noqa: E731
    assert index_staleness(_frame("2026-10-08"), "20261012", today=date(2026, 10, 12), is_market_day=cal) is None


def test_historical_request_is_judged_against_its_end_date():
    assert index_staleness(_frame("2026-06-30"), "20260630", today=date(2026, 10, 8), is_market_day=_weekdays) is None


def test_real_calendar_knows_hangul_day():
    from check_market_day import is_market_day

    assert not is_market_day(date(2026, 10, 9))
    assert not is_market_day(date(2026, 10, 5))
    assert is_market_day(date(2026, 10, 8))


class _Source:
    def __init__(self, name, frame=None, error=None):
        self.name, self.frame, self.error, self.calls = name, frame, error, 0

    def index_history(self, code, start, end):
        self.calls += 1
        if self.error:
            raise self.error
        return self.frame


@pytest.fixture
def chain(monkeypatch):
    holder = {}

    def install(*sources):
        holder["chain"] = SourceChain(list(sources))
        md.set_default_chain(holder["chain"])
        return holder["chain"]

    monkeypatch.setattr(
        "cores.market_data.freshness._default_is_market_day", _weekdays
    )
    import cores.market_data.freshness as freshness

    real = freshness.index_staleness
    monkeypatch.setattr(
        freshness,
        "index_staleness",
        lambda frame, end: real(frame, end, today=date(2026, 10, 8), is_market_day=_weekdays),
    )
    yield install
    md.set_default_chain(None)


def test_stale_first_source_falls_through_to_fresh_one(chain):
    stale = _Source("fdr", _frame("2026-09-17"))
    fresh = _Source("naver", _frame("2026-10-08"))
    chain(stale, fresh)
    out = md.get_index_ohlcv_by_date("20260201", "20261008", "1001")
    assert out.index.max() == pd.Timestamp("2026-10-08")
    assert stale.calls == 1 and fresh.calls == 1


def test_every_source_stale_returns_empty_not_stale(chain):
    from cores.market_data.source import Unavailable as U

    chain(_Source("kis", error=U("EGW00201")), _Source("fdr", _frame("2026-09-17")))
    assert md.get_index_ohlcv_by_date("20260201", "20261008", "1001").empty


def test_plain_fetch_is_unchanged():
    frame = _frame("2026-09-17")
    assert SourceChain([_Source("fdr", frame)]).fetch("index_history", "1001", "a", "b") is frame


def test_fetch_validated_lists_rejections():
    chain = SourceChain([_Source("fdr", _frame("2026-09-17"))])
    with pytest.raises(Unavailable, match="fdr: stale"):
        chain.fetch_validated("index_history", lambda f: "stale: test", "1001", "a", "b")


# ---- data_prefetch: regime becomes "unknown" and an alert is raised ----------


def test_prefetch_marks_regime_unknown_and_alerts(monkeypatch):
    from cores import data_prefetch

    class _Server:
        @staticmethod
        def get_index_ohlcv(start, end, code):
            return {"error": "시장 데이터 조회에 실패했습니다."}

    alerts = []
    monkeypatch.setattr(data_prefetch, "_get_mcp_server_module", lambda: _Server)
    monkeypatch.setattr(data_prefetch, "prefetch_index_ohlcv", lambda *a, **k: "")
    monkeypatch.setattr(data_prefetch, "_prefetch_sector_info", lambda *a, **k: "{}")
    monkeypatch.setattr(data_prefetch, "_log_regime_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(data_prefetch, "_alert_index_unavailable", alerts.append)
    result = data_prefetch.prefetch_macro_intelligence_data("20261008")
    assert result["computed_regime"]["market_regime"] == "unknown"
    assert result["computed_regime"]["regime_unavailable"] is True
    assert alerts == ["20261008"]


def test_unknown_regime_is_not_a_tradable_regime():
    from cores.buy_gate import normalize_regime
    from cores.regime_policy import enforce_computed_regime
    from cores import data_prefetch

    merged = enforce_computed_regime({"market_regime": "moderate_bull"}, data_prefetch._unknown_kr_regime())
    assert merged["market_regime"] == "unknown"
    assert merged["llm_market_regime"] == "moderate_bull"
    assert normalize_regime(merged["market_regime"]) is None


def test_ops_alert_sends_once_per_day_and_hides_token(tmp_path, monkeypatch, caplog):
    from datetime import datetime
    from prism_core import ops_alert

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "42, 43")
    monkeypatch.delenv("PRISM_OPS_ALERT_CHAT_ID", raising=False)
    sent = []
    state = tmp_path / "state.json"
    now = datetime(2026, 10, 8, 9, 0)
    poster = lambda token, chat, text: sent.append((chat, text)) or True  # noqa: E731
    assert ops_alert.send_ops_alert("k", "hello", poster=poster, state_path=state, now=now)
    assert not ops_alert.send_ops_alert("k", "hello", poster=poster, state_path=state, now=now)
    assert sent == [("42", "hello")]

    def boom(token, chat, text):
        raise RuntimeError(f"https://api.telegram.org/bot{token}/sendMessage failed")

    assert not ops_alert.send_ops_alert("other", "x", poster=boom, state_path=state, now=now)
    assert "SECRET" not in caplog.text


def test_ops_alert_without_target_logs_only(tmp_path, monkeypatch):
    from prism_core import ops_alert

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.delenv("TELEGRAM_ADMIN_IDS", raising=False)
    monkeypatch.delenv("PRISM_OPS_ALERT_CHAT_ID", raising=False)
    called = []
    assert not ops_alert.send_ops_alert("k", "x", poster=lambda *a: called.append(a), state_path=tmp_path / "s")
    assert called == []
