from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from impty import Regime, TerminalSession


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
        assert "hello world" in "\n".join(s.lines())


def test_send_is_received_by_child() -> None:
    prog = "import sys; print(sys.stdin.readline().strip().upper())"
    with TerminalSession([sys.executable, "-c", prog]) as s:
        s.send("abc\r")
        s.wait_idle()
        assert "ABC" in "\n".join(s.lines())


def test_wait_idle_returns_while_child_blocks_on_input() -> None:
    # The readiness trap: child prints a prompt then blocks on stdin with no
    # EOF. A blocking read would hang forever; select-based wait_idle returns.
    prog = "import sys; sys.stdout.write('Name? '); sys.stdout.flush(); sys.stdin.readline()"
    with TerminalSession([sys.executable, "-c", prog]) as s:
        settled = s.wait_idle(idle=0.2, hard=5.0)
        assert settled is True
        assert "Name?" in "\n".join(s.lines())


def test_wait_idle_hard_timeout_returns_false_on_chatty_child() -> None:
    # A child that never goes quiet hits the hard cap and reports it.
    prog = (
        "import sys,time\n"
        "while True: sys.stdout.write('x'); sys.stdout.flush(); time.sleep(0.01)"
    )
    with TerminalSession([sys.executable, "-c", prog]) as s:
        assert s.wait_idle(idle=0.5, hard=1.0) is False


# A child that parks quietly, then on one keystroke reacts AFTER a delay longer
# than `idle`, in raw mode so there is no tty echo within the idle window. This
# is the "type-then-look" failure shape: a key triggering a delayed reaction
# (RPC/LLM) with no immediate redraw.
_DELAYED_REACTION_CHILD = (
    "import sys, os, tty, termios, time\n"
    "fd = sys.stdin.fileno()\n"
    "old = termios.tcgetattr(fd); tty.setraw(fd)\n"
    "os.write(1, b'READY\\n')\n"
    "os.read(fd, 1)\n"            # block until the keystroke
    "time.sleep(0.6)\n"          # delayed reaction, > idle, no echo
    "os.write(1, b'REACTION\\n')\n"
    "termios.tcsetattr(fd, termios.TCSADRAIN, old)\n"
    "time.sleep(5)\n"
)


def test_wait_idle_since_waits_for_delayed_reaction_not_stale_screen() -> None:
    # Regression: with `since`, a wait must not settle on the pre-send screen
    # just because the LAST output (the drainer's _last_data_t) is seconds old.
    # It must wait for the reaction to THIS keystroke to actually arrive.
    with TerminalSession([sys.executable, "-c", _DELAYED_REACTION_CHILD]) as s:
        s.wait_idle(idle=0.2, hard=5.0)
        assert "READY" in "\n".join(s.lines())
        time.sleep(0.5)  # let _last_data_t go stale relative to the send
        since = time.monotonic()
        s.send("x")
        settled = s.wait_idle(idle=0.2, hard=5.0, since=since)
        assert settled is True
        # Without the since boundary this returns at ~0.2s on the stale READY
        # screen, before the +0.6s reaction lands.
        assert "REACTION" in "\n".join(s.lines())


def test_default_term_is_set_for_color_fidelity() -> None:
    # The child inherits TERM=xterm-256color by default, so a real program's
    # color emission (and thus cell().fg) is deterministic across machines/CI.
    prog = "import os; print(os.environ.get('TERM', 'UNSET'))"
    with TerminalSession([sys.executable, "-c", prog]) as s:
        s.wait_idle()
        assert "xterm-256color" in "\n".join(s.lines())


def test_transcript_captures_scrolled_output() -> None:
    prog = "[print(f'line {i:02d}') for i in range(1, 41)]"
    with TerminalSession([sys.executable, "-c", prog], cols=80, rows=24) as s:
        s.wait_idle()
        lines = [ln for ln in s.lines() if ln.startswith("line ")]
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
        assert "CPR=1;1" in "\n".join(s.lines())


# A child that issues CPR and gives the terminal only a SHORT window to answer,
# WITHOUT the test draining during that window. A real terminal answers within
# milliseconds no matter what the controlling program is doing; impty must too —
# so a CPR issued in an inter-call gap (no wait_idle in flight) is still answered.
# This is the prompt_toolkit "doesn't support CPR" degradation reduced to its core:
# prompt_toolkit waits 2s, but the mechanism is identical and this is fast.
_CPR_SHORT_WINDOW_CHILD = (
    "import sys, os, select, tty, termios, time\n"
    "fd = sys.stdin.fileno()\n"
    "old = termios.tcgetattr(fd); tty.setraw(fd)\n"
    "buf = b''\n"
    "try:\n"
    "    os.write(1, b'\\x1b[6n')\n"
    "    deadline = time.monotonic() + 0.4\n"   # short answer window
    "    while time.monotonic() < deadline:\n"
    "        r, _, _ = select.select([fd], [], [], deadline - time.monotonic())\n"
    "        if not r: break\n"
    "        c = os.read(fd, 1)\n"
    "        if not c or c == b'R': break\n"
    "        buf += c\n"
    "finally:\n"
    "    termios.tcsetattr(fd, termios.TCSADRAIN, old)\n"
    "got = (b'CPR=' + buf.lstrip(b'\\x1b[')) if buf else b'NO-CPR'\n"
    "os.write(1, b'\\n' + got + b'\\n')\n"
    "time.sleep(5)\n"
)


def test_cpr_answered_without_a_concurrent_wait_idle() -> None:
    with TerminalSession([sys.executable, "-c", _CPR_SHORT_WINDOW_CHILD]) as s:
        # Deliberately do NOT drain while the child's 0.4s CPR window is open —
        # mimics an MCP inter-call gap. Only a background drainer can answer in
        # time; an on-demand-only reader would miss the window → child prints
        # NO-CPR. Sleep past the child's window before the first wait_idle.
        time.sleep(0.8)
        s.wait_idle(idle=0.3, hard=3.0)
        out = "\n".join(s.lines())
        assert "CPR=1;1" in out
        assert "NO-CPR" not in out


def test_drainer_survives_a_feed_error_and_keeps_draining(caplog) -> None:
    # The drainer is a permanent reliability daemon (answer CPR in inter-call
    # gaps). One bad feed — a pyte parse bug on a pathological sequence — must
    # not kill it, else the degradation it exists to prevent silently returns.
    # The child sleeps briefly so the flaky feed is installed before BOOM lands;
    # BOOM raises (simulated parse failure) and is dropped, GOOD drains after.
    prog = (
        "import sys, time\n"
        "time.sleep(0.3)\n"
        "sys.stdout.write('BOOM\\n'); sys.stdout.flush()\n"
        "time.sleep(0.4)\n"
        "sys.stdout.write('GOOD\\n'); sys.stdout.flush()\n"
        "time.sleep(5)\n"
    )
    with TerminalSession([sys.executable, "-c", prog]) as s:
        real_feed = s._stream.feed

        def flaky(data: bytes) -> None:
            if b"BOOM" in data:
                raise RuntimeError("simulated pyte parse failure")
            real_feed(data)

        s._stream.feed = flaky  # type: ignore[method-assign]
        # Don't hold the lock: let the background drainer hit the error on the
        # BOOM chunk and the real output on the GOOD chunk.
        with caplog.at_level("ERROR", logger="impty.session"):
            time.sleep(1.2)
        out = "\n".join(s.lines())
        assert s._drainer.is_alive()        # daemon survived the bad feed
        assert "draining pty" in caplog.text  # and logged it (observable, not silent)
        assert "BOOM" not in out            # the bad chunk was dropped
        assert "GOOD" in out                # later output still drained


def test_cursor_parks_after_prompt_string() -> None:
    # "Waiting for input looks like": cursor sits immediately after the prompt
    # text, on the bottom non-blank row, with no further output. This is the
    # cheap signal an agent-arbiter uses to recognize a prompt — no /proc.
    # NB: lines() right-trims each row, so the rendered row is "Project name?"
    # (no trailing space); the cursor x still sits PAST the space at col 14.
    # Assert the two facts separately — don't expect the trailing space in text.
    prog = (
        "import sys; sys.stdout.write('Project name? ');"
        " sys.stdout.flush(); sys.stdin.readline()"
    )
    with TerminalSession([sys.executable, "-c", prog]) as s:
        s.wait_idle(idle=0.2, hard=5.0)
        x, y = s.cursor()
        win = s.lines(limit=s.rows)          # exactly rows, aligned with cell()/cursor()
        assert win[y].rstrip() == "Project name?"         # row y of the window == cursor's row
        assert x == len("Project name? ")                 # cursor past the trailing space


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
        assert "READY" in "\n".join(s.lines())
        try:
            s.send("\x03")
            rc = s.run_to_completion(hard=5.0)
            assert rc == 42
            assert "GOT-SIGINT" in "\n".join(s.lines())
        finally:
            if s.is_alive():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(s.pid, signal.SIGKILL)


def test_send_signal_delivers_sigint() -> None:
    # Direct path: send_signal() delivers SIGINT to the child's process group
    # regardless of tty mode, bypassing the line discipline.
    with TerminalSession([sys.executable, "-c", _SIGINT_CHILD]) as s:
        s.wait_idle(idle=0.2, hard=5.0)
        assert "READY" in "\n".join(s.lines())
        try:
            s.send_signal(signal.SIGINT)
            rc = s.run_to_completion(hard=5.0)
            assert rc == 42
            assert "GOT-SIGINT" in "\n".join(s.lines())
        finally:
            if s.is_alive():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(s.pid, signal.SIGKILL)


def test_run_to_completion_drains_flood_and_keeps_trailing_sentinel() -> None:
    # AC1+AC2: a child that floods ≫ one tty-buffer, then prints a sentinel and
    # exits. run_to_completion must drain PAST the buffer-fill point to the end,
    # WITHOUT relying on the hard cap. The assertion below only needs to tell an
    # EOF-drain apart from a hard-cap stall, so it asserts elapsed ≪ hard. It is
    # deliberately NOT a tight sub-second bound: the drain's own cost is dominated
    # by pyte's feed() rendering the ~2350-line flood through its history buffer
    # (~0.85 s on an idle box, and it grows under CPU load) — a 1 s bound collides
    # with that legitimate cost and flakes. A hard-cap stall would instead make
    # elapsed ≈ hard, so `< hard / 2` separates the two with a wide, load-robust
    # margin (real drain ~1-3 s under load vs. a 10 s cap).
    prog = (
        "import sys\n"
        "sys.stdout.write('A' * 200000)\n"  # ≫ 64 KiB kernel tty buffer
        "sys.stdout.write('\\nDONE-1\\n')\n"  # trailing sentinel AFTER the flood
        "sys.stdout.flush()\n"
    )
    with TerminalSession([sys.executable, "-c", prog]) as s:
        hard = 10.0
        start = time.monotonic()
        rc = s.run_to_completion(hard=hard)
        elapsed = time.monotonic() - start
        assert rc == 0
        assert elapsed < hard / 2, f"drained via hard cap, not EOF (elapsed={elapsed:.2f}s)"
        # Assert on the pyte-RENDERED line: the pty line discipline maps \n→\r\n
        # (ONLCR), so the raw master tail isn't byte-exact. transcript() is
        # history-bounded (pyte history=2000), but the trailing line survives.
        assert "DONE-1" in "\n".join(s.lines())


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
        assert "DONE-5" in "\n".join(s.lines())


# A child that forks a grandchild which calls setsid() (so it survives the
# child's exit), inherits the pty slave (fds 0/1/2) and keeps it open, pinning
# the slave so the master never sees EOF. The child records the grandchild PID
# to a side-channel file (argv[1]) — NOT the slave — then exits with code 7.
#
# Pipe handshake (do NOT remove): with a controlling terminal, the child's exit
# sends SIGHUP to its foreground process group. The grandchild must be OUT of
# that pgrp (setsid'd) BEFORE the child exits, or it dies on SIGHUP, its slave
# fds close, and the master hits EOF — a FALSE pass via the EOF path even if the
# per-iteration poll() regressed. The fork-time pipe orders child-exit after
# grandchild-setsid: the child blocks on os.read(r) until the grandchild writes.
# Create the pipe BEFORE fork so both ends are inherited; each side closes the
# end it does not use (so the child's read can't hang on a write end it holds).
# signal(SIGHUP, SIG_IGN) is defense-in-depth for the fork->SIG_IGN window; keep
# it too.
_QUIET_GRANDCHILD = (
    "import os, signal, sys, time\n"
    "pidfile = sys.argv[1]\n"
    "r, w = os.pipe()\n"  # BEFORE fork: both ends inherited by child + grandchild
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    os.close(r)\n"  # grandchild uses only the write end
    "    signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"  # cover fork->SIG_IGN window
    "    os.setsid()\n"  # leave the fg pgrp; keep inherited slave fds open
    "    os.write(w, b'x')\n"  # handshake: tell the child we've detached
    "    os.close(w)\n"  # symmetric close; no dangling write end on the long-lived gc
    "    time.sleep(30)\n"  # >> grace; silently pins the slave by staying alive
    "    os._exit(0)\n"
    "else:\n"
    "    os.close(w)\n"  # child uses only the read end (so the read can't self-hang)
    "    os.read(r, 1)\n"  # block until the gc has setsid'd out of the fg pgrp
    "    with open(pidfile, 'w') as f:\n"  # side channel (NOT the slave)
    "        f.write(str(pid))\n"  # explicit flush — os._exit() skips stdio flush
    "    os._exit(7)\n"
)

# Same shape, but the grandchild streams continuously and the child stays alive
# ~0.2 s while it streams — so Phase 1 sees select() always-readable and MUST
# re-check poll() every iteration to exit. The grandchild self-cleans on EIO
# when the master closes.
#
# Pipe handshake (do NOT remove — same rationale as _QUIET_GRANDCHILD): orders
# child-exit after grandchild-setsid so the grandchild can't die on SIGHUP and
# cause a false EOF-path pass. The child reads the handshake byte FIRST, THEN
# sleeps 0.2 s — ordering preserved AND window preserved. The sleep(0.2) is
# load-bearing: it is the only reason select() stays continuously readable while
# the child is alive, which is what forces the per-iteration poll() check. A
# naive "read byte -> exit immediately" handshake would delete the window — the
# child would exit right after setsid, Phase 1 would see a dead child on its
# first check, and the test would pass (rc 7, elapsed < 2.0) WITHOUT witnessing
# the per-iteration poll. Do NOT collapse the read and the sleep, and do NOT
# remove either.
_STREAMING_GRANDCHILD = (
    "import os, signal, sys, time\n"
    "pidfile = sys.argv[1]\n"
    "r, w = os.pipe()\n"  # BEFORE fork: both ends inherited by child + grandchild
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    os.close(r)\n"
    "    signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "    os.setsid()\n"
    "    os.write(w, b'x')\n"  # handshake: detached
    "    os.close(w)\n"
    "    while True:\n"
    "        try:\n"
    "            os.write(1, b'g')\n"  # stream to the slave forever
    "        except OSError:\n"
    "            os._exit(0)\n"  # master closed → EIO → self-clean
    "        time.sleep(0.001)\n"
    "else:\n"
    "    os.close(w)\n"
    "    os.read(r, 1)\n"  # handshake FIRST: proves the gc detached...
    "    time.sleep(0.2)\n"  # ...THEN the alive-window — load-bearing, do NOT remove
    "    with open(pidfile, 'w') as f:\n"
    "        f.write(str(pid))\n"  # explicit flush — os._exit() skips stdio flush
    "    os._exit(7)\n"
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
            # elapsed < bound is the ONLY thing that catches a regressed per-
            # iteration poll(): a regression leaves select() always-readable and
            # stalls to the hard cap, blowing this bound. The grandchild handshake
            # keeps the master from EOF so this bound stays meaningful (no EOF-path
            # shortcut). The test does not otherwise witness which internal path
            # ran — this assertion is load-bearing, not decorative.
            assert elapsed < 2.0, f"stalled toward hard cap (elapsed={elapsed:.2f}s)"
            gc_pid = int(pidfile.read_text())
    finally:
        # The streaming grandchild self-cleans on EIO, but kill by PID uniformly
        # in case it's mid-sleep when the master closes.
        if gc_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(gc_pid, signal.SIGKILL)


_ALT_FIXTURE = (
    "import sys, time\n"
    "sys.stdout.write('before-alt\\n'); sys.stdout.flush()\n"
    "sys.stdout.write('\\x1b[?1049h\\x1b[2J\\x1b[HINSIDE-ALT'); sys.stdout.flush()\n"
    "time.sleep(0.3)\n"                       # quiet window: settles while in alt
    "sys.stdout.write('\\x1b[?1049l'); sys.stdout.flush()\n"
)


def test_alt_screen_buffer_swap_end_to_end() -> None:
    # Prove the buffer swap over a real pty: in alt only the alt frame shows and
    # regime is SCREEN; after exit the pre-alt screen is restored and regime is
    # SCROLL.
    with TerminalSession([sys.executable, "-c", _ALT_FIXTURE]) as s:
        s.wait_idle(idle=0.2, hard=5.0)          # settles mid-alt (0.3s sleep)
        assert s.regime() is Regime.SCREEN
        body = "\n".join(s.lines())
        assert "INSIDE-ALT" in body
        assert "before-alt" not in body          # no scrollback while in alt
        s.run_to_completion(hard=5.0)            # drives past the 1049l exit
        assert s.regime() is Regime.SCROLL
        assert "before-alt" in "\n".join(s.lines())   # pre-alt restored


_INLINE_FIXTURE = (
    "import sys, time\n"
    "for i in range(8):\n"
    "    sys.stdout.write(f'scroll line {i}\\n')\n"   # real scrollback (8 > rows)
    "sys.stdout.write('PROMPT> ')\n"                  # live bottom region
    "sys.stdout.flush()\n"
    "time.sleep(0.5)\n"
)


def test_inline_redraw_scroll_shape_history_plus_live_bottom() -> None:
    with TerminalSession([sys.executable, "-c", _INLINE_FIXTURE], cols=40, rows=6) as s:
        s.wait_idle(idle=0.2, hard=5.0)
        assert s.regime() is Regime.SCROLL
        full = s.lines()
        win = s.lines(limit=s.rows)
        assert "scroll line 0" in full           # scrolled into history
        assert "scroll line 0" not in win        # not in the live window
        assert len(win) == s.rows
        assert win[-1].rstrip().endswith("PROMPT>")
        x, _ = s.cursor()
        assert x == len("PROMPT> ")              # cursor parked past the prompt


def test_resize_pads_and_clips_live_screen() -> None:
    with TerminalSession([sys.executable, "-c", "import time; time.sleep(2)"],
                         cols=40, rows=10) as s:
        s.wait_idle(idle=0.1, hard=2.0)
        s.resize(rows=5, cols=20)
        assert s.rows == 5 and s.cols == 20
        assert len(s.lines(limit=s.rows)) == 5
        cx, cy = s.cursor()
        assert 0 <= cx <= 19 and 0 <= cy <= 4    # cursor in bounds after shrink
        s.cell(19, 4)                            # bottom-right corner addressable
