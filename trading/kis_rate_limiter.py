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
- ``PRISM_KIS_MIN_INTERVAL_PAPER_SEC`` / ``PRISM_KIS_MIN_INTERVAL_REAL_SEC`` —
  minimum gap between any two calls (default paper 0.6s, real 0). The paper
  server rejected back-to-back pairs even when only 2 calls fell in a second
  (2026-10-21: 670 EGW00201/day), so calls are spread evenly, not burst.
- ``PRISM_KIS_RATE_COOLDOWN_SEC`` (default 1.0) — after any process sees
  EGW00201, every process pauses this long (``note_rate_limited``).

The limiter only paces; it never drops a call (retries live in ``kis_auth``). If the shared file
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
_DEFAULT_PAPER_MIN_INTERVAL_SEC = 0.6
_DEFAULT_REAL_MIN_INTERVAL_SEC = 0.0
_DEFAULT_COOLDOWN_SEC = 1.0
_MAX_COOLDOWN_SEC = 10.0
_COOLDOWN_TAG = "c:"

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_local_lock = threading.Lock()
_local_stamps: list[float] = []
_local_cooldown = [0.0]
_fallback_warned = False

stats = {"calls": 0, "waits": 0, "waited_sec": 0.0, "shared": 0, "local": 0, "cooldowns": 0}


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


def min_interval(paper: bool) -> float:
    if paper:
        value = env_float("PRISM_KIS_MIN_INTERVAL_PAPER_SEC", _DEFAULT_PAPER_MIN_INTERVAL_SEC)
    else:
        value = env_float("PRISM_KIS_MIN_INTERVAL_REAL_SEC", _DEFAULT_REAL_MIN_INTERVAL_SEC)
    return min(max(0.0, value), _MAX_SINGLE_WAIT_SEC)


def cooldown_seconds() -> float:
    value = env_float("PRISM_KIS_RATE_COOLDOWN_SEC", _DEFAULT_COOLDOWN_SEC)
    return min(max(0.0, value), _MAX_COOLDOWN_SEC)


def _required_wait(
    stamps: list[float], now: float, limit: int, gap: float = 0.0, cooldown_until: float = 0.0
) -> float:
    """Seconds to wait so this call is at most the ``limit``-th in a window,
    at least ``gap`` after the previous call, and not inside a cooldown."""
    wait = max(0.0, cooldown_until - now)
    past = [s for s in stamps if s <= now + WINDOW_SEC]
    if gap > 0 and past:
        wait = max(wait, max(past) + gap - now)
    recent = sorted(s for s in stamps if now - s < WINDOW_SEC + _SAFETY_SEC and s <= now + WINDOW_SEC)
    if len(recent) >= limit:
        # The call that would leave the window first decides when a slot frees up.
        oldest_blocking = recent[len(recent) - limit]
        wait = max(wait, oldest_blocking + WINDOW_SEC + _SAFETY_SEC - now)
    return max(0.0, wait)


def _parse_state(text: str) -> tuple[list[float], float]:
    stamps: list[float] = []
    cooldown_until = 0.0
    for token in text.split():
        try:
            if token.startswith(_COOLDOWN_TAG):
                cooldown_until = max(cooldown_until, float(token[len(_COOLDOWN_TAG):]))
            else:
                stamps.append(float(token))
        except ValueError:
            continue
    return stamps, cooldown_until


def _parse(text: str) -> list[float]:
    return _parse_state(text)[0]


def _format_state(stamps: list[float], cooldown_until: float, now: float) -> str:
    parts = [f"{s:.6f}" for s in stamps]
    if cooldown_until > now:
        parts.append(f"{_COOLDOWN_TAG}{cooldown_until:.6f}")
    return " ".join(parts)


def _acquire_shared(
    limit: int, clock: Callable[[], float], sleep: Callable[[float], None], gap: float = 0.0
) -> float:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    waited = 0.0
    with open(path, "a+", encoding="ascii") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            stamps, cooldown_until = _parse_state(handle.read())
            now = clock()
            wait = min(_required_wait(stamps, now, limit, gap, cooldown_until), _MAX_COOLDOWN_SEC)
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
            handle.write(_format_state(keep[-max(limit, 1) * 4:], cooldown_until, now))
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return waited


def _acquire_local(
    limit: int, clock: Callable[[], float], sleep: Callable[[float], None], gap: float = 0.0
) -> float:
    with _local_lock:
        now = clock()
        wait = min(
            _required_wait(_local_stamps, now, limit, gap, _local_cooldown[0]), _MAX_COOLDOWN_SEC
        )
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
    gap = min_interval(paper)
    waited = 0.0
    try:
        if fcntl is None:
            raise OSError("fcntl unavailable")
        waited = _acquire_shared(limit, clock, sleep, gap)
        stats["shared"] += 1
    except OSError as exc:
        if not _fallback_warned:
            _fallback_warned = True
            logger.warning(
                "KIS rate limiter: shared state unavailable (%s); pacing this process only",
                type(exc).__name__,
            )
        waited = _acquire_local(limit, clock, sleep, gap)
        stats["local"] += 1
    stats["calls"] += 1
    if waited > 0:
        stats["waits"] += 1
        stats["waited_sec"] += waited
        logger.debug("KIS rate limiter waited %.3fs (limit=%s/s)", waited, limit)
    return waited


def note_rate_limited(
    *,
    seconds: float | None = None,
    clock: Callable[[], float] = time.time,
) -> float:
    """Record that KIS just answered EGW00201: every process pauses until the
    returned timestamp. Never raises; returns the cooldown end (0 if disabled)."""
    global _fallback_warned
    if not env_bool("PRISM_KIS_RATE_LIMIT_ENABLED", True):
        return 0.0
    span = cooldown_seconds() if seconds is None else min(max(0.0, seconds), _MAX_COOLDOWN_SEC)
    if span <= 0:
        return 0.0
    now = clock()
    until = now + span
    stats["cooldowns"] += 1
    try:
        if fcntl is None:
            raise OSError("fcntl unavailable")
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="ascii") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                stamps, current = _parse_state(handle.read())
                until = max(until, current)
                handle.seek(0)
                handle.truncate()
                handle.write(_format_state(stamps, until, now))
                handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        with _local_lock:
            _local_cooldown[0] = max(_local_cooldown[0], until)
    return until
