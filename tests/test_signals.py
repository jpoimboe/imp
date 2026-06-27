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
import os
import signal

import pytest

from imp.cli.signals import InterruptCoordinator, install_process_group


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


def test_dispatch_signal_escalates_after_window_with_wedged_signal_pause() -> None:
    import signal as _sig
    coord = InterruptCoordinator()
    t = {"now": 100.0}
    coord._now = lambda: t["now"]

    # Simulate a wedged signal_pause: should_pause already set, interrupt never
    # returns. _dispatch_signal must still detect escalation without awaiting it.
    coord._should_pause = True
    coord._first_gesture_at = 100.0
    esc_fired = {"v": False}
    coord._trigger_escalation = lambda: esc_fired.__setitem__("v", True)

    t["now"] = 100.5
    coord._dispatch_signal(_sig.SIGINT)   # within 1 s → suppressed, no escalation
    assert esc_fired["v"] is False

    t["now"] = 101.5
    coord._dispatch_signal(_sig.SIGINT)   # >1 s, stop unwinding → escalate
    assert esc_fired["v"] is True


def test_dispatch_sigint_at_prompt_routes_to_exit_not_receiver() -> None:
    """An out-of-band SIGINT while AT a prompt (handle_sigint=False so it lands
    in the coordinator's loop handler, not as a prompt_toolkit KBI) must take the
    exit path — NOT the receiver/signal_pause/runner path (that is the
    DURING-A-RUN behavior)."""
    import signal as _sig
    coord = InterruptCoordinator()
    coord.set_at_prompt(True)

    exited = {"reason": None}
    coord.set_exit_hook(lambda reason: exited.__setitem__("reason", reason))

    # A receiver send-stream is wired but must NOT be touched on this path.
    sent: list = []

    class _Send:
        def send_nowait(self, item):  # type: ignore[no-untyped-def]
            sent.append(item)

    coord._sig_send = _Send()

    coord._dispatch_signal(_sig.SIGINT)

    assert exited["reason"] == "ctrl-c"          # exit hook fired with ctrl-c
    assert coord.should_pause is True
    assert coord.pause_reason == "ctrl-c"
    assert sent == []                            # receiver path NOT taken


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


def test_dispatch_second_sigint_at_prompt_still_escalates_after_window() -> None:
    """A SECOND out-of-band SIGINT while at a prompt AND already pausing still
    hits the existing escalation/suppression gate (force-quit): suppressed within
    the window, escalates after it."""
    import signal as _sig
    coord = InterruptCoordinator()
    coord.set_at_prompt(True)
    t = {"now": 100.0}
    coord._now = lambda: t["now"]

    exited = {"reason": None}
    coord.set_exit_hook(lambda reason: exited.__setitem__("reason", reason))
    esc_fired = {"v": False}
    coord.set_escalation_hook(lambda: esc_fired.__setitem__("v", True))

    # First at-prompt SIGINT → exit path, registers the gesture (first-wins).
    coord._dispatch_signal(_sig.SIGINT)
    assert exited["reason"] == "ctrl-c"
    assert coord.should_pause is True

    # Second within the suppression window → suppressed, no escalation.
    t["now"] = 100.5
    coord._dispatch_signal(_sig.SIGINT)
    assert esc_fired["v"] is False

    # After the window, the stop is unwinding (should_pause) → escalation fires.
    t["now"] = 101.5
    coord._dispatch_signal(_sig.SIGINT)
    assert esc_fired["v"] is True


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
