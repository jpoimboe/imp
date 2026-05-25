import multiprocessing as mp
from pathlib import Path

import pytest

from imp.engine.state import LockHeld, ProjectLock


def test_acquire_and_release(tmp_path: Path) -> None:
    lock = ProjectLock(tmp_path / ".imp" / "lock")
    with lock.acquire():
        assert (tmp_path / ".imp" / "lock").exists()
    # Re-acquire after release
    with lock.acquire():
        pass


def _hold_lock(path: str, sync_path: str, ready: str) -> None:
    from imp.engine.state import ProjectLock as PL
    with PL(Path(path)).acquire():
        Path(ready).touch()
        # Spin until told to release
        while not Path(sync_path).exists():
            import time
            time.sleep(0.05)


def test_contention_blocks(tmp_path: Path) -> None:
    lock_path = tmp_path / ".imp" / "lock"
    lock_path.parent.mkdir(parents=True)
    sync = tmp_path / "release.flag"
    ready = tmp_path / "ready.flag"

    p = mp.Process(target=_hold_lock, args=(str(lock_path), str(sync), str(ready)))
    p.start()
    try:
        while not ready.exists():
            import time
            time.sleep(0.05)
        with pytest.raises(LockHeld), ProjectLock(lock_path).acquire(timeout=0.5):
            pass
    finally:
        sync.touch()
        p.join(timeout=5)
        assert not p.is_alive()


def test_lock_records_pid(tmp_path: Path) -> None:
    import os
    lock_path = tmp_path / ".imp" / "lock"
    lock = ProjectLock(lock_path)
    with lock.acquire():
        text = lock_path.read_text()
        assert str(os.getpid()) in text
