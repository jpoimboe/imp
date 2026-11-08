# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Per-control-root counting semaphore for verify-gate serialization, with priority.

N slots (N = config.max_parallel_test_runs) are lock files under
<control_root>/.git/imp/test-lane/slot-<i>, each held via an exclusive fd-tied
fcntl.flock. A waiter polls all N slots. A priority waiter (one holding a merge lane)
holds LOCK_SH on `priority` for its whole wait; a non-priority attempt takes no slot
while that marker is held. Non-priority attempts probe the marker under a LOCK_EX on
`probe`, so they never read each other as a priority waiter. Every lock is fd-tied, so
closing the fd (release, or the kernel when the daemon dies) frees it and no stale-lock
recovery is needed.
"""
import fcntl
import os
import time
from collections.abc import Callable
from pathlib import Path

POLL_INTERVAL_S = 1.0


class WaitAborted(Exception):
    """The wait's abort predicate fired before a slot was acquired."""


def _lane_dir(control_root: Path) -> Path:
    return control_root / ".git" / "imp" / "test-lane"


def _open(control_root: Path, name: str) -> int:
    d = _lane_dir(control_root)
    d.mkdir(parents=True, exist_ok=True)
    return os.open(d / name, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o644)


def _scan_slots(control_root: Path, n: int) -> int | None:
    for i in range(1, n + 1):
        fd = _open(control_root, f"slot-{i}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            os.close(fd)
            if not isinstance(exc, BlockingIOError):
                raise
    return None


def try_acquire(control_root: Path, n: int, *, priority: bool = False) -> int | None:
    """Scan slots 1..n without waiting for one. Return a held fd, or None if all are
    busy or (non-priority only) a priority waiter is waiting."""
    if priority:
        return _scan_slots(control_root, n)
    probe = _open(control_root, "probe")
    try:
        fcntl.flock(probe, fcntl.LOCK_EX)
        marker = _open(control_root, "priority")
        try:
            try:
                fcntl.flock(marker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            return _scan_slots(control_root, n)
        finally:
            os.close(marker)
    finally:
        os.close(probe)


def _hold_priority(control_root: Path) -> int:
    fd = _open(control_root, "priority")
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
    except OSError:
        os.close(fd)
        raise
    return fd


def _poll_sleep() -> None:
    time.sleep(POLL_INTERVAL_S)


def acquire_blocking(control_root: Path, n: int, *, priority: bool = False,
                     abort: Callable[[], bool] | None = None) -> int:
    """Poll slots 1..n until one frees; return a held fd. Call from a worker thread.

    Raises WaitAborted, holding nothing, once abort() is true.
    """
    marker = _hold_priority(control_root) if priority else None
    try:
        while True:
            if abort is not None and abort():
                raise WaitAborted
            fd = try_acquire(control_root, n, priority=priority)
            if fd is not None:
                return fd
            _poll_sleep()
    finally:
        if marker is not None:
            os.close(marker)


def release(fd: int) -> None:
    """Release the slot by closing its fd (closing drops the flock)."""
    os.close(fd)
