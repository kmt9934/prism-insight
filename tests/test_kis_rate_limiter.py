"""Shared KIS call pacing (trading.kis_rate_limiter) and its kis_auth hook."""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import time
from pathlib import Path

import pytest

if "KIS_CONFIG_ROOT" not in os.environ:
    _root = Path(tempfile.mkdtemp(prefix="kis-limiter-"))
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

from trading import kis_auth, kis_rate_limiter  # noqa: E402


class _Clock:
    def __init__(self, start=1000.0):
        self.now = start
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "kis_rate.state"
    monkeypatch.setenv("PRISM_KIS_RATE_LIMIT_FILE", str(path))
    monkeypatch.delenv("PRISM_KIS_RATE_LIMIT_ENABLED", raising=False)
    monkeypatch.delenv("PRISM_KIS_RATE_LIMIT_PAPER_PER_SEC", raising=False)
    return path


def test_paper_limit_is_two_calls_per_second(state_file):
    clock = _Clock()
    waits, starts = [], []
    for _ in range(6):
        waits.append(kis_rate_limiter.acquire(True, clock=clock, sleep=clock.sleep))
        starts.append(clock())
    assert waits[0] == 0 and waits[1] == 0
    assert waits[2] > 0.9  # the third call in the same second must wait
    assert sum(1 for w in waits if w > 0) >= 2
    # No more than two call starts fit in any one-second window.
    for i in range(len(starts) - 2):
        assert starts[i + 2] - starts[i] >= kis_rate_limiter.WINDOW_SEC


def test_real_server_uses_its_own_budget(state_file):
    clock = _Clock()
    waits = [kis_rate_limiter.acquire(False, clock=clock, sleep=clock.sleep) for _ in range(10)]
    assert waits == [0.0] * 10


def test_kill_switch_disables_pacing(state_file, monkeypatch):
    monkeypatch.setenv("PRISM_KIS_RATE_LIMIT_ENABLED", "false")
    clock = _Clock()
    assert [kis_rate_limiter.acquire(True, clock=clock, sleep=clock.sleep) for _ in range(5)] == [0.0] * 5
    assert not state_file.exists()


def test_unusable_state_file_falls_back_to_process_pacing(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("PRISM_KIS_RATE_LIMIT_FILE", str(blocker / "state"))
    monkeypatch.setattr(kis_rate_limiter, "_local_stamps", [])
    clock = _Clock()
    waits = [kis_rate_limiter.acquire(True, clock=clock, sleep=clock.sleep) for _ in range(3)]
    assert waits[:2] == [0.0, 0.0] and waits[2] > 0


def _worker(path, count, queue):
    os.environ["PRISM_KIS_RATE_LIMIT_FILE"] = path
    from trading import kis_rate_limiter as limiter

    stamps = []
    for _ in range(count):
        limiter.acquire(True)
        stamps.append(time.time())
    queue.put(stamps)


@pytest.mark.skipif(kis_rate_limiter.fcntl is None, reason="needs fcntl")
def test_two_processes_share_one_budget(tmp_path):
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    path = str(tmp_path / "shared.state")
    procs = [ctx.Process(target=_worker, args=(path, 3, queue)) for _ in range(2)]
    for proc in procs:
        proc.start()
    stamps = sorted(queue.get(timeout=30) + queue.get(timeout=30))
    for proc in procs:
        proc.join(timeout=30)
    # Six calls at two per second need at least two full windows.
    assert stamps[-1] - stamps[0] >= 2 * kis_rate_limiter.WINDOW_SEC - 0.05
    for i in range(len(stamps) - 2):
        assert stamps[i + 2] - stamps[i] >= kis_rate_limiter.WINDOW_SEC - 0.02


def test_kis_auth_paces_get_and_post_but_never_retries_post(monkeypatch):
    paced = []
    monkeypatch.setattr(kis_auth, "_pace_kis_call", lambda: paced.append(1))
    monkeypatch.setattr(kis_auth.time, "sleep", lambda _s: None)

    class _Resp:
        status_code = 500
        text = '{"rt_cd":"1","msg_cd":"EGW00201","msg1":"초당 거래건수를 초과하였습니다."}'

    posts = []
    monkeypatch.setattr(kis_auth.requests, "post", lambda *a, **k: posts.append(1) or _Resp())
    monkeypatch.setattr(kis_auth, "_getBaseHeader", lambda: {})
    monkeypatch.setattr(kis_auth, "getTREnv", lambda: type("T", (), {"my_url": "https://example.com"})())
    result = kis_auth._url_fetch("/uapi/domestic-stock/v1/trading/order-cash", "VTTC0012U", "", {}, postFlag=True)
    assert posts == [1]  # one POST, even though it was rate limited
    assert paced == [1]
    assert not result.isOK()

    gets = []
    monkeypatch.setattr(kis_auth.requests, "get", lambda *a, **k: gets.append(1) or _Resp())
    kis_auth._get_with_retry("https://example.com/x", {}, {})
    assert gets == [1] and paced == [1, 1]


def test_broken_limiter_never_blocks_calls(monkeypatch):
    class _Broken:
        @staticmethod
        def acquire(_paper):
            raise RuntimeError("boom")

    monkeypatch.setattr(kis_auth, "_rate_limiter_module", _Broken)
    kis_auth._pace_kis_call()  # must not raise
