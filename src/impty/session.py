"""A terminal session: spawn an interactive program on a real pty, feed its
output into a long-lived pyte screen, send raw keystrokes, and read snapshots.

Deterministic plumbing only — no agent, no semantic translation. The pty +
spawn idiom matches tests/test_human_gate_pty.py (stdlib pty, start_new_session)."""

from __future__ import annotations

import contextlib
import enum
import errno
import fcntl
import os
import pty
import select
import struct
import subprocess
import termios
import time
from collections.abc import Callable, Mapping, Sequence

import pyte

from impty import view
from impty.view import Cell

_READ_CHUNK = 65536
_FLOOR = 0.01  # 10 ms — minimum strictly-positive select() slice (deadline safety)
_POLL = 0.02  # 20 ms — nominal Phase-1 poll cadence cap
_GRACE = 0.1  # 100 ms — Phase-2 best-effort sweep margin (latency/flake knob, not correctness)


def _select_slice(remaining: float) -> float:
    """A small, strictly-positive select() slice: poll at most every _POLL s,
    never block past the remaining budget, never <= 0 (so select() stays valid
    right at a deadline)."""
    return max(_FLOOR, min(remaining, _POLL))


class _StepResult(enum.Enum):
    """Outcome of one drain step. DATA: bytes read and fed to pyte. EOF: the
    slave is fully closed. QUIET: the select slice elapsed with nothing
    readable."""

    DATA = "data"
    EOF = "eof"
    QUIET = "quiet"


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class _ReplyScreen(pyte.HistoryScreen):
    """A pyte screen that writes terminal replies back to the program.

    pyte computes answers to query sequences — cursor-position reports (CPR /
    DSR 6: ESC[6n) and device-attribute requests — and emits them via
    write_process_input(), which is a no-op by default. A real terminal feeds
    those replies into the program's stdin. prompt_toolkit and curses apps
    BLOCK on the CPR reply before enabling normal key bindings, so without it
    line input degrades (Enter stops submitting). We forward the reply to the
    pty master so the program reads it, just like a real terminal."""

    reply: Callable[[str], None] | None = None  # set after the pty opens

    def write_process_input(self, data: str) -> None:
        if self.reply is not None:
            self.reply(data)


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
        self._screen = _ReplyScreen(cols, rows, history=history, ratio=0.5)
        self._stream = pyte.ByteStream(self._screen)
        self._master, slave = pty.openpty()
        # Forward pyte's terminal-query replies (CPR, device attributes) to the
        # program's stdin, exactly as a real terminal does.
        self._screen.reply = self._write_reply
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

    def _write_reply(self, data: str) -> None:
        """Feed a pyte-generated terminal reply (CPR / device-attribute answer)
        back into the program's stdin. Best-effort and non-blocking: only write
        when the master is immediately writable, else drop the reply — a drain
        loop must never block on a reply when the child has stopped reading its
        stdin and the input buffer is full."""
        _, writable, _ = select.select([], [self._master], [], 0)
        if writable:
            with contextlib.suppress(OSError):
                os.write(self._master, data.encode("utf-8"))

    def send(self, data: bytes | str) -> None:
        """Write raw keystrokes to the program. Strings are UTF-8 encoded.
        Keys are literal bytes: '\\r' Enter, '\\x1b[B' Down, '\\x03' Ctrl-C, '\\t' Tab."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        os.write(self._master, data)

    def _read_step(self, timeout: float) -> _StepResult:
        """One select→read→feed step, classified tri-state.

        DATA  — bytes were read and fed to pyte.
        EOF   — the slave is fully closed: os.read returned b'', or raised
                OSError with errno == EIO (the Linux master-EOF surface; macOS
                yields b''). Other OSErrors propagate as genuine errors.
        QUIET — the select slice elapsed with nothing readable."""
        readable, _, _ = select.select([self._master], [], [], timeout)
        if not readable:
            return _StepResult.QUIET
        try:
            chunk = os.read(self._master, _READ_CHUNK)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return _StepResult.EOF
            raise
        if not chunk:
            return _StepResult.EOF
        self._stream.feed(chunk)
        return _StepResult.DATA

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
            result = self._read_step(idle)
            if result is _StepResult.EOF:
                return True  # child exited / pty closed
            if result is _StepResult.DATA:
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
