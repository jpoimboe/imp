from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers.claude import ClaudeAgentRunner
from tests.async_wait import HANG_BACKSTOP, wait_until
from tests.cc_fakes import (
    GatedClient,
    assistant,
    message_start,
    result,
    text,
    tool_result,
    tool_use,
    user,
)

# 160k handoff limit: a 170k message_start crosses it.
MODEL = "claude-sonnet-4-6"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


def _inv(tmp_path: Path, *, agent_id: str = "step:p/s") -> AgentInvocation:
    (tmp_path / "art").mkdir(exist_ok=True)
    return AgentInvocation(
        agent_def=_agent_def(), provider_name="claude",
        model_name=MODEL, system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "wf"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id=agent_id, event_log=InMemoryEventLog())


def _complete() -> Any:
    return assistant(text("done"),
                     tool_use("c1", "complete_step", {"status": "Success"}))


def _interrupted_result() -> Any:
    return result(subtype="error_during_execution", is_error=True)


def _runner(scripted: list[Any], *, stall_interrupt: bool = False,
            user_input_fn: Any = None,
            ) -> tuple[ClaudeAgentRunner, dict[str, GatedClient]]:
    holder: dict[str, GatedClient] = {}

    def factory(opts: Any) -> GatedClient:
        holder["c"] = GatedClient(opts, scripted_messages=scripted,
                                  stall_interrupt=stall_interrupt)
        return holder["c"]

    return ClaudeAgentRunner(model_name=MODEL, client_factory=factory,
                             user_input_fn=user_input_fn), holder


class _Run:
    """Drives runner.run(); parks after the first `hold_on` event until finish()."""

    def __init__(self, runner: ClaudeAgentRunner, inv: AgentInvocation,
                 *, hold_on: str | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.held = asyncio.Event()
        self._release = asyncio.Event()
        self._hold_on = hold_on
        self.task = asyncio.create_task(self._drive(runner, inv))

    async def _drive(self, runner: ClaudeAgentRunner, inv: AgentInvocation) -> None:
        async with contextlib.aclosing(runner.run(inv)) as run:
            async for ev in run:
                self.events.append(ev)
                if ev["type"] == self._hold_on and not self.held.is_set():
                    self.held.set()
                    await self._release.wait()

    async def finish(self) -> list[dict[str, Any]]:
        self._release.set()
        with anyio.fail_after(HANG_BACKSTOP):
            await self.task
        return self.events


async def _parked_at(holder: dict[str, GatedClient], gate: asyncio.Event) -> GatedClient:
    await wait_until(lambda: "c" in holder and holder["c"].waiting is gate)
    return holder["c"]


async def _interrupt_in_flight(runner: ClaudeAgentRunner, client: GatedClient,
                               *, sdk_interrupts: int) -> asyncio.Task[None]:
    task = asyncio.create_task(runner.interrupt())
    await wait_until(lambda: task.done() or client.interrupts == sdk_interrupts)
    assert not task.done(), "interrupt() returned while a turn was in flight"
    return task


async def _acked(aw: Awaitable[None]) -> None:
    with anyio.fail_after(HANG_BACKSTOP):
        await aw


@pytest.mark.anyio
async def test_steer_acks_then_a_second_interrupt_waits_for_its_own_turn(
        tmp_path: Path) -> None:
    g1, g2, g3 = asyncio.Event(), asyncio.Event(), asyncio.Event()
    runner, holder = _runner(
        [g1, _interrupted_result(), g2, _complete(), g3, _interrupted_result()],
        user_input_fn=lambda _prompt: "redirect")
    run = _Run(runner, _inv(tmp_path), hold_on="agent_result")

    client = await _parked_at(holder, g1)
    first = await _interrupt_in_flight(runner, client, sdk_interrupts=1)
    g1.set()
    await _parked_at(holder, g2)
    await _acked(first)
    assert client.queries[-1] == "redirect"

    second = await _interrupt_in_flight(runner, client, sdk_interrupts=2)
    g2.set()
    await _parked_at(holder, g3)
    assert not second.done(), "a stale signal from the steer acked a mid-turn interrupt"
    g3.set()
    await wait_until(run.held.is_set)
    await _acked(second)
    # complete branch: the unconsumed interrupt became a cancel, no re-query
    assert runner._cancel is True
    assert len(client.queries) == 2
    events = await run.finish()
    assert events[-1]["type"] == "agent_result"
    assert events[-1]["payload"] == {"status": "Success"}


@pytest.mark.anyio
async def test_interrupt_racing_a_natural_finish_acks_and_keeps_the_result(
        tmp_path: Path) -> None:
    gate = asyncio.Event()
    runner, holder = _runner([_complete(), gate, result(subtype="success")])
    run = _Run(runner, _inv(tmp_path), hold_on="agent_result")

    client = await _parked_at(holder, gate)
    await wait_until(lambda: client.interrupts == 1)  # complete_step's own preempt
    it = await _interrupt_in_flight(runner, client, sdk_interrupts=2)
    gate.set()
    await wait_until(run.held.is_set)
    await _acked(it)
    assert runner._cancel is True
    events = await run.finish()
    assert events[-1]["type"] == "agent_result"
    assert events[-1]["payload"] == {"status": "Success"}
    assert len(client.queries) == 1


@pytest.mark.anyio
async def test_interrupt_after_the_final_result_acks_without_an_sdk_interrupt(
        tmp_path: Path) -> None:
    runner, holder = _runner([_complete(), result(subtype="success")])
    run = _Run(runner, _inv(tmp_path), hold_on="agent_result")

    await wait_until(run.held.is_set)
    client = holder["c"]
    assert not client.closed
    sent = client.interrupts
    await _acked(runner.interrupt())
    assert client.interrupts == sent
    assert runner._cancel is True
    events = await run.finish()
    assert events[-1]["type"] == "agent_result"


@pytest.mark.anyio
async def test_unanswered_sdk_interrupt_does_not_hold_the_ack(tmp_path: Path) -> None:
    gate = asyncio.Event()
    runner, holder = _runner([_complete(), gate, result(subtype="success")],
                             stall_interrupt=True)
    run = _Run(runner, _inv(tmp_path), hold_on="agent_result")

    client = await _parked_at(holder, gate)
    await wait_until(lambda: client.interrupts == 1)
    it = await _interrupt_in_flight(runner, client, sdk_interrupts=2)
    gate.set()
    await wait_until(run.held.is_set)
    await _acked(it)
    await run.finish()
    assert len(client.interrupt_tasks) == 2
    assert all(t.done() and t.cancelled() for t in client.interrupt_tasks)


@pytest.mark.anyio
async def test_interrupt_converted_by_handoff_suppresses_the_requery(
        tmp_path: Path) -> None:
    gate = asyncio.Event()
    runner, holder = _runner([
        message_start("m1", {"input_tokens": 170_000}),
        assistant(text("working")),
        gate,
        _interrupted_result(),
    ])
    inv = _inv(tmp_path)
    run = _Run(runner, inv, hold_on="error")

    client = await _parked_at(holder, gate)
    await wait_until(lambda: client.interrupts == 1)  # the handoff's own interrupt
    it = await _interrupt_in_flight(runner, client, sdk_interrupts=2)
    gate.set()
    await wait_until(run.held.is_set)
    await _acked(it)
    events = await run.finish()
    assert len(client.queries) == 1
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["category"] == "config"
    assert events[-1]["payload"]["kind"] == "interrupted"
    assert not [e for e in inv.event_log.read()
                if e["type"] == "engine_message"
                and e["payload"]["subtype"] == "continuation"]


@pytest.mark.anyio
async def test_stream_closing_without_a_result_acks_at_teardown(tmp_path: Path) -> None:
    gate = asyncio.Event()
    runner, holder = _runner([gate])
    run = _Run(runner, _inv(tmp_path))

    client = await _parked_at(holder, gate)
    it = await _interrupt_in_flight(runner, client, sdk_interrupts=1)
    gate.set()
    events = await run.finish()
    await _acked(it)
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["category"] == "network"
    assert events[-1]["payload"]["kind"] == "no_result_message"


@pytest.mark.anyio
async def test_interrupt_while_parked_on_save_acks_without_an_sdk_interrupt(
        tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator

    coord = InterruptCoordinator()
    saves: list[Any] = []
    coord.set_save_hook(saves.append)
    runner, holder = _runner([
        assistant(tool_use("s1", "save_and_exit", {})),
        user(tool_result("s1", "Paused. The session closes at the end of this turn.")),
    ])
    run = _Run(runner, replace(_inv(tmp_path), coordinator=coord))

    await wait_until(lambda: bool(saves))
    it = asyncio.create_task(runner.interrupt())
    await wait_until(lambda: it.done() or holder["c"].interrupts > 0)
    assert it.done()
    assert holder["c"].interrupts == 0
    assert runner._cancel is True
    run.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run.task


@pytest.mark.anyio
async def test_ctrl_c_during_a_claude_subagent_turn_end_never_escalates(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from imp.cli import signals as _signals
    from imp.cli.signals import InterruptCoordinator
    from imp.engine.agent_run import drive_invocation
    from tests.test_interrupt_events import RecordingLog

    # Backstop only, under pytest's 60s timeout: a regression journals "timeout".
    monkeypatch.setattr(_signals, "_COOPERATIVE_DEADLINE_S", 30.0)
    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    gate = asyncio.Event()
    runner, holder = _runner([_complete(), gate, result(subtype="success")])
    sub_inv = replace(_inv(tmp_path, agent_id="sub:code-reviewer/1"), coordinator=coord)
    parent_calls: list[str] = []

    async def parent_interrupt() -> None:
        parent_calls.append("interrupt")

    async def parent_kill() -> None:
        parent_calls.append("force_kill")

    with coord.runner_attached(parent_interrupt, force_kill=parent_kill,
                               agent_id="step:p/s"):
        drive = asyncio.create_task(drive_invocation(
            sub_inv, runner, coordinator=coord, log=None, emit_events=False))
        client = await _parked_at(holder, gate)
        await wait_until(lambda: client.interrupts == 1)
        deliver = asyncio.create_task(coord._deliver_interrupt())
        await wait_until(lambda: client.interrupts == 2)
        gate.set()
        with anyio.fail_after(HANG_BACKSTOP):
            await deliver
            outcome = await drive

    delivered = [(r["payload"]["agent_id"], r["payload"]["tier"], r["payload"]["outcome"])
                 for r in log.records if r["type"] == "interrupt_delivered"]
    assert delivered == [
        ("sub:code-reviewer/1", "cooperative", "ok"),
        ("step:p/s", "cooperative", "ok"),
    ]
    assert not any(r["type"] == "interrupt_escalated" for r in log.records)
    assert parent_calls == ["interrupt"]
    assert outcome.output == {"status": "Success"}


@pytest.mark.anyio
async def test_interrupt_racing_a_bare_turn_end_skips_the_continuation(
        tmp_path: Path) -> None:
    gate = asyncio.Event()
    calls: list[str] = []

    def cont(last_text: str) -> tuple[str, bool]:
        calls.append(last_text)
        return "keep going", False

    runner, holder = _runner([gate, assistant(text("thinking aloud")),
                              result(subtype="success")])
    run = _Run(runner, replace(_inv(tmp_path), continuation_fn=cont))

    client = await _parked_at(holder, gate)
    it = await _interrupt_in_flight(runner, client, sdk_interrupts=1)
    gate.set()
    events = await run.finish()
    await _acked(it)
    assert calls == []
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["kind"] == "interrupted"
    assert len(client.queries) == 1
