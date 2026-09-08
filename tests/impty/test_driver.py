from __future__ import annotations

import contextlib
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from impty import TerminalSession
from impty.driver import ImptyDriver, _decode_keys, _format_exit
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


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_resize_delivers_sigwinch_and_child_sees_new_width() -> None:
    d = ImptyDriver()
    d.start([sys.executable, "-c", _RESIZE_CHILD], cols=80, rows=24)
    try:
        d.wait(idle=0.2, hard=30.0)  # wait for READY
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
        screen = d.resize(30, 100, wait=0.5)  # rows=30, cols=100
        dt = time.monotonic() - t
        assert "WIDTH=100" in screen
        assert dt < 3.0
    finally:
        d.stop()


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_wait_for_change_blocks_until_new_output_arrives(tmp_path: Path) -> None:
    flag = tmp_path / "go"
    d = ImptyDriver()
    d.start([sys.executable, "-c", _POLL_FOR_FILE_CHILD, str(flag)])
    try:
        quiet = d.wait(idle=0.2, hard=5.0)
        assert quiet.startswith("[settled]")
        assert "READY" in quiet
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
