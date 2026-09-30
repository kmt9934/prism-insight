"""The morning cache warmer must fetch the same exact bars the batch reads."""
import importlib.util
from pathlib import Path
import sys

import pandas as pd

from cores.kis_market_snapshot import KisMasterData, KisSnapshotError

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "prefetch_kr_previous_history", ROOT / "tools" / "prefetch_kr_previous_history.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _master():
    cap = pd.DataFrame({"시가총액": [1e11, 0.0, 5e10]}, index=["005930", "123450", "000660"])
    return KisMasterData({"005930": "A", "123450": "B", "000660": "C"}, cap, {}, "20260930",
                         {}, {}, {})


def test_prefetches_previous_session_for_nonzero_cap(monkeypatch):
    module = _load()
    calls = {}
    monkeypatch.setattr(module, "_today_kst", lambda: "20260930")
    monkeypatch.setattr(module, "is_market_day", lambda _day: True)
    monkeypatch.setattr(module, "previous_session", lambda _d: "20260929")
    monkeypatch.setattr(module, "fetch_kis_master_data", _master)

    def history(codes, prev_date, **kwargs):
        calls.update(codes=codes, prev_date=prev_date, **kwargs)
        return pd.DataFrame(index=codes)

    monkeypatch.setattr(module, "fetch_kis_previous_history", history)
    monkeypatch.setattr(sys, "argv", ["prefetch"])
    assert module.main() == 0
    assert calls["codes"] == ["000660", "005930"]
    assert calls["prev_date"] == "20260929"
    assert calls["request_interval_sec"] == 0.5


def test_non_market_day_skips_and_partial_failure_reports(monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "_today_kst", lambda: "20261003")
    monkeypatch.setattr(module, "is_market_day", lambda _day: False)
    monkeypatch.setattr(module, "fetch_kis_master_data",
                        lambda: (_ for _ in ()).throw(AssertionError("must not fetch")))
    monkeypatch.setattr(sys, "argv", ["prefetch"])
    assert module.main() == 0

    monkeypatch.setattr(module, "is_market_day", lambda _day: True)
    monkeypatch.setattr(module, "previous_session", lambda _d: "20261002")
    monkeypatch.setattr(module, "fetch_kis_master_data", _master)

    def failing(*_args, **_kwargs):
        raise KisSnapshotError("KIS history deadline; coverage=10/2")

    monkeypatch.setattr(module, "fetch_kis_previous_history", failing)
    assert module.main() == 1
