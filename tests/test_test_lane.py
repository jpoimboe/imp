import fcntl
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


def _waiter(root: Path, n: int, name: str, results: Result, *, priority: bool = False,
            abort: Callable[[], bool] | None = None) -> threading.Thread:
    def _run() -> None:
        try:
            results.put((name, test_lane.acquire_blocking(
                root, n, priority=priority, abort=abort)))
        except test_lane.WaitAborted:
            results.put((name, None))
    t = threading.Thread(target=_run, name=name, daemon=True)
    t.start()
    return t


def _hold_marker(root: Path) -> int:
    """Hold the priority marker the way a priority waiter does, as a bare fd."""
    _lane(root).mkdir(parents=True, exist_ok=True)
    fd = os.open(_lane(root) / "priority", os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_SH)
    return fd


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


def test_priority_waiter_jumps_earlier_non_priority_waiter(
        tmp_path: Path, polls: _Polls) -> None:
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    results: Result = queue.Queue()
    _waiter(tmp_path, 1, "W", results)
    polls.wait_polled("W")
    _waiter(tmp_path, 1, "P", results, priority=True)
    polls.wait_polled("P")
    test_lane.release(held)
    name, fd = results.get(timeout=_WAIT)
    assert name == "P" and fd is not None
    assert results.empty()              # W still waiting: P holds the only slot
    test_lane.release(fd)
    name, fd = results.get(timeout=_WAIT)
    assert name == "W" and fd is not None
    test_lane.release(fd)


def test_all_priority_waiters_go_before_non_priority(tmp_path: Path, polls: _Polls) -> None:
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    results: Result = queue.Queue()
    _waiter(tmp_path, 1, "W", results)
    polls.wait_polled("W")
    _waiter(tmp_path, 1, "P1", results, priority=True)
    _waiter(tmp_path, 1, "P2", results, priority=True)
    polls.wait_polled("P1", "P2")
    test_lane.release(held)
    first, fd = results.get(timeout=_WAIT)
    assert first in {"P1", "P2"} and fd is not None
    test_lane.release(fd)
    second, fd = results.get(timeout=_WAIT)
    assert {first, second} == {"P1", "P2"} and fd is not None
    test_lane.release(fd)
    last, fd = results.get(timeout=_WAIT)
    assert last == "W" and fd is not None
    test_lane.release(fd)


def test_aborted_priority_waiter_stops_holding_off_non_priority(
        tmp_path: Path, polls: _Polls) -> None:
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    stop = threading.Event()
    results: Result = queue.Queue()
    _waiter(tmp_path, 1, "W", results)
    polls.wait_polled("W")
    p = _waiter(tmp_path, 1, "P", results, priority=True, abort=stop.is_set)
    polls.wait_polled("P")
    stop.set()
    assert results.get(timeout=_WAIT) == ("P", None)
    p.join(_WAIT)
    test_lane.release(held)
    name, fd = results.get(timeout=_WAIT)
    assert name == "W" and fd is not None
    test_lane.release(fd)


def test_priority_abort_releases_marker(tmp_path: Path) -> None:
    with pytest.raises(test_lane.WaitAborted):
        test_lane.acquire_blocking(tmp_path, 1, priority=True, abort=lambda: True)
    fd = test_lane.try_acquire(tmp_path, 1)    # a leaked marker would make this None
    assert fd is not None
    test_lane.release(fd)


def test_closed_priority_fd_frees_non_priority_waiters(tmp_path: Path, polls: _Polls) -> None:
    marker = _hold_marker(tmp_path)            # a priority waiter whose process then dies
    results: Result = queue.Queue()
    _waiter(tmp_path, 1, "W", results)
    polls.wait_polled("W")
    assert results.empty()                     # slot free, but the marker holds W off
    os.close(marker)
    name, fd = results.get(timeout=_WAIT)
    assert name == "W" and fd is not None
    test_lane.release(fd)


def test_non_priority_try_acquire_yields_to_waiting_priority(
        tmp_path: Path, polls: _Polls) -> None:
    held = test_lane.try_acquire(tmp_path, 1)
    assert held is not None
    gate = polls.park("P")
    results: Result = queue.Queue()
    _waiter(tmp_path, 1, "P", results, priority=True)
    polls.wait_polled("P")
    test_lane.release(held)                    # slot free while P is parked between polls
    assert test_lane.try_acquire(tmp_path, 1) is None
    gate.set()
    name, fd = results.get(timeout=_WAIT)
    assert name == "P" and fd is not None
    test_lane.release(fd)
    fd = test_lane.try_acquire(tmp_path, 1)    # no priority waiter left
    assert fd is not None
    test_lane.release(fd)


def test_priority_try_acquire_ignores_marker(tmp_path: Path) -> None:
    marker = _hold_marker(tmp_path)
    assert test_lane.try_acquire(tmp_path, 1) is None
    fd = test_lane.try_acquire(tmp_path, 1, priority=True)
    assert fd is not None
    test_lane.release(fd)
    os.close(marker)


def test_concurrent_non_priority_try_acquire_never_misses_a_free_slot(tmp_path: Path) -> None:
    n = 4
    for _ in range(200):
        barrier = threading.Barrier(n)
        got: list[int | None] = [None] * n

        def _try(i: int, barrier: threading.Barrier = barrier,
                 got: list[int | None] = got) -> None:
            barrier.wait(_WAIT)
            got[i] = test_lane.try_acquire(tmp_path, n)

        threads = [threading.Thread(target=_try, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(_WAIT)
        assert all(fd is not None for fd in got)
        for fd in got:
            assert fd is not None
            test_lane.release(fd)


def test_mixed_version_waiters_never_exceed_slot_cap(tmp_path: Path, polls: _Polls) -> None:
    n, rounds = 2, 20
    lock = threading.Lock()
    active = peak = 0
    errors: list[BaseException] = []

    def _hold(fd: int) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.002)
        with lock:
            active -= 1
        os.close(fd)

    def _old_code() -> None:              # pre-change acquire: block on slot-1's flock
        try:
            for _ in range(rounds):
                fd = os.open(_lane(tmp_path) / "slot-1", os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
                _hold(fd)
        except BaseException as exc:
            errors.append(exc)

    def _new_code(priority: bool) -> None:
        try:
            for _ in range(rounds):
                _hold(test_lane.acquire_blocking(tmp_path, n, priority=priority))
        except BaseException as exc:
            errors.append(exc)

    _lane(tmp_path).mkdir(parents=True)
    threads = [threading.Thread(target=_old_code, daemon=True)] + [
        threading.Thread(target=_new_code, args=(p,), daemon=True)
        for p in (True, True, False, False)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)
    assert not errors
    assert 1 <= peak <= n
