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
