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
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence

import pyte

from impty import view
from impty.screen import Regime, TerminalScreen
from impty.view import Cell

_READ_CHUNK = 65536
_FLOOR = 0.01  # 10 ms — minimum strictly-positive select() slice (deadline safety)
_POLL = 0.02  # 20 ms — nominal Phase-1 poll cadence cap
_GRACE = 0.1  # 100 ms — Phase-2 best-effort sweep margin (latency/flake knob, not correctness)
_DRAIN_YIELD = 0.002  # 2 ms — gap the background drainer leaves between reads so a
# tool-call thread waiting on the io lock is never starved by the drainer.


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
        self._screen = TerminalScreen(cols, rows, history=history, ratio=0.5)
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
        # One reader of the master at a time. wait_idle/run_to_completion hold
        # this for their whole duration; the background drainer (below) holds it
        # only between tool calls. Reentrant so a `paused()` caller can use the
        # locked accessors without self-deadlock. Guards pyte too (feed + reads).
        self._io_lock = threading.RLock()
        # monotonic time of the most recent DATA read (by anyone), or None if no
        # output has ever been read. One fact, one variable — "have we seen
        # output?" is just `is not None`, so the settle test can't conflate
        # "saw something, ever" with "saw something recently".
        self._last_data_t: float | None = None
        self._stop = False
        self._drainer = threading.Thread(
            target=self._drain_forever, name="impty-drainer", daemon=True,
        )
        self._drainer.start()

    def _drain_forever(self) -> None:
        """Continuously drain the master into pyte whenever no tool call holds
        the io lock, so terminal queries (CPR/DSR-6, device attributes) are
        answered within milliseconds regardless of MCP-call timing — exactly
        like a real terminal. Without this, a CPR issued in an inter-call gap
        goes unread past prompt_toolkit's 2 s timeout and it degrades ("terminal
        doesn't support CPR"). wait_idle/run_to_completion take the SAME lock, so
        there is only ever one reader of the master. Exits on EOF, a closed
        master, or close() setting _stop."""
        while not self._stop:
            with self._io_lock:
                try:
                    result = self._read_step(_POLL)
                except (OSError, ValueError):
                    return  # master closed under us (close() races the read)
                if result is _StepResult.EOF:
                    return  # slave fully closed — nothing more will ever arrive
            time.sleep(_DRAIN_YIELD)  # leave the lock free for a waiting tool call

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        """Hold the io lock so a caller can read a CONSISTENT frame — several
        accessors, or the raw `screen` for rendering — without the background
        drainer mutating pyte mid-read. Reentrant: the locked accessors compose
        inside it."""
        with self._io_lock:
            yield

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def screen(self) -> TerminalScreen:
        """The live pyte screen — for renderers (impty.render) that need the cell
        grid + attributes, not just the text lines() returns."""
        return self._screen

    @property
    def rows(self) -> int:
        """Live screen height (authoritative; tracks resize())."""
        return self._screen.lines

    @property
    def cols(self) -> int:
        """Live screen width (authoritative; tracks resize())."""
        return self._screen.columns

    def _write_reply(self, data: str) -> None:
        """Feed a pyte-generated terminal reply (CPR / device-attribute answer)
        back into the program's stdin. Best-effort and non-blocking: only write
        when the master is immediately writable, else drop the reply — a drain
        loop must never block on a reply when the child has stopped reading its
        stdin and the input buffer is full.

        Serialized with send() under _io_lock so the two master-write paths never
        interleave a reply mid-keystroke-sequence. Reentrant: this runs inside
        pyte.feed(), which the drainer/wait_idle already call holding _io_lock, so
        re-taking it here is a no-op recursion on the same thread."""
        with self._io_lock:
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
        # Serialize with the drainer's reply writes (_write_reply runs under this
        # same lock inside pyte.feed). Without it, a large send (>PIPE_BUF, split
        # across write syscalls) could interleave with a CPR reply and deliver a
        # corrupted escape sequence to the child. Brief contention with the
        # drainer's read slice is fine.
        with self._io_lock:
            os.write(self._master, data)

    def send_signal(self, sig: int = signal.SIGINT) -> None:
        """Deliver a real signal to the child's foreground process group, bypassing
        the tty line discipline (works regardless of cooked/raw mode). Mirrors what
        the tty would do on ^C by signalling the whole process group, not just the
        immediate child. A no-op if the child has already exited (and been reaped).

        Reaches only the child's process group: a grandchild that has setsid()'d
        into its own session is deliberately NOT signalled, exactly like a real
        Ctrl-C (which hits only the controlling terminal's foreground pgrp). Do not
        "fix" this to walk descendants.

        We pass os.getpgid(pid) (not pid) to killpg so the argument is self-
        documenting as a pgid and stays correct if start_new_session is ever
        reconsidered. start_new_session=True isolates the child in its own
        session/pgrp (pgid == pid), so killpg can only ever hit the child's group,
        never the test runner. The getpgid->killpg TOCTOU is a microsecond window
        on a not-aggressively-recycled pgid; ProcessLookupError suppression covers
        both calls (dead-child contract: a reaped child makes both raise ESRCH)."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(self._proc.pid), sig)

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
        self._last_data_t = time.monotonic()
        return _StepResult.DATA

    def wait_idle(
        self, idle: float = 0.2, hard: float = 10.0, *, since: float = 0.0
    ) -> bool:
        """Block until the child produces output and then goes quiet for `idle`
        seconds (or exits, or `hard` elapses), feeding everything to pyte.

        Non-blocking via select(). A quiet window BEFORE any qualifying output
        has been read does NOT count as settled — that would race a slow
        fork/interpreter startup and return an empty screen. So we settle only
        once qualifying output has been read (normal case), or the child has
        already exited (nothing more is coming). Returns True when settled, False
        if `hard` elapsed while output kept arriving. The final select() may
        block up to `idle`, so the effective wall-clock cap is ~`hard + idle`.

        `since` is a monotonic timestamp: only output read at or after it
        qualifies. Default 0.0 counts ANY output ever read — including what the
        background drainer consumed during an inter-call gap — which is what a
        bare `wait`/`start` wants (don't spin to `hard` re-waiting for output
        that already landed). For type-then-look, callers pass `since` = a moment
        just before their keystroke/signal, so a STALE pre-action screen (the
        drainer's `_last_data_t` may be seconds old) can't masquerade as 'already
        settled': we keep waiting for the reaction to actually arrive and go
        quiet. The drainer can't 'steal' that reaction either — its `_last_data_t`
        stamp is still `>= since`, so drainer-consumed reaction bytes qualify just
        as if we'd read them ourselves.

        `_last_data_t` is shared state updated by `_read_step` whoever calls it,
        all under `_io_lock`."""
        with self._io_lock:
            start = time.monotonic()
            while True:
                result = self._read_step(idle)
                now = time.monotonic()
                if result is _StepResult.EOF:
                    return True  # child exited / pty closed
                last = self._last_data_t
                if last is not None and last >= since:  # qualifying output seen...
                    if (now - last) >= idle:
                        return True  # ...by us or the drainer, then quiet
                elif self._proc.poll() is not None:
                    return True  # child exited having produced nothing qualifying
                if now - start > hard:
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
        with self._io_lock:
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
    def lines(self, limit: int | None = None) -> list[str]:
        """Rendered text rows over scrollback + the live screen. With a `limit`
        (e.g. limit=rows) returns exactly the last N rows, aligned row-for-row
        with cell()/cursor(); with no limit returns the whole strip (trailing
        blanks trimmed). See view.lines."""
        with self._io_lock:
            return view.lines(self._screen, limit)

    def cell(self, x: int, y: int) -> Cell:
        with self._io_lock:
            return view.cell_at(self._screen, x, y)

    def cursor(self) -> tuple[int, int]:
        with self._io_lock:
            return view.cursor_pos(self._screen)

    def regime(self) -> Regime:
        with self._io_lock:
            return view.regime(self._screen)

    def is_alt_screen(self) -> bool:
        with self._io_lock:
            return view.is_alt_screen(self._screen)

    def resize(self, rows: int, cols: int) -> None:
        """Resize the terminal. ioctl(TIOCSWINSZ) on the pty MASTER (the slave is
        closed at spawn); the kernel then auto-sends SIGWINCH to the child's
        foreground pgrp. Also resizes the live pyte screen (and, if alt is active,
        the saved pre-alt buffer — see TerminalScreen.resize). This is pyte's
        pad/clip/delete, NOT true reflow/rewrap."""
        with self._io_lock:
            _set_winsize(self._master, rows, cols)
            self._screen.resize(lines=rows, columns=cols)

    # --- lifecycle -------------------------------------------------------
    def is_alive(self) -> bool:
        return self._proc.poll() is None

    def wait(self, timeout: float | None = None) -> int:
        return self._proc.wait(timeout=timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Stop the background drainer and let it exit its current read BEFORE we
        # close the master out from under it (it catches the close, but joining
        # first keeps teardown clean and race-free).
        self._stop = True
        drainer = getattr(self, "_drainer", None)
        if drainer is not None:
            drainer.join(timeout=2.0)
        with contextlib.suppress(OSError):
            os.close(self._master)
        if self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait()

    def __enter__(self) -> TerminalSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
