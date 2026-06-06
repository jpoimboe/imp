"""A terminal session: spawn an interactive program on a real pty, feed its
output into a long-lived pyte screen, send raw keystrokes, and read snapshots.

Deterministic plumbing only — no agent, no semantic translation. The pty +
spawn idiom matches tests/test_human_gate_pty.py (stdlib pty, start_new_session)."""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import select
import struct
import subprocess
import termios
import time
from collections.abc import Mapping, Sequence

import pyte

from impty import view
from impty.view import Cell


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class TerminalSession:
    """Drive one interactive program over a pty, rendered by pyte.

    Read methods return *snapshots* of the current screen; pyte maintains all
    continuity, so callers never stitch reads together.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cols: int = 80,
        rows: int = 24,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        history: int = 2000,
    ) -> None:
        self._cols = cols
        self._rows = rows
        self._screen = pyte.HistoryScreen(cols, rows, history=history, ratio=0.5)
        self._stream = pyte.ByteStream(self._screen)
        self._master, slave = pty.openpty()
        _set_winsize(slave, rows, cols)
        # Default TERM so color emission (and thus cell().fg) is deterministic
        # across machines/CI where TERM may be unset or minimal. An explicit
        # env= with its own TERM wins via setdefault.
        child_env = dict(os.environ if env is None else env)
        child_env.setdefault("TERM", "xterm-256color")
        self._proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=child_env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        self._closed = False

    @property
    def pid(self) -> int:
        return self._proc.pid

    def send(self, data: bytes | str) -> None:
        """Write raw keystrokes to the program. Strings are UTF-8 encoded.
        Keys are literal bytes: '\\r' Enter, '\\x1b[B' Down, '\\x03' Ctrl-C, '\\t' Tab."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        os.write(self._master, data)

    def wait_idle(self, idle: float = 0.2, hard: float = 10.0) -> bool:
        """Block until the child produces output and then goes quiet for `idle`
        seconds (or exits, or `hard` elapses), feeding everything to pyte.

        Non-blocking via select(). A quiet window BEFORE any output has been
        read does NOT count as settled — that would race a slow fork/interpreter
        startup and return an empty screen. So we settle only once we've seen at
        least one byte (normal case), or the child has already exited (nothing
        more is coming). Returns True when settled, False if `hard` elapsed while
        output kept arriving. The final select() may block up to `idle`, so the
        effective wall-clock cap is ~`hard + idle`."""
        start = time.monotonic()
        saw_output = False
        while True:
            readable, _, _ = select.select([self._master], [], [], idle)
            if readable:
                try:
                    chunk = os.read(self._master, 65536)
                except OSError:
                    return True  # pty closed
                if not chunk:
                    return True  # EOF — child exited
                self._stream.feed(chunk)
                saw_output = True
            elif saw_output or self._proc.poll() is not None:
                return True  # quiet after output, or child already gone
            # else: quiet but nothing yet and child still starting — keep waiting
            if time.monotonic() - start > hard:
                return False

    # --- read snapshots (delegate to pure view helpers) ------------------
    def viewport(self) -> str:
        return view.viewport_text(self._screen)

    def transcript(self, limit: int | None = None) -> str:
        return view.transcript_text(self._screen, limit)

    def cell(self, x: int, y: int) -> Cell:
        return view.cell_at(self._screen, x, y)

    def cursor(self) -> tuple[int, int]:
        return view.cursor_pos(self._screen)

    def is_alt_screen(self) -> bool:
        return view.is_alt_screen(self._screen)

    # --- lifecycle -------------------------------------------------------
    def is_alive(self) -> bool:
        return self._proc.poll() is None

    def wait(self, timeout: float | None = None) -> int:
        return self._proc.wait(timeout=timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError):
            os.close(self._master)
        if self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait()

    def __enter__(self) -> TerminalSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
