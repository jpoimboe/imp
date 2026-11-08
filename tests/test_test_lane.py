import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from imp.engine import test_lane

_WAIT = 10.0

Result = queue.Queue[tuple[str, int | None]]


class _Polls:
    """Stands in for test_lane's poll sleep: records threads that made a failed attempt."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._polled: set[str] = set()
        self._parked: dict[str, threading.Event] = {}

    def sleep(self) -> None:
        name = threading.current_thread().name
        with self._cond:
            self._polled.add(name)
            self._cond.notify_all()
            gate = self._parked.get(name)
        if gate is not None:
            gate.wait(_WAIT)
        time.sleep(0.002)

    def park(self, name: str) -> threading.Event:
        gate = threading.Event()
        with self._cond:
            self._parked[name] = gate
        return gate

    def wait_polled(self, *names: str) -> None:
        with self._cond:
            assert self._cond.wait_for(lambda: set(names) <= self._polled, _WAIT)


@pytest.fixture
def polls(monkeypatch: pytest.MonkeyPatch) -> _Polls:
    p = _Polls()
    monkeypatch.setattr(test_lane, "_poll_sleep", p.sleep)
    return p


def _lane(root: Path) -> Path:
    return root / ".git" / "imp" / "test-lane"


def _waiter(root: Path, n: int, name: str, results: Result, *,
            abort: Callable[[], bool] | None = None) -> threading.Thread:
    def _run() -> None:
        try:
            results.put((name, test_lane.acquire_blocking(root, n, abort=abort)))
        except test_lane.WaitAborted:
            results.put((name, None))
    t = threading.Thread(target=_run, name=name, daemon=True)
    t.start()
    return t


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


def test_acquire_blocking_waits_until_slot_free(tmp_path: Path, polls: _Polls) -> None:
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    results: Result = queue.Queue()
    _waiter(tmp_path, 1, "W", results)
    polls.wait_polled("W")
    assert results.empty()             # still waiting while the sole slot is held
    test_lane.release(held)
    name, fd = results.get(timeout=_WAIT)
    assert name == "W" and fd is not None
    test_lane.release(fd)


def test_waiter_takes_any_slot_that_frees(tmp_path: Path, polls: _Polls) -> None:
    slot1 = test_lane.try_acquire(tmp_path, 2)
    slot2 = test_lane.try_acquire(tmp_path, 2)
    assert slot1 is not None and slot2 is not None
    results: Result = queue.Queue()
    _waiter(tmp_path, 2, "W", results)
    polls.wait_polled("W")
    test_lane.release(slot2)
    _, fd = results.get(timeout=_WAIT)
    assert fd is not None
    assert os.fstat(fd).st_ino == os.stat(_lane(tmp_path) / "slot-2").st_ino
    assert test_lane.try_acquire(tmp_path, 2) is None     # slot-1 is still held
    test_lane.release(fd)
    test_lane.release(slot1)


def test_abort_ends_a_contended_wait(tmp_path: Path, polls: _Polls) -> None:
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    stop = threading.Event()
    results: Result = queue.Queue()
    t = _waiter(tmp_path, 1, "W", results, abort=stop.is_set)
    polls.wait_polled("W")
    stop.set()
    assert results.get(timeout=_WAIT) == ("W", None)
    t.join(_WAIT)
    assert not t.is_alive()
    test_lane.release(held)


def test_abort_already_set_takes_no_free_slot(tmp_path: Path) -> None:
    with pytest.raises(test_lane.WaitAborted):
        test_lane.acquire_blocking(tmp_path, 1, abort=lambda: True)
    fd = test_lane.try_acquire(tmp_path, 1)               # the free slot was left untouched
    assert fd is not None
    test_lane.release(fd)
