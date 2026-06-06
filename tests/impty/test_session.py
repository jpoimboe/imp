from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from impty import TerminalSession


def _can_pty() -> bool:
    try:
        import pty

        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _can_pty(), reason="pty not available")


def test_spawn_and_read_startup_output() -> None:
    with TerminalSession([sys.executable, "-c", "print('hello world')"]) as s:
        s.wait_idle()
        assert "hello world" in s.viewport()


def test_send_is_received_by_child() -> None:
    prog = "import sys; print(sys.stdin.readline().strip().upper())"
    with TerminalSession([sys.executable, "-c", prog]) as s:
        s.send("abc\r")
        s.wait_idle()
        assert "ABC" in s.viewport()


def test_wait_idle_returns_while_child_blocks_on_input() -> None:
    # The readiness trap: child prints a prompt then blocks on stdin with no
    # EOF. A blocking read would hang forever; select-based wait_idle returns.
    prog = "import sys; sys.stdout.write('Name? '); sys.stdout.flush(); sys.stdin.readline()"
    with TerminalSession([sys.executable, "-c", prog]) as s:
        settled = s.wait_idle(idle=0.2, hard=5.0)
        assert settled is True
        assert "Name?" in s.viewport()


def test_wait_idle_hard_timeout_returns_false_on_chatty_child() -> None:
    # A child that never goes quiet hits the hard cap and reports it.
    prog = (
        "import sys,time\n"
        "while True: sys.stdout.write('x'); sys.stdout.flush(); time.sleep(0.01)"
    )
    with TerminalSession([sys.executable, "-c", prog]) as s:
        assert s.wait_idle(idle=0.5, hard=1.0) is False


def test_default_term_is_set_for_color_fidelity() -> None:
    # The child inherits TERM=xterm-256color by default, so a real program's
    # color emission (and thus cell().fg) is deterministic across machines/CI.
    prog = "import os; print(os.environ.get('TERM', 'UNSET'))"
    with TerminalSession([sys.executable, "-c", prog]) as s:
        s.wait_idle()
        assert "xterm-256color" in s.viewport()


def test_transcript_captures_scrolled_output() -> None:
    prog = "[print(f'line {i:02d}') for i in range(1, 41)]"
    with TerminalSession([sys.executable, "-c", prog], cols=80, rows=24) as s:
        s.wait_idle()
        transcript = s.transcript()
        lines = [ln for ln in transcript.split("\n") if ln.startswith("line ")]
        assert lines[0] == "line 01"
        assert lines[-1] == "line 40"
        assert len(lines) == 40


def test_is_alive_and_wait_report_exit() -> None:
    with TerminalSession([sys.executable, "-c", "print('bye')"]) as s:
        rc = s.wait(timeout=5.0)
        assert rc == 0
        assert s.is_alive() is False


def test_cli_under_test_is_not_alt_screen() -> None:
    with TerminalSession([sys.executable, "-c", "print('plain cli')"]) as s:
        s.wait_idle()
        assert s.is_alt_screen() is False


def test_cpr_query_is_answered_so_program_can_read_reply() -> None:
    # A real terminal answers a cursor-position request (CPR / DSR 6: ESC[6n)
    # by writing ESC[<row>;<col>R back to the program's stdin. prompt_toolkit
    # (and curses apps) issue this and BLOCK on the reply before enabling their
    # normal key bindings; without it, line input degrades and Enter never
    # submits. The child here emits ESC[6n, reads the reply byte-by-byte until
    # 'R', and echoes the row;col payload — proving the reply reached it.
    # Raw mode mirrors what CPR users (prompt_toolkit, curses) actually do:
    # read the reply byte-by-byte with no line buffering and no echo.
    prog = (
        "import sys, tty, termios\n"
        "fd = sys.stdin.fileno()\n"
        "old = termios.tcgetattr(fd)\n"
        "tty.setraw(fd)\n"
        "try:\n"
        "    sys.stdout.write('\\x1b[6n'); sys.stdout.flush()\n"
        "    buf = ''\n"
        "    while True:\n"
        "        c = sys.stdin.read(1)\n"
        "        if not c or c == 'R': break\n"
        "        buf += c\n"
        "finally:\n"
        "    termios.tcsetattr(fd, termios.TCSADRAIN, old)\n"
        "sys.stdout.write('CPR=' + buf.lstrip('\\x1b[') + '\\n'); sys.stdout.flush()\n"
    )
    with TerminalSession([sys.executable, "-c", prog]) as s:
        s.wait_idle(idle=0.3, hard=5.0)
        # Cursor starts at row 1, col 1 (1-based, as DSR reports it).
        assert "CPR=1;1" in s.viewport()


def test_cursor_parks_after_prompt_string() -> None:
    # "Waiting for input looks like": cursor sits immediately after the prompt
    # text, on the bottom non-blank row, with no further output. This is the
    # cheap signal an agent-arbiter uses to recognize a prompt — no /proc.
    # NB: viewport_text right-trims, so the rendered row is "Project name?"
    # (no trailing space); the cursor x still sits PAST the space at col 14.
    # Assert the two facts separately — don't expect the trailing space in text.
    prog = (
        "import sys; sys.stdout.write('Project name? ');"
        " sys.stdout.flush(); sys.stdin.readline()"
    )
    with TerminalSession([sys.executable, "-c", prog]) as s:
        s.wait_idle(idle=0.2, hard=5.0)
        x, y = s.cursor()
        assert s.viewport().split("\n")[y].rstrip() == "Project name?"  # text (trimmed)
        assert x == len("Project name? ")                               # cursor past the space


# A child that installs a SIGINT handler emitting an async-signal-safe marker and
# exiting via the handler. The ready marker uses ordinary buffered output + flush
# (it prints before any signal can fire); the HANDLER must use os.write(1, ...) —
# print() from a signal handler followed by os._exit(42) would drop the marker
# (buffered I/O is signal-unsafe and _exit skips the flush). Sleeps well past the
# test deadline so the ONLY exit path is the handler.
_SIGINT_CHILD = (
    "import os, signal, sys, time\n"
    "def _handler(signum, frame):\n"
    "    os.write(1, b'GOT-SIGINT\\n')\n"
    "    os._exit(42)\n"
    "signal.signal(signal.SIGINT, _handler)\n"
    "print('READY', flush=True)\n"
    "time.sleep(30)\n"
)


def test_ctrl_c_byte_delivers_sigint() -> None:
    # Faithful byte path (cooked mode): with a controlling terminal set up, the
    # tty line discipline turns send("\x03") into a real SIGINT to the child's
    # foreground process group, firing its handler.
    with TerminalSession([sys.executable, "-c", _SIGINT_CHILD]) as s:
        s.wait_idle(idle=0.2, hard=5.0)
        assert "READY" in s.viewport()
        try:
            s.send("\x03")
            rc = s.run_to_completion(hard=5.0)
            assert rc == 42
            assert "GOT-SIGINT" in s.transcript()
        finally:
            if s.is_alive():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(s.pid, signal.SIGKILL)


def test_run_to_completion_drains_flood_and_keeps_trailing_sentinel() -> None:
    # AC1+AC2: a child that floods ≫ one tty-buffer, then prints a sentinel and
    # exits. run_to_completion must drain PAST the buffer-fill point to the end,
    # WITHOUT relying on the hard cap — so assert elapsed ≪ hard with a wide
    # margin (sub-second vs. multi-second, so it won't flake under CI load).
    prog = (
        "import sys\n"
        "sys.stdout.write('A' * 200000)\n"  # ≫ 64 KiB kernel tty buffer
        "sys.stdout.write('\\nDONE-1\\n')\n"  # trailing sentinel AFTER the flood
        "sys.stdout.flush()\n"
    )
    with TerminalSession([sys.executable, "-c", prog]) as s:
        start = time.monotonic()
        rc = s.run_to_completion(hard=5.0)
        elapsed = time.monotonic() - start
        assert rc == 0
        assert elapsed < 1.0, f"drained via hard cap, not EOF (elapsed={elapsed:.2f}s)"
        # Assert on the pyte-RENDERED line: the pty line discipline maps \n→\r\n
        # (ONLCR), so the raw master tail isn't byte-exact. transcript() is
        # history-bounded (pyte history=2000), but the trailing line survives.
        assert "DONE-1" in s.transcript()


def test_run_to_completion_raises_when_child_never_exits() -> None:
    # AC3: a child that stays alive (silent, never exits) must hit the hard cap
    # and raise TimeoutExpired, leaving the process for close() to reap.
    prog = "import time\nwhile True: time.sleep(0.05)"
    with TerminalSession([sys.executable, "-c", prog]) as s:
        with pytest.raises(subprocess.TimeoutExpired):
            s.run_to_completion(hard=0.3)
        assert s.is_alive() is True  # left running for close()


def test_wait_hangs_then_run_to_completion_drains_same_session() -> None:
    # AC5: the OLD wait()-only path hangs on a flooding child (blocked in
    # write() because nobody drains the master). The SAME still-blocked child is
    # then driven by run_to_completion() on the SAME session, draining it.
    prog = (
        "import sys\n"
        "sys.stdin.readline()\n"  # wait for the send() below
        "sys.stdout.write('A' * 300000)\n"  # ≥ 256 KiB ≫ kernel tty buffer
        "sys.stdout.write('\\nDONE-5\\n')\n"
        "sys.stdout.flush()\n"
    )
    with TerminalSession([sys.executable, "-c", prog]) as s:
        s.send("go\r")  # triggers the flood; NO wait_idle/drain before the foil
        # `short` is deterministically safe: the child cannot have exited within
        # 0.5 s — it is either still in readline() or blocked in write() (nobody
        # drains the master, so the flood fills the tty buffer well before
        # 300 KB). Either way the process is alive, so wait()'s proc.wait(0.5)
        # raises TimeoutExpired (the old hang). The raise leaves it untouched.
        with pytest.raises(subprocess.TimeoutExpired):
            s.wait(timeout=0.5)
        rc = s.run_to_completion(hard=5.0)  # same blocked child, now drained
        assert rc == 0
        assert "DONE-5" in s.transcript()


# A child that forks a grandchild which calls setsid() (so it survives the
# child's exit), inherits the pty slave (fds 0/1/2) and keeps it open, pinning
# the slave so the master never sees EOF. The child records the grandchild PID
# to a side-channel file (argv[1]) — NOT the slave — then exits with code 7.
_QUIET_GRANDCHILD = (
    "import os, sys, time\n"
    "pidfile = sys.argv[1]\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    os.setsid()\n"  # detach into own session: outlive the child, hold slave
    "    time.sleep(30)\n"  # ≫ grace; silently pins the slave
    "    os._exit(0)\n"
    "else:\n"
    "    with open(pidfile, 'w') as f:\n"  # side channel (NOT the slave)
    "        f.write(str(pid))\n"  # explicit flush — os._exit() skips stdio flush
    "    os._exit(7)\n"
)

# Same shape, but the grandchild streams continuously and the child stays alive
# ~0.2 s while it streams — so Phase 1 sees select() always-readable and MUST
# re-check poll() every iteration to exit. The grandchild self-cleans on EIO
# when the master closes.
_STREAMING_GRANDCHILD = (
    "import os, sys, time\n"
    "pidfile = sys.argv[1]\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    os.setsid()\n"
    "    while True:\n"
    "        try:\n"
    "            os.write(1, b'g')\n"  # stream to the slave forever
    "        except OSError:\n"
    "            os._exit(0)\n"  # master closed → EIO → self-clean
    "        time.sleep(0.001)\n"
    "else:\n"
    "    time.sleep(0.2)\n"  # stay alive while the gc streams...
    "    with open(pidfile, 'w') as f:\n"
    "        f.write(str(pid))\n"  # explicit flush — os._exit() skips stdio flush
    "    os._exit(7)\n"  # ...so Phase 1 must re-check poll() each iteration
)


def test_run_to_completion_fast_on_quiet_slave_pinning_grandchild(tmp_path: Path) -> None:
    # AC4 (quiet): child exits while a silent grandchild pins the slave. Phase 2
    # stops on the first quiet select → fast completion, not a hard-cap stall.
    pidfile = tmp_path / "gc.pid"
    gc_pid: int | None = None
    try:
        with TerminalSession([sys.executable, "-c", _QUIET_GRANDCHILD, str(pidfile)]) as s:
            start = time.monotonic()
            rc = s.run_to_completion(hard=5.0)
            elapsed = time.monotonic() - start
            assert rc == 7
            assert elapsed < 1.0, f"stalled toward hard cap (elapsed={elapsed:.2f}s)"
            gc_pid = int(pidfile.read_text())
    finally:
        # The sleep-blocked grandchild ignores os.close(master); reap it by PID
        # so it can't leak past the test reparented to a subreaper.
        if gc_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(gc_pid, signal.SIGKILL)


def test_run_to_completion_fast_on_streaming_slave_pinning_grandchild(
    tmp_path: Path,
) -> None:
    # AC4 (streaming): the grandchild streams continuously and the child stays
    # alive ~0.2 s while it does. Continuous output keeps select() always
    # readable, so ONLY a per-iteration poll() check lets Phase 1 exit — a
    # not-readable-branch-only poll() would stall to the hard cap. Phase 2 then
    # stops on `grace` expiry.
    pidfile = tmp_path / "gc.pid"
    gc_pid: int | None = None
    try:
        with TerminalSession(
            [sys.executable, "-c", _STREAMING_GRANDCHILD, str(pidfile)]
        ) as s:
            start = time.monotonic()
            rc = s.run_to_completion(hard=5.0)
            elapsed = time.monotonic() - start
            assert rc == 7
            assert elapsed < 2.0, f"stalled toward hard cap (elapsed={elapsed:.2f}s)"
            gc_pid = int(pidfile.read_text())
    finally:
        # The streaming grandchild self-cleans on EIO, but kill by PID uniformly
        # in case it's mid-sleep when the master closes.
        if gc_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(gc_pid, signal.SIGKILL)
