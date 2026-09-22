"""Fail fast when a host still points Korean market data at Kakao login.

The morning batch used to start ``kospi_kosdaq_stock_server``, which logs into
the KRX Data Marketplace with Kakao and then waits on Playwright for 2FA.
That wait does not time out when the phone never shows an approval prompt.
This module refuses that server before a child process is spawned, and blocks
in-process imports of the same clients so a leftover call cannot open a browser.
"""

from __future__ import annotations

import logging
import sys
from importlib.machinery import ModuleSpec
from types import ModuleType

logger = logging.getLogger(__name__)

_BLOCKED_ROOTS = ("krx_data_client", "kospi_kosdaq_stock_server")
_LEGACY_MARKERS = _BLOCKED_ROOTS
_SECRET_ENV_KEYS = ("KAKAO_ID", "KAKAO_PW", "KRX_ID", "KRX_PW")
_REPLACEMENT_ARGS = ["-m", "cores.market_data.mcp_server"]
_installed = False


class KakaoKrxLoginDisabled(RuntimeError):
    """Raised instead of waiting for an interactive KRX session."""


class _BlockedModule(ModuleType):
    def __getattr__(self, name: str):
        raise KakaoKrxLoginDisabled(
            "Kakao/Playwright KRX login is disabled "
            f"({self.__name__}.{name}). "
            "Use KIS, FinanceDataReader, or Naver. "
            "KRX Open API is previous-session EOD only and is not started here."
        )


class _BlockLoader:
    def create_module(self, spec):
        return _BlockedModule(spec.name)

    def exec_module(self, module):
        return None


class _BlockFinder:
    def find_spec(self, fullname, path, target=None):
        root = fullname.split(".", 1)[0]
        if root not in _BLOCKED_ROOTS:
            return None
        return ModuleSpec(fullname, _BlockLoader(), is_package=False)


def install_kakao_login_block() -> None:
    """Install the import block and sanitize mcp-agent settings.

    The import block is once-only. The mcp-agent patch is retried so a later
    import of that package is still covered if it was missing on the first call.
    """
    global _installed
    if not any(isinstance(finder, _BlockFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _BlockFinder())
    for name in _BLOCKED_ROOTS:
        current = sys.modules.get(name)
        if current is not None and not isinstance(current, _BlockedModule):
            logger.error(
                "Replacing already-imported %s so it cannot continue a Kakao login",
                name,
            )
        sys.modules[name] = _BlockedModule(name)
    _patch_mcp_agent_settings()
    if not _installed:
        _installed = True
        logger.info(
            "Kakao/Playwright KRX login disabled; market data stays on KIS, FDR, and Naver"
        )


def disable_kakao_krx_servers(raw: dict) -> dict:
    """Rewrite a YAML config so the legacy KRX server is never launched."""
    servers = _server_entries(raw)
    for name, spec in list(servers.items()):
        if isinstance(spec, dict):
            _rewrite_mapping(name, spec)
    return raw


def sanitize_mcp_settings(settings):
    """Rewrite an mcp-agent Settings object in place. Secrets are not logged."""
    mcp = getattr(settings, "mcp", None)
    servers = getattr(mcp, "servers", None) if mcp is not None else None
    if not servers:
        return settings
    for name, spec in list(servers.items()):
        if isinstance(spec, dict):
            _rewrite_mapping(name, spec)
            continue
        command = str(getattr(spec, "command", "") or "")
        args = list(getattr(spec, "args", None) or [])
        blob = " ".join([command, *[str(item) for item in args]])
        env = dict(getattr(spec, "env", None) or {})
        if not _needs_rewrite(blob, env):
            continue
        if _is_legacy_command(blob):
            logger.error(
                "Refusing Kakao/Playwright KRX MCP server %s; "
                "using cores.market_data.mcp_server",
                name,
            )
            if "python" not in command:
                command = "python3"
            spec.command = command
            spec.args = list(_REPLACEMENT_ARGS)
        for key in _SECRET_ENV_KEYS:
            env.pop(key, None)
        try:
            spec.env = env
        except Exception:
            logger.error("Could not clear legacy login env on MCP server %s", name)
    return settings


def _patch_mcp_agent_settings() -> None:
    try:
        import mcp_agent.config as config
    except ImportError:
        return

    original = config.get_settings
    if getattr(original, "_prism_kakao_guard", False):
        wrapped = original
    else:
        def wrapped(*args, **kwargs):
            return sanitize_mcp_settings(original(*args, **kwargs))

        wrapped._prism_kakao_guard = True  # type: ignore[attr-defined]
        config.get_settings = wrapped

    try:
        import mcp_agent.app as app_module
    except ImportError:
        return
    if getattr(app_module, "get_settings", None) is original or not getattr(
        app_module.get_settings, "_prism_kakao_guard", False
    ):
        app_module.get_settings = wrapped


def _server_entries(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    if "servers" in raw and "mcp" not in raw:
        servers = raw.get("servers") or {}
    else:
        servers = ((raw.get("mcp") or {}).get("servers") or {})
    return servers if isinstance(servers, dict) else {}


def _is_legacy_command(blob: str) -> bool:
    return any(marker in blob for marker in _LEGACY_MARKERS)


def _needs_rewrite(blob: str, env: dict) -> bool:
    return _is_legacy_command(blob) or any(key in env for key in _SECRET_ENV_KEYS)


def _rewrite_mapping(name: str, spec: dict) -> None:
    args = spec.get("args") or []
    if not isinstance(args, list):
        args = [args]
    blob = " ".join([str(spec.get("command") or ""), *[str(item) for item in args]])
    env = dict(spec.get("env") or {})
    if not _needs_rewrite(blob, env):
        return
    if _is_legacy_command(blob):
        logger.error(
            "Refusing Kakao/Playwright KRX MCP server %s; "
            "using cores.market_data.mcp_server",
            name,
        )
        command = str(spec.get("command") or "python3")
        if "python" not in command:
            command = "python3"
        spec["command"] = command
        spec["args"] = list(_REPLACEMENT_ARGS)
    for key in _SECRET_ENV_KEYS:
        env.pop(key, None)
    spec["env"] = env
