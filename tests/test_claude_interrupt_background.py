from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers.claude import ClaudeAgentRunner
from tests.cc_fakes import (
    FakeClaudeSDKClient,
    assistant,
    message_start,
    result,
    text,
    tool_use,
)


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


def _inv(tmp_path: Path, log: InMemoryEventLog) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(), provider_name="claude",
        model_name="claude-sonnet-4-6", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "wf"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log)


class StalledInterruptClient(FakeClaudeSDKClient):
    """interrupt() never gets a reply; a context-usage probe is a tripwire."""

    def __init__(self, opts: Any, *, scripted_messages: list[Any]) -> None:
        super().__init__(opts, scripted_messages=scripted_messages)
        self.interrupt_tasks: list[asyncio.Task[Any]] = []
        self.probes: list[str] = []

    async def interrupt(self) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.interrupt_tasks.append(task)
        self.interrupts += 1
        await asyncio.Event().wait()

    async def get_context_usage(self) -> Any:
        self.probes.append("get_context_usage")
        raise AssertionError("get_context_usage must not be called")


async def _drive(tmp_path: Path, scripted: list[Any]) -> tuple[
        list[dict[str, Any]], StalledInterruptClient]:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    holder: dict[str, StalledInterruptClient] = {}

    def factory(opts: Any) -> StalledInterruptClient:
        holder["client"] = StalledInterruptClient(opts, scripted_messages=scripted)
        return holder["client"]

    runner = ClaudeAgentRunner(model_name="claude-sonnet-4-6", client_factory=factory)
    with anyio.fail_after(5):
        events = [ev async for ev in runner.run(_inv(tmp_path, log))]
    return events, holder["client"]


@pytest.mark.anyio
async def test_stalled_complete_step_interrupt_does_not_block_the_step(
        tmp_path: Path) -> None:
    events, client = await _drive(tmp_path, [
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(subtype="error_during_execution", is_error=True),
    ])

    assert [e["type"] for e in events if e["type"] != "tool_heartbeat"] == [
        "agent_result"]
    assert events[-1]["payload"] == {"status": "Success"}
    assert client.probes == []
    assert len(client.interrupt_tasks) == 1
    assert all(t.done() and t.cancelled() for t in client.interrupt_tasks)


@pytest.mark.anyio
async def test_stalled_handoff_and_complete_step_interrupts_both_cancelled(
        tmp_path: Path) -> None:
    # 170k crosses claude-sonnet-4-6's 160k handoff limit: "working" fires the
    # handoff interrupt, then complete_step fires a second one in the same session.
    events, client = await _drive(tmp_path, [
        message_start("m1", {"input_tokens": 170_000}),
        assistant(text("working")),
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(subtype="error_during_execution", is_error=True),
    ])

    assert events[-1]["type"] == "agent_result"
    assert events[-1]["payload"] == {"status": "Success"}
    assert client.probes == []
    assert client.interrupts == 2
    assert len(set(client.interrupt_tasks)) == 2
    assert all(t.done() and t.cancelled() for t in client.interrupt_tasks)
