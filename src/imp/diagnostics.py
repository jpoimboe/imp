# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import os
import signal
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class StackDumper:
    """Handle for an installed SIGUSR1 stack dumper. Long-lived processes never
    uninstall; tests call uninstall() to restore process signal state."""

    dump_path: Path
    _read_fd: int
    _write_fd: int
    _thread: threading.Thread
    _prev_handler: object

    def uninstall(self) -> None:
        signal.signal(signal.SIGUSR1, self._prev_handler)  # type: ignore[arg-type]
        with contextlib.suppress(OSError):
            os.close(self._write_fd)  # unblocks the diagnostics thread's os.read with EOF
        self._thread.join(timeout=2)
        with contextlib.suppress(OSError):
            os.close(self._read_fd)


def _write_asyncio_section(dump_path: Path, loop: asyncio.AbstractEventLoop | None) -> None:
    with open(dump_path, "a", encoding="utf-8") as f:
        f.write("-- asyncio tasks --\n")
        if loop is None:
            f.write("(no running event loop)\n")
            return
        try:
            tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
        except RuntimeError as exc:
            f.write(f"(could not enumerate tasks: {exc})\n")
            return
        if not tasks:
            f.write("(no pending tasks)\n")
            return
        for task in tasks:
            f.write(f"Task {task!r}:\n")
            task.print_stack(file=f)


def install_stack_dumper(dump_path: Path) -> StackDumper:
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    loop_box: list[asyncio.AbstractEventLoop | None] = [None]

    def _handler(_signum: int, _frame: object) -> None:
        try:
            loop_box[0] = asyncio.get_running_loop()
        except RuntimeError:
            loop_box[0] = None
        stamp = datetime.now(UTC).isoformat()
        with open(dump_path, "a", encoding="utf-8") as f:
            f.write(f"\n==== imp stack dump · {stamp} · pid {os.getpid()} ====\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
            f.flush()
        with contextlib.suppress(OSError):
            os.write(write_fd, b"\x00")

    def _diag_loop() -> None:
        while True:
            try:
                data = os.read(read_fd, 4096)
            except OSError:
                return
            if not data:
                return
            # One wake byte per signal; emit one asyncio section per byte so a
            # rapid burst that coalesces into a single read still produces one
            # section per SIGUSR1 (matching the per-signal header count).
            for _ in data:
                _write_asyncio_section(dump_path, loop_box[0])

    thread = threading.Thread(target=_diag_loop, name="imp-stack-diag", daemon=True)
    thread.start()
    prev = signal.signal(signal.SIGUSR1, _handler)
    return StackDumper(dump_path, read_fd, write_fd, thread, prev)
