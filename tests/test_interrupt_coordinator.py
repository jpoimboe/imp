from __future__ import annotations

import contextlib

import pytest

from collections.abc import AsyncIterator

from imp.cli import signals as _signals
from imp.cli.signals import InterruptCoordinator


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_runner_attached_saves_and_restores_previous() -> None:
    coord = InterruptCoordinator()

    async def parent_interrupt() -> None: ...
    async def child_interrupt() -> None: ...

    coord.attach_runner(parent_interrupt)
    assert coord._runner_interrupt is parent_interrupt

    with coord.runner_attached(child_interrupt):
        assert coord._runner_interrupt is child_interrupt

    # Restored, NOT cleared to None.
    assert coord._runner_interrupt is parent_interrupt


def test_runner_attached_restores_on_exception() -> None:
    coord = InterruptCoordinator()

    async def parent_interrupt() -> None: ...
    async def child_interrupt() -> None: ...

    coord.attach_runner(parent_interrupt)
    try:
        with coord.runner_attached(child_interrupt):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert coord._runner_interrupt is parent_interrupt


def test_runner_attached_from_empty_restores_none() -> None:
    coord = InterruptCoordinator()

    async def child_interrupt() -> None: ...

    with coord.runner_attached(child_interrupt):
        assert coord._runner_interrupt is child_interrupt
    assert coord._runner_interrupt is None


def test_reason_for_signal_maps_sigint_and_sigterm() -> None:
    import signal
    assert _signals._reason_for_signal(signal.SIGINT) == "ctrl-c"
    assert _signals._reason_for_signal(signal.SIGTERM) == "sigterm"


@pytest.mark.anyio
async def test_signal_pause_records_reason_first_wins() -> None:
    assert InterruptCoordinator().pause_reason is None   # initial state, fresh instance
    coord = InterruptCoordinator()
    await coord.signal_pause(reason="esc")
    assert coord.should_pause is True
    assert coord.pause_reason == "esc"
    await coord.signal_pause(reason="ctrl-c")     # later call must NOT overwrite
    assert coord.pause_reason == "esc"


@pytest.mark.anyio
async def test_signal_pause_default_reason_is_ctrl_c() -> None:
    coord = InterruptCoordinator()
    await coord.signal_pause()
    assert coord.pause_reason == "ctrl-c"


@pytest.mark.anyio
async def test_tty_quiesced_noop_when_no_reader_attached() -> None:
    coord = InterruptCoordinator()
    entered = False
    async with coord.tty_quiesced():
        entered = True
    assert entered


@pytest.mark.anyio
async def test_tty_quiesced_nestable_when_no_reader_attached() -> None:
    coord = InterruptCoordinator()
    async with coord.tty_quiesced(), coord.tty_quiesced():
        pass


@pytest.mark.anyio
async def test_tty_quiesced_delegates_to_attached_reader() -> None:
    coord = InterruptCoordinator()
    enter_calls: list[int] = []
    exit_calls: list[int] = []

    class _FakeReader:
        @contextlib.asynccontextmanager
        async def tty_quiesced(self) -> AsyncIterator[None]:
            enter_calls.append(1)
            try:
                yield
            finally:
                exit_calls.append(1)

    coord.attach_esc_reader(_FakeReader())
    async with coord.tty_quiesced():
        pass
    assert enter_calls == [1] and exit_calls == [1]

    coord.detach_esc_reader()
    async with coord.tty_quiesced():
        pass
    assert enter_calls == [1]                     # back to no-op
