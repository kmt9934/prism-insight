import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

if "KIS_CONFIG_ROOT" not in os.environ:
    _root = Path(tempfile.mkdtemp(prefix="kis-retry-"))
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

from trading import kis_auth


class _Resp:
    status_code = 200
    text = "{}"
    headers = {}

    def json(self):
        return {"rt_cd": "0"}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(kis_auth.time, "sleep", lambda _s: None)


def test_get_retries_read_timeout_then_succeeds(monkeypatch):
    calls = []

    def fake_get(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ReadTimeout("slow")
        return _Resp()

    monkeypatch.setattr(kis_auth.requests, "get", fake_get)
    assert kis_auth._get_with_retry("https://x/quotations/inquire-price", {}, {}).status_code == 200
    assert len(calls) == 2


def test_get_gives_up_after_three_attempts(monkeypatch):
    calls = []

    def fake_get(*_a, **_k):
        calls.append(1)
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(kis_auth.requests, "get", fake_get)
    with pytest.raises(requests.exceptions.ConnectionError):
        kis_auth._get_with_retry("https://x/y", {}, {})
    assert len(calls) == 3


def test_post_is_never_retried(monkeypatch):
    calls = []

    def fake_post(*_a, **_k):
        calls.append(1)
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(kis_auth.requests, "post", fake_post)
    monkeypatch.setattr(kis_auth, "getTREnv", lambda: SimpleNamespace(my_url="https://x"))
    monkeypatch.setattr(kis_auth, "_getBaseHeader", lambda: {})
    monkeypatch.setattr(kis_auth, "isPaperTrading", lambda: True)
    with pytest.raises(requests.exceptions.ReadTimeout):
        kis_auth._url_fetch("/uapi/order-cash", "TTTC0802U", "", {}, postFlag=True)
    assert len(calls) == 1
