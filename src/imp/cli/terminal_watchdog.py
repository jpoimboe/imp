# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Bound the pause latency when the terminal driving a leased session goes away."""
from __future__ import annotations

import errno
import time
from collections.abc import Callable
from typing import Any


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
