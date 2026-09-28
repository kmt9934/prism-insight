#!/usr/bin/env python3
"""Deploy preflight: refuse a tree whose modules no longer fit together.

Checks (non-zero exit on any failure):
  a) every repository-local ``import x`` / ``from x import y`` in runtime code
     resolves to an existing module and name (optional imports inside
     ``try/except ImportError`` are reported as warnings only);
  b) the cron/bot entrypoints import cleanly (child process, outbound network
     blocked, ``__main__`` never executed);
  c) key cross-module call sites still bind to their callee signatures;
  d) docker/crontab and tracked ``*.sh`` files contain no CRLF.

It never places orders, calls a broker, or sends a Telegram message: (a), (c)
and (d) are pure source inspection, and (b) only imports modules with sockets
disabled.

Usage:
  python3 tools/deploy_preflight.py              # all checks
  python3 tools/deploy_preflight.py --static-only  # skip (b), e.g. on a dev box
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent

RUNTIME_PACKAGES = (
    "cores", "tracking", "trading", "messaging", "prism_core", "observability", "tools",
)
EXCLUDED_PARTS = {"tests", "__pycache__", ".venv", "venv", "node_modules", ".git", "archive"}
# Files that put prism-us on sys.path resolve `cores` to prism-us/cores at
# runtime, which static resolution against the repo root cannot follow.
MARKET_SWITCH_MARKER = "prism-us"

ENTRYPOINT_MODULES = (
    "stock_analysis_orchestrator",
    "stock_tracking_enhanced_agent",
    "stock_tracking_agent",
    "trigger_batch",
    "performance_tracker_batch",
    "telegram_ai_bot",
)
ENTRYPOINT_FILES = (
    "tools/hardstop_seller.py",
    "tools/trend_exit_seller.py",
    "tools/fill_chaser.py",
    "tools/buy_intensity_luna_recommend.py",
    "tools/shadow_lifecycle.py",
)

# (callee module file, callee function, caller files) — call sites are read
# from the callers so the check follows whatever arguments they pass today.
SIGNATURE_CONTRACTS = (
    ("cores/buy_gate.py", "evaluate_production_buy_gate",
     ("stock_tracking_agent.py", "prism-us/us_stock_tracking_agent.py")),
    ("tracking/helpers.py", "evaluate_pyramid_add_gate",
     ("stock_tracking_agent.py",)),
    ("trigger_batch.py", "run_batch",
     ("stock_analysis_orchestrator.py",)),
    ("messaging/korean_trading_message.py", "render_korean_trading_message",
     ("stock_tracking_agent.py", "prism-us/us_stock_tracking_agent.py")),
)

CRLF_FILES = ("docker/crontab",)

OPTIONAL_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


# ── (a) static local import resolution ────────────────────────────────────────

def _runtime_files() -> list[Path]:
    files = [p for p in ROOT.glob("*.py")]
    for pkg in RUNTIME_PACKAGES:
        base = ROOT / pkg
        if base.is_dir():
            files.extend(base.rglob("*.py"))
    return sorted(
        p for p in files
        if not EXCLUDED_PARTS.intersection(p.relative_to(ROOT).parts)
        and not p.name.startswith("test_")
    )


def _local_roots() -> set[str]:
    roots = {p.stem for p in ROOT.glob("*.py")}
    roots.update(p.name for p in ROOT.iterdir() if p.is_dir() and (p / "__init__.py").is_file())
    for pkg in RUNTIME_PACKAGES:
        if (ROOT / pkg).is_dir():
            roots.add(pkg)
    return roots


def _module_path(dotted: str, search: Iterable[Path]) -> Path | None:
    parts = dotted.split(".")
    for base in search:
        candidate = base.joinpath(*parts)
        if (candidate / "__init__.py").is_file():
            return candidate / "__init__.py"
        if candidate.with_suffix(".py").is_file():
            return candidate.with_suffix(".py")
        if candidate.is_dir():  # namespace package
            return candidate
    return None


_NAME_CACHE: dict[Path, tuple[set[str], bool]] = {}


def _defined_names(path: Path) -> tuple[set[str], bool]:
    """Top-level names of a module and whether it accepts any name."""
    if path in _NAME_CACHE:
        return _NAME_CACHE[path]
    if path.is_dir():
        result = ({p.stem for p in path.glob("*.py")} | {p.name for p in path.iterdir() if p.is_dir()}, False)
        _NAME_CACHE[path] = result
        return result
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        _NAME_CACHE[path] = (set(), True)
        return _NAME_CACHE[path]
    names: set[str] = set()
    open_ended = False

    def visit(stmts):
        nonlocal open_ended
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
                if node.name == "__getattr__":
                    open_ended = True
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    for n in ast.walk(target):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        open_ended = True
                    else:
                        names.add(alias.asname or alias.name)
            elif isinstance(node, (ast.If, ast.With, ast.AsyncWith, ast.For, ast.While)):
                visit(node.body)
                visit(getattr(node, "orelse", []))
            elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
                visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)

    visit(tree.body)
    if path.name == "__init__.py":
        pkg_dir = path.parent
        names.update(p.stem for p in pkg_dir.glob("*.py"))
        names.update(p.name for p in pkg_dir.iterdir() if p.is_dir())
    _NAME_CACHE[path] = (names, open_ended)
    return _NAME_CACHE[path]


def _optional_import_nodes(tree: ast.AST) -> set[int]:
    optional: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        caught = set()
        for handler in node.handlers:
            if handler.type is None:
                caught.add("BaseException")
            else:
                for n in ast.walk(handler.type):
                    if isinstance(n, ast.Name):
                        caught.add(n.id)
                    elif isinstance(n, ast.Attribute):
                        caught.add(n.attr)
        if caught & OPTIONAL_IMPORT_ERRORS:
            for stmt in node.body:
                for n in ast.walk(stmt):
                    if isinstance(n, (ast.Import, ast.ImportFrom)):
                        optional.add(id(n))
    return optional


def check_static_imports(report: Report) -> int:
    roots = _local_roots()
    checked = 0
    for path in _runtime_files():
        rel = path.relative_to(ROOT).as_posix()
        try:
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            report.fail(f"[static] {rel}: syntax error: {exc}")
            continue
        except UnicodeDecodeError as exc:
            report.fail(f"[static] {rel}: not UTF-8: {exc}")
            continue
        optional = _optional_import_nodes(tree)
        market_switch = MARKET_SWITCH_MARKER in source
        search = (ROOT, path.parent)
        for node in ast.walk(tree):
            problems: list[str] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] not in roots:
                        continue
                    checked += 1
                    if _module_path(alias.name, search) is None:
                        problems.append(f"import {alias.name}: module not found")
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = path.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    dotted = node.module or ""
                    mod_path = _module_path(dotted, (base,)) if dotted else base / "__init__.py"
                    label = "." * node.level + dotted
                else:
                    if not node.module or node.module.split(".")[0] not in roots:
                        continue
                    mod_path = _module_path(node.module, search)
                    label = node.module
                checked += 1
                if mod_path is None or not mod_path.exists():
                    problems.append(f"from {label} import ...: module not found")
                else:
                    names, open_ended = _defined_names(mod_path)
                    if not open_ended:
                        for alias in node.names:
                            if alias.name != "*" and alias.name not in names:
                                problems.append(f"from {label} import {alias.name}: name not defined")
            for problem in problems:
                msg = f"[static] {rel}:{node.lineno}: {problem}"
                if id(node) in optional:
                    report.warn(msg + " (optional import)")
                elif market_switch:
                    report.warn(msg + " (file switches sys.path to prism-us)")
                else:
                    report.fail(msg)
    return checked


# ── (b) entrypoint imports (child process, sockets blocked) ───────────────────

_CHILD = r"""
import importlib, importlib.util, json, socket, sys, traceback
def _blocked(*a, **k):
    raise OSError("network disabled by deploy_preflight")
socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
root, kind, target = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, root)
try:
    if kind == "module":
        importlib.import_module(target)
    else:
        spec = importlib.util.spec_from_file_location("_preflight_entry", target)
        module = importlib.util.module_from_spec(spec)
        sys.modules["_preflight_entry"] = module
        spec.loader.exec_module(module)
    print(json.dumps({"ok": True}))
except BaseException as exc:
    print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                      "trace": traceback.format_exc()[-2000:]}))
"""


def check_entrypoints(report: Report, timeout: int) -> None:
    env = dict(os.environ)
    env.update({
        "PRISM_DEPLOY_PREFLIGHT": "1",
        "TELEGRAM_BOT_TOKEN": "",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    targets = [("module", m) for m in ENTRYPOINT_MODULES]
    targets += [("file", str(ROOT / f)) for f in ENTRYPOINT_FILES]
    for kind, target in targets:
        label = target if kind == "module" else Path(target).relative_to(ROOT).as_posix()
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _CHILD, str(ROOT), kind, target],
                cwd=str(ROOT), env=env, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            report.fail(f"[import] {label}: timed out after {timeout}s")
            continue
        line = next((l for l in reversed(proc.stdout.splitlines()) if l.startswith("{")), "")
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            report.fail(f"[import] {label}: no result (rc={proc.returncode}) {proc.stderr[-500:]}")
            continue
        if result.get("ok"):
            print(f"  ok  import {label}")
        else:
            report.fail(f"[import] {label}: {result.get('error')}\n{result.get('trace', '')}")


# ── (c) call-site vs signature ────────────────────────────────────────────────

def _signature_from_ast(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> inspect.Signature:
    a = fn.args
    params: list[inspect.Parameter] = []
    positional = list(a.posonlyargs) + list(a.args)
    n_defaults = len(a.defaults)
    for i, arg in enumerate(positional):
        kind = (inspect.Parameter.POSITIONAL_ONLY if i < len(a.posonlyargs)
                else inspect.Parameter.POSITIONAL_OR_KEYWORD)
        has_default = i >= len(positional) - n_defaults
        params.append(inspect.Parameter(arg.arg, kind,
                                        default=None if has_default else inspect.Parameter.empty))
    if a.vararg:
        params.append(inspect.Parameter(a.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        params.append(inspect.Parameter(arg.arg, inspect.Parameter.KEYWORD_ONLY,
                                        default=None if default is not None else inspect.Parameter.empty))
    if a.kwarg:
        params.append(inspect.Parameter(a.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(params)


def _find_function(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def check_signatures(report: Report) -> None:
    for callee_file, func_name, callers in SIGNATURE_CONTRACTS:
        callee_path = ROOT / callee_file
        if not callee_path.is_file():
            report.fail(f"[signature] {callee_file} missing (needed for {func_name})")
            continue
        fn = _find_function(callee_path, func_name)
        if fn is None:
            report.fail(f"[signature] {callee_file}: {func_name} not defined at module level")
            continue
        sig = _signature_from_ast(fn)
        n_calls = 0
        for caller in callers:
            caller_path = ROOT / caller
            if not caller_path.is_file():
                continue
            tree = ast.parse(caller_path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                called = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if called != func_name:
                    continue
                if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                    continue
                n_calls += 1
                try:
                    sig.bind(*([None] * len(node.args)), **{k.arg: None for k in node.keywords})
                except TypeError as exc:
                    report.fail(f"[signature] {caller}:{node.lineno}: {func_name}{sig} rejects call: {exc}")
        if n_calls == 0:
            report.warn(f"[signature] no direct call sites found for {func_name} in {', '.join(callers)}")
        else:
            print(f"  ok  {func_name}: {n_calls} call site(s) bind")


# ── (d) CRLF ─────────────────────────────────────────────────────────────────

def _tracked_shell_scripts() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "*.sh"], cwd=str(ROOT),
                             capture_output=True, text=True, check=True).stdout
        return [l for l in out.splitlines() if l.strip()]
    except (OSError, subprocess.CalledProcessError):
        return [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.sh")
                if not EXCLUDED_PARTS.intersection(p.relative_to(ROOT).parts)]


def check_crlf(report: Report) -> None:
    for rel in list(CRLF_FILES) + _tracked_shell_scripts():
        path = ROOT / rel
        if path.is_file() and b"\r\n" in path.read_bytes():
            report.fail(f"[crlf] {rel} contains CRLF line endings")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--static-only", action="store_true",
                        help="skip entrypoint imports (for hosts without runtime deps)")
    parser.add_argument("--import-timeout", type=int, default=120)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    report = Report()
    print("[a] static local imports")
    checked = check_static_imports(report)
    print(f"  checked {checked} local import statements")
    if args.static_only:
        print("[b] entrypoint imports: skipped (--static-only)")
    else:
        print("[b] entrypoint imports")
        check_entrypoints(report, args.import_timeout)
    print("[c] call signatures")
    check_signatures(report)
    print("[d] CRLF")
    check_crlf(report)

    for msg in report.warnings:
        print(f"WARN {msg}")
    for msg in report.failures:
        print(f"FAIL {msg}")
    if report.failures:
        print(f"PREFLIGHT FAILED: {len(report.failures)} problem(s)")
        return 1
    print("PREFLIGHT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
