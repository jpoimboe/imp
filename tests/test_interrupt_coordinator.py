from __future__ import annotations

import signal

import anyio
import pytest

from imp.cli import signals as _signals
from imp.cli.signals import InterruptCoordinator


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _tops(coord: InterruptCoordinator) -> list:
    return [att.interrupt for att in coord._runner_stack]


def test_runner_attached_pushes_and_pops() -> None:
    coord = InterruptCoordinator()

    async def parent_interrupt() -> None: ...
    async def child_interrupt() -> None: ...

    coord.attach_runner(parent_interrupt)
    assert _tops(coord) == [parent_interrupt]

    with coord.runner_attached(child_interrupt):
        # innermost is on top; the parent is still on the stack (full-chain).
        assert _tops(coord) == [parent_interrupt, child_interrupt]

    # child popped, parent remains (restored, NOT cleared).
    assert _tops(coord) == [parent_interrupt]


def test_runner_attached_pops_on_exception() -> None:
    coord = InterruptCoordinator()

    async def parent_interrupt() -> None: ...
    async def child_interrupt() -> None: ...

    coord.attach_runner(parent_interrupt)
    try:
        with coord.runner_attached(child_interrupt):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert _tops(coord) == [parent_interrupt]


def test_runner_attached_from_empty_pops_to_empty() -> None:
    coord = InterruptCoordinator()

    async def child_interrupt() -> None: ...

    with coord.runner_attached(child_interrupt):
        assert _tops(coord) == [child_interrupt]
    assert _tops(coord) == []


def test_runner_attached_carries_force_kill_and_agent_id() -> None:
    coord = InterruptCoordinator()

    async def i() -> None: ...
    async def fk() -> None: ...
    async def interrupt_ack() -> None: ...

    with coord.runner_attached(
        i, force_kill=fk, interrupt_ack=interrupt_ack, agent_id="agent-7",
    ):
        att = coord._runner_stack[-1]
        assert att.interrupt is i
        assert att.force_kill is fk
        assert att.interrupt_ack is interrupt_ack
        assert att.agent_id == "agent-7"


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


def test_begin_end_request_watchdog_depth_counter_and_listeners() -> None:
    coord = InterruptCoordinator()
    assert coord.request_active is False  # default
    fired = {"n": 0}
    coord.add_activity_listener(lambda: fired.__setitem__("n", fired["n"] + 1))

    coord.begin_request_watchdog()
    assert coord.request_active is True
    assert fired["n"] == 1  # note_activity fired on begin

    coord.begin_request_watchdog()  # a second overlapping request scope
    assert coord.request_active is True
    assert fired["n"] == 2

    coord.end_request_watchdog()  # one request still in flight
    assert coord.request_active is True  # depth still > 0 -> NOT a bare bool
    assert fired["n"] == 3

    coord.end_request_watchdog()  # the last in-flight request completes
    assert coord.request_active is False  # depth back to 0
    assert fired["n"] == 4  # note_activity fired again on the final end


def test_begin_end_backoff_wait_depth_counter_and_listeners() -> None:
    coord = InterruptCoordinator()
    assert coord.backoff_wait_active is False  # default
    fired = {"n": 0}
    coord.add_activity_listener(lambda: fired.__setitem__("n", fired["n"] + 1))

    coord.begin_backoff_wait()
    assert coord.backoff_wait_active is True
    assert fired["n"] == 1  # note_activity fired on begin

    coord.begin_backoff_wait()  # a second concurrent backoff sleep
    assert coord.backoff_wait_active is True
    assert fired["n"] == 2

    coord.end_backoff_wait()  # one sleep still in flight
    assert coord.backoff_wait_active is True  # depth still > 0 -> NOT a bare bool
    assert fired["n"] == 3

    coord.end_backoff_wait()  # the last sleep ends
    assert coord.backoff_wait_active is False  # depth back to 0
    assert fired["n"] == 4  # note_activity fired again on the final end


@pytest.mark.anyio
async def test_leave_mid_run_stamps_reason_and_takes_leave_path() -> None:
    coord = InterruptCoordinator()
    coord.escalation_event = anyio.Event()
    runner_called = {"n": 0}

    async def runner_interrupt() -> None:
        runner_called["n"] += 1

    coord.attach_runner(runner_interrupt)

    await coord.leave_mid_run(reason="ctrl-d")

    assert coord.leave_reason == "ctrl-d"    # trigger stamped for the choke point
    assert runner_called["n"] == 0           # NOT the ctrl-c signal_pause runner
    assert coord.should_pause is True        # rc 130 path
    assert coord.pause_reason == "ctrl-d"
    assert coord.escalation_event.is_set()   # detach driven


def test_dispatch_signal_leave_branch_stamps_the_trigger() -> None:
    coord = InterruptCoordinator()
    fired: list[bool] = []
    coord.set_escalation_hook(lambda: fired.append(True))

    coord._dispatch_signal(signal.SIGTERM)
    assert coord.leave_reason == "sigterm"
    assert fired == [True]

    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        coord2 = InterruptCoordinator()
        coord2.set_escalation_hook(lambda: None)
        coord2._dispatch_signal(sighup)
        assert coord2.leave_reason == "sighup"


@pytest.mark.anyio
async def test_on_received_signal_force_sentinel_forwards_force() -> None:
    from imp.cli.signals import _FORCE_FORWARD

    coord = InterruptCoordinator()
    forwarded = {"n": 0}

    async def _force() -> None:
        forwarded["n"] += 1

    coord.set_force_forward_hook(_force)
    await coord._on_received_signal(_FORCE_FORWARD)

    assert forwarded["n"] == 1
    assert coord.should_pause is False   # a force forward never sets the pause flag


@pytest.mark.anyio
async def test_on_received_signal_ordinary_signal_takes_pause_path() -> None:
    import signal

    coord = InterruptCoordinator()
    await coord._on_received_signal(signal.SIGINT)

    assert coord.should_pause is True
    assert coord.pause_reason == "ctrl-c"


def test_end_gesture_round_clears_latch_for_a_fresh_round() -> None:
    """When the run yields to the paused prompt the ctrl-c round is over: the
    latch clears so the next gesture is a fresh first gesture. Narrower than
    reset() — leave_reason / escalated / _yielded are left untouched."""
    coord = InterruptCoordinator()
    # A controlled clock: _first_gesture_at is stamped at 123.0 and "now" is 123.5,
    # WITHIN the 1s suppression window. So if end_gesture_round did NOT clear the
    # window, _register_gesture would return False (suppressed); only a genuine
    # clear-to-None makes the next gesture a fresh first gesture that returns True.
    coord._now = lambda: 123.5
    coord._should_pause = True
    coord.pause_reason = "ctrl-c"
    coord._first_gesture_at = 123.0
    coord.leave_reason = "sigterm"
    coord.escalated = True
    _yielded_sentinel = object()
    coord._yielded = _yielded_sentinel
    coord._interrupted = True

    coord.end_gesture_round()

    assert coord.should_pause is False
    assert coord.pause_reason is None
    assert coord._first_gesture_at is None
    assert coord.leave_reason == "sigterm"          # narrower than reset()
    assert coord.escalated is True                  # narrower than reset()
    assert coord._yielded is _yielded_sentinel      # narrower than reset()
    assert coord.interrupted is True                 # durable 130 signal, narrower than reset()
    # Falsifiable now: without the clear, _now()=123.5 is <1s from 123.0 → False.
    assert coord._register_gesture(kind="ctrl-c") is True   # fresh first gesture


@pytest.mark.anyio
async def test_signal_pause_dispatches_to_innermost_runner_only() -> None:
    """Pins no-spawner inline dispatch to the top of the attach stack, not the parent."""
    coord = InterruptCoordinator()
    calls: list[str] = []

    async def parent_interrupt() -> None:
        calls.append("parent")

    async def child_interrupt() -> None:
        calls.append("child")

    coord.attach_runner(parent_interrupt)
    with coord.runner_attached(child_interrupt):
        await coord.signal_pause(reason="ctrl-c")

    assert calls == ["child"]


@pytest.mark.anyio
async def test_interrupted_flag_survives_end_gesture_round() -> None:
    """Regression: end_gesture_round clears the live gesture latch (should_pause)
    so the next ctrl-c is a fresh gesture, but must PRESERVE the durable
    `interrupted` flag that _session_exit_code reads to return 130 for an
    interrupt-paused session."""
    coord = InterruptCoordinator()
    await coord.signal_pause(reason="ctrl-c")   # no runner attached: sets flags, returns
    assert coord.should_pause is True
    assert coord.interrupted is True
    coord.end_gesture_round()
    assert coord.should_pause is False   # live latch cleared (fresh next gesture)
    assert coord.interrupted is True     # durable signal survives -> rc 130 preserved


def test_pending_model_take_is_one_shot() -> None:
    from imp.cli.signals import InterruptCoordinator

    coord = InterruptCoordinator()
    assert coord.take_pending_model() is None
    coord.set_pending_model("anthropic:claude-fable-5")
    assert coord.take_pending_model() == "anthropic:claude-fable-5"
    assert coord.take_pending_model() is None


def test_pending_model_survives_reset_and_gesture_round() -> None:
    from imp.cli.signals import InterruptCoordinator

    coord = InterruptCoordinator()
    coord.set_pending_model("anthropic:claude-fable-5")
    coord.reset()
    assert coord.take_pending_model() == "anthropic:claude-fable-5"

    coord.set_pending_model("anthropic:claude-fable-5")
    coord.end_gesture_round()
    assert coord.take_pending_model() == "anthropic:claude-fable-5"


@pytest.mark.anyio
async def test_signal_pause_side_effect_reasons_do_not_latch_interrupted() -> None:
    # A between-turns /compact or /model is a side-effect pause, NOT an interrupt
    # gesture: it must set should_pause (the daemon loop + mid-generation path
    # rely on it) but must leave the durable interrupted latch False, so the
    # resume machinery solicits the user instead of continuing a stray turn.
    for reason in ("compact", "model-override"):
        coord = InterruptCoordinator()
        await coord.signal_pause(reason=reason)
        assert coord.should_pause is True
        assert coord.interrupted is False


@pytest.mark.anyio
async def test_signal_pause_genuine_reason_latches_interrupted() -> None:
    # A genuine interrupt/leave gesture still sets the durable latch.
    coord = InterruptCoordinator()
    await coord.signal_pause(reason="ctrl-c")
    assert coord.should_pause is True
    assert coord.interrupted is True


def test_backoff_wait_arm_gates_retry_now_latch() -> None:
    coord = InterruptCoordinator()
    # Not armed: a retry-now request is ignored, never stored.
    assert coord.backoff_wait_active is False
    coord.request_retry_now()
    assert coord.retry_now_requested is False
    # Armed: the request latches.
    coord.begin_backoff_wait()
    assert coord.backoff_wait_active is True
    coord.request_retry_now()
    assert coord.retry_now_requested is True
    # take_retry_now consumes it (one-shot).
    assert coord.take_retry_now() is True
    assert coord.retry_now_requested is False
    assert coord.take_retry_now() is False
    coord.end_backoff_wait()
    assert coord.backoff_wait_active is False
    coord.request_retry_now()
    assert coord.retry_now_requested is False


def test_backoff_wait_depth_counter_holds_across_concurrent_waits() -> None:
    coord = InterruptCoordinator()
    coord.begin_backoff_wait()
    coord.begin_backoff_wait()   # a second concurrent backoff sleep
    coord.end_backoff_wait()
    # One wait still active -> a request must still latch (NOT a bare bool).
    assert coord.backoff_wait_active is True
    coord.request_retry_now()
    assert coord.retry_now_requested is True
    coord.end_backoff_wait()
    assert coord.backoff_wait_active is False


def test_end_backoff_wait_does_not_consume_latch() -> None:
    # Clear-on-consume is the caller's job (take_retry_now), NOT disarm: a latch
    # set during the wait must survive end_backoff_wait so the loop can consume it.
    coord = InterruptCoordinator()
    coord.begin_backoff_wait()
    coord.request_retry_now()
    coord.end_backoff_wait()
    assert coord.retry_now_requested is True


def test_reset_clears_backoff_wait_and_retry_now() -> None:
    # reset() runs between session-loop rounds; the new gesture fields must clear
    # too, for symmetry with should_pause/pause_reason.
    coord = InterruptCoordinator()
    coord.begin_backoff_wait()
    coord.request_retry_now()
    coord.reset()
    assert coord.backoff_wait_active is False
    assert coord.retry_now_requested is False


@pytest.mark.anyio
async def test_dispatch_retry_now_awaits_hook_when_attached() -> None:
    coord = InterruptCoordinator()
    calls: list[str] = []

    async def hook() -> None:
        calls.append("forwarded")

    coord.begin_backoff_wait()          # arm the gate so the assertion is falsifiable
    coord.set_retry_now_hook(hook)
    await coord.dispatch_retry_now()
    # With a hook attached the forward is awaited and NO local latch is set
    # (the daemon owns the arm-gate on the far side of the boundary).
    assert calls == ["forwarded"]
    assert coord.retry_now_requested is False


@pytest.mark.anyio
async def test_dispatch_retry_now_falls_back_to_local_when_no_hook() -> None:
    coord = InterruptCoordinator()
    # No hook: purely local/in-process run. The local request_retry_now() runs,
    # which latches only while a backoff wait is armed.
    coord.begin_backoff_wait()
    await coord.dispatch_retry_now()
    assert coord.retry_now_requested is True


@pytest.mark.anyio
async def test_dispatch_retry_now_local_fallback_respects_arm_gate() -> None:
    coord = InterruptCoordinator()
    # No hook and no armed wait: the local fallback is a no-op (stray `r`).
    await coord.dispatch_retry_now()
    assert coord.retry_now_requested is False


@pytest.mark.anyio
async def test_signal_pause_ctrl_c_after_side_effect_still_latches() -> None:
    # A genuine ctrl-c arriving AFTER a prior side-effect pause must still latch
    # the durable interrupted flag — the gate keys on the current call's reason,
    # not the first pause reason.
    coord = InterruptCoordinator()
    await coord.signal_pause(reason="compact")
    assert coord.interrupted is False
    await coord.signal_pause(reason="ctrl-c")
    assert coord.interrupted is True
