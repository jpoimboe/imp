from __future__ import annotations

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
    await coord.signal_pause(reason="ctrl-c")
    assert coord.should_pause is True
    assert coord.pause_reason == "ctrl-c"
    await coord.signal_pause(reason="sigterm")    # later call must NOT overwrite
    assert coord.pause_reason == "ctrl-c"


@pytest.mark.anyio
async def test_signal_pause_default_reason_is_ctrl_c() -> None:
    coord = InterruptCoordinator()
    await coord.signal_pause()
    assert coord.pause_reason == "ctrl-c"


@pytest.mark.anyio
async def test_reset_clears_pause_keeps_interjects() -> None:
    coord = InterruptCoordinator()
    coord.enqueue_interject("keep me")
    await coord.signal_pause(reason="ctrl-c")
    assert coord.should_pause is True

    coord.reset()

    assert coord.should_pause is False
    assert coord.pause_reason is None
    assert coord.drain_interject() == "keep me"  # interjects preserved


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
    assert coord._register_gesture(kind="ctrl-c") is False     # within 1 s → suppressed
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


def test_clear_pause_resets_flag_and_reason_but_keeps_gesture() -> None:
    coord = InterruptCoordinator()
    coord._should_pause = True
    coord.pause_reason = "ctrl-c"
    coord._first_gesture_at = 123.0
    coord.escalated = True

    coord.clear_pause()

    # The pause is consumed: the run continues, not pauses.
    assert coord.should_pause is False
    assert coord.pause_reason is None
    # The gesture/escalation window is untouched so a follow-up force-quit
    # Ctrl-C still escalates.
    assert coord._first_gesture_at == 123.0
    assert coord.escalated is True


def test_begin_end_solicit_watchdog_toggles_flag_and_fires_listeners() -> None:
    coord = InterruptCoordinator()
    assert coord.solicit_active is False  # default
    fired = {"n": 0}
    coord.add_activity_listener(lambda: fired.__setitem__("n", fired["n"] + 1))

    coord.begin_solicit_watchdog()
    assert coord.solicit_active is True
    assert fired["n"] == 1  # note_activity fired the registered listener

    coord.end_solicit_watchdog()
    assert coord.solicit_active is False
    assert fired["n"] == 2  # fired again on end


def test_begin_end_tool_watchdog_depth_counter_and_listeners() -> None:
    coord = InterruptCoordinator()
    assert coord.tool_active is False  # default
    fired = {"n": 0}
    coord.add_activity_listener(lambda: fired.__setitem__("n", fired["n"] + 1))

    coord.begin_tool_watchdog()
    assert coord.tool_active is True
    assert fired["n"] == 1  # note_activity fired on begin

    coord.begin_tool_watchdog()  # a second concurrent foreground tool
    assert coord.tool_active is True
    assert fired["n"] == 2

    coord.end_tool_watchdog()  # one tool still in flight
    assert coord.tool_active is True  # depth still > 0 -> NOT a bare bool
    assert fired["n"] == 3

    coord.end_tool_watchdog()  # the last in-flight tool returns
    assert coord.tool_active is False  # depth back to 0
    assert fired["n"] == 4  # note_activity fired again on the final end
