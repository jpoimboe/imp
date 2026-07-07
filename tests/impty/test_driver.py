from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

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

_RESIZE_CHILD = (
    "import os, signal, sys, time, tty\n"
    "def _handler(signum, frame):\n"
    "    cols = os.get_terminal_size(sys.stdin.fileno()).columns\n"
    "    os.write(1, b'WIDTH=' + str(cols).encode() + b'\\r\\n')\n"
    "signal.signal(signal.SIGWINCH, _handler)\n"
    "tty.setraw(sys.stdin.fileno())\n"
    "os.write(1, b'READY\\r\\n')\n"
    "time.sleep(30)\n"
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


def test_tool_methods_expose_the_eight_tools_in_order() -> None:
    names = [m.__name__ for m in ImptyDriver().tool_methods()]
    assert names == [
        "start", "view", "send", "send_signal", "resize",
        "wait", "snapshot", "stop"]


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
        # The child handler writes GOT-SIGINT then os._exit(): under load the pty
        # output settles (so send_signal returns) a beat before the kernel finishes
        # the exit and poll() reaps it, so the one snapshot can still read alive=True.
        # Poll the liveness this test actually asserts on instead of trusting a single
        # post-signal render.
        deadline = time.monotonic() + 5.0
        while "alive=True" in screen.splitlines()[0] and time.monotonic() < deadline:
            time.sleep(0.01)
            screen = d.view()
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


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_returns_still_busy_after_hard_cap() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _ANIMATING_CHILD])
    try:
        d.wait(idle=0.2, hard=0.3)  # wait for READY
        result = d.wait(idle=5.0, hard=0.4)  # hard cap fires: never settles
        assert result.startswith("[still busy after hard cap]")
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_snapshot_writes_png(tmp_path: Path) -> None:
    out = tmp_path / "snap.png"
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        result = d.snapshot(path=str(out))
        assert out.exists() and out.stat().st_size > 0
        assert str(out) in result
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_send_wait_true_returns_screen_not_sent_marker() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _ANIMATING_CHILD])
    try:
        d.wait(idle=0.2, hard=5.0)  # wait for READY
        result = d.send("x", wait=True)
        assert result != "[sent]"
        assert result.startswith("[")  # status header
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_resize_delivers_sigwinch_and_child_sees_new_width() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _RESIZE_CHILD], cols=80, rows=24)
    try:
        d.wait(idle=0.2, hard=5.0)  # wait for READY
        screen = d.resize(30, 100)  # rows=30, cols=100
        # The live session reflects the new geometry.
        assert d._require().cols == 100
        assert d._require().rows == 30
        # The child observed SIGWINCH and rendered its new width; poll like the
        # send_signal e2e test since the reaction can land a beat after settle.
        deadline = time.monotonic() + 5.0
        while "WIDTH=100" not in screen and time.monotonic() < deadline:
            time.sleep(0.02)
            screen = d.view(full=True)
        assert "WIDTH=100" in screen
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_resize_wait_false_returns_marker_without_render() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _RESIZE_CHILD])
    try:
        assert d.resize(30, 100, wait=False) == "[resized]"
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_resize_wait_true_returns_screen_not_resized_marker() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _RESIZE_CHILD])
    try:
        d.wait(idle=0.2, hard=5.0)  # wait for READY
        result = d.resize(30, 100, wait=True)
        assert result != "[resized]"
        assert result.startswith("[")  # status header
    finally:
        d.stop()
