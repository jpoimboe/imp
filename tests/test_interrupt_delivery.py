from __future__ import annotations

from typing import Any

import anyio
import pytest

from imp.cli import signals as _signals
from imp.cli.signals import InterruptCoordinator
from tests.async_wait import wait_until
from tests.test_interrupt_events import RecordingLog  # reuse the fake


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_delivery_cooperative_ok_records_and_skips_hard() -> None:
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    calls: list[str] = []

    async def coop() -> None:
        calls.append("interrupt")

    async def fk() -> None:
        calls.append("force_kill")

    with coord.runner_attached(coop, force_kill=fk, agent_id="a1"):
        await coord._deliver_interrupt()

    assert calls == ["interrupt"]  # cooperative landed; no hard tier
    delivered = [r["payload"] for r in log.records if r["type"] == "interrupt_delivered"]
    assert len(delivered) == 1
    assert delivered[0]["agent_id"] == "a1"
    assert delivered[0]["tier"] == "cooperative"
    assert delivered[0]["outcome"] == "ok"
    assert isinstance(delivered[0]["duration_ms"], int)


@pytest.mark.anyio
async def test_delivery_timeout_escalates_to_force_kill(monkeypatch: Any) -> None:
    monkeypatch.setattr(_signals, "_COOPERATIVE_DEADLINE_S", 0.05)
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    killed: list[str] = []

    async def hang() -> None:
        await anyio.sleep_forever()

    async def fk() -> None:
        killed.append("a1")

    with coord.runner_attached(hang, force_kill=fk, agent_id="a1"):
        with anyio.fail_after(30):  # deadlock backstop only
            await coord._deliver_interrupt()

    assert killed == ["a1"]  # hard tier fired after the 2s (here 0.05s) bound
    tiers = [(r["payload"]["tier"], r["payload"]["outcome"])
             for r in log.records if r["type"] == "interrupt_delivered"]
    assert ("cooperative", "timeout") in tiers
    assert ("hard", "ok") in tiers
    esc = [r["payload"] for r in log.records if r["type"] == "interrupt_escalated"]
    assert esc == [{"reason": "cooperative_timeout", "killed": ["a1"]}]


@pytest.mark.anyio
async def test_delivery_error_runs_hard_tier_without_escalation() -> None:
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    killed: list[str] = []

    async def boom() -> None:
        raise RuntimeError("interrupt blew up")

    async def fk() -> None:
        killed.append("a1")

    with coord.runner_attached(boom, force_kill=fk, agent_id="a1"):
        await coord._deliver_interrupt()

    assert killed == ["a1"]  # hard tier still runs on an error outcome
    delivered = [r["payload"] for r in log.records if r["type"] == "interrupt_delivered"]
    tiers = [(d["tier"], d["outcome"]) for d in delivered]
    assert ("cooperative", "error") in tiers
    assert ("hard", "ok") in tiers
    # error outcome does NOT emit interrupt_escalated (escalation record is timeout-only)
    assert [r for r in log.records if r["type"] == "interrupt_escalated"] == []


@pytest.mark.anyio
async def test_delivery_timeout_without_force_kill_no_escalation_no_hard(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(_signals, "_COOPERATIVE_DEADLINE_S", 0.05)
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)

    async def hang() -> None:
        await anyio.sleep_forever()

    # No force_kill: the hard tier is a no-op and no cooperative_timeout escalation fires.
    with coord.runner_attached(hang, agent_id="a1"):
        with anyio.fail_after(30):  # deadlock backstop only
            await coord._deliver_interrupt()

    delivered = [r["payload"] for r in log.records if r["type"] == "interrupt_delivered"]
    tiers = [(d["tier"], d["outcome"]) for d in delivered]
    assert ("cooperative", "timeout") in tiers
    assert all(t[0] != "hard" for t in tiers)  # no hard-tier record without force_kill
    assert [r for r in log.records if r["type"] == "interrupt_escalated"] == []


@pytest.mark.anyio
async def test_delivery_walks_stack_innermost_first() -> None:
    coord = InterruptCoordinator()
    coord.set_event_log(RecordingLog())
    order: list[str] = []

    async def parent() -> None:
        order.append("parent")

    async def child() -> None:
        order.append("child")

    with (
        coord.runner_attached(parent, agent_id="parent"),
        coord.runner_attached(child, agent_id="child"),
    ):
        await coord._deliver_interrupt()

    assert order == ["child", "parent"]  # innermost first, then each ancestor


@pytest.mark.anyio
async def test_signal_pause_fire_and_forget_does_not_block() -> None:
    coord = InterruptCoordinator()
    coord.set_event_log(RecordingLog())
    started = anyio.Event()
    release = anyio.Event()

    async def hanging_interrupt() -> None:
        started.set()
        await release.wait()  # would wedge an inline await forever

    async with anyio.create_task_group() as tg:
        coord.set_delivery_spawner(tg.start_soon)
        coord.attach_runner(hanging_interrupt)
        # With a spawner, signal_pause returns promptly despite the hang.
        with anyio.fail_after(30):  # deadlock backstop only
            await coord.signal_pause(reason="ctrl-c", via="frame")
        assert coord.should_pause is True
        assert coord.pause_reason == "ctrl-c"
        await started.wait()  # delivery really ran (fire-and-forget), still hanging
        release.set()         # let the spawned delivery task finish
        coord.note_yield()    # disarm the spawned yield watchdog so the group closes


@pytest.mark.anyio
async def test_signal_pause_inline_when_no_spawner() -> None:
    coord = InterruptCoordinator()
    coord.set_event_log(RecordingLog())
    calls: list[str] = []

    async def coop() -> None:
        calls.append("interrupt")

    coord.attach_runner(coop)
    await coord.signal_pause(reason="ctrl-c")  # no spawner -> inline cooperative
    assert calls == ["interrupt"]


@pytest.mark.anyio
async def test_signal_pause_empty_stack_does_not_arm_watchdog(monkeypatch: Any) -> None:
    """A leave gesture (ctrl-d) at an already-yielded prompt has an empty attach
    stack. With a spawner registered it must NOT arm the yield watchdog — that would
    stall the pump task group's close for the full window and false-fire
    interrupt_escalated{no_yield, killed: []}."""
    monkeypatch.setattr(_signals, "_YIELD_DEADLINE_S", 0.05)
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)

    async with anyio.create_task_group() as tg:
        coord.set_delivery_spawner(tg.start_soon)
        # No runner attached (the run has already yielded).
        with anyio.fail_after(30):  # deadlock backstop only
            await coord.signal_pause(reason="ctrl-d", via="frame")

    # NOTE: fully falsifiable only once the real _yield_watchdog lands (later task);
    # against the current stub this pins the no-escalation invariant.
    assert coord.should_pause is True
    assert coord.pause_reason == "ctrl-d"
    assert [r for r in log.records if r["type"] == "interrupt_escalated"] == []


@pytest.mark.anyio
async def test_yield_watchdog_escalates_when_no_yield(monkeypatch: Any) -> None:
    monkeypatch.setattr(_signals, "_YIELD_DEADLINE_S", 0.05)
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    killed: list[str] = []

    async def fk_parent() -> None:
        killed.append("parent")

    async def fk_child() -> None:
        killed.append("child")

    with (
        coord.runner_attached(lambda: None, force_kill=fk_parent, agent_id="parent"),
        coord.runner_attached(lambda: None, force_kill=fk_child, agent_id="child"),
    ):
        coord._first_gesture_at = coord._now()
        coord._yielded = anyio.Event()  # never set -> no yield
        with anyio.fail_after(30):  # deadlock backstop only
            await coord._yield_watchdog()

    assert set(killed) == {"parent", "child"}  # whole stack hard-killed
    esc = [r["payload"] for r in log.records if r["type"] == "interrupt_escalated"]
    assert len(esc) == 1
    assert esc[0]["reason"] == "no_yield"
    assert set(esc[0]["killed"]) == {"parent", "child"}


@pytest.mark.anyio
async def test_note_yield_disarms_watchdog(monkeypatch: Any) -> None:
    monkeypatch.setattr(_signals, "_YIELD_DEADLINE_S", 5.0)
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    killed: list[str] = []

    async def fk_x() -> None:
        killed.append("x")

    with coord.runner_attached(lambda: None, force_kill=fk_x, agent_id="x"):
        coord._first_gesture_at = coord._now()
        coord._yielded = anyio.Event()
        coord.note_yield()  # run yielded before the deadline
        with anyio.fail_after(30):  # deadlock backstop only
            await coord._yield_watchdog()

    assert killed == []  # yielded in time -> no escalation
    assert [r for r in log.records if r["type"] == "interrupt_escalated"] == []


@pytest.mark.anyio
async def test_signal_force_skips_cooperative_and_hard_kills_whole_stack() -> None:
    """A FORCE (second-gesture) delivery must NOT call any cooperative interrupt();
    it hard-kills every runner on the attach stack and journals
    interrupt_escalated{second_gesture} plus one interrupt_delivered{hard} each."""
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)

    coop_calls = {"n": 0}

    async def coop() -> None:
        coop_calls["n"] += 1

    killed: list[str] = []

    async def fk_parent() -> None:
        killed.append("parent")

    async def fk_child() -> None:
        killed.append("child")

    with (
        coord.runner_attached(coop, force_kill=fk_parent, agent_id="parent"),
        coord.runner_attached(coop, force_kill=fk_child, agent_id="child"),
    ):
        await coord.signal_force()

    assert coop_calls["n"] == 0                    # cooperative tier skipped
    assert killed == ["parent", "child"]           # whole stack hard-killed
    delivered = [(r["payload"]["agent_id"], r["payload"]["tier"],
                  r["payload"]["outcome"])
                 for r in log.records if r["type"] == "interrupt_delivered"]
    assert delivered == [("parent", "hard", "ok"), ("child", "hard", "ok")]
    esc = [r["payload"] for r in log.records if r["type"] == "interrupt_escalated"]
    assert esc == [{"reason": "second_gesture", "killed": ["parent", "child"]}]


@pytest.mark.anyio
async def test_signal_force_disarms_the_yield_watchdog(monkeypatch: Any) -> None:
    """signal_force calls note_yield() to disarm the watchdog the first gesture
    armed. After a FORCE, a subsequently-awaited _yield_watchdog must NOT fire its
    own interrupt_escalated{no_yield} — the only escalation is the FORCE's
    second_gesture record."""
    monkeypatch.setattr(_signals, "_YIELD_DEADLINE_S", 5.0)
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)

    async def fk() -> None: ...

    with coord.runner_attached(lambda: None, force_kill=fk, agent_id="a1"):
        # Arm the watchdog exactly as the first gesture would.
        coord._first_gesture_at = coord._now()
        coord._yielded = anyio.Event()
        await coord.signal_force()          # calls note_yield() -> disarms watchdog
        with anyio.fail_after(30):  # deadlock backstop only
            await coord._yield_watchdog()   # must return promptly, no no_yield

    reasons = [r["payload"]["reason"]
               for r in log.records if r["type"] == "interrupt_escalated"]
    assert reasons == ["second_gesture"]    # no no_yield escalation fired


class _WatchedEvent:
    """anyio.Event that records when a waiter actually blocks on it."""

    def __init__(self) -> None:
        self._evt = anyio.Event()
        self.waited = anyio.Event()

    def is_set(self) -> bool:
        return self._evt.is_set()

    def set(self) -> None:
        self._evt.set()

    async def wait(self) -> None:
        self.waited.set()
        await self._evt.wait()


@pytest.mark.anyio
async def test_yield_watchdog_window_starts_at_arming_not_at_the_gesture(
    monkeypatch: Any,
) -> None:
    """A delivery deferred to a later attach is armed long after the keypress. The
    watchdog must give that runner the full window from the arming — deriving the
    deadline from the gesture hard-kills it instantly, with zero grace."""
    # A window a saturated -n auto run cannot burn through before note_yield lands.
    # The green path never waits it out; the red path is unaffected, since the gesture
    # is already 100s stale on the injected clock.
    monkeypatch.setattr(_signals, "_YIELD_DEADLINE_S", 60.0)
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    coord._now = lambda: 1000.0        # inject the clock: the gesture was 100s ago
    coord._first_gesture_at = 900.0
    killed: list[str] = []

    async def fk() -> None:
        killed.append("r")

    evt = _WatchedEvent()
    coord._yielded = evt
    with coord.runner_attached(lambda: None, force_kill=fk, agent_id="r"):
        async with anyio.create_task_group() as tg:
            tg.start_soon(coord._yield_watchdog)
            # Resolves fast in BOTH states: green -> the watchdog blocked on the
            # yield event; red -> it escalated instead of ever waiting.
            await wait_until(lambda: evt.waited.is_set() or any(
                r["type"] == "interrupt_escalated" for r in log.records))
            coord.note_yield()      # the run yields inside the window -> clean disarm

    assert killed == []             # no zero-grace hard kill
    assert [r for r in log.records if r["type"] == "interrupt_escalated"] == []


async def _drain(tg: Any) -> None:
    """Let every task spawned before this one reach its first checkpoint: the spawner
    is FIFO, so a sentinel that has run proves any earlier task already started."""
    done = anyio.Event()

    async def _sentinel() -> None:
        done.set()

    tg.start_soon(_sentinel)
    await done.wait()


@pytest.mark.anyio
async def test_pending_gesture_is_delivered_at_the_next_attach(
    monkeypatch: Any,
) -> None:
    """A genuine gesture that finds the daemon's stack empty is held, then delivered
    when a runner attaches — with a full watchdog window measured from that attach,
    not an instant hard kill of a runner that never had a chance to yield."""
    # As above: a window the poll below cannot lose a race against under load.
    monkeypatch.setattr(_signals, "_YIELD_DEADLINE_S", 60.0)
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    t = {"now": 100.0}
    coord._now = lambda: t["now"]     # inject the clock (no real sleeping)
    interrupted: list[str] = []
    killed: list[str] = []

    async def _interrupt() -> None:
        interrupted.append("r")

    async def _force_kill() -> None:
        killed.append("r")

    async with anyio.create_task_group() as tg:
        coord.set_delivery_spawner(tg.start_soon)     # daemon-shaped coordinator
        await coord.signal_pause(reason="ctrl-c", via="frame")   # empty runner stack
        assert [r["type"] for r in log.records] == ["interrupt_requested"]
        assert interrupted == []                      # nothing to deliver to yet
        # Push the injected clock past the gesture's whole (patched) window. Inert
        # under the fixed watchdog, which no longer reads _now — it is a guard that
        # fails loudly if the deadline is ever re-derived from the gesture again.
        t["now"] = 161.0
        with coord.runner_attached(_interrupt, force_kill=_force_kill,
                                   agent_id="step:p/s"):
            await wait_until(lambda: any(
                r["type"] == "interrupt_delivered" for r in log.records))
            coord.note_yield()          # the run yields inside the window

    assert interrupted == ["r"]
    delivered = [(r["payload"]["agent_id"], r["payload"]["tier"], r["payload"]["outcome"])
                 for r in log.records if r["type"] == "interrupt_delivered"]
    assert delivered == [("step:p/s", "cooperative", "ok")]
    assert killed == []             # the fresh runner is not hard-killed
    assert [r for r in log.records if r["type"] == "interrupt_escalated"] == []


@pytest.mark.anyio
async def test_client_coordinator_holds_no_pending_delivery() -> None:
    """A spawner-less (client) coordinator behaves exactly as today: a gesture over an
    empty stack delivers nothing, and a later attach delivers nothing either."""
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    interrupted: list[str] = []

    async def _interrupt() -> None:
        interrupted.append("r")

    await coord.signal_pause(reason="ctrl-c", via="frame")   # empty stack, no spawner
    with coord.runner_attached(_interrupt, agent_id="r"):
        pass          # no spawner => any delivery would have been inline and observable

    assert interrupted == []
    assert [r["type"] for r in log.records] == ["interrupt_requested"]


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["compact", "model-override"])
async def test_side_effect_reasons_hold_no_pending_delivery(reason: str) -> None:
    """compact / model-override are side-effect pauses, not gestures: the empty-stack
    branch holds nothing for them, so a later attach is never interrupted."""
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    interrupted: list[str] = []
    killed: list[str] = []

    async def _interrupt() -> None:
        interrupted.append("r")

    async def _force_kill() -> None:
        killed.append("r")

    async with anyio.create_task_group() as tg:
        coord.set_delivery_spawner(tg.start_soon)
        await coord.signal_pause(reason=reason, via="frame")   # empty stack
        with coord.runner_attached(_interrupt, force_kill=_force_kill, agent_id="r"):
            await _drain(tg)

    assert interrupted == []
    assert killed == []
    assert [r["type"] for r in log.records] == ["interrupt_requested"]


@pytest.mark.anyio
@pytest.mark.parametrize("drop", ["reset", "clear_pause"])
async def test_reset_and_clear_pause_drop_a_pending_delivery(drop: str) -> None:
    """A held gesture must not outlive its round. The daemon's between-rounds reset()
    and the inline-resolved steer's clear_pause() (which clears should_pause, so no
    pause is pending any more) both drop it: the next attach is not interrupted."""
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    interrupted: list[str] = []
    killed: list[str] = []

    async def _interrupt() -> None:
        interrupted.append("r")

    async def _force_kill() -> None:
        killed.append("r")

    async with anyio.create_task_group() as tg:
        coord.set_delivery_spawner(tg.start_soon)
        await coord.signal_pause(reason="ctrl-c", via="frame")   # empty stack
        getattr(coord, drop)()
        with coord.runner_attached(_interrupt, force_kill=_force_kill, agent_id="r"):
            await _drain(tg)

    assert interrupted == []
    assert killed == []
    assert [r["type"] for r in log.records] == ["interrupt_requested"]
