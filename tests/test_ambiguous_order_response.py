"""Ambiguous KIS order responses are verified against order history, never re-sent."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

if "KIS_CONFIG_ROOT" not in os.environ:
    _root = Path(tempfile.mkdtemp(prefix="kis-ambiguous-"))
    (_root / "kis_devlp.yaml").write_text(
        "default_mode: demo\nauto_trading: true\ndefault_product_code: \"01\"\n"
        "default_unit_amount: 100000\ndefault_unit_amount_usd: 250\n"
        "my_app: PSREALKEY\nmy_sec: s\npaper_app: PSVTTESTKEY\npaper_sec: s\n"
        "my_htsid: t\nprod: https://example.com\nvps: https://example.com\n"
        "ops: wss://example.com\nvops: wss://example.com\nmy_agent: t\n"
        "accounts:\n  - name: demo\n    mode: demo\n    account: \"12345678\"\n"
        "    product: \"01\"\n    market: all\n    primary: true\n",
        encoding="utf-8",
    )
    os.environ["KIS_CONFIG_ROOT"] = str(_root)

from trading import domestic_stock_trading as mod  # noqa: E402

IGW = '{"rt_cd":"0","msg_cd":"IGW00009","msg1":"응답전문 구성 중 오류가 발생하였습니다."}'
RATE = '{"rt_cd":"1","msg_cd":"EGW00201","msg1":"초당 거래건수를 초과하였습니다."}'


def _error(code, text):
    return SimpleNamespace(isOK=lambda: False, getErrorCode=lambda: code, getErrorMessage=lambda: text)


def _history(rows, ok=True):
    return SimpleNamespace(isOK=lambda: ok, getBody=lambda: SimpleNamespace(output1=rows))


def _trader(order_response, history=None):
    trader = mod.DomesticStockTrading.__new__(mod.DomesticStockTrading)
    trader.buy_amount = 1_000_000
    trader.auto_trading = True
    trader.mode = "demo"
    trader.trenv = SimpleNamespace(my_acct="fake", my_prod="01")
    trader.calculate_buy_quantity = MagicMock(return_value=3)
    trader.get_holding_quantity = MagicMock(return_value=3)
    trader._request = MagicMock(return_value=order_response)
    trader._request_with_retry = MagicMock(return_value=history)
    return trader


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)


def _row(qty="3", side="02", code="005930"):
    now = mod.datetime.datetime.now(mod.KST).strftime("%H%M%S")
    return {"pdno": code, "sll_buy_dvsn_cd": side, "ord_qty": qty, "ord_tmd": now, "odno": "0000012345"}


def test_igw00009_with_matching_history_is_a_success_and_not_resent():
    trader = _trader(_error(500, IGW), _history([_row()]))
    result = trader.buy_market_price("005930")
    assert result["success"] is True
    assert result["order_no"] == "0000012345"
    assert result["verified_after_ambiguous"] is True
    assert trader._request.call_count == 1  # the order POST was sent once
    api_url, tr_id, params = trader._request_with_retry.call_args.args
    assert api_url.endswith("inquire-daily-ccld") and tr_id == "VTTC0081R"
    assert params["PDNO"] == "005930" and params["SLL_BUY_DVSN_CD"] == "02"


def test_igw00009_without_matching_history_stays_failed():
    trader = _trader(_error(500, IGW), _history([_row(qty="7")]))
    result = trader.buy_market_price("005930")
    assert result["success"] is False
    assert not result.get("outcome_unknown")
    assert trader._request.call_count == 1


def test_unreadable_history_reports_unknown_not_failed():
    trader = _trader(_error(500, IGW), _history(None, ok=False))
    result = trader.buy_market_price("005930")
    assert result["success"] is False and result["outcome_unknown"] is True
    assert trader._request.call_count == 1


def test_sell_uses_sell_side_in_history_lookup():
    trader = _trader(_error(502, "Bad Gateway"), _history([_row(side="01")]))
    result = trader.sell_all_market_price("005930", quantity=3)
    assert result["success"] is True
    assert trader._request_with_retry.call_args.args[2]["SLL_BUY_DVSN_CD"] == "01"


def test_reserved_order_ambiguity_is_unknown_without_history_lookup():
    trader = _trader(_error(500, IGW))
    result = trader.buy_reserved_order("058610", limit_price=102_800)
    assert result["success"] is False and result["outcome_unknown"] is True
    trader._request_with_retry.assert_not_called()
    assert trader._request.call_count == 1


def test_rate_limit_and_business_rejections_are_plain_failures():
    for response in (_error(500, RATE), _error("APBK0952", "주문가능금액을 초과했습니다")):
        trader = _trader(response)
        result = trader.buy_market_price("005930")
        assert result["success"] is False and not result.get("outcome_unknown")
        trader._request_with_retry.assert_not_called()
