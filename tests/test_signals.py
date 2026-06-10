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

from imp.cli.signals import InterruptCoordinator


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


def test_dispatch_sigterm_routes_to_exit_immediately() -> None:
    import signal as _sig
    coord = InterruptCoordinator()
    exited = {"v": False}
    coord._trigger_exit = lambda reason: exited.__setitem__("v", reason)
    coord._dispatch_signal(_sig.SIGTERM)
    assert exited["v"] == "sigterm"       # bypasses receiver/signal_pause/interrupt()
    # The SIGTERM branch sets pause state synchronously (the loop's
    # post-cancellation path reads these to persist status=paused).
    assert coord._should_pause is True
    assert coord.pause_reason == "sigterm"
