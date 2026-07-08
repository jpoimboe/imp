from __future__ import annotations

from typing import Any

import anyio
import pytest

from imp.cli import signals as _signals
from imp.cli.signals import InterruptCoordinator
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
        with anyio.fail_after(1):
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
        with anyio.fail_after(1):
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
        with anyio.fail_after(1):
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
        with anyio.fail_after(1):
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
        with anyio.fail_after(1):
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
        with anyio.fail_after(1):
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
        with anyio.fail_after(1):
            await coord._yield_watchdog()   # must return promptly, no no_yield

    reasons = [r["payload"]["reason"]
               for r in log.records if r["type"] == "interrupt_escalated"]
    assert reasons == ["second_gesture"]    # no no_yield escalation fired
