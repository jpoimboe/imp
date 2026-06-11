from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest

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


@pytest.mark.anyio
async def test_reset_clears_pause_keeps_interjects_and_unparks() -> None:
    coord = InterruptCoordinator()
    coord.enqueue_interject("keep me")
    await coord.signal_pause(reason="esc")
    assert coord.should_pause is True

    unparked: list[int] = []

    class _FakeReader:
        def __init__(self) -> None:
            self._parked_on_pause = True
        def unpark_on_reset(self) -> None:
            self._parked_on_pause = False
            unparked.append(1)

    coord.attach_esc_reader(_FakeReader())
    coord.reset()

    assert coord.should_pause is False
    assert coord.pause_reason is None
    assert coord.drain_interject() == "keep me"  # interjects preserved
    assert unparked == [1]


def test_reset_clears_suppression_window() -> None:
    # A fresh session-loop round must start a NEW suppression window — reset()
    # clears _first_gesture_at so a Ctrl-C after a reset is not permanently
    # suppressed by a gesture from the prior round.
    coord = InterruptCoordinator()
    assert coord._register_gesture(kind="ctrl-c") is True
    assert coord._first_gesture_at is not None
    coord.reset()
    assert coord._first_gesture_at is None
    assert coord._register_gesture(kind="ctrl-c") is True  # new window opens


def test_suppression_window_from_first_gesture() -> None:
    coord = InterruptCoordinator()
    t = {"now": 100.0}
    coord._now = lambda: t["now"]  # inject monotonic clock

    assert coord._register_gesture(kind="ctrl-c") is True   # first → allowed
    t["now"] = 100.5
    assert coord._register_gesture(kind="esc") is False     # within 1 s → suppressed
    t["now"] = 100.9
    assert coord._register_gesture(kind="ctrl-c") is False  # window not restarted
    t["now"] = 101.2
    # >1 s from the FIRST gesture → escalation territory (allowed through).
    assert coord._register_gesture(kind="ctrl-c") is True


def test_sigterm_is_exempt_from_suppression() -> None:
    coord = InterruptCoordinator()
    t = {"now": 100.0}
    coord._now = lambda: t["now"]
    assert coord._register_gesture(kind="ctrl-c") is True
    t["now"] = 100.3
    # A supervisor SIGTERM 0.3 s later must NOT be suppressed.
    assert coord._register_gesture(kind="sigterm") is True


def test_should_pause_is_latched_until_reset() -> None:
    # should_pause is a one-way latch: once set it stays True across in-run ops,
    # cleared only by reset() between session-loop rounds.
    from imp.cli.signals import InterruptCoordinator

    coord = InterruptCoordinator()
    assert coord.should_pause is False
    coord.request_exit("ctrl-c")
    assert coord.should_pause is True
    # In-run operations must not clear the latch.
    coord.drain_interject()
    _ = coord.pause_reason
    coord.enqueue_interject("note")
    coord.drain_interject()
    assert coord.should_pause is True
    # Only reset() (between rounds) clears it.
    coord.reset()
    assert coord.should_pause is False
