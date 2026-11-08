# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Per-control-root counting semaphore for verify-gate serialization.

N slots (N = config.max_parallel_test_runs) are lock files under
<control_root>/.git/imp/test-lane/slot-<i>. A slot is held via an fd-tied
fcntl.flock; closing the fd (release, or the kernel closing it when the daemon
dies) frees it, so no stale-lock recovery is needed.
"""
import fcntl
import os
import time
from collections.abc import Callable
from pathlib import Path

POLL_INTERVAL_S = 1.0


class WaitAborted(Exception):
    """The wait's abort predicate fired before a slot was acquired."""


def _slot_dir(control_root: Path) -> Path:
    return control_root / ".git" / "imp" / "test-lane"


def _open_slot(control_root: Path, i: int) -> int:
    d = _slot_dir(control_root)
    d.mkdir(parents=True, exist_ok=True)
    return os.open(d / f"slot-{i}", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o644)


def try_acquire(control_root: Path, n: int) -> int | None:
    """Non-blocking scan of slots 1..n. Return a held fd, or None if all busy."""
    for i in range(1, n + 1):
        fd = _open_slot(control_root, i)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            os.close(fd)
            if not isinstance(exc, BlockingIOError):
                raise
    return None


def _poll_sleep() -> None:
    time.sleep(POLL_INTERVAL_S)


def acquire_blocking(control_root: Path, n: int, *,
                     abort: Callable[[], bool] | None = None) -> int:
    """Poll slots 1..n until one frees; return a held fd. Call from a worker thread.

    Raises WaitAborted, holding nothing, once abort() is true.
    """
    while True:
        if abort is not None and abort():
            raise WaitAborted
        fd = try_acquire(control_root, n)
        if fd is not None:
            return fd
        _poll_sleep()


def release(fd: int) -> None:
    """Release the slot by closing its fd (closing drops the flock)."""
    os.close(fd)
