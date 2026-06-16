from __future__ import annotations

import contextlib
import os
import signal
import sys
import time

import pytest

from impty.driver import ImptyDriver, _decode_keys
from tests.impty.e2e_support import can_pty

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

_ANIMATING_CHILD = (
    "import os, sys, select, time, tty\n"
    "tty.setraw(sys.stdin.fileno())\n"
    "os.write(1, b'READY\\r\\n')\n"
    "time.sleep(0.4)\n"
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
    assert _decode_keys("foo\\r") == "foo\r"


def test_decodes_hex_and_escape_mnemonics() -> None:
    assert _decode_keys("\\x03") == "\x03"
    assert _decode_keys("\\t") == "\t"
    assert _decode_keys("\\x1b[B") == "\x1b[B"


def test_leaves_plain_text_and_unicode_untouched() -> None:
    assert _decode_keys("café résumé") == "café résumé"


def test_leaves_unrecognized_backslash_literal() -> None:
    assert _decode_keys("a\\z b") == "a\\z b"


def test_tool_methods_expose_the_seven_tools_in_order() -> None:
    names = [m.__name__ for m in ImptyDriver().tool_methods()]
    assert names == [
        "start", "view", "send", "send_signal", "wait", "snapshot", "stop"]


def test_require_raises_before_start() -> None:
    with pytest.raises(RuntimeError, match="no session"):
        ImptyDriver()._require()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_close_kills_the_running_session() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; time.sleep(30)"])
    pid = d._require().pid
    d.close()
    # close() ends the session and kills the pty child group.
    assert d._session is None
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    d.close()  # idempotent no-op


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_start_replaces_existing_session() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; time.sleep(30)"])
    first_pid = d._require().pid
    d.start([sys.executable, "-c", "import time; time.sleep(30)"])
    assert d._require().pid != first_pid
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(first_pid, 0)
    d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_send_signal_delivers_sigint_regardless_of_tty_mode() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _SIGINT_CHILD])
    try:
        d.wait(idle=0.2, hard=5.0)
        screen = d.send_signal(signal.SIGINT)
        assert "GOT-SIGINT" in screen
        assert "alive=False" in screen.splitlines()[0]
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(d._require().pid, signal.SIGKILL)
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_start_passes_env_to_child() -> None:
    prog = (
        "import os, time\n"
        "print('VAL=' + os.environ.get('IMPTY_TEST', 'MISSING'))\n"
        "time.sleep(1)")
    d = ImptyDriver()
    d.start([sys.executable, "-c", prog], env={"IMPTY_TEST": "hello"})
    try:
        assert "VAL=hello" in d.view(full=True)
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_view_current_screen_by_default_and_full_on_demand() -> None:
    prog = "import time\n[print('line', i) for i in range(40)]\ntime.sleep(1)"
    d = ImptyDriver()
    d.start([sys.executable, "-c", prog])
    try:
        v = d.view()
        assert "regime=scroll" in v.splitlines()[0]
        assert "line 39" in v
        assert "line 0" not in v
        assert "line 0" in d.view(full=True)
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_send_numeric_wait_settles_fixed_time_on_animating_app() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _ANIMATING_CHILD])
    try:
        t = time.monotonic()
        screen = d.send("x", wait=0.5)
        dt = time.monotonic() - t
        assert "GOT-X" in screen
        assert dt < 3.0
        assert d.send("q", wait=False) == "[sent]"
    finally:
        d.stop()


def test_stop_with_no_session_is_a_noop_message() -> None:
    assert ImptyDriver().stop() == "[no session]"
