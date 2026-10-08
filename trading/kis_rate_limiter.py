"""Cross-process pacing for KIS REST calls.

Every loop in the container (morning history prefetch, batch analysis,
hardstop/trend-exit sellers, order placement) shares one KIS app key, and KIS
rejects the whole key with EGW00201 once it sees too many calls in a second.
Per-process sleeps cannot see each other, so this module keeps the timestamps
of recent calls in a small file guarded by ``fcntl.flock``: before a call, the
caller waits until fewer than ``limit`` calls started in the last second.

Defaults: 2 calls/sec for the paper (mock) server, 15 for the real server.
Knobs (read at call time, tolerant parsing):

- ``PRISM_KIS_RATE_LIMIT_ENABLED`` (default true) — emergency off switch.
- ``PRISM_KIS_RATE_LIMIT_PAPER_PER_SEC`` / ``PRISM_KIS_RATE_LIMIT_REAL_PER_SEC``.
- ``PRISM_KIS_RATE_LIMIT_FILE`` — state file (default ``runtime/kis_rate_limit.state``).

The limiter only paces; it never retries or drops a call. If the shared file
cannot be used (no ``fcntl``, read-only disk) it falls back to a per-process
limiter and logs one warning, so a broken lock never blocks trading.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

try:  # POSIX only; Windows dev machines use the in-process fallback.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None  # type: ignore

try:
    from prism_core.env_config import env_bool, env_float
except ImportError:  # pragma: no cover - repo root not on sys.path
    def env_bool(name: str, default: bool) -> bool:
        raw = (os.getenv(name) or "").strip().lower()
        return default if not raw else raw not in {"0", "false", "no", "off"}

    def env_float(name: str, default: float) -> float:
        try:
            return float((os.getenv(name) or "").strip() or default)
        except ValueError:
            return default

logger = logging.getLogger(__name__)

WINDOW_SEC = 1.0
# A little slack so clock jitter between processes never lets a third call in.
_SAFETY_SEC = 0.05
_MAX_SINGLE_WAIT_SEC = 5.0
_DEFAULT_PAPER_PER_SEC = 2.0
_DEFAULT_REAL_PER_SEC = 15.0

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_local_lock = threading.Lock()
_local_stamps: list[float] = []
_fallback_warned = False

stats = {"calls": 0, "waits": 0, "waited_sec": 0.0, "shared": 0, "local": 0}


def _state_path() -> Path:
    raw = (os.getenv("PRISM_KIS_RATE_LIMIT_FILE") or "").strip()
    if raw:
        return Path(raw)
    return _PROJECT_ROOT / "runtime" / "kis_rate_limit.state"


def calls_per_second(paper: bool) -> float:
    if paper:
        value = env_float("PRISM_KIS_RATE_LIMIT_PAPER_PER_SEC", _DEFAULT_PAPER_PER_SEC)
    else:
        value = env_float("PRISM_KIS_RATE_LIMIT_REAL_PER_SEC", _DEFAULT_REAL_PER_SEC)
    return value if value > 0 else (_DEFAULT_PAPER_PER_SEC if paper else _DEFAULT_REAL_PER_SEC)


def _required_wait(stamps: list[float], now: float, limit: int) -> float:
    """Seconds to wait so that this call is at most the ``limit``-th in a window."""
    recent = sorted(s for s in stamps if now - s < WINDOW_SEC + _SAFETY_SEC and s <= now + WINDOW_SEC)
    if len(recent) < limit:
        return 0.0
    # The call that would leave the window first decides when a slot frees up.
    oldest_blocking = recent[len(recent) - limit]
    return max(0.0, oldest_blocking + WINDOW_SEC + _SAFETY_SEC - now)


def _parse(text: str) -> list[float]:
    stamps = []
    for token in text.split():
        try:
            stamps.append(float(token))
        except ValueError:
            continue
    return stamps


def _acquire_shared(limit: int, clock: Callable[[], float], sleep: Callable[[float], None]) -> float:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    waited = 0.0
    with open(path, "a+", encoding="ascii") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            stamps = _parse(handle.read())
            now = clock()
            wait = min(_required_wait(stamps, now, limit), _MAX_SINGLE_WAIT_SEC)
            if wait > 0:
                # Holding the lock while sleeping keeps waiters in FIFO-ish order;
                # the wait is bounded by one window.
                sleep(wait)
                waited = wait
                now = clock()
            keep = [s for s in stamps if now - s < WINDOW_SEC + _SAFETY_SEC and s <= now + WINDOW_SEC]
            keep.append(now)
            handle.seek(0)
            handle.truncate()
            handle.write(" ".join(f"{s:.6f}" for s in keep[-max(limit, 1) * 4:]))
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return waited


def _acquire_local(limit: int, clock: Callable[[], float], sleep: Callable[[float], None]) -> float:
    with _local_lock:
        now = clock()
        wait = min(_required_wait(_local_stamps, now, limit), _MAX_SINGLE_WAIT_SEC)
        if wait > 0:
            sleep(wait)
            now = clock()
        _local_stamps[:] = [s for s in _local_stamps if now - s < WINDOW_SEC + _SAFETY_SEC]
        _local_stamps.append(now)
        return wait


def acquire(
    paper: bool,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> float:
    """Block until one more KIS call fits the shared budget. Returns seconds waited."""
    global _fallback_warned
    if not env_bool("PRISM_KIS_RATE_LIMIT_ENABLED", True):
        return 0.0
    limit = max(1, int(calls_per_second(paper)))
    waited = 0.0
    try:
        if fcntl is None:
            raise OSError("fcntl unavailable")
        waited = _acquire_shared(limit, clock, sleep)
        stats["shared"] += 1
    except OSError as exc:
        if not _fallback_warned:
            _fallback_warned = True
            logger.warning(
                "KIS rate limiter: shared state unavailable (%s); pacing this process only",
                type(exc).__name__,
            )
        waited = _acquire_local(limit, clock, sleep)
        stats["local"] += 1
    stats["calls"] += 1
    if waited > 0:
        stats["waits"] += 1
        stats["waited_sec"] += waited
        logger.debug("KIS rate limiter waited %.3fs (limit=%s/s)", waited, limit)
    return waited
