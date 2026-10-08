"""kis_source._fetch retries EGW00201 (1s, 2s; three tries) and nothing else."""

from __future__ import annotations

import pytest

pytest.importorskip("pandas")

from cores.market_data import kis_source as module  # noqa: E402
from cores.market_data.kis_source import KisSource  # noqa: E402
from cores.market_data.source import Unavailable  # noqa: E402

RATE = '{"rt_cd":"1","msg_cd":"EGW00201","msg1":"초당 거래건수를 초과하였습니다."}'


class _Body:
    output2 = [{"stck_bsop_date": "20261008"}]


class _Resp:
    def __init__(self, ok, error=""):
        self._ok, self._error = ok, error

    def isOK(self):  # noqa: N802
        return self._ok

    def getBody(self):  # noqa: N802
        return _Body()

    def getErrorMessage(self):  # noqa: N802
        return self._error


class _Client:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def _request(self, api_url, tr_id, params, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def slept(monkeypatch):
    waits = []
    monkeypatch.setattr(module, "sleep", waits.append)
    return waits


def _source(client):
    source = KisSource()
    source._client = client
    return source


def test_rate_limited_then_ok(slept):
    client = _Client([_Resp(False, RATE), _Resp(True)])
    body = _source(client)._fetch("/x", "FHKUP03500100", {})
    assert body.output2
    assert slept == [1.0]
    assert all("postFlag" not in call for call in client.calls)  # GET only


def test_gives_up_after_three_tries(slept):
    client = _Client([_Resp(False, RATE)] * 3)
    with pytest.raises(Unavailable, match="EGW00201"):
        _source(client)._fetch("/x", "FHKUP03500100", {})
    assert slept == [1.0, 2.0]
    assert len(client.calls) == 3


def test_rate_limit_raised_as_exception_is_retried(slept):
    client = _Client([RuntimeError(RATE), _Resp(True)])
    _source(client)._fetch("/x", "FHKST03010100", {})
    assert slept == [1.0]


def test_other_rejections_are_not_retried(slept):
    client = _Client([_Resp(False, "APBK0013 bad ticker")])
    with pytest.raises(Unavailable, match="APBK0013"):
        _source(client)._fetch("/x", "FHKST03010100", {})
    assert slept == [] and len(client.calls) == 1
