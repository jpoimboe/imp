from __future__ import annotations

import contextlib
import errno
import io
import os
import pty
import threading
from collections.abc import Callable

import pytest

from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.terminal_title import TerminalTitle
from imp.cli.terminal_watchdog import WatchedStream
from tests.async_wait import HANG_BACKSTOP


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
