"""InterruptCoordinator: signal-driven pause behavior.

Spawning a real signal handler in tests is messy (signals interact with the
test runner). Instead we test the coordinator's `signal_pause()` method
directly, simulating what the signal receiver would do.

The exception is `test_signal_receiver_survives_prompt_via_rearm`, which DOES
exercise real loop signal handlers + a real prompt_toolkit prompt — that's the
whole point of the regression. It only ever delivers a real SIGINT while the
coordinator's handler is armed (so it's caught, never raised as
KeyboardInterrupt); the broken-after-prompt state is asserted via handler
registration, not by sending a signal."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

import pytest

from imp.cli.signals import (
    InterruptCoordinator,
    install_process_group,
    sigint_cancels_scope,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_install_process_group_does_not_move_runner() -> None:
    """install_process_group() must never relocate the *calling* process into a
    new process group during tests. If it did, an in-process CLI test would rip
    pytest out of make's foreground group and the runner's next tty write would
    SIGTTOU-stop it (the intermittent `make test` hang). The autouse
    `_no_inprocess_setpgrp` conftest fixture guarantees this; this is its canary.
    """
    before = os.getpgrp()
    install_process_group()
    assert os.getpgrp() == before


@pytest.mark.anyio
async def test_coordinator_signal_pause_invokes_runner_interrupt_and_sets_flag() -> None:
    coord = InterruptCoordinator()
    called: list = []

    async def _interrupt() -> None:
        called.append(True)

    coord.attach_runner(_interrupt)
    assert coord.should_pause is False
    await coord.signal_pause()
    assert coord.should_pause is True
    assert called == [True]


@pytest.mark.anyio
async def test_coordinator_signal_pause_idempotent_no_runner() -> None:
    """Second signal with no runner attached is a no-op (idempotent)."""
    coord = InterruptCoordinator()
    await coord.signal_pause()
    await coord.signal_pause()
    assert coord.should_pause is True


@pytest.mark.anyio
async def test_coordinator_detach_runner_before_pause() -> None:
    """If runner detached before signal, no callback fires."""
    coord = InterruptCoordinator()
    called: list = []

    async def _interrupt() -> None:
        called.append(True)

    coord.attach_runner(_interrupt)
    coord.detach_runner()
    await coord.signal_pause()
    assert called == []
    assert coord.should_pause is True


@pytest.mark.anyio
async def test_signal_receiver_survives_prompt_via_rearm() -> None:
    """Regression: a prompt_toolkit prompt(handle_sigint=True) calls
    loop.remove_signal_handler(SIGINT) on exit, silently deleting the loop's
    SIGINT dispatch entry — so without re-arming, every interactive prompt
    permanently disables mid-run Ctrl-C (the codex `paused (unknown)` bug).
    `rearm_signal_handlers()` (called by ask_line_with_slash on prompt exit)
    must restore delivery."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    coord = InterruptCoordinator()

    async def _sigint() -> bool:
        coord._should_pause = False
        coord.pause_reason = None
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(0.3)
        return coord.should_pause

    async with asyncio.timeout(10):
        async with __import__("anyio").create_task_group() as tg:
            tg.start_soon(coord.run_signal_receiver)
            await asyncio.sleep(0.1)  # let the receiver install loop handlers
            loop = asyncio.get_running_loop()

            # Armed: a real SIGINT is caught by the coordinator (not raised).
            assert signal.SIGINT in loop._signal_handlers
            assert await _sigint() is True

            # A real prompt deletes the SIGINT loop handler on exit.
            with create_pipe_input() as pipe:
                pipe.send_text("hello\n")
                session: PromptSession[str] = PromptSession(
                    input=pipe, output=DummyOutput())
                await session.prompt_async("> ", handle_sigint=True)
            # The bug: handler gone. (Assert by registration — do NOT send a real
            # SIGINT here; with no Handle it could surface as KeyboardInterrupt.)
            assert signal.SIGINT not in loop._signal_handlers

            # The fix: re-arm restores the handler and delivery.
            coord.rearm_signal_handlers()
            assert signal.SIGINT in loop._signal_handlers
            assert await _sigint() is True

            tg.cancel_scope.cancel()


def test_dispatch_second_ctrl_c_forwards_force_not_escalation_after_window() -> None:
    """A repeat ctrl-c while a stop is unwinding enqueues a FORCE forward (the
    daemon hard-kills and the client stays attached) — it does NOT fire
    _trigger_escalation (ctrl-c never force-quits to bash). Suppressed within the
    1s window; forwarded after it."""
    import signal as _sig

    from imp.cli.signals import _FORCE_FORWARD

    coord = InterruptCoordinator()
    t = {"now": 100.0}
    coord._now = lambda: t["now"]

    # A stop is already unwinding (first gesture landed).
    coord._should_pause = True
    coord._first_gesture_at = 100.0
    esc_fired = {"v": False}
    coord.set_escalation_hook(lambda: esc_fired.__setitem__("v", True))

    sent: list = []

    class _Send:
        def send_nowait(self, item):  # type: ignore[no-untyped-def]
            sent.append(item)

    coord._sig_send = _Send()

    t["now"] = 100.5
    coord._dispatch_signal(_sig.SIGINT)   # within 1s → suppressed
    assert sent == []
    assert esc_fired["v"] is False

    t["now"] = 101.5
    coord._dispatch_signal(_sig.SIGINT)   # >1s → FORCE forward, NOT escalation
    assert sent == [_FORCE_FORWARD]
    assert esc_fired["v"] is False


def test_dispatch_sigint_at_prompt_feeds_ctrl_c_and_never_exits(monkeypatch) -> None:
    """An out-of-band SIGINT while AT a prompt is routed through the prompt's own
    Ctrl-C binding (clear/hint) — it does NOT take the exit path, does NOT set the
    pause flag, and does NOT enqueue to the receiver. Ctrl-c never exits."""
    import signal as _sig

    import imp.cli.signals as _signals

    coord = InterruptCoordinator()
    coord.set_at_prompt(True)

    fed = {"n": 0}
    monkeypatch.setattr(_signals, "_feed_prompt_ctrl_c",
                        lambda: fed.__setitem__("n", fed["n"] + 1))

    exited = {"reason": None}
    coord.set_exit_hook(lambda reason: exited.__setitem__("reason", reason))

    sent: list = []

    class _Send:
        def send_nowait(self, item):  # type: ignore[no-untyped-def]
            sent.append(item)

    coord._sig_send = _Send()

    coord._dispatch_signal(_sig.SIGINT)

    assert fed["n"] == 1                  # prompt ctrl-c binding invoked
    assert exited["reason"] is None       # exit path NOT taken
    assert coord.should_pause is False    # never sets pause / never exits
    assert sent == []                     # receiver path NOT taken
    assert coord._first_gesture_at is None  # gesture window left untouched (AC4)


def test_dispatch_sigint_during_run_takes_receiver_path_not_exit() -> None:
    """Headline guard: a SIGINT during a RUN (_at_prompt False) must NOT fire the
    exit hook — it enqueues to the receiver (→ signal_pause → runner interrupt →
    pause-to-prompt). This is the headline mid-run Ctrl-C capability."""
    import signal as _sig
    coord = InterruptCoordinator()
    assert coord._at_prompt is False             # default: during a run

    exit_fired = {"v": False}
    coord.set_exit_hook(lambda reason: exit_fired.__setitem__("v", True))

    sent: list = []

    class _Send:
        def send_nowait(self, item):  # type: ignore[no-untyped-def]
            sent.append(item)

    coord._sig_send = _Send()

    coord._dispatch_signal(_sig.SIGINT)

    assert exit_fired["v"] is False              # exit path NOT taken
    assert sent == [_sig.SIGINT]                 # receiver-enqueue path taken


def test_repeated_sigint_at_prompt_never_escalates_or_exits(monkeypatch) -> None:
    """Repeated out-of-band SIGINTs at a prompt keep clearing/hinting via the
    prompt binding — they NEVER escalate to a force-quit and NEVER exit, however
    many arrive and however far apart (ctrl-c never exits at a prompt)."""
    import signal as _sig

    import imp.cli.signals as _signals

    coord = InterruptCoordinator()
    coord.set_at_prompt(True)
    t = {"now": 100.0}
    coord._now = lambda: t["now"]

    fed = {"n": 0}
    monkeypatch.setattr(_signals, "_feed_prompt_ctrl_c",
                        lambda: fed.__setitem__("n", fed["n"] + 1))

    exited = {"v": False}
    coord.set_exit_hook(lambda _reason: exited.__setitem__("v", True))
    esc_fired = {"v": False}
    coord.set_escalation_hook(lambda: esc_fired.__setitem__("v", True))

    coord._dispatch_signal(_sig.SIGINT)
    t["now"] = 101.5                      # >1s later
    coord._dispatch_signal(_sig.SIGINT)

    assert fed["n"] == 2
    assert exited["v"] is False
    assert esc_fired["v"] is False
    assert coord.should_pause is False
    assert coord._first_gesture_at is None  # window never stamped at the prompt (AC4)


def test_dispatch_sighup_fires_leave_not_pause() -> None:
    import signal as _sig
    coord = InterruptCoordinator()
    left = {"v": False}
    coord.set_escalation_hook(lambda: left.__setitem__("v", True))

    coord._dispatch_signal(_sig.SIGHUP)

    assert left["v"] is True             # SIGHUP is a leave (escalation) gesture
    assert coord.should_pause is False   # never touches pause state


@pytest.mark.anyio
async def test_run_signal_receiver_installs_sighup_handler() -> None:
    import anyio
    coord = InterruptCoordinator()
    async with anyio.create_task_group() as tg:
        tg.start_soon(coord.run_signal_receiver)
        await asyncio.sleep(0.1)   # let the receiver install loop handlers
        loop = asyncio.get_running_loop()
        assert signal.SIGINT in loop._signal_handlers
        assert signal.SIGTERM in loop._signal_handlers
        assert signal.SIGHUP in loop._signal_handlers
        tg.cancel_scope.cancel()


def test_dispatch_sigterm_fires_leave_not_pause() -> None:
    import signal as _sig
    coord = InterruptCoordinator()
    left = {"v": False}
    coord.set_escalation_hook(lambda: left.__setitem__("v", True))
    exited = {"v": False}
    coord.set_exit_hook(lambda _reason: exited.__setitem__("v", True))

    coord._dispatch_signal(_sig.SIGTERM)

    assert left["v"] is True             # SIGTERM is a leave (escalation) gesture
    assert exited["v"] is False          # NOT the old pause/exit path
    assert coord.should_pause is False   # the client does not decide the run's fate
    assert coord.pause_reason is None


def _can_pty() -> bool:
    try:
        import pty
        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


def _patch_dev_tty(monkeypatch, slave_fd: int) -> None:
    real_open = os.open

    def fake_open(path: str, flags: int, mode: int = 0o777) -> int:
        if path == "/dev/tty":
            return os.dup(slave_fd)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fake_open)


@pytest.mark.skipif(not _can_pty(), reason="pty not available")
@pytest.mark.anyio
async def test_ctrl_d_byte_routes_to_leave_path_not_signal_pause(monkeypatch) -> None:
    import pty

    import anyio

    from imp.cli.signals import CtrlDReader
    master, slave = pty.openpty()
    try:
        _patch_dev_tty(monkeypatch, slave)
        coord = InterruptCoordinator()
        coord.escalation_event = anyio.Event()
        runner_called = {"n": 0}

        async def runner_interrupt() -> None:
            runner_called["n"] += 1

        coord.attach_runner(runner_interrupt)  # the signal_pause/ctrl-c forward
        reader = CtrlDReader(coord)
        async with anyio.create_task_group() as tg:
            await tg.start(reader.run)
            os.write(master, b"\x04")
            with anyio.fail_after(5):
                while not coord.should_pause:
                    await anyio.sleep(0.02)
            tg.cancel_scope.cancel()
        assert coord.should_pause is True
        assert coord.pause_reason == "ctrl-d"
        assert coord.escalation_event.is_set()   # leave path taken
        assert runner_called["n"] == 0           # NOT signal_pause
    finally:
        os.close(master)
        with contextlib.suppress(OSError):
            os.close(slave)


@pytest.mark.skipif(not _can_pty(), reason="pty not available")
@pytest.mark.anyio
async def test_tty_quiesced_parks_reader_then_rearms(monkeypatch) -> None:
    import pty

    import anyio

    from imp.cli.signals import CtrlDReader
    master, slave = pty.openpty()
    try:
        _patch_dev_tty(monkeypatch, slave)
        coord = InterruptCoordinator()
        coord.escalation_event = anyio.Event()
        reader = CtrlDReader(coord)
        async with anyio.create_task_group() as tg:
            await tg.start(reader.run)
            # While quiesced the reader is parked (termios restored, not reading),
            # so it acts on nothing. We do NOT write \x04 here: under the restored
            # canonical mode \x04 is VEOF and the line discipline would consume it
            # as end-of-input rather than buffer it as a literal byte. Parking is
            # proven by should_pause staying False across the sleep.
            async with coord.tty_quiesced():
                await anyio.sleep(0.2)
                assert coord.should_pause is False
            # Re-armed to cbreak on exit: a mid-run \x04 is now an ordinary raw
            # byte the reader reads and routes to the leave path.
            os.write(master, b"\x04")
            with anyio.fail_after(5):
                while not coord.should_pause:
                    await anyio.sleep(0.02)
            assert coord.pause_reason == "ctrl-d"
            tg.cancel_scope.cancel()
    finally:
        os.close(master)
        with contextlib.suppress(OSError):
            os.close(slave)


@pytest.mark.anyio
async def test_sigint_guard_cancels_prompt_and_restores_handler() -> None:
    """An OS SIGINT delivered while inside the guard cancels the wrapped body and
    surfaces as a KeyboardInterrupt (never a raw interpreter KBI); on exit the
    guard restores the exact prior SIGINT disposition it found on entry."""
    def _sentinel(_signum, _frame):  # a distinct prior handler to restore
        raise AssertionError("prior handler must not actually fire")

    prev = signal.signal(signal.SIGINT, _sentinel)
    try:
        raised = False
        async with asyncio.timeout(10):
            try:
                async with sigint_cancels_scope():
                    os.kill(os.getpid(), signal.SIGINT)
                    await asyncio.sleep(5)  # cancelled well before this elapses
            except KeyboardInterrupt:
                raised = True
        assert raised is True
        # The guard restored the exact disposition present on entry — NOT the
        # asyncio default that remove_signal_handler would otherwise leave.
        assert signal.getsignal(signal.SIGINT) is _sentinel
    finally:
        signal.signal(signal.SIGINT, prev)


@pytest.mark.anyio
async def test_sigint_guard_clean_body_does_not_raise() -> None:
    """A body that completes with no SIGINT runs to completion and the guard
    raises nothing."""
    ran = False
    async with sigint_cancels_scope():
        ran = True
    assert ran is True


@pytest.mark.anyio
async def test_sigint_guard_degrades_to_noop_when_unsupported(monkeypatch) -> None:
    """Where loop.add_signal_handler is unsupported (Windows), the guard degrades
    to a no-op: the body still runs and no error propagates."""
    loop = asyncio.get_running_loop()

    def _unsupported(*_a, **_k):
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", _unsupported)
    ran = False
    async with sigint_cancels_scope():
        ran = True
    assert ran is True


@pytest.mark.anyio
async def test_sigint_guard_passes_body_exception_through_and_restores() -> None:
    """A non-cancellation exception raised in the body (e.g. the EOFError the
    opening prompt legitimately raises) propagates unchanged — never swallowed
    nor replaced by KeyboardInterrupt — and the guard still restores the prior
    SIGINT disposition on the way out."""
    def _sentinel(_signum, _frame):
        raise AssertionError("prior handler must not actually fire")

    prev = signal.signal(signal.SIGINT, _sentinel)
    try:
        with pytest.raises(EOFError):
            async with sigint_cancels_scope():
                raise EOFError
        assert signal.getsignal(signal.SIGINT) is _sentinel
    finally:
        signal.signal(signal.SIGINT, prev)


def test_feed_prompt_ctrl_c_is_a_noop_with_no_running_app() -> None:
    """With no prompt_toolkit app running, feeding a synthetic ctrl-c is a
    harmless no-op — it never raises and never exits."""
    from imp.cli.signals import _feed_prompt_ctrl_c

    _feed_prompt_ctrl_c()   # must return cleanly


def test_prompt_sigint_does_not_pollute_next_round_suppression(monkeypatch) -> None:
    """Regression (AC4): an out-of-band SIGINT at a prompt must NOT stamp the
    gesture window. Otherwise the NEXT run round's <1s double-tap suppression is
    defeated — the first real ctrl-c would inherit a stale window and a rapid
    second ctrl-c would forward FORCE (hard-kill) instead of being suppressed."""
    import signal as _sig

    import imp.cli.signals as _signals
    from imp.cli.signals import _FORCE_FORWARD

    coord = InterruptCoordinator()
    monkeypatch.setattr(_signals, "_feed_prompt_ctrl_c", lambda: None)
    t = {"now": 100.0}
    coord._now = lambda: t["now"]

    # 1) kill -INT at a prompt — must leave the gesture window untouched.
    coord.set_at_prompt(True)
    coord._dispatch_signal(_sig.SIGINT)
    assert coord._first_gesture_at is None

    # 2) Next run round (not at a prompt): a first real ctrl-c, then a rapid
    #    (<1s) second. The window must be fresh, so the second is SUPPRESSED.
    coord.set_at_prompt(False)
    sent: list = []

    class _Send:
        def send_nowait(self, item):  # type: ignore[no-untyped-def]
            sent.append(item)

    coord._sig_send = _Send()

    t["now"] = 110.0
    coord._dispatch_signal(_sig.SIGINT)       # first real gesture → enqueue SIGINT
    assert sent == [_sig.SIGINT]
    assert coord._first_gesture_at == 110.0   # window opened HERE, not at the prompt
    # Simulate the receiver's signal_pause landing so the repeat hits the gate.
    coord._should_pause = True

    t["now"] = 110.2                          # rapid second, <1s from the first
    coord._dispatch_signal(_sig.SIGINT)
    assert _FORCE_FORWARD not in sent         # suppressed within the fresh window
