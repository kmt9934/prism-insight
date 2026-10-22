"""EGW00201: even spacing, shared cooldown, GET backoff, POST sent once."""
import types
import os
import tempfile
from pathlib import Path


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

import pytest

from trading import kis_rate_limiter as rl
from trading import kis_auth as ka


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_KIS_RATE_LIMIT_FILE", str(tmp_path / "s.state"))
    monkeypatch.setenv("PRISM_KIS_RATE_LIMIT_ENABLED", "true")
    yield


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_paper_calls_are_spread_not_burst():
    c = Clock()
    for _ in range(5):
        rl.acquire(True, clock=c, sleep=c.sleep)
    stamps = sorted(rl._parse((open(rl._state_path()).read())))
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.6 - 1e-6 for g in gaps)


def test_required_wait_gap_and_cooldown():
    assert rl._required_wait([10.0], 10.1, 2, gap=0.6) == pytest.approx(0.5)
    assert rl._required_wait([], 10.0, 2, cooldown_until=11.0) == pytest.approx(1.0)
    assert rl._required_wait([], 10.0, 2) == 0.0


def test_cooldown_is_shared_via_state_file():
    c = Clock()
    rl.acquire(True, clock=c, sleep=c.sleep)
    until = rl.note_rate_limited(seconds=2.0, clock=c)
    assert until == pytest.approx(c.t + 2.0)
    stamps, cd = rl._parse_state(open(rl._state_path()).read())
    assert cd == pytest.approx(until) and len(stamps) == 1
    rl.acquire(True, clock=c, sleep=c.sleep)
    assert c.t >= until


def _resp(code, text):
    return types.SimpleNamespace(status_code=code, text=text)


RATE = '{"rt_cd":"1","msg_cd":"EGW00201","msg1":"초당 거래건수를 초과하였습니다."}'


def test_get_retries_on_egw00201_then_succeeds(monkeypatch):
    seq = [_resp(500, RATE), _resp(500, RATE), _resp(200, "{}")]
    monkeypatch.setattr(ka, "_get_with_retry", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(ka.time, "sleep", lambda s: None)
    notes = []
    monkeypatch.setattr(ka, "_note_kis_rate_limited", lambda: notes.append(1))
    res = ka._get_with_rate_retry("u", {}, {}, "FHKST01010100")
    assert res.status_code == 200 and len(notes) == 2


def test_get_gives_up_after_max(monkeypatch):
    monkeypatch.setenv("PRISM_KIS_RATE_RETRY_MAX", "2")
    calls = []
    monkeypatch.setattr(ka, "_get_with_retry", lambda *a, **k: calls.append(1) or _resp(500, RATE))
    monkeypatch.setattr(ka.time, "sleep", lambda s: None)
    monkeypatch.setattr(ka, "_note_kis_rate_limited", lambda: None)
    res = ka._get_with_rate_retry("u", {}, {})
    assert res.status_code == 500 and len(calls) == 3


def test_other_errors_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(ka, "_get_with_retry", lambda *a, **k: calls.append(1) or _resp(500, '{"msg_cd":"EGW00123"}'))
    ka._get_with_rate_retry("u", {}, {})
    assert len(calls) == 1


def test_backoff_grows():
    d = ka._rate_retry_delays()
    assert len(d) == 4 and d[0] < d[1] < d[2] < d[3]
