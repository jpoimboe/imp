from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time

import pytest

import impty.mcp as mcp_mod
from impty.mcp import _decode_keys
from tests.impty.e2e_support import can_pty

# A child that turns SIGINT into an async-signal-safe marker and exits via the
# handler (mirrors tests/impty/test_session.py). Runs in RAW mode (tty.setraw =>
# ISIG off) so a bare \x03 byte would NOT become a signal — only send_signal's
# direct killpg can fire the handler here. That is exactly send_signal's reason
# to exist, so the "regardless of tty mode" test must drive the raw path, not
# cooked mode where send("\x03") would also work. Raw mode disables ONLCR, so
# the markers carry explicit \r\n. Sleeps past the test deadline so the only
# exit path is the handler firing.
_SIGINT_CHILD = (
    "import os, signal, sys, time, tty\n"
    "def _handler(signum, frame):\n"
    "    os.write(1, b'GOT-SIGINT\\r\\n')\n"
    "    os._exit(42)\n"
    "signal.signal(signal.SIGINT, _handler)\n"
    "tty.setraw(sys.stdin.fileno())\n"
    "os.write(1, b'READY\\r\\n')\n"
    "time.sleep(30)\n"
)


# A full-screen-ish child that NEVER goes quiet: after a brief READY pause (so
# start()'s wait_idle settles), it repaints every 20ms forever. Reacts to 'x'
# (-> GOT-X marker) and 'q' (exit). Drives the "animating app" settle behaviour.
_ANIMATING_CHILD = (
    "import os, sys, select, time, tty\n"
    "tty.setraw(sys.stdin.fileno())\n"
    "os.write(1, b'READY\\r\\n')\n"
    "time.sleep(0.4)\n"                       # let start() settle on READY
    "hit = b''\n"
    "end = time.time() + 30\n"
    "while time.time() < end:\n"
    "    r, _, _ = select.select([sys.stdin], [], [], 0)\n"
    "    if r:\n"
    "        c = os.read(sys.stdin.fileno(), 1)\n"
    "        if c == b'x': hit = b'GOT-X'\n"
    "        elif c == b'q': break\n"
    "    os.write(1, b'\\rtick ' + hit + b'   ')\n"
    "    time.sleep(0.02)\n"
)


def test_decodes_carriage_return_so_enter_submits() -> None:
    # The send() contract documents Enter=\r; the agent→MCP JSON boundary
    # delivers a written "\r" as the two literal chars backslash + 'r', so the
    # tool must decode it into a real carriage return.
    assert _decode_keys("foo\\r") == "foo\r"


def test_decodes_hex_and_escape_mnemonics() -> None:
    assert _decode_keys("\\x03") == "\x03"  # Ctrl-C
    assert _decode_keys("\\t") == "\t"  # Tab
    assert _decode_keys("\\x1b[B") == "\x1b[B"  # Down arrow: ESC then literal [B


def test_leaves_plain_text_and_unicode_untouched() -> None:
    # A keystroke tool mustn't corrupt ordinary typed text, including non-ASCII.
    assert _decode_keys("café résumé") == "café résumé"


def test_leaves_unrecognized_backslash_literal() -> None:
    # A lone backslash that isn't a known escape stays as-is (no crash).
    assert _decode_keys("a\\z b") == "a\\z b"


def test_transcript_tool_is_removed_view_remains() -> None:
    names = {t.name for t in asyncio.run(mcp_mod.mcp.list_tools())}
    assert "transcript" not in names
    assert {"start", "view", "send", "wait", "snapshot", "stop", "send_signal"} <= names


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_send_signal_tool_delivers_sigint_regardless_of_tty_mode() -> None:
    # The mode-independent escape hatch: send_signal() reaches the child's process
    # group directly, so a driver doesn't have to scrape the pid and `kill -INT`
    # when the tty happens to be in raw mode (ISIG off). It fires the child's
    # SIGINT handler even though no \x03 byte was sent.
    mcp_mod.start([sys.executable, "-c", _SIGINT_CHILD])
    try:
        mcp_mod.wait(idle=0.2, hard=5.0)
        screen = mcp_mod.send_signal(signal.SIGINT)
        assert "GOT-SIGINT" in screen
        assert "alive=False" in screen.splitlines()[0]
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(mcp_mod._require().pid, signal.SIGKILL)
        mcp_mod.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_start_passes_env_to_child() -> None:
    # The env param lets a driver set variables (e.g. API keys) WITHOUT a
    # `bash -lc "export …; exec …"` wrapper — which would otherwise put bash, not
    # the target program, in the foreground process group. Spawn directly with
    # env and confirm the child sees it.
    prog = "import os, time\nprint('VAL=' + os.environ.get('IMPTY_TEST', 'MISSING'))\ntime.sleep(1)"
    mcp_mod.start([sys.executable, "-c", prog], env={"IMPTY_TEST": "hello"})
    try:
        assert "VAL=hello" in mcp_mod.view(full=True)
    finally:
        mcp_mod.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_view_returns_current_screen_by_default_and_full_on_demand() -> None:
    prog = "import time\n[print('line', i) for i in range(40)]\ntime.sleep(1)"
    mcp_mod.start([sys.executable, "-c", prog])
    try:
        v = mcp_mod.view()
        assert "regime=scroll" in v.splitlines()[0]   # status header reports regime
        assert "line 39" in v                          # the live window
        assert "line 0" not in v                       # whole strip NOT dumped by default
        full = mcp_mod.view(full=True)
        assert "line 0" in full                        # full strip on demand
    finally:
        mcp_mod.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_send_numeric_wait_settles_a_fixed_time_on_an_always_animating_app() -> None:
    # An app that repaints every 20ms NEVER goes quiet, so wait=True blocks the
    # whole settle cap on every keystroke (a live TUI / spinner). A numeric wait
    # settles a FIXED duration instead — long enough for the reaction to render —
    # and returns promptly.
    mcp_mod.start([sys.executable, "-c", _ANIMATING_CHILD])
    try:
        t = time.monotonic()
        screen = mcp_mod.send("x", wait=0.5)         # fixed 0.5s settle
        dt = time.monotonic() - t
        assert "GOT-X" in screen                     # the reaction rendered
        assert dt < 3.0                              # NOT a full settle-cap block
        assert mcp_mod.send("q", wait=False) == "[sent]"   # bool path unchanged
    finally:
        mcp_mod.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_default_settle_cap_is_short_not_fifteen_seconds() -> None:
    # The settle policy lives in TerminalSession (DEFAULT_SETTLE_HARD); the MCP
    # tools inherit it, they don't set their own. On a never-quiet app the default
    # wait() returns at the (short) hard ceiling — guards against the cap creeping
    # back up to the old 15s, and against the MCP layer re-introducing its own.
    from impty.session import DEFAULT_SETTLE_HARD

    assert DEFAULT_SETTLE_HARD <= 5.0                # policy stays responsive
    mcp_mod.start([sys.executable, "-c", _ANIMATING_CHILD])
    try:
        t = time.monotonic()
        mcp_mod.wait()                               # no args -> inherit the default cap
        dt = time.monotonic() - t
        assert dt < DEFAULT_SETTLE_HARD + 1.5        # capped near the default, not 15s
        mcp_mod.send("q", wait=False)
    finally:
        mcp_mod.stop()
