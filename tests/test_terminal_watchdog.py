from __future__ import annotations

import contextlib
import errno
import inspect
import io
import os
import pty
import threading
import time
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest

from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.terminal_title import TerminalTitle
from imp.cli.terminal_watchdog import (
    WATCHDOG_THREAD_NAME,
    WRITE_WEDGE_SECONDS,
    WatchedStream,
    WriteWatchdog,
)
from imp.remote.session.client import SessionClient
from tests.async_wait import HANG_BACKSTOP, wait_until


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


class _Blocking:
    """A sink whose write parks until released, so a wedge is deterministic."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def isatty(self) -> bool:
        return True

    def write(self, data: str) -> int:
        self.entered.set()
        self.release.wait(HANG_BACKSTOP)
        return len(data)

    def flush(self) -> None: ...


class _FlushFails:
    def isatty(self) -> bool:
        return True

    def write(self, data: str) -> int:
        return len(data)

    def flush(self) -> None:
        raise OSError(errno.EIO, "Input/output error")


class _WriteBrokenPipe:
    def isatty(self) -> bool:
        return True

    def write(self, data: str) -> int:
        raise OSError(errno.EPIPE, "Broken pipe")

    def flush(self) -> None: ...


def _watchdog_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == WATCHDOG_THREAD_NAME}


def _drain_until(master: int, start: threading.Event, done: threading.Event) -> None:
    """Read the pty master once `start` fires so the wedged writer can proceed."""
    start.wait()
    os.set_blocking(master, False)
    while not done.is_set():
        try:
            if not os.read(master, 65536):
                return
        except BlockingIOError:
            time.sleep(0.005)


def _assert_stamped_while_blocked(
    watched: WatchedStream, sink: _Blocking, write: Callable[[], object],
) -> None:
    """AC 5(b): the stamp stays set for the whole in-flight write, cleared once it returns."""
    returned = threading.Event()

    def _run() -> None:
        write()
        returned.set()

    writer = threading.Thread(target=_run, daemon=True)
    writer.start()
    try:
        assert sink.entered.wait(HANG_BACKSTOP)
        assert watched.write_started_at is not None
    finally:
        # Release even on a failed assert, so the sink never strands the writer thread.
        sink.release.set()
    assert returned.wait(HANG_BACKSTOP)
    writer.join(HANG_BACKSTOP)
    assert watched.write_started_at is None


def test_stamp_is_set_while_a_write_is_in_flight_and_cleared_when_it_returns() -> None:
    sink = _Blocking()
    watched = WatchedStream(sink)
    _assert_stamped_while_blocked(watched, sink, lambda: watched.write("footer"))


def test_a_live_status_footer_write_leaves_the_stamp_set_while_it_blocks() -> None:
    sink = _Blocking()
    watched = WatchedStream(sink)
    status = LiveStatus(watched)
    status._width_fn = lambda: 80
    _assert_stamped_while_blocked(
        watched, sink, lambda: status.update(lambda _frame: ["row one"]))


def test_a_live_driver_write_leaves_the_stamp_set_while_it_blocks() -> None:
    sink = _Blocking()
    watched = WatchedStream(sink)
    driver = LiveDriver(
        stream=watched, color=False, width=80, status=LiveStatus(watched))
    _assert_stamped_while_blocked(
        watched, sink, lambda: driver.append_committed_reply("hi"))


def test_a_terminal_title_write_leaves_the_stamp_set_while_it_blocks() -> None:
    sink = _Blocking()
    watched = WatchedStream(sink)
    _assert_stamped_while_blocked(
        watched, sink,
        lambda: TerminalTitle(watched, "fefu01", "a title", project="proj"))


def test_isatty_and_ordinary_attributes_delegate_to_the_wrapped_stream() -> None:
    tty = WatchedStream(_TTY())
    pipe = WatchedStream(_Pipe())
    assert tty.isatty() is True
    assert pipe.isatty() is False
    tty.write("hi")
    assert tty.getvalue() == "hi"


def test_terminal_title_constructs_and_emits_osc_through_the_wrapper() -> None:
    watched = WatchedStream(_TTY())
    TerminalTitle(watched, "fefu01", "a title", project="proj")
    assert "\x1b]2;" in watched.getvalue()
    assert "fefu01" in watched.getvalue()


def test_live_status_footer_stays_enabled_through_the_wrapper() -> None:
    watched = WatchedStream(_TTY())
    status = LiveStatus(watched)
    status._width_fn = lambda: 80
    status.update(lambda _frame: ["row one"])
    assert "row one" in watched.getvalue()


def test_eio_on_write_runs_the_terminal_gone_hook_and_does_not_raise() -> None:
    master, slave = pty.openpty()
    watched = WatchedStream(os.fdopen(slave, "w", buffering=1, errors="replace"))
    calls: list[str] = []
    watched.on_terminal_gone = lambda: calls.append("gone")
    os.close(master)
    try:
        watched.write("footer\n")
    finally:
        with contextlib.suppress(OSError):
            watched.close()
    assert calls == ["gone"]
    assert watched.write_started_at is None


def test_eio_on_flush_runs_the_terminal_gone_hook_and_does_not_raise() -> None:
    watched = WatchedStream(_FlushFails())
    calls: list[str] = []
    watched.on_terminal_gone = lambda: calls.append("gone")
    watched.flush()
    assert calls == ["gone"]
    assert watched.write_started_at is None


def test_any_other_oserror_propagates_untouched() -> None:
    watched = WatchedStream(_WriteBrokenPipe())
    calls: list[str] = []
    watched.on_terminal_gone = lambda: calls.append("gone")
    with pytest.raises(OSError) as err:
        watched.write("footer")
    assert err.value.errno == errno.EPIPE
    assert calls == []
    assert watched.write_started_at is None


def test_eio_propagates_when_no_hook_is_armed() -> None:
    watched = WatchedStream(_FlushFails())
    with pytest.raises(OSError) as err:
        watched.flush()
    assert err.value.errno == errno.EIO


def test_default_threshold_is_twenty_seconds() -> None:
    assert WRITE_WEDGE_SECONDS == 20.0
    assert inspect.signature(WriteWatchdog).parameters["threshold"].default == WRITE_WEDGE_SECONDS


def test_watchdog_trips_when_one_write_stays_in_flight_past_the_threshold() -> None:
    sink = _Blocking()
    watched = WatchedStream(sink)
    tripped = threading.Event()
    watchdog = WriteWatchdog(watched, tripped.set, threshold=0.2, interval=0.02)
    watchdog.start()
    writer = threading.Thread(target=lambda: watched.write("footer"), daemon=True)
    writer.start()
    try:
        assert sink.entered.wait(HANG_BACKSTOP)
        assert tripped.wait(HANG_BACKSTOP)
    finally:
        sink.release.set()
        writer.join(HANG_BACKSTOP)
        watchdog.stop()


def test_watchdog_does_not_trip_while_writes_complete_normally() -> None:
    watched = WatchedStream(_TTY())
    tripped = threading.Event()
    # Threshold far above anything a StringIO write can hold, so a saturated box
    # cannot false-fire; the drive window may stretch without changing the verdict.
    watchdog = WriteWatchdog(watched, tripped.set, threshold=5.0, interval=0.01)
    watchdog.start()
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            watched.write("tick")
    finally:
        watchdog.stop()
    assert not tripped.is_set()


def test_watchdog_does_not_trip_with_no_write_in_flight() -> None:
    # AC 4(b): a loop stalled for any other reason sets no stamp, and the stamp is the
    # watchdog's only input — so no stall duration can trip it.
    watched = WatchedStream(_TTY())
    tripped = threading.Event()
    watchdog = WriteWatchdog(watched, tripped.set, threshold=0.05, interval=0.01)
    watchdog.start()
    try:
        assert not tripped.wait(0.5)
    finally:
        watchdog.stop()
    assert not tripped.is_set()


def test_stop_joins_the_poller_thread() -> None:
    watched = WatchedStream(_TTY())
    before = _watchdog_threads()
    watchdog = WriteWatchdog(watched, lambda: None, threshold=5.0, interval=0.01)
    watchdog.start()
    spawned = _watchdog_threads() - before
    assert len(spawned) == 1
    watchdog.stop()
    assert not any(t.is_alive() for t in spawned)


def test_watchdog_trips_on_a_real_live_status_footer_wedged_against_a_pty() -> None:
    master, slave = pty.openpty()
    watched = WatchedStream(os.fdopen(slave, "w", buffering=1, errors="replace"))
    status = LiveStatus(watched)
    status._width_fn = lambda: 120
    trips: list[str] = []
    tripped = threading.Event()
    done = threading.Event()

    # `trips` records the watchdog specifically; the teardown below sets `tripped`
    # unconditionally, so only `trips` can distinguish a real trip from cleanup.
    def _on_trip() -> None:
        trips.append("wedged")
        tripped.set()

    drainer = threading.Thread(
        target=_drain_until, args=(master, tripped, done), daemon=True)
    drainer.start()
    watchdog = WriteWatchdog(watched, _on_trip, threshold=1.0, interval=0.02)
    watchdog.start()

    def _drive() -> None:
        # Nobody reads the master: the slave's buffer fills and _draw wedges inside
        # write(2). Only the watchdog's trip (which releases the drainer) ends this loop.
        status.update(lambda _frame: ["x" * 100, "y" * 100, "z" * 100])
        while not tripped.is_set():
            status._frame += 1
            status._draw()

    # The wedge runs off the main thread on purpose: a restarted write(2) ignores
    # SIGALRM, so a main-thread wedge would outlast pytest-timeout and hang the worker.
    driver = threading.Thread(target=_drive, daemon=True)
    driver.start()
    try:
        assert tripped.wait(HANG_BACKSTOP)
    finally:
        # Order matters: `tripped` releases the drainer, which unwedges the driver;
        # `done` may only stop the drainer once the driver is out of write(2).
        tripped.set()
        driver.join(HANG_BACKSTOP)
        done.set()
        drainer.join(HANG_BACKSTOP)
        watchdog.stop()
        with contextlib.suppress(OSError):
            watched.close()
        os.close(master)
    assert trips == ["wedged"]     # tripped exactly once, by the watchdog
    assert watched.write_started_at is None


@pytest.mark.anyio
async def test_shutdown_socket_from_a_thread_delivers_eof_while_the_loop_is_wedged(
    tmp_path: Path,
) -> None:
    eof: list[bool] = []

    async def _handler(stream: anyio.abc.SocketStream) -> None:
        try:
            while True:
                await stream.receive()
        except anyio.EndOfStream:
            eof.append(True)

    path = str(tmp_path / "s.sock")
    master, slave = pty.openpty()
    watched = WatchedStream(os.fdopen(slave, "w", buffering=1, errors="replace"))
    torn = threading.Event()
    done = threading.Event()
    failures: list[Exception] = []
    drainer = threading.Thread(target=_drain_until, args=(master, torn, done), daemon=True)
    drainer.start()
    # Parenthesized rather than nested (SIM117); exit order is unchanged.
    async with (
        await anyio.create_unix_listener(path) as listener,
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(listener.serve, _handler)
        client = SessionClient(path, "fefu01", "T", on_event=lambda _e: None)
        await client.connect()

        def _tear_down() -> None:
            # Act only once a terminal write has genuinely been in flight a while:
            # that IS the frozen-loop condition this teardown must survive.
            deadline = time.monotonic() + HANG_BACKSTOP
            try:
                while time.monotonic() < deadline:
                    started = watched.write_started_at
                    if started is not None and time.monotonic() - started > 0.2:
                        break
                    time.sleep(0.01)
                client.shutdown_socket()
            except Exception as err:
                failures.append(err)
            finally:
                # Release the drainer whatever the teardown did: the main thread sits in a
                # write(2) the kernel restarts across SIGALRM, so pytest-timeout cannot
                # free it and a broken teardown would hang the worker instead of failing.
                torn.set()

        tearer = threading.Thread(target=_tear_down, daemon=True)
        tearer.start()
        # Freeze the loop inside write(2) on a pty nobody reads.
        for _ in range(400):
            watched.write("x" * 512 + "\n")
        tearer.join(HANG_BACKSTOP)
        assert not failures
        await wait_until(lambda: bool(eof))
        tg.cancel_scope.cancel()
    done.set()
    drainer.join(HANG_BACKSTOP)
    with contextlib.suppress(OSError):
        watched.close()
    os.close(master)
    assert eof == [True]
