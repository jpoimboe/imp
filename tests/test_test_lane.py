import threading
import time
from pathlib import Path

from imp.engine import test_lane


def test_try_acquire_returns_fd_when_free(tmp_path: Path) -> None:
    fd = test_lane.try_acquire(tmp_path, 1)
    assert fd is not None
    test_lane.release(fd)


def test_try_acquire_none_when_all_slots_held(tmp_path: Path) -> None:
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    assert test_lane.try_acquire(tmp_path, 1) is None   # sole slot busy
    test_lane.release(held)
    reacquired = test_lane.try_acquire(tmp_path, 1)      # freed -> available again
    assert reacquired is not None
    test_lane.release(reacquired)


def test_two_slots_allow_two_holders(tmp_path: Path) -> None:
    a = test_lane.try_acquire(tmp_path, 2)
    b = test_lane.try_acquire(tmp_path, 2)
    assert a is not None and b is not None
    assert test_lane.try_acquire(tmp_path, 2) is None    # both slots busy
    test_lane.release(a)
    test_lane.release(b)


def test_acquire_blocking_returns_immediately_when_free(tmp_path: Path) -> None:
    fd = test_lane.acquire_blocking(tmp_path, 1)
    assert fd is not None
    test_lane.release(fd)


def test_slot_files_live_under_git_imp_test_lane(tmp_path: Path) -> None:
    fd = test_lane.try_acquire(tmp_path, 1)
    assert fd is not None
    assert (tmp_path / ".git" / "imp" / "test-lane" / "slot-1").exists()
    test_lane.release(fd)


def test_acquire_blocking_waits_until_slot_free(tmp_path: Path) -> None:
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    results: list[int] = []

    def _blocking() -> None:
        results.append(test_lane.acquire_blocking(tmp_path, 1))

    t = threading.Thread(target=_blocking)
    t.start()
    time.sleep(0.1)
    assert results == []            # still blocked while slot-1 is held
    test_lane.release(held)
    t.join(timeout=2)
    assert len(results) == 1        # unblocked once the slot freed
    test_lane.release(results[0])
