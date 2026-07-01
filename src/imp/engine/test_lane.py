"""Per-control-root counting semaphore for verify-gate serialization.

N slots (N = config.max_parallel_test_runs) are lock files under
<control_root>/.git/imp/test-lane/slot-<i>. A slot is held via an fd-tied
fcntl.flock; closing the fd (release, or the kernel closing it when the daemon
dies) frees it, so no stale-lock recovery is needed.
"""
import fcntl
import os
from pathlib import Path


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


def acquire_blocking(control_root: Path, n: int) -> int:
    """Block until a slot is free; return a held fd. Call from a worker thread."""
    fd = try_acquire(control_root, n)
    if fd is not None:
        return fd
    fd = _open_slot(control_root, 1)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)   # blocks until slot-1 frees
    except OSError:
        os.close(fd)
        raise
    return fd


def release(fd: int) -> None:
    """Release the slot by closing its fd (closing drops the flock)."""
    os.close(fd)
