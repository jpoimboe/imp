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
