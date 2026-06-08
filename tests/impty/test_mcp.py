from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys

import pytest

import impty.mcp as mcp_mod
from impty.mcp import _decode_keys

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


def _can_pty() -> bool:
    try:
        import pty

        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


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


@pytest.mark.skipif(not _can_pty(), reason="pty not available")
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


@pytest.mark.skipif(not _can_pty(), reason="pty not available")
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
