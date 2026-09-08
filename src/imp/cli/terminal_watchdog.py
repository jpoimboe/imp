# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Bound the pause latency when the terminal driving a leased session goes away."""
from __future__ import annotations

import contextlib
import errno
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from imp.remote.session.client import SessionClient

WRITE_WEDGE_SECONDS = 20.0
POLL_SECONDS = 1.0
WATCHDOG_THREAD_NAME = "imp-write-watchdog"


class WatchedStream:
    """A transparent stdout proxy that stamps each in-flight write/flush.

    Wrapping the stream, rather than bracketing call sites, means a future write
    site participates by construction.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.write_started_at: float | None = None
        self.on_terminal_gone: Callable[[], None] | None = None

    def write(self, data: str) -> int:
        self.write_started_at = time.monotonic()
        try:
            # Annotated local, not a bare return: _stream is Any and mypy is strict.
            written: int = self._stream.write(data)
            return written
        except OSError as err:
            if err.errno != errno.EIO or self.on_terminal_gone is None:
                raise
            # The hook exits the process; returning keeps a vanished terminal from
            # unwinding the frontend through the very sink that just faulted.
            self.on_terminal_gone()
            return len(data)
        finally:
            self.write_started_at = None

    def flush(self) -> None:
        self.write_started_at = time.monotonic()
        try:
            self._stream.flush()
        except OSError as err:
            if err.errno != errno.EIO or self.on_terminal_gone is None:
                raise
            self.on_terminal_gone()
        finally:
            self.write_started_at = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class WriteWatchdog:
    """Polls the write stamp from a plain thread — the event loop is what freezes."""

    def __init__(
        self,
        stream: WatchedStream,
        on_trip: Callable[[], None],
        *,
        threshold: float = WRITE_WEDGE_SECONDS,
        interval: float = POLL_SECONDS,
    ) -> None:
        self._stream = stream
        self._on_trip = on_trip
        self._threshold = threshold
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._poll, name=WATCHDOG_THREAD_NAME, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _poll(self) -> None:
        while not self._stop.wait(self._interval):
            started = self._stream.write_started_at
            if started is not None and time.monotonic() - started >= self._threshold:
                self._on_trip()
                return


def terminal_gone(client: SessionClient) -> None:
    """Drop the lease so the daemon pauses; hard exit — unwinding writes to the dead terminal."""
    client.shutdown_socket()
    os._exit(1)


@contextlib.contextmanager
def watch_terminal(
    stream: WatchedStream | None, on_gone: Callable[[], None],
) -> Iterator[None]:
    """Arm the wedge watchdog and the EIO hook for the life of a leased attach."""
    if stream is None:
        yield
        return
    watchdog = WriteWatchdog(stream, on_gone)
    stream.on_terminal_gone = on_gone
    watchdog.start()
    try:
        yield
    finally:
        stream.on_terminal_gone = None
        watchdog.stop()
