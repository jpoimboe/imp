# tests/test_openai_eviction.py
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.openai import OpenAIAgentRunner
from tests.openai_fakes import FakeResponsesClient, resp_function_call, resp_response

PLACEHOLDER = "[old tool result cleared]"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step", "read_file"}), subagents=(),
        output_schema=OutputSchema(
            fields=(
                OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),
                OutputField(name="note", type="string", required=False),
            ),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _history() -> tuple[Message, ...]:
    msgs: list[Message] = [Message(role="user", content=(TextBlock(text="go"),))]
    for t in range(5):  # assistant turns 0..4
        msgs.append(Message(role="assistant", content=(
            ToolUseBlock(id=f"u{t}", name="read_file", input={"path": "x"}),)))
        msgs.append(Message(role="user", content=(
            ToolResultBlock(tool_use_id=f"u{t}", content=f"body-{t}"),)))
    return tuple(msgs)


def _inv(tmp_path: Path, *, event_log: InMemoryEventLog) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="openai", model_name="gpt-4o-mini",
        system_prompt="be terse",
        initial_messages=_history(),
        change_context=ChangeContext(
            id="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "wf"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=event_log,
    )


def _seed_events(tmp_path: Path, timestamp: str) -> None:
    (tmp_path / "events.jsonl").write_text(
        json.dumps({"type": "llm_response", "timestamp": timestamp}) + "\n")


def _outputs(call: dict) -> dict[str, str]:
    return {item["call_id"]: item["output"]
            for item in call["input"]
            if item.get("type") == "function_call_output"}


@pytest.mark.anyio
async def test_cold_resume_evicts_old_compactable_output(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    _seed_events(tmp_path, "2000-01-01T00:00:00+00:00")
    fake = FakeResponsesClient([resp_response(
        resp_function_call("done", "complete_step",
                           '{"status": "Success", "note": "ok"}'))])
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    inv = _inv(tmp_path, event_log=InMemoryEventLog())
    async for _ in runner.run(inv):
        pass
    outputs = _outputs(fake.responses.calls[0])
    assert outputs["u0"] == PLACEHOLDER
    assert outputs["u4"] == "body-4"
    names = {item["call_id"]: item["name"]
             for item in fake.responses.calls[0]["input"]
             if item.get("type") == "function_call"}
    assert names["u0"] == "read_file"
    transcript = (inv.transcript_dir / "transcript.jsonl").read_text()
    assert "body-0" in transcript
    assert PLACEHOLDER not in transcript


@pytest.mark.anyio
async def test_warm_resume_sends_full_outputs(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    _seed_events(tmp_path, datetime.now(UTC).isoformat())
    fake = FakeResponsesClient([resp_response(
        resp_function_call("done", "complete_step",
                           '{"status": "Success", "note": "ok"}'))])
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path, event_log=InMemoryEventLog())):
        pass
    outputs = _outputs(fake.responses.calls[0])
    assert outputs == {f"u{t}": f"body-{t}" for t in range(5)}


@pytest.mark.anyio
async def test_evicted_set_frozen_across_tool_rounds(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    _seed_events(tmp_path, "2000-01-01T00:00:00+00:00")
    fake = FakeResponsesClient([
        resp_response(resp_function_call("r1", "read_file",
                                         '{"path": "nope.txt"}')),
        resp_response(resp_function_call("done", "complete_step",
                                         '{"status": "Success", "note": "ok"}')),
    ])
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini",
                               client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path, event_log=InMemoryEventLog())):
        pass
    outputs = _outputs(fake.responses.calls[1])
    assert outputs["u0"] == PLACEHOLDER
    assert outputs["u1"] == "body-1"
