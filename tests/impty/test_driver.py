from __future__ import annotations

import contextlib
import os
import pty
import re
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from impty import TerminalSession
from impty.driver import (
    ImptyDriver,
    PtySpawn,
    _decode_keys,
    _format_exit,
    _resolve_key,
)
from impty.render import _MAX_DIM
from impty.session import (
    DEFAULT_CHANGE_HARD,
    DEFAULT_SETTLE_HARD,
    DEFAULT_SETTLE_IDLE,
    MAX_CALL_HARD,
    STOP_GRACE,
)
from tests.impty.e2e_support import E2E_POLL_TIMEOUT, _pid_gone, _poll, can_pty

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

_POLL_FOR_FILE_CHILD = (
    "import os, sys, time\n"
    "os.write(1, b'READY\\r\\n')\n"
    "p = sys.argv[1]\n"
    "while not os.path.exists(p):\n"
    "    time.sleep(0.05)\n"
    "os.write(1, b'PROGRESS\\r\\n')\n"
    "time.sleep(30)\n"
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

_ECHO_CHILD = (
    "import os, sys, tty\n"
    "tty.setraw(sys.stdin.fileno())\n"
    "os.write(1, b'READY\\r\\n')\n"
    "buf = b''\n"
    "while True:\n"
    "    c = os.read(sys.stdin.fileno(), 1)\n"
    "    if not c: break\n"
    "    buf += c\n"
    "    os.write(1, b'GOT ' + buf.hex().encode() + b'\\r\\n')\n"
)

_MARKER_THEN_TAIL_CHILD = (
    "import os, sys, time\n"
    "os.write(1, b'READY\\r\\n')\n"
    "p = sys.argv[1]\n"
    "while not os.path.exists(p):\n"
    "    time.sleep(0.05)\n"
    "os.write(1, b'MARKER\\r\\n')\n"
    "time.sleep(0.5)\n"
    "os.write(1, b'TAIL\\r\\n')\n"
    "time.sleep(30)\n"
)

_SCROLL_OFF_CHILD = (
    "import time\n"
    "print('SCROLLED-AWAY')\n"
    "for i in range(60):\n"
    "    print('filler', i)\n"
    "print('BOTTOM', flush=True)\n"
    "time.sleep(30)\n"
)


def _received(driver: ImptyDriver) -> str:
    """Hex of everything the echo child has received so far — its last GOT line."""
    got = [ln for ln in driver.view().splitlines() if ln.startswith("GOT ")]
    return got[-1].split(" ", 1)[1].strip() if got else ""


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


# The key table exactly as the spec pins it; the resolver is checked against THIS,
# never the other way round.
_PINNED_KEY_BYTES = {
    "enter": "\r", "return": "\r", "tab": "\t", "shift-tab": "\x1b[Z",
    "escape": "\x1b", "esc": "\x1b", "space": " ", "backspace": "\x7f",
    "delete": "\x1b[3~", "insert": "\x1b[2~",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "home": "\x1b[H", "end": "\x1b[F", "pageup": "\x1b[5~", "pagedown": "\x1b[6~",
    "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS",
    "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~", "f8": "\x1b[19~",
    "f9": "\x1b[20~", "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
    "ctrl-[": "\x1b", "ctrl-\\": "\x1c", "ctrl-]": "\x1d", "ctrl-^": "\x1e",
    "ctrl-_": "\x1f",
}

_UNKNOWN_KEY_MESSAGE = (
    "unknown key name 'contrl-c' — valid: enter, tab, shift-tab, escape, space, "
    "backspace, delete, insert, up, down, left, right, home, end, pageup, pagedown, "
    "f1-f12, ctrl-<a-z>, alt-<key>; for anything else pass the raw bytes in data="
)


@pytest.mark.parametrize(("name", "expected"), sorted(_PINNED_KEY_BYTES.items()))
def test_key_names_resolve_to_the_pinned_bytes(name: str, expected: str) -> None:
    assert _resolve_key(name) == expected


def test_ctrl_letter_range_is_the_ascii_control_codes() -> None:
    # ctrl-a..ctrl-z are \x01..\x1a — the ASCII definition, not a table lookup.
    assert [_resolve_key(f"ctrl-{c}") for c in "abcdefghijklmnopqrstuvwxyz"] == [
        chr(i) for i in range(1, 27)
    ]


def test_alt_prefixes_escape_onto_the_resolved_remainder() -> None:
    assert _resolve_key("alt-enter") == "\x1b\r"
    assert _resolve_key("alt-x") == "\x1bx"
    assert _resolve_key("alt-_") == "\x1b_"
    # The remainder folds through its OWN stages: page_up -> pageup.
    assert _resolve_key("Alt+Page_Up") == "\x1b\x1b[5~"
    # A bare-char remainder is sent AS WRITTEN: name-folding must not rewrite the
    # literal byte, or alt-A would silently deliver ESC a and alt-+ would deliver ESC -.
    assert _resolve_key("alt-A") == "\x1bA"
    assert _resolve_key("alt-+") == "\x1b+"
    assert _resolve_key("Alt_A") == "\x1bA"


def test_name_normalization_folds_spellings_and_keeps_ctrl_underscore() -> None:
    assert _resolve_key("Ctrl+C") == "\x03"
    assert _resolve_key("ctrl-c") == "\x03"
    assert _resolve_key("CTRL_C") == "\x03"
    assert _resolve_key("Page_Up") == "\x1b[5~"
    assert _resolve_key("PageUp") == "\x1b[5~"
    assert _resolve_key("pageup") == "\x1b[5~"
    # The pair no single-stage rule can satisfy at once.
    assert _resolve_key("shift_tab") == "\x1b[Z"
    assert _resolve_key("ctrl-_") == "\x1f"


def test_unknown_key_name_message_quotes_the_name_as_written() -> None:
    with pytest.raises(ValueError) as exc:
        _resolve_key("contrl-c")
    assert str(exc.value) == _UNKNOWN_KEY_MESSAGE


def test_keys_is_a_closed_vocabulary_so_a_bare_char_is_not_a_key() -> None:
    # A lone printable char is only meaningful after alt-; on its own it belongs in data=.
    with pytest.raises(ValueError):
        _resolve_key("x")


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
def test_start_refuses_to_replace_a_live_session() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; print('FIRST'); time.sleep(30)"])
    try:
        # start()'s internal settle caps at 3s and returns whatever pyte holds, so a
        # saturated runner can push interpreter startup past it. Settle on a generous
        # explicit hard before reading screen text — same reason as the comment at
        # test_wait_clamped_for_change_reports_no_change_with_the_resume_clause.
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        assert "FIRST" in d.view()
        first_pid = d._require().pid
        with pytest.raises(RuntimeError) as excinfo:
            d.start([sys.executable, "-c", "import time; time.sleep(30)"])
        assert str(excinfo.value) == "session already running — call stop() first"
        # The refused call killed nothing: same session, same live process, same screen.
        assert d._require().pid == first_pid
        assert d._require().is_alive()
        assert "FIRST" in d.view()
        # stop() is the documented recovery path, so the next start() must succeed.
        d.stop()
        screen = d.start(
            [sys.executable, "-c", "import time; print('SECOND'); time.sleep(30)"])
        assert screen.splitlines()[0].startswith("[alive=True")
        assert d._require().pid != first_pid
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        assert "SECOND" in d.view()
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_start_replaces_a_session_whose_program_exited() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "print('FIRST')"])
    first_pid = d._require().pid
    try:
        # Poll for the exit rather than sleeping a fixed interval: under a saturated
        # parallel run the reap can lag the settled screen by a while.
        deadline = time.monotonic() + 20.0
        while d._require().is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not d._require().is_alive()
        screen = d.start(
            [sys.executable, "-c", "import time; print('SECOND'); time.sleep(30)"])
        assert screen.splitlines()[0].startswith("[alive=True")
        # start() cleared the baseline, so this render is a full screen. This
        # integration assertion is load-safe; the pty-free two-session test is
        # the deterministic mutation guard for the reset itself.
        assert screen.splitlines()[1] == "[full screen]"
        assert d._require().pid != first_pid
        # Same 3s-cap hazard: settle explicitly, then read the marker off view().
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        assert "SECOND" in d.view()
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_send_signal_delivers_sigint_regardless_of_tty_mode() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _SIGINT_CHILD])
    try:
        d.wait(idle=0.2, hard=30.0)
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
        assert result.startswith("[still busy after 0.4s hard cap]")
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
        d.wait(idle=0.2, hard=30.0)  # wait for READY
        result = d.send("x", wait=True)
        assert result != "[sent]"
        assert result.startswith("[")  # status header
    finally:
        d.stop()


def test_send_with_neither_data_nor_keys_is_an_error() -> None:
    with pytest.raises(ValueError) as exc:
        ImptyDriver().send()
    assert str(exc.value) == "send needs data= text, keys=[...], or both"


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_send_delivers_data_then_the_named_keys_in_order() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _ECHO_CHILD])
    try:
        d.wait(idle=0.2, hard=30.0)  # READY
        d.send(data="foo", keys=["enter"])
        assert _poll(lambda: _received(d) == b"foo\r".hex(), E2E_POLL_TIMEOUT)
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_send_writes_data_and_keys_in_a_single_pty_write() -> None:
    # One write, or pyte's CPR/DA reply writer could land between the typed text and
    # the key that commits it.
    d = ImptyDriver()
    d.start([sys.executable, "-c", _ECHO_CHILD])
    try:
        d.wait(idle=0.2, hard=30.0)  # READY
        session = d._require()
        writes: list[str] = []
        real = session.send

        def spy(payload: bytes | str) -> None:
            writes.append(str(payload))
            real(payload)

        session.send = spy
        d.send(data="hi", keys=["space", "enter"])
        assert writes == ["hi \r"]
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_unknown_key_name_delivers_nothing_to_the_program() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _ECHO_CHILD])
    try:
        d.wait(idle=0.2, hard=30.0)  # READY
        with pytest.raises(ValueError) as exc:
            d.send(data="foo", keys=["contrl-c"])
        assert str(exc.value) == _UNKNOWN_KEY_MESSAGE
        # A probe the child DOES get proves the failed call wrote nothing: its
        # cumulative buffer is the probe alone, not "foo" followed by the probe.
        d.send(keys=["tab"])
        assert _poll(lambda: _received(d) == b"\t".hex(), E2E_POLL_TIMEOUT)
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_bad_key_later_in_the_list_sends_none_of_the_earlier_ones() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _ECHO_CHILD])
    try:
        d.wait(idle=0.2, hard=30.0)  # READY
        with pytest.raises(ValueError):
            d.send(keys=["space", "nope"])
        d.send(keys=["tab"])
        assert _poll(lambda: _received(d) == b"\t".hex(), E2E_POLL_TIMEOUT)
    finally:
        d.stop()


def test_send_docstring_teaches_the_key_vocabulary() -> None:
    doc = ImptyDriver.send.__doc__ or ""
    assert 'keys=["enter"]' in doc
    assert "ctrl-<a-z>" in doc
    assert "single write" in doc


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_resize_delivers_sigwinch_and_child_sees_new_width() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _RESIZE_CHILD], cols=80, rows=24)
    try:
        d.wait(idle=0.2, hard=30.0)  # wait for READY
        d.resize(30, 100)  # rows=30, cols=100
        # The live session reflects the new geometry.
        assert d._require().cols == 100
        assert d._require().rows == 30
        # Poll only through the full-screen path: this test is about SIGWINCH
        # delivery, not about the drive calls' diff format.
        assert _poll(
            lambda: "WIDTH=100" in d.view(full=True),
            timeout=E2E_POLL_TIMEOUT,
        ), "child never reported resized width"
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
        d.wait(idle=0.2, hard=30.0)  # wait for READY
        result = d.resize(30, 100, wait=True)
        assert result != "[resized]"
        assert result.startswith("[")  # status header
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_resize_numeric_wait_settles_fixed_time() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _RESIZE_CHILD], cols=80, rows=24)
    try:
        d.wait(idle=0.2, hard=30.0)  # wait for READY
        t = time.monotonic()
        rendered = d.resize(30, 100, wait=0.5)  # rows=30, cols=100
        dt = time.monotonic() - t
        # Before the asynchronous handler paints WIDTH=100, the six-row insertion
        # is shorter; afterward the length rule falls back to the full screen.
        body = rendered.partition("\n")[2]
        assert body.splitlines()[0] in {"[rows 24-29 inserted]", "[full screen]"}
        # Poll child output separately from the fixed-settle return.
        assert _poll(
            lambda: "WIDTH=100" in d.view(full=True),
            timeout=E2E_POLL_TIMEOUT,
        ), "child never reported resized width"
        assert dt < 3.0
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_for_change_blocks_until_new_output_arrives(tmp_path: Path) -> None:
    flag = tmp_path / "go"
    d = ImptyDriver()
    d.start([sys.executable, "-c", _POLL_FOR_FILE_CHILD, str(flag)])
    try:
        # start()'s settle caps at 3s, so a saturated runner can leave READY out
        # of the baseline. Settle on a generous explicit hard and read READY off
        # view(), which normalizes the baseline to the whole screen — only then
        # does a settled wait have a stable "nothing new" reading.
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        assert "READY" in d.view()
        quiet = d.wait(idle=0.2, hard=5.0)
        assert quiet.startswith("[settled]")
        # The agent has already been shown READY, so a settled wait reports no rows.
        assert quiet.splitlines()[2:] == ["[no text change]"]
        assert "PROGRESS" not in quiet
        # Arm the touch and only THEN call the wait, so the new output cannot
        # possibly predate the `since` stamp the call takes.
        timer = threading.Timer(1.0, flag.touch)
        timer.start()
        try:
            t0 = time.monotonic()
            # Generous, but under the global 60 s pytest timeout so a genuine
            # failure reports as an assertion rather than an outer-timeout hang.
            result = d.wait(idle=0.2, hard=30.0, for_change=True)
            elapsed = time.monotonic() - t0
        finally:
            timer.cancel()
        assert result.startswith("[settled]")
        assert "PROGRESS" in result
        # A plain wait returns on the already-quiet screen in ~idle; this one had
        # to block for output that did not exist when it was called.
        assert elapsed >= 0.5
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_for_change_reports_no_change_after_hard_cap() -> None:
    prog = "import time; print('READY'); time.sleep(30)"
    d = ImptyDriver()
    d.start([sys.executable, "-c", prog])
    try:
        assert d.wait(idle=0.2, hard=5.0).splitlines()[0] == "[settled]"
        result = d.wait(idle=0.2, hard=0.5, for_change=True)
        assert result.startswith("[no change after 0.5s hard cap]")
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_for_change_reports_still_busy_when_output_never_quiets() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _ANIMATING_CHILD])
    try:
        # The child sleeps 0.4s after READY before it starts ticking, and change
        # mode gets no cushion from that stale stamp — so poll until the tick
        # stream is actually running (the drainer keeps pyte current).
        deadline = time.monotonic() + 10.0
        screen = d.view()
        while "tick" not in screen and time.monotonic() < deadline:
            time.sleep(0.05)
            screen = d.view()
        assert "tick" in screen
        # idle=5.0 can never be satisfied by a 0.02s tick loop, so the cap fires
        # however generous `hard` is: output arrived, it just never quieted. Size
        # `hard` for a saturated runner — the child must land at least one tick
        # inside the window or the header would (correctly) read "no change".
        result = d.wait(idle=5.0, hard=6.0, for_change=True)
        assert result.startswith("[still busy after 6s hard cap]")
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_normal_mode_on_silent_child_still_reports_still_busy() -> None:
    # last_output_at is None (no byte ever read) and the child has not exited:
    # normal mode must keep its two headers and never say "no change".
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        result = d.wait(idle=0.2, hard=0.5)
        assert result.startswith("[still busy after 0.5s hard cap]")
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_default_hard_resolves_per_mode() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; print('READY'); time.sleep(30)"])
    try:
        recorded: list[tuple[float, float]] = []

        def _record(**kwargs: float) -> bool:
            recorded.append((kwargs["hard"], kwargs["since"]))
            return True

        d._require().wait_idle = _record  # type: ignore[method-assign]
        d.wait()
        d.wait(for_change=True)
        assert [hard for hard, _ in recorded] == [3.0, 30.0]
        assert recorded[0][1] == 0.0
        assert recorded[1][1] > 0.0
    finally:
        d.stop()


def test_call_budget_constants_are_the_documented_values() -> None:
    # Pinned as constants — never by blocking for them.
    assert MAX_CALL_HARD == 240.0
    assert DEFAULT_SETTLE_HARD == 3.0
    assert DEFAULT_CHANGE_HARD == 30.0
    assert DEFAULT_SETTLE_IDLE == 0.3


def test_wait_docstring_states_the_per_call_cap() -> None:
    doc = ImptyDriver.wait.__doc__ or ""
    assert f"{MAX_CALL_HARD:g}s" in doc
    assert "call wait() again" in doc


def test_send_and_resize_docstrings_state_the_per_call_cap() -> None:
    cap = f"{MAX_CALL_HARD:g}s"
    assert cap in (ImptyDriver.send.__doc__ or "")
    assert cap in (ImptyDriver.resize.__doc__ or "")


def test_readme_clamp_sentence_states_the_per_call_cap() -> None:
    import impty
    readme = (Path(impty.__file__).parent / "README.md").read_text()
    line = next((ln for ln in readme.splitlines() if "MAX_CALL_HARD" in ln), None)
    assert line is not None, "README has no line mentioning MAX_CALL_HARD"
    assert f"{MAX_CALL_HARD:g}s" in line


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_clamps_hard_to_the_call_cap_on_a_busy_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("impty.driver.MAX_CALL_HARD", 0.5)
    d = ImptyDriver()
    d.start([sys.executable, "-c", _ANIMATING_CHILD])
    try:
        t0 = time.monotonic()
        result = d.wait(idle=5.0, hard=30.0)
        elapsed = time.monotonic() - t0
        assert result.startswith(
            "[still busy after 0.5s call cap (30s requested)"
            " — call wait() again to keep watching]")
        # Generous outer bound: the clamp returns at ~0.5s, the unclamped call at 30s.
        assert elapsed < 10.0
        # exactly at the cap uses strict >, so == cap → plain hard-cap form, no resume clause
        at_cap = d.wait(idle=5.0, hard=0.5)
        assert at_cap.startswith("[still busy after 0.5s hard cap]")
        assert "call wait() again" not in at_cap
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_clamped_for_change_reports_no_change_with_the_resume_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; print('READY'); time.sleep(30)"])
    try:
        # start()'s internal settle can return un-settled if a saturated runner
        # pushes interpreter startup past its 3s cap; READY would then land INSIDE
        # the clamped window below and flip the header to "still busy". Settle on a
        # generous explicit hard first, and only then arm the clamp.
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        assert "READY" in d.view()
        monkeypatch.setattr("impty.driver.MAX_CALL_HARD", 0.5)
        t0 = time.monotonic()
        result = d.wait(idle=0.2, hard=30.0, for_change=True)
        elapsed = time.monotonic() - t0
        assert result.startswith(
            "[no change after 0.5s call cap (30s requested)"
            " — call wait() again to keep watching]")
        assert elapsed < 10.0
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_for_text_keeps_watching_a_program_that_already_went_quiet(
    tmp_path: Path,
) -> None:
    # The criterion a predicate checked around an UNSUPPRESSED quiet settle-return
    # fails: the child is quiet when the call starts, so that build returns a
    # not-seen header within milliseconds.
    flag = tmp_path / "go"
    d = ImptyDriver()
    d.start([sys.executable, "-c", _POLL_FOR_FILE_CHILD, str(flag)])
    try:
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        assert "PROGRESS" not in d.view()
        # Arm the touch and only THEN call the wait, so the marker cannot possibly
        # have landed before the call — however loaded the runner is.
        timer = threading.Timer(1.0, flag.touch)
        timer.start()
        try:
            t0 = time.monotonic()
            result = d.wait(for_text="PROGRESS", idle=0.2, hard=30.0)
            elapsed = time.monotonic() - t0
        finally:
            timer.cancel()
        assert result.splitlines()[0] == "[found]"
        assert "PROGRESS" in result
        assert elapsed >= 0.5   # it blocked past the earlier output's settle...
        assert elapsed < 20.0   # ...and returned on the marker, not at the 30s cap
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_for_text_already_on_screen_returns_at_once() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; print('READY'); time.sleep(30)"])
    try:
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        t0 = time.monotonic()
        result = d.wait(for_text="READY", idle=0.2, hard=30.0)
        elapsed = time.monotonic() - t0
        assert result.splitlines()[0] == "[found]"
        # The up-front scan, not a block toward the 30s cap.
        assert elapsed < 10.0
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_for_text_names_the_program_exiting_rather_than_a_phantom_cap() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "print('BYE')"])
    try:
        t0 = time.monotonic()
        result = d.wait(for_text="NEVER", idle=0.2, hard=30.0)
        elapsed = time.monotonic() - t0
        # Exact at EOF; alive=False is NOT asserted here — it comes from the
        # lagging reap, so it belongs to a poll (see _dead_header), not this frame.
        assert result.splitlines()[0] == "[not seen — program exited]"
        assert elapsed < 20.0  # EOF ended the wait; the 30s cap never elapsed
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_for_text_never_seen_reports_the_hard_cap_and_blocks_for_it() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; print('READY'); time.sleep(30)"])
    try:
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        t0 = time.monotonic()
        result = d.wait(for_text="NEVER", idle=0.2, hard=0.5)
        elapsed = time.monotonic() - t0
        assert result.splitlines()[0] == "[not seen after 0.5s hard cap]"
        assert elapsed >= 0.5  # the header's elapsed-time claim is true
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_for_text_clamped_reports_the_call_cap_with_the_resume_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; print('READY'); time.sleep(30)"])
    try:
        # Settle on a generous explicit hard BEFORE arming the clamp: a saturated
        # runner can push interpreter startup past start()'s own 3s cap.
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        monkeypatch.setattr("impty.driver.MAX_CALL_HARD", 0.5)
        t0 = time.monotonic()
        result = d.wait(for_text="NEVER", idle=0.2, hard=30.0)
        elapsed = time.monotonic() - t0
        assert result.splitlines()[0] == (
            "[not seen after 0.5s call cap (30s requested)"
            " — call wait() again to keep watching]")
        assert elapsed < 10.0
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_pattern_anchors_per_screen_line_and_never_crosses_one() -> None:
    prog = "import time; print('ALPHA'); print('BETA'); time.sleep(30)"
    d = ImptyDriver()
    d.start([sys.executable, "-c", prog])
    try:
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        assert d.wait(pattern=r"AL.HA", idle=0.2, hard=5.0).splitlines()[0] == "[found]"
        # MULTILINE: ^ anchors to a SCREEN LINE; the body starts with ALPHA, so
        # without it this pattern could never match.
        assert d.wait(pattern=r"^BETA", idle=0.2, hard=5.0).splitlines()[0] == "[found]"
        # DOTALL is off, so `.` never bridges the two rows (rows are rstripped and
        # joined with "\n", so with DOTALL this WOULD match).
        assert d.wait(pattern=r"ALPHA.BETA", idle=0.2, hard=0.5).splitlines()[0] == (
            "[not seen after 0.5s hard cap]")
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_matcher_defaults_to_the_change_hard_and_scans_from_zero() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; print('READY'); time.sleep(30)"])
    try:
        recorded: list[dict[str, Any]] = []

        def _record(**kwargs: Any) -> bool:
            recorded.append(kwargs)
            return True

        d._require().wait_idle = _record  # type: ignore[method-assign]
        d.wait(for_text="anything")
        # A matcher waits for something that has not happened yet, so it inherits
        # the change budget, not the 3s settle one...
        assert recorded[0]["hard"] == DEFAULT_CHANGE_HARD
        # ...and needs no `since` boundary: the predicate is its own stale-screen gate.
        assert recorded[0]["since"] == 0.0
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_for_text_scans_the_screen_window_not_the_scrollback() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _SCROLL_OFF_CHILD])
    try:
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        assert "SCROLLED-AWAY" in d.view(full=True)  # still in history...
        assert "SCROLLED-AWAY" not in d.view()       # ...but off the screen window
        result = d.wait(for_text="SCROLLED-AWAY", idle=0.2, hard=0.5)
        # A strip-scanning implementation reports [found] here.
        assert result.splitlines()[0] == "[not seen after 0.5s hard cap]"
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_settles_after_the_match_so_the_finished_reaction_is_on_screen(
    tmp_path: Path,
) -> None:
    flag = tmp_path / "go"
    d = ImptyDriver()
    d.start([sys.executable, "-c", _MARKER_THEN_TAIL_CHILD, str(flag)])
    try:
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")
        # Armed so MARKER lands while the wait is already running.
        timer = threading.Timer(0.5, flag.touch)
        timer.start()
        try:
            # idle=3s sits far above the child's own 0.5s MARKER->TAIL gap, so the
            # tail cannot fall outside the settle window even on a saturated runner
            # — the 0.3s default could.
            result = d.wait(for_text="MARKER", idle=3.0, hard=30.0)
        finally:
            timer.cancel()
        assert result.splitlines()[0] == "[found]"
        # A call that returned the instant it matched shows a half-drawn reaction.
        assert "TAIL" in result
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_matcher_on_a_never_quiet_program_still_reports_found() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _ANIMATING_CHILD])
    try:
        # The child sleeps 0.4s after READY before it starts ticking; poll for the
        # real signal rather than sleeping past it.
        assert _poll(lambda: "tick" in d.view(), E2E_POLL_TIMEOUT)
        # idle=5s can never be satisfied by a 0.02s tick loop, so the cap fires —
        # but a match that landed is a success, not a timeout.
        result = d.wait(for_text="tick", idle=5.0, hard=6.0)
        assert result.splitlines()[0] == "[found]"
    finally:
        d.stop()


def test_wait_matcher_argument_conflicts_raise_value_error() -> None:
    # No session: these are argument errors, raised before anything blocks.
    d = ImptyDriver()
    with pytest.raises(ValueError) as exc:
        d.wait(for_text="a", pattern="b")
    assert str(exc.value) == "for_text and pattern are mutually exclusive — pass one"
    with pytest.raises(ValueError) as exc:
        d.wait(for_change=True, for_text="a")
    assert str(exc.value) == "for_change cannot be combined with for_text/pattern"
    # The re.error text is quoted verbatim, so derive it rather than hardcoding a
    # message CPython may reword between versions.
    with pytest.raises(re.error) as re_exc:
        re.compile("(unclosed")
    with pytest.raises(ValueError) as exc:
        d.wait(pattern="(unclosed")
    assert str(exc.value) == f"invalid pattern: {re_exc.value}"


def test_wait_docstring_teaches_the_text_matchers() -> None:
    doc = ImptyDriver.wait.__doc__ or ""
    assert "for_text=" in doc
    assert "pattern=" in doc
    assert "[found]" in doc
    assert "[not seen — program exited]" in doc
    # A matcher wait can be clamped too, so the call-cap guidance must not sit
    # INSIDE the "Without a matcher" listing: an agent told the clamp applies only
    # to the other headers would not know to call again after a clamped not-seen.
    clamp_guidance = doc.split("Without a matcher")[1]
    assert "call cap" in clamp_guidance
    assert "not seen" in clamp_guidance


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_large_idle_cannot_escape_the_call_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole point of the deadline fix: `idle` is NOT clamped, so the ceiling
    # only holds because `hard` bounds every select slice.
    monkeypatch.setattr("impty.driver.MAX_CALL_HARD", 0.5)
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; print('READY'); time.sleep(30)"])
    try:
        t0 = time.monotonic()
        result = d.wait(idle=30.0, hard=1800.0)
        elapsed = time.monotonic() - t0
        # for_change defaults False, so even an already-quiet child reports busy.
        assert result.startswith(
            "[still busy after 0.5s call cap (1800s requested)"
            " — call wait() again to keep watching]")
        # Neither ~30s (the idle window) nor cap + idle.
        assert elapsed < 10.0
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_send_numeric_wait_is_clamped_and_reports_the_clamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _ANIMATING_CHILD])
    try:
        monkeypatch.setattr("impty.driver.MAX_CALL_HARD", 0.2)
        clamped = d.send("x", wait=1.0)
        assert clamped.splitlines()[0] == "[waited 0.2s call cap (1s requested)]"
        # exactly at the cap (strict > boundary): no [waited …] prefix
        at_cap = d.send("x", wait=0.2)
        assert at_cap.splitlines()[0].startswith("[alive=")
        # A request already inside the ceiling emits no such line.
        unclamped = d.send("x", wait=0.1)
        assert unclamped.splitlines()[0].startswith("[alive=")
        assert d.send("q", wait=False) == "[sent]"
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_resize_numeric_wait_is_clamped_and_reports_the_clamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _RESIZE_CHILD], cols=80, rows=24)
    try:
        assert d.wait(idle=0.2, hard=20.0).startswith("[settled]")  # READY consumed
        monkeypatch.setattr("impty.driver.MAX_CALL_HARD", 0.2)
        clamped = d.resize(30, 100, wait=1.0)
        assert clamped.splitlines()[0] == "[waited 0.2s call cap (1s requested)]"
        # exactly at the cap (strict > boundary): no [waited …] prefix
        at_cap = d.resize(30, 100, wait=0.2)
        assert at_cap.splitlines()[0].startswith("[alive=")
        unclamped = d.resize(24, 80, wait=0.1)
        assert unclamped.splitlines()[0].startswith("[alive=")
        assert d.resize(30, 100, wait=False) == "[resized]"
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_snapshot_of_a_large_terminal_stays_within_the_render_ceiling(
    tmp_path: Path,
) -> None:
    # The driver passes no `scale`, so it inherits auto-fit with no call-site change:
    # a 150x44 session is 2772 px wide at a fixed 3x, over the ceiling a vision
    # pipeline downscales at.
    out = tmp_path / "big.png"
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; time.sleep(30)"], cols=150, rows=44)
    try:
        d.snapshot(path=str(out))
    finally:
        d.stop()
    with Image.open(out) as img:
        img.load()
        assert img.width <= _MAX_DIM
        assert img.height <= _MAX_DIM


def _unnamed_signal_number() -> int:
    """A signal number this platform has no `signal.Signals` name for (one past the
    highest it does), so the fallback branch is exercised without hardcoding a number
    that is SIGRTMIN on Linux and out of range on macOS."""
    return max(int(s) for s in signal.Signals) + 1


def test_format_exit_renders_a_normal_status_as_its_number() -> None:
    assert _format_exit(0) == "0"
    assert _format_exit(3) == "3"
    assert _format_exit(130) == "130"  # a program that HANDLED SIGINT and exited 130


def test_format_exit_names_the_signal_for_a_signal_death() -> None:
    assert _format_exit(-signal.SIGINT) == "SIGINT"
    assert _format_exit(-signal.SIGTERM) == "SIGTERM"
    assert _format_exit(-signal.SIGKILL) == "SIGKILL"


def test_format_exit_falls_back_to_sig_n_for_an_unnamed_signal() -> None:
    n = _unnamed_signal_number()
    assert _format_exit(-n) == f"SIG{n}"


_DEFAULT_SIGINT_CHILD = (
    "import os, signal, sys, time, tty\n"
    "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"  # die on SIGINT, no KeyboardInterrupt
    "tty.setraw(sys.stdin.fileno())\n"
    "os.write(1, b'READY\\r\\n')\n"
    "time.sleep(30)\n"
)


def _dead_header(d: ImptyDriver) -> str:
    """The view header once the child is gone. Polls the real signal — the reap can
    lag the settled screen by a lot under a saturated -n auto run — with a budget
    that stays inside the 60s per-test timeout."""
    deadline = time.monotonic() + 30.0
    header = d.view().splitlines()[0]
    while "alive=False" not in header and time.monotonic() < deadline:
        time.sleep(0.02)
        header = d.view().splitlines()[0]
    return header


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_header_reports_a_clean_exit_right_after_alive() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "print('BYE')"])
    try:
        header = _dead_header(d)
        assert re.fullmatch(
            r"\[alive=False exit=0 regime=(scroll|screen) cursor=\(\d+,\d+\)\]", header,
        ), header
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_header_reports_a_nonzero_exit_status() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import sys; sys.exit(3)"])
    try:
        assert "alive=False exit=3 " in _dead_header(d)
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_header_names_the_signal_that_killed_the_child() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _DEFAULT_SIGINT_CHILD])
    try:
        d.wait(idle=0.2, hard=30.0)  # wait for READY
        d.send_signal(signal.SIGINT)
        header = _dead_header(d)
        assert "alive=False exit=SIGINT " in header
        assert "exit=-" not in header  # never the bare negative Popen code
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_header_carries_no_exit_field_while_the_child_is_alive() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        header = d.view().splitlines()[0]
        assert "exit=" not in header
        assert re.fullmatch(
            r"\[alive=True regime=(scroll|screen) cursor=\(\d+,\d+\)\]", header,
        ), header
    finally:
        d.stop()


def test_view_docstring_describes_the_exit_field() -> None:
    doc = ImptyDriver.view.__doc__ or ""
    assert "exit=0" in doc
    assert "exit=SIGINT" in doc


def test_readme_documents_returncode_and_the_header_exit_field() -> None:
    import impty
    readme = (Path(impty.__file__).parent / "README.md").read_text()
    line = next((ln for ln in readme.splitlines() if "`returncode" in ln), None)
    assert line is not None, "README has no line documenting returncode"
    assert "int | None" in line
    assert "exit=SIGINT" in readme


# 16 KB of goodbye, sized from both directions and measured on this checkout:
#   - INSIDE the pty buffer, so the child dumps the whole payload and exits without
#     ever blocking on its own write. (Capacity is not one fixed number — it falls as
#     the writer's chunk size rises: measured here, 19457 B at 1-byte writes, 18265 B
#     at the 65-byte lines this child writes, 12288 B at 4 KB. The ~16.4 KB payload
#     fits at the size that actually applies, and the drainer is reading throughout
#     besides.) Its exit therefore never waits on pyte finishing a feed inside the
#     grace window — the dependency that makes this criterion false-fire under load.
#     Measured at 6x CPU oversubscription: 14/14 clean here, where a 32 KB payload
#     (which does block) fails 4/14 with `[stopped: exit=SIGKILL]`, killed mid-cleanup.
#   - still far more than the drainer can consume in the instant between the child's
#     exit and close() proceeding, so with the post-exit drain rung removed the tail
#     is lost 6/6 at this size. The criterion stays fully falsifiable.
_GOODBYE_BYTES = 16384

_SIGTERM_GOODBYE_CHILD = (
    "import os, signal, sys, time, tty\n"
    "LINE = b'CLEANUP ' + b'x' * 55 + b'\\r\\n'\n"
    f"N = {_GOODBYE_BYTES} // len(LINE)\n"
    "def _handler(signum, frame):\n"
    "    for _ in range(N):\n"
    "        os.write(1, LINE)\n"
    "    os.write(1, b'GOODBYE-TAIL\\r\\n')\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, _handler)\n"
    "tty.setraw(sys.stdin.fileno())\n"
    "os.write(1, b'READY\\r\\n')\n"
    "time.sleep(30)\n"
)

_SIGTERM_IGNORING_CHILD = (
    "import os, signal, sys, time, tty\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "tty.setraw(sys.stdin.fileno())\n"
    "os.write(1, b'READY\\r\\n')\n"
    "time.sleep(30)\n"
)


def _started(d: ImptyDriver) -> None:
    """Poll until the child's READY reaches the screen. start()'s own settle caps at
    3 s, which a saturated -n auto run can push interpreter startup past — and a
    stop() landing before the child installs its SIGTERM handler would kill it with
    the default disposition and read the wrong exit status."""
    assert _poll(lambda: "READY" in d.view(), timeout=E2E_POLL_TIMEOUT), \
        "child never reached READY"


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_stop_reports_a_clean_exit_and_keeps_the_tail_of_the_goodbye() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _SIGTERM_GOODBYE_CHILD])
    _started(d)
    out = d.stop()
    assert out.splitlines()[0] == "[stopped: exit=0]"
    # The LAST line the child wrote: present only if the post-exit drain rung ran.
    assert "GOODBYE-TAIL" in out
    assert "[alive=" not in out          # one header line, not two
    assert d._session is None


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_stop_escalates_to_sigkill_when_the_program_ignores_sigterm() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _SIGTERM_IGNORING_CHILD])
    _started(d)
    pid = d._require().pid
    out = d.stop()
    assert out.splitlines()[0] == "[stopped: exit=SIGKILL]"
    assert _poll(lambda: _pid_gone(pid), timeout=E2E_POLL_TIMEOUT), \
        "program survived stop()"
    assert d._session is None


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_start_failure_closes_the_unsettled_session_without_grace(monkeypatch) -> None:
    seen: list[float] = []
    real_close = TerminalSession.close

    def spy_close(self: TerminalSession, grace: float = STOP_GRACE) -> int:
        seen.append(grace)
        return real_close(self, grace=grace)

    def boom(self: TerminalSession, *args: object, **kwargs: object) -> bool:
        raise RuntimeError("settle blew up")

    monkeypatch.setattr(TerminalSession, "wait_idle", boom)
    monkeypatch.setattr(TerminalSession, "close", spy_close)
    d = ImptyDriver()
    with pytest.raises(RuntimeError, match="settle blew up"):
        d.start([sys.executable, "-c", "import time; time.sleep(30)"])
    # A program that never reached a usable state gets no SIGTERM courtesy.
    assert seen == [0.0]
    assert d._session is None


def test_stop_docstring_describes_the_graceful_ladder() -> None:
    doc = ImptyDriver.stop.__doc__ or ""
    assert "SIGTERM" in doc
    assert "SIGKILL" in doc
    assert "[stopped: exit=" in doc


def test_readme_documents_close_grace_and_its_return() -> None:
    import impty
    readme = (Path(impty.__file__).parent / "README.md").read_text()
    line = next((ln for ln in readme.splitlines() if ln.startswith("- `close(")), None)
    assert line is not None, "README has no line documenting close()"
    assert "grace" in line
    assert "-> int" in line
    assert "SIGTERM" in line and "SIGKILL" in line


def _spy_spawn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record the arguments `ImptyDriver.start` hands `TerminalSession`, then spawn
    for real — so every assertion is about a session that actually ran."""
    seen: dict[str, Any] = {}
    real_init = TerminalSession.__init__

    def spy(self: TerminalSession, argv: Sequence[str], **kwargs: Any) -> None:
        seen["argv"] = list(argv)
        seen.update(kwargs)
        real_init(self, argv, **kwargs)

    monkeypatch.setattr(TerminalSession, "__init__", spy)
    return seen


@pytest.mark.skipif(not can_pty(), reason="pty not available")
@pytest.mark.parametrize("cwd_case", ["omitted", "relative", "absolute"])
def test_start_with_no_hook_preserves_host_cwd_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cwd_case: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    relative = tmp_path / "relative"
    absolute = tmp_path / "absolute"
    relative.mkdir()
    absolute.mkdir()
    requested = {
        "omitted": None,
        "relative": "relative",
        "absolute": str(absolute),
    }[cwd_case]
    expected = str(tmp_path if requested is None else tmp_path / requested)
    seen = _spy_spawn(monkeypatch)
    argv = [
        sys.executable, "-c",
        "import os, sys; print('CWD-OK' if os.getcwd() == sys.argv[1] else 'CWD-BAD')",
        expected,
    ]
    d = ImptyDriver()
    try:
        screen = d.start(argv) if cwd_case == "omitted" else d.start(argv, cwd=requested)
    finally:
        d.stop()
    assert seen["argv"] == argv
    assert seen["cwd"] == requested
    assert seen.get("pass_fds", ()) == ()
    assert "CWD-OK" in screen


@pytest.mark.skipif(not can_pty(), reason="pty not available")
@pytest.mark.parametrize("planned", [False, True])
@pytest.mark.parametrize("cwd_case", ["omitted", "empty", "dot", "relative", "absolute"])
def test_cwd_resolver_runs_before_bare_or_planned_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planned: bool, cwd_case: str,
) -> None:
    relative = tmp_path / "relative"
    absolute = tmp_path / "absolute"
    resolved = tmp_path / "resolved"
    relative.mkdir()
    absolute.mkdir()
    resolved.mkdir()
    requested = {
        "omitted": None,
        "empty": "",
        "dot": ".",
        "relative": "relative",
        "absolute": f"{tmp_path}//absolute",
    }[cwd_case]
    resolver_calls: list[str | None] = []
    hook_cwds: list[str | None] = []
    seen = _spy_spawn(monkeypatch)

    def resolve_cwd(cwd: str | None) -> str | None:
        resolver_calls.append(cwd)
        return str(resolved)

    def hook(
        argv: Sequence[str], cwd: str | None, env: Mapping[str, str] | None,
    ) -> PtySpawn | None:
        hook_cwds.append(cwd)
        if not planned:
            return None
        return PtySpawn(
            argv=[
                sys.executable, "-c",
                "import os, sys; os.chdir(sys.argv[1]); "
                "print('CWD-OK' if os.getcwd() == sys.argv[1] and os.environ.get('PATH') "
                "else 'CWD-BAD')",
                cwd or "",
            ],
            env={"PATH": os.environ["PATH"]},
        )

    argv = [
        sys.executable, "-c",
        "import os, sys; print('CWD-OK' if os.getcwd() == sys.argv[1] else 'CWD-BAD')",
        str(resolved),
    ]
    d = ImptyDriver(sandbox=hook, cwd_resolver=resolve_cwd)
    try:
        screen = d.start(argv, cwd=requested)
    finally:
        d.stop()
    assert resolver_calls == [requested]
    assert hook_cwds == [str(resolved)]
    assert seen["cwd"] is None if planned else seen["cwd"] == str(resolved)
    assert "CWD-OK" in screen


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_hook_returning_none_spawns_bare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # How `/sandbox off` is expressed: no flag, no driver state — the hook just
    # declines, per call.
    seen = _spy_spawn(monkeypatch)
    calls: list[tuple[list[str], str | None, dict[str, str] | None]] = []

    def hook(
        argv: Sequence[str], cwd: str | None, env: Mapping[str, str] | None,
    ) -> PtySpawn | None:
        calls.append((list(argv), cwd, None if env is None else dict(env)))
        return None

    argv = [sys.executable, "-c", "print('BARE-RAN')"]
    d = ImptyDriver(sandbox=hook)
    try:
        screen = d.start(argv, cwd=str(tmp_path))
    finally:
        d.stop()
    assert calls == [(argv, str(tmp_path), None)]
    assert seen["argv"] == argv
    assert seen["cwd"] == str(tmp_path)
    assert "BARE-RAN" in screen


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_hook_plan_replaces_the_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_spawn(monkeypatch)
    plan_prog = (
        "import os\n"
        "print('PLANNED-RAN',\n"
        "      'PLANNED=' + os.environ.get('PLANNED', '<none>'),\n"
        "      'HOME=' + os.environ.get('HOME', '<none>'))\n"
    )
    plan_argv = [sys.executable, "-c", plan_prog]
    # No HOME: the plan's env must reach the child AS GIVEN. A session that unioned
    # os.environ under it would leak the host environment into a curated child env.
    plan_env = {"PLANNED": "yes", "PATH": os.environ["PATH"]}
    r, w = os.pipe()

    def hook(
        argv: Sequence[str], cwd: str | None, env: Mapping[str, str] | None,
    ) -> PtySpawn | None:
        return PtySpawn(argv=plan_argv, env=plan_env, pass_fds=(w,))

    d = ImptyDriver(sandbox=hook)
    try:
        screen = d.start([sys.executable, "-c", "print('NEVER')"], cwd=str(tmp_path))
    finally:
        d.stop()
        os.close(r)
        os.close(w)
    assert seen["argv"] == plan_argv
    # Load-bearing: Popen's cwd= resolves in the CALLER's mount namespace, so a
    # plan's cwd must ride inside its own argv, never here.
    assert seen["cwd"] is None
    assert seen["pass_fds"] == (w,)
    assert "PLANNED-RAN PLANNED=yes HOME=<none>" in screen
    assert "NEVER" not in screen


def test_send_signal_copy_names_the_terminals_foreground_group() -> None:
    from impty.mcp import THIN_DESCRIPTIONS

    assert THIN_DESCRIPTIONS["send_signal"] == (
        "Send a real signal (default SIGINT) to the terminal's foreground process group."
    )
    # Whitespace-normalized (the repo idiom, tests/test_impty_agent_surfaces.py:34):
    # the pinned paragraph hard-wraps mid-phrase, so a raw substring test on __doc__
    # would assert something the verbatim copy cannot contain.
    doc = " ".join((ImptyDriver.send_signal.__doc__ or "").split())
    assert "the group the terminal holds in the foreground" in doc
    assert "in a shell it hits the running job rather than the shell" in doc
    assert "child's process group" not in doc
    # The OPENING sentence, not just the pinned paragraph: it says "the program's
    # process group" today, and editing only the pinned block would leave the
    # docstring contradicting itself with nothing to catch it.
    assert "program's process group" not in doc
    import impty
    readme = (Path(impty.__file__).parent / "README.md").read_text()
    bullet = next(
        (ln for ln in readme.splitlines() if ln.startswith("- `send_signal(")), None)
    assert bullet is not None, "README has no line documenting send_signal()"
    assert "terminal's FOREGROUND process group" in bullet
    # Every stale mention, not just the bullet: the MCP tool list further down names
    # send_signal's target too. close()'s bullet is exempt — its ladder really does
    # keep the child's own group, which is the whole point of the split.
    stale = [
        ln for ln in readme.splitlines()
        if "child's process group" in ln and not ln.startswith("- `close(")
    ]
    assert stale == [], stale


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_start_env_extends_the_base_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Under a sandbox a replacing env= is a footgun: one variable would drop PATH,
    # HOME and the IMP_* variables the contract depends on. Both markers are set on
    # the parent here, and TERM is deleted from it, so no assertion below depends on
    # the runner's ambient environment (a runner with TERM=dumb would otherwise carry
    # its own value through the overlay and past the session's setdefault).
    monkeypatch.setenv("IMPTY_BASE_ONLY", "base")
    monkeypatch.setenv("IMPTY_OVERRIDDEN", "base")
    monkeypatch.delenv("TERM", raising=False)
    prog = (
        "import os\n"
        "print('BASE=' + os.environ.get('IMPTY_BASE_ONLY', '<none>'),\n"
        "      'OVER=' + os.environ.get('IMPTY_OVERRIDDEN', '<none>'),\n"
        "      'TERM=' + os.environ.get('TERM', '<none>'))\n"
    )
    d = ImptyDriver()
    try:
        screen = d.start([sys.executable, "-c", prog],
                         env={"IMPTY_OVERRIDDEN": "overlay"})
    finally:
        d.stop()
    assert "BASE=base OVER=overlay TERM=xterm-256color" in screen


_JOURNAL_CHILD = (
    "import os, sys, time\n"
    "os.write(1, sys.argv[1].encode())\n"
    "time.sleep(30)\n"
)

_COOKED_CHILD = (
    "import sys, time\n"
    "sys.stdout.write('READY\\n')\n"
    "sys.stdout.flush()\n"
    "line = sys.stdin.readline()\n"
    "sys.stdout.write('GOT=%d\\n' % len(line))\n"
    "sys.stdout.flush()\n"
    "time.sleep(30)\n"
)


def _next_free_fd() -> int:
    fd = os.open(os.devnull, os.O_RDONLY)
    os.close(fd)
    return fd


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_repeated_failing_start_leaks_no_fds(tmp_path: Path) -> None:
    d = ImptyDriver(journal_dir=tmp_path)
    exceptions: list[BaseException] = []
    try:
        baseline = _next_free_fd()
        for _ in range(5):
            with pytest.raises(FileNotFoundError) as ei:
                d.start([str(tmp_path / "nonexistent-binary")])
            exceptions.append(ei.value)  # keep tracebacks alive; see comment below
        after = _next_free_fd()
        # Slack of 5 tolerates incidental fd churn while a real 4-fd-per-attempt
        # leak over 5 attempts would produce ~20.
        assert after - baseline < 5
    finally:
        d.close()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_failing_start_with_no_journal_dir_raises_file_not_found(tmp_path: Path) -> None:
    d = ImptyDriver()
    with pytest.raises(FileNotFoundError):
        d.start([str(tmp_path / "nonexistent-binary")])


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_each_session_of_one_driver_gets_its_own_numbered_journal(
    tmp_path: Path,
) -> None:
    d = ImptyDriver(journal_dir=tmp_path)
    try:
        d.start([sys.executable, "-c", _JOURNAL_CHILD, "FIRST-RUN"])
        d.stop()
        d.start([sys.executable, "-c", _JOURNAL_CHILD, "SECOND-RUN"])
        d.stop()
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "pty-1.jsonl", "pty-1.raw", "pty-2.jsonl", "pty-2.raw",
        ]
        first = (tmp_path / "pty-1.raw").read_bytes()
        second = (tmp_path / "pty-2.raw").read_bytes()
        assert b"FIRST-RUN" in first and b"SECOND-RUN" not in first
        assert b"SECOND-RUN" in second and b"FIRST-RUN" not in second
    finally:
        d.close()


def test_last_session_index_unit(tmp_path: Path) -> None:
    from impty.driver import _last_session_index

    # absent or non-directory -> 0
    assert _last_session_index(tmp_path / "nonexistent") == 0

    # empty dir -> 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _last_session_index(empty) == 0

    # unrelated filenames ignored
    d = tmp_path / "mixed"
    d.mkdir()
    (d / "stderr.log").write_bytes(b"")
    (d / "transcript.jsonl").write_bytes(b"")
    assert _last_session_index(d) == 0

    # pty-1.raw, pty-1.jsonl, pty-2.raw.1 -> max is 2
    (d / "pty-1.raw").write_bytes(b"")
    (d / "pty-1.jsonl").write_bytes(b"")
    (d / "pty-2.raw.1").write_bytes(b"")
    assert _last_session_index(d) == 2

    # not-a-directory: parent is a regular file -> 0
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")
    assert _last_session_index(blocker / "steps") == 0

    # unlistable directory (mode 0o000) -> 0, not a PermissionError
    if os.geteuid() != 0:
        unlistable = tmp_path / "unlistable"
        unlistable.mkdir()
        (unlistable / "pty-3.raw").write_bytes(b"")
        unlistable.chmod(0o000)
        try:
            assert _last_session_index(unlistable) == 0
        finally:
            unlistable.chmod(0o755)


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_resumed_step_driver_numbers_from_next_session(tmp_path: Path) -> None:
    """A second ImptyDriver over the same journal dir (resumed-step case) must start
    at N+1, not 1, so it can't truncate the first driver's pty-1.raw."""
    da = ImptyDriver(journal_dir=tmp_path)
    try:
        da.start([sys.executable, "-c", _JOURNAL_CHILD, "DRIVER-A"])
        da.stop()
    finally:
        da.close()

    raw1_bytes = (tmp_path / "pty-1.raw").read_bytes()
    assert b"DRIVER-A" in raw1_bytes

    db = ImptyDriver(journal_dir=tmp_path)
    try:
        db.start([sys.executable, "-c", _JOURNAL_CHILD, "DRIVER-B"])
        db.stop()
    finally:
        db.close()

    # B wrote into pty-2.*, not pty-1.*
    assert (tmp_path / "pty-2.raw").exists()
    assert b"DRIVER-B" in (tmp_path / "pty-2.raw").read_bytes()
    # A's journal is byte-intact
    assert (tmp_path / "pty-1.raw").read_bytes() == raw1_bytes
    # B wrote nothing into pty-1.*
    assert b"DRIVER-B" not in (tmp_path / "pty-1.raw").read_bytes()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_driver_with_no_journal_dir_writes_no_journal_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    d = ImptyDriver()
    d.start([sys.executable, "-c", _JOURNAL_CHILD, "PLAIN-RUN"])
    try:
        assert "PLAIN-RUN" in d.view()
    finally:
        d.stop()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_an_unwritable_journal_dir_leaves_the_drive_working(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    d = ImptyDriver(journal_dir=blocker / "steps")
    screen = d.start([sys.executable, "-c", _COOKED_CHILD])
    try:
        assert "READY" in screen
        d.send("hello\r", wait=False)
        # Poll rather than settle: the cooked-mode echo of the keystroke is itself
        # output, so a wait() could go quiet on the echo before GOT= lands.
        assert _poll(lambda: "GOT=6" in d.view(), timeout=E2E_POLL_TIMEOUT)
    finally:
        assert d.stop().startswith("[stopped: exit=")
    assert [p.name for p in tmp_path.iterdir()] == ["blocker"]


def test_start_copy_states_the_env_overlay_and_the_hook() -> None:
    doc = ImptyDriver.start.__doc__ or ""
    assert (
        "`env` entries are ADDED to the base environment, overriding same-named ones"
        in doc
    )
    assert "with no hook it runs bare" in doc
    import impty
    readme = (Path(impty.__file__).parent / "README.md").read_text()
    # The playbook is agent-facing copy this change falsifies, and it is NOT one of
    # the files spec §8 enumerates — so it is pinned here or nowhere.
    playbook = (
        Path(__file__).parents[2] / "tests" / "playbooks" / "imp-ux-playbook.md"
    ).read_text()
    texts = (readme, playbook,
             *(m.__doc__ or "" for m in ImptyDriver().tool_methods()))
    for text in texts:
        # Normalized, like the assertion above: EVERY one of these sources wraps the
        # claim mid-phrase today (`REPLACES the\n        inherited environment` in the
        # docstring, `REPLACES the inherited\nenvironment` in the playbook), so a raw
        # substring test would pass vacuously and pin nothing at all.
        flat = " ".join(text.split())
        assert "REPLACES the inherited environment" not in flat
        assert "replaces the environment" not in flat


def test_readme_documents_the_optional_cwd_resolver_contract() -> None:
    import impty

    readme = (Path(impty.__file__).parent / "README.md").read_text()
    section = readme.split("## Embedding: the sandbox hook", 1)[1].split("\n## ", 1)[0]
    copy = " ".join(section.split())
    assert "ImptyDriver(cwd_resolver=...)" in copy
    assert "on every `start()` before the sandbox hook or bare spawn is selected" in copy
    assert "the transformed cwd is passed to either route" in copy
    assert (
        "omitted, relative, and absolute cwd values retain the host process's normal "
        "`Popen` handling"
    ) in copy


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_plans_allocator_supplies_the_sessions_pty(tmp_path: Path) -> None:
    # The sandbox's shape: the host hands the driver the pty itself, and the driven
    # program must land on THAT pts, not on one the session made for itself.
    made: list[str] = []

    def allocator() -> tuple[int, int]:
        master, slave = pty.openpty()
        made.append(os.ttyname(slave))
        return master, slave

    plan_argv = [sys.executable, "-c", "import os; print('TTY=' + os.ttyname(0))"]

    def hook(
        argv: Sequence[str], cwd: str | None, env: Mapping[str, str] | None,
    ) -> PtySpawn | None:
        return PtySpawn(argv=plan_argv, env={"PATH": os.environ["PATH"]},
                        openpty=allocator)

    d = ImptyDriver(sandbox=hook)
    try:
        d.start([sys.executable, "-c", "print('NEVER')"], cwd=str(tmp_path))
        # Poll for the marker the payload prints: start()'s settle can return before a
        # loaded box has exec'd it, and settling on silence is exactly the flake shape.
        assert _poll(lambda: "TTY=" in d.view(), timeout=E2E_POLL_TIMEOUT), \
            "the planned payload never printed its tty"
        screen = d.view()
    finally:
        d.stop()
    assert len(made) == 1
    assert f"TTY={made[0]}" in screen
    assert "NEVER" not in screen


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_plan_without_an_allocator_keeps_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # /sandbox on, but a host with nothing special to say about the pty: the session
    # must get the stdlib allocator, never a None the hook left unset.
    seen = _spy_spawn(monkeypatch)
    plan_argv = [sys.executable, "-c", "import os; print('TTY=' + os.ttyname(0))"]

    def hook(
        argv: Sequence[str], cwd: str | None, env: Mapping[str, str] | None,
    ) -> PtySpawn | None:
        return PtySpawn(argv=plan_argv, env={"PATH": os.environ["PATH"]})

    d = ImptyDriver(sandbox=hook)
    try:
        d.start([sys.executable, "-c", "print('NEVER')"], cwd=str(tmp_path))
        assert _poll(lambda: "TTY=" in d.view(), timeout=E2E_POLL_TIMEOUT), \
            "the planned payload never printed its tty"
        screen = d.view()
    finally:
        d.stop()
    assert seen["openpty"] is pty.openpty
    assert re.search(r"TTY=/dev/pts/\d+", screen)


def test_readme_documents_the_pty_spawn_openpty_field() -> None:
    import impty
    readme = (Path(impty.__file__).parent / "README.md").read_text()
    # Whitespace-normalized (the repo idiom): the paragraph hard-wraps mid-phrase, so
    # a raw substring test would pin nothing.
    flat = " ".join(readme.split())
    assert "`PtySpawn(argv, env, pass_fds, openpty)`" in flat
    assert "the host's own pty allocator" in flat
    assert "`TerminalSession(openpty=…)`" in flat


def _wrote_path(reply: str) -> Path:
    assert reply.startswith("[wrote ") and reply.endswith("]"), reply
    return Path(reply[len("[wrote "):-1])


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_snapshot_defaults_into_the_configured_dir_creating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    snaps = tmp_path / "quest" / "snapshots"  # does not exist yet
    d = ImptyDriver(snapshot_dir=snaps)
    d.start([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        written = _wrote_path(d.snapshot())
        explicit = tmp_path / "explicit.png"
        assert _wrote_path(d.snapshot(path=str(explicit))) == explicit
    finally:
        d.stop()
    assert written.parent == snaps
    assert written.stat().st_size > 0
    assert list(systmp.iterdir()) == []


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_snapshot_without_a_configured_dir_lands_in_the_system_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    d = ImptyDriver()
    d.start([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        written = _wrote_path(d.snapshot())
    finally:
        d.stop()
    assert written.parent == systmp


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_snapshot_dir_that_cannot_be_created_falls_back_to_the_system_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    d = ImptyDriver(snapshot_dir=blocker / "snapshots")  # must not raise
    d.start([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        written = _wrote_path(d.snapshot())
    finally:
        d.stop()
    assert written.parent == systmp
