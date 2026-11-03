from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from claude_agent_sdk import ClaudeSDKClient, Transport

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


_ASSISTANT_COMPLETING = {
    "type": "assistant", "session_id": "s", "uuid": "a1",
    "message": {
        "id": "msg_1", "role": "assistant", "model": "claude-sonnet-4-6",
        "content": [
            {"type": "text", "text": "done"},
            {"type": "tool_use", "id": "c1", "name": "mcp__imp__complete_step",
             "input": {"status": "Success"}}]}}

_RESULT_INTERRUPTED = {
    "type": "result", "subtype": "error_during_execution", "is_error": True,
    "duration_ms": 0, "duration_api_ms": 0, "num_turns": 1, "session_id": "s"}


class InterruptAfterFloodTransport(Transport):
    """A fake CLI that answers `interrupt` only after a stream_event flood larger
    than the SDK's 100-message reader buffer: the live ordering that deadlocks
    an inline `await client.interrupt()`."""

    FLOOD = 150

    def __init__(self) -> None:
        self._out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.writes: list[dict[str, Any]] = []

    async def connect(self) -> None:
        pass

    async def write(self, data: str) -> None:
        msg = json.loads(data)
        self.writes.append(msg)
        if msg["type"] == "user":
            self._out.put_nowait(_ASSISTANT_COMPLETING)
            return
        if msg["type"] != "control_request":
            return
        rid = msg["request_id"]
        is_interrupt = msg["request"]["subtype"] == "interrupt"
        if is_interrupt:
            for i in range(self.FLOOD):
                self._out.put_nowait({
                    "type": "stream_event", "uuid": f"e{i}", "session_id": "s",
                    "event": {"type": "content_block_delta", "index": 0,
                              "delta": {"type": "text_delta", "text": "x"}}})
        self._out.put_nowait({"type": "control_response", "response": {
            "subtype": "success", "request_id": rid, "response": {}}})
        if is_interrupt:
            self._out.put_nowait(_RESULT_INTERRUPTED)

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            frame = await self._out.get()
            yield frame

    async def close(self) -> None:
        pass

    def is_ready(self) -> bool:
        return True

    async def end_input(self) -> None:
        pass


@pytest.mark.anyio
async def test_interrupt_reply_behind_a_full_reader_buffer_does_not_deadlock(
        tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    transport = InterruptAfterFloodTransport()
    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda opts: ClaudeSDKClient(opts, transport=transport))

    # Well under the SDK's 60s control-request timeout an inline await would hit.
    with anyio.fail_after(10):
        async with contextlib.aclosing(runner.run(_inv(tmp_path, log))) as run:
            events = [ev async for ev in run]

    assert events[-1]["type"] == "agent_result"
    assert events[-1]["payload"] == {"status": "Success"}
    assert [w["request"]["subtype"] for w in transport.writes
            if w["type"] == "control_request"] == ["initialize", "interrupt"]
