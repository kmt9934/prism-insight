"""Regression tests for the mini2 hot-fixes ported onto main."""

import ast
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Tuple
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _extract(source_file, name, namespace):
    """Load one function without importing the module (kis_auth reads config at import)."""
    source = ROOT / source_file
    node = next(n for n in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[name]


def test_demo_mode_accepts_ps_paper_keys_and_prod_rejects_psvt():
    validate = _extract("trading/kis_auth.py", "validate_credentials", {"Tuple": Tuple})
    assert validate("PSVTabcdef1234", "vps") == (True, "")
    assert validate("PSabcdef123456", "vps") == (True, "")
    ok, message = validate("PSVTabcdef1234", "prod")
    assert ok is False and "PSVT" in message
    assert validate("PSabcdef123456", "prod") == (True, "")


def test_dart_prefetch_is_empty_without_api_key(monkeypatch):
    dart = pytest.importorskip("cores.dart_fundamentals")
    monkeypatch.setattr(dart, "_api_key", lambda: "")
    assert dart.prefetch_dart_fundamentals_markdown("삼성전자", "005930", "20260928") == ""


def test_dart_prefetch_swallows_provider_errors(monkeypatch):
    dart = pytest.importorskip("cores.dart_fundamentals")
    monkeypatch.setattr(dart, "_api_key", lambda: "key")

    def boom(*_args, **_kwargs):
        raise RuntimeError("DART down")

    monkeypatch.setattr(dart, "format_fundamentals_markdown", boom)
    assert dart.prefetch_dart_fundamentals_markdown("삼성전자", "005930", "20260928") == ""


def test_company_status_prompt_leads_with_dart_block():
    agents = pytest.importorskip("cores.agents.company_info_agents")
    urls = {"기업현황": "https://example.invalid/status"}
    block = "## Open DART 공시 재무 (검증된 정형 데이터)"
    with_dart = agents.create_company_status_agent(
        "삼성전자", "005930", "20260928", urls, "ko", prefetched_dart=block
    ).instruction
    without = agents.create_company_status_agent(
        "삼성전자", "005930", "20260928", urls, "ko"
    ).instruction
    assert with_dart.startswith(block)
    assert block not in without


def test_placeholder_company_name_is_resolved(monkeypatch):
    helpers = pytest.importorskip("tracking.helpers")
    fake = types.ModuleType("cores.market_data")
    fake.get_market_ticker_name = lambda ticker: {"005930": "삼성전자"}.get(ticker, ticker)
    monkeypatch.setitem(sys.modules, "cores.market_data", fake)
    assert helpers.extract_ticker_info("reports/005930_Stock_20260928_morning.pdf") == ("005930", "삼성전자")
    assert helpers.extract_ticker_info("reports/005930_삼성전자_20260928.pdf") == ("005930", "삼성전자")
    assert helpers.resolve_company_name("000000", "Stock_000000") == "Stock_000000"


async def _run_send_telegram_message(monkeypatch, messages):
    send = _extract("stock_tracking_agent.py", "send_telegram_message",
                    {"logger": MagicMock(), "require_execution_runtime": lambda agent: None})
    portfolio = types.ModuleType("portfolio_broadcast")
    portfolio.should_send_portfolio = lambda *args, **kwargs: False
    monkeypatch.setitem(sys.modules, "portfolio_broadcast", portfolio)
    agent = SimpleNamespace(message_queue=list(messages), _msg_types=["analysis"] * len(messages))
    agent._clear_message_queue = lambda: None
    assert await send(agent, None, language="ko") is True
    return [text for _type, text in agent.last_batch_messages]


@pytest.mark.asyncio
async def test_decision_summary_falls_back_to_plain_text_when_renderer_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "messaging.korean_trading_message", None)
    messages = ["결정: Enter F1 MA20", "매수 Score: 8"]
    assert await _run_send_telegram_message(monkeypatch, messages) == messages


@pytest.mark.asyncio
async def test_decision_summary_keeps_message_when_render_raises(monkeypatch):
    fake = types.ModuleType("messaging.korean_trading_message")

    def render(text):
        if "bad" in text:
            raise ValueError("render bug")
        return text.upper()

    fake.render_korean_trading_message = render
    monkeypatch.setitem(sys.modules, "messaging.korean_trading_message", fake)
    assert await _run_send_telegram_message(monkeypatch, ["ok", "bad"]) == ["OK", "bad"]
