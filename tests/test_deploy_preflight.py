import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "deploy_preflight.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def preflight(tmp_path, monkeypatch):
    module = _load("deploy_preflight_under_test", monkeypatch)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    module._NAME_CACHE.clear()
    return module


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_static_imports_flag_missing_module_and_name(preflight, tmp_path):
    _write(tmp_path, "messaging/__init__.py", "")
    _write(tmp_path, "messaging/present.py", "def render():\n    return ''\n")
    _write(tmp_path, "stock_tracking_agent.py",
           "from messaging.present import render\n"
           "from messaging.present import missing_name\n"
           "def f():\n"
           "    from messaging.korean_trading_message import render_korean_trading_message\n"
           "try:\n"
           "    from messaging.optional_mod import x\n"
           "except ImportError:\n"
           "    x = None\n")
    report = preflight.Report()
    preflight.check_static_imports(report)
    joined = "\n".join(report.failures)
    assert "missing_name: name not defined" in joined
    assert "messaging.korean_trading_message import ...: module not found" in joined
    assert "import render" not in joined
    assert any("optional_mod" in w for w in report.warnings)
    assert not any("optional_mod" in f for f in report.failures)


def test_signature_contract_detects_unexpected_keyword(preflight, tmp_path, monkeypatch):
    _write(tmp_path, "cores/buy_gate.py",
           "def evaluate_production_buy_gate(scenario, *, current_price, market_regime=None):\n"
           "    return {}\n")
    _write(tmp_path, "stock_tracking_agent.py",
           "from cores.buy_gate import evaluate_production_buy_gate\n"
           "evaluate_production_buy_gate({}, current_price=1, market_pulse=None)\n")
    monkeypatch.setattr(preflight, "SIGNATURE_CONTRACTS", (
        ("cores/buy_gate.py", "evaluate_production_buy_gate", ("stock_tracking_agent.py",)),
    ))
    report = preflight.Report()
    preflight.check_signatures(report)
    assert len(report.failures) == 1
    assert "market_pulse" in report.failures[0]


def test_signature_contract_accepts_matching_call(preflight, tmp_path, monkeypatch):
    _write(tmp_path, "trigger_batch.py",
           "def run_batch(trigger_time, log_level='INFO', output_file=None, macro_context=None,\n"
           "              *, watch_batch_ref=None):\n"
           "    return []\n")
    _write(tmp_path, "stock_analysis_orchestrator.py",
           "run_batch('morning', 'INFO', 'x.json', macro_context={}, watch_batch_ref=None)\n")
    monkeypatch.setattr(preflight, "SIGNATURE_CONTRACTS", (
        ("trigger_batch.py", "run_batch", ("stock_analysis_orchestrator.py",)),
    ))
    report = preflight.Report()
    preflight.check_signatures(report)
    assert report.failures == []


def test_crlf_detection(preflight, tmp_path, monkeypatch):
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "crontab").write_bytes(b"0 8 * * * echo hi\r\n")
    (tmp_path / "ok.sh").write_bytes(b"#!/bin/bash\necho ok\n")
    monkeypatch.setattr(preflight, "_tracked_shell_scripts", lambda: ["ok.sh"])
    report = preflight.Report()
    preflight.check_crlf(report)
    assert report.failures == ["[crlf] docker/crontab contains CRLF line endings"]


def test_repository_static_preflight_passes(monkeypatch):
    module = _load("deploy_preflight_repo", monkeypatch)
    report = module.Report()
    module.check_static_imports(report)
    module.check_signatures(report)
    assert report.failures == []
