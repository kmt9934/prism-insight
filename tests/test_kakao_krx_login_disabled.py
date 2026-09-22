"""Morning market data must not wait on Kakao/Playwright KRX login."""
import sys
import time

import pytest

from cores.market_data.kakao_login_guard import (
    KakaoKrxLoginDisabled,
    disable_kakao_krx_servers,
    install_kakao_login_block,
    sanitize_mcp_settings,
)
from cores.market_data.source_order import sanitize_source_order


def test_source_order_drops_krx_and_keeps_public_fallbacks():
    assert sanitize_source_order("kis,naver,krx") == "kis,naver"
    assert sanitize_source_order("krx") == "kis,fdr,naver"
    assert sanitize_source_order("fdr,krx") == "fdr"
    assert sanitize_source_order("") == "kis,fdr,naver"


def test_legacy_mcp_server_is_rewritten_and_secrets_removed():
    raw = {
        "mcp": {
            "servers": {
                "kospi_kosdaq": {
                    "command": "python3",
                    "args": ["-m", "kospi_kosdaq_stock_server"],
                    "env": {"KAKAO_ID": "person@example.com", "KAKAO_PW": "secret", "PYTHONPATH": "."},
                }
            }
        }
    }
    disable_kakao_krx_servers(raw)
    spec = raw["mcp"]["servers"]["kospi_kosdaq"]
    assert spec["args"] == ["-m", "cores.market_data.mcp_server"]
    assert "KAKAO_ID" not in spec["env"]
    assert "KAKAO_PW" not in spec["env"]
    assert spec["env"]["PYTHONPATH"] == "."


def test_settings_object_refuses_krx_data_client_command():
    class Spec:
        def __init__(self):
            self.command = "/usr/bin/python3"
            self.args = ["-m", "krx_data_client"]
            self.env = {"KRX_ID": "id", "KRX_PW": "pw"}

    class Settings:
        def __init__(self):
            self.mcp = type("M", (), {})()
            self.mcp.servers = {"kospi_kosdaq": Spec()}

    settings = sanitize_mcp_settings(Settings())
    spec = settings.mcp.servers["kospi_kosdaq"]
    assert spec.args == ["-m", "cores.market_data.mcp_server"]
    assert spec.env == {}


def test_registry_loader_rewrites_legacy_server(tmp_path):
    from cores.llm.config_loader import load_mcp_registry

    path = tmp_path / "mcp.yaml"
    path.write_text(
        "mcp:\n"
        "  servers:\n"
        "    kospi_kosdaq:\n"
        "      command: python3\n"
        "      args: ['-m', 'kospi_kosdaq_stock_server']\n"
        "      env:\n"
        "        KAKAO_PW: secret\n",
        encoding="utf-8",
    )
    spec = load_mcp_registry(path).get("kospi_kosdaq")
    assert spec.args == ("-m", "cores.market_data.mcp_server")
    assert "KAKAO_PW" not in spec.env


def test_blocked_import_fails_fast_without_waiting():
    install_kakao_login_block()
    started = time.monotonic()
    with pytest.raises(KakaoKrxLoginDisabled):
        import krx_data_client
        krx_data_client.get_market_ohlcv_by_ticker("20260922")
    assert time.monotonic() - started < 2
    assert "krx_data_client" in sys.modules
