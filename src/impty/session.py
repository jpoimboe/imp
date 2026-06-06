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
import signal
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


def _make_controlling_tty(
    _ioctl: Callable[..., int] = fcntl.ioctl,
    _tiocsctty: int = termios.TIOCSCTTY,
) -> None:
    # start_new_session already ran setsid() in the child, making it a session
    # leader with no controlling tty. Claim the slave (now fd 0) as this
    # session's controlling terminal so the line discipline turns ^C/^Z/^\ into
    # real signals to our foreground process group. MUST stay exactly this one
    # ioctl — no logging, no allocation, no added Python — because it runs
    # post-fork() in a process that may hold threads (the MCP server forks from a
    # worker thread). The names are bound as default arguments so the post-fork
    # body resolves them via LOAD_FAST off the function object, touching neither
    # the module dict nor any attribute lookup at fork time. Do NOT add anything
    # here; any addition reintroduces the fork-under-threads deadlock window.
    _ioctl(0, _tiocsctty, 0)


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
            preexec_fn=_make_controlling_tty,
        )
        os.close(slave)
        self._closed = False

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def screen(self) -> _ReplyScreen:
        """The live pyte screen — for renderers (impty.render) that need the cell
        grid + attributes, not just the text viewport()/transcript() return."""
        return self._screen

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
        Keys are literal bytes: '\\r' Enter, '\\x1b[B' Down, '\\t' Tab.

        '\\x03' (Ctrl-C) behaves exactly as on a real terminal: in cooked mode
        (ISIG on) the tty line discipline turns it into a real SIGINT to the
        child's foreground process group (the controlling terminal is set up at
        spawn); in raw mode (ISIG off, e.g. prompt_toolkit) it stays a raw byte
        the app turns into its own KeyboardInterrupt. For a mode-independent
        signal, use send_signal()."""
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

    def run_to_completion(self, hard: float = 60.0) -> int:
        """Drive the child to exit, feeding ALL remaining output to pyte first.

        Two phases:
          Phase 1 (child running) — drain the master, checking proc.poll()
            EVERY iteration, until the child exits (poll() set) or the slave
            closes (EOF). Checking poll() each iteration (not only when
            not-readable) is what lets a continuously-streaming, slave-pinning
            grandchild terminate instead of stalling to the hard cap.
          Phase 2 (child exited) — a time-bounded sweep of the kernel buffer
            until EOF or the first quiet select, or `grace` expiry. In the
            normal flood case (no grandchild) the exited child's output is
            finite and fully buffered, so Phase 2 reaches EOF in microseconds →
            full capture. A slave-pinning grandchild yields a fast completion
            instead of a `hard`-length stall (its trailing bytes may be
            truncated by `grace` — desired; they are the grandchild's).

        Returns the child's exit code. Raises subprocess.TimeoutExpired if
        `hard` elapses while the child is STILL ALIVE; the process is left
        as-is for the caller's close() to tear down. The raise may originate
        from either the Phase-1 deadline check (child still running) or the
        final reap (child closed the slave but hasn't called _exit() yet) —
        in both cases `hard` elapsed with the child alive. `hard=60.0` is a
        fast-path/test default — streaming (LLM) callers pass a larger budget."""
        deadline = time.monotonic() + hard
        # Phase 1 — child running: drain, watching for exit every iteration.
        while self._proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self._proc.args, hard)
            if self._read_step(_select_slice(remaining)) is _StepResult.EOF:
                break
        # Phase 2 — child exited: bounded sweep of buffered bytes.
        grace_deadline = time.monotonic() + _GRACE
        while True:
            remaining = grace_deadline - time.monotonic()
            if remaining <= 0:
                break
            if self._read_step(_select_slice(remaining)) is not _StepResult.DATA:
                break  # EOF (full capture) or QUIET (nothing more) — done
        # Reap, bounded by the remaining hard budget (floored so a Phase-2 stop
        # right at the deadline can't hand wait() a ~0 timeout and spuriously
        # raise for an already-dead child).
        reap = max(_FLOOR, deadline - time.monotonic())
        return self._proc.wait(timeout=reap)

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
