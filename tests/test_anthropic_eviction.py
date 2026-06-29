# tests/test_anthropic_eviction.py
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
from imp.providers.anthropic import AnthropicAgentRunner
from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

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
        provider_name="anthropic", model_name="claude-sonnet-4-6",
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


def _result_bodies(call: dict) -> dict[str, str]:
    return {blk["tool_use_id"]: blk["content"]
            for msg in call["messages"] for blk in msg["content"]
            if blk.get("type") == "tool_result"}


@pytest.mark.anyio
async def test_cold_resume_evicts_old_compactable_body(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    _seed_events(tmp_path, "2000-01-01T00:00:00+00:00")
    fake = FakeAsyncAnthropic([response(
        tool_use("done", "complete_step", {"status": "Success", "note": "ok"}))])
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    inv = _inv(tmp_path, event_log=InMemoryEventLog())
    async for _ in runner.run(inv):
        pass
    bodies = _result_bodies(fake.messages.calls[0])
    assert bodies["u0"] == PLACEHOLDER
    assert bodies["u4"] == "body-4"
    names = {blk["id"]: blk["name"]
             for msg in fake.messages.calls[0]["messages"]
             for blk in msg["content"] if blk.get("type") == "tool_use"}
    assert names["u0"] == "read_file"
    transcript = (inv.transcript_dir / "transcript.jsonl").read_text()
    assert "body-0" in transcript
    assert PLACEHOLDER not in transcript


@pytest.mark.anyio
async def test_warm_resume_sends_full_bodies(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    _seed_events(tmp_path, datetime.now(UTC).isoformat())
    fake = FakeAsyncAnthropic([response(
        tool_use("done", "complete_step", {"status": "Success", "note": "ok"}))])
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path, event_log=InMemoryEventLog())):
        pass
    bodies = _result_bodies(fake.messages.calls[0])
    assert bodies == {f"u{t}": f"body-{t}" for t in range(5)}


@pytest.mark.anyio
async def test_evicted_set_frozen_across_tool_rounds(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    _seed_events(tmp_path, "2000-01-01T00:00:00+00:00")
    fake = FakeAsyncAnthropic([
        response(tool_use("r1", "read_file", {"path": "nope.txt"})),
        response(tool_use("done", "complete_step",
                          {"status": "Success", "note": "ok"})),
    ])
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path, event_log=InMemoryEventLog())):
        pass
    # Round 1 adds an assistant turn, so a recomputed window would now also
    # evict u1; the frozen set (computed once = {u0}) leaves u1 full.
    bodies = _result_bodies(fake.messages.calls[1])
    assert bodies["u0"] == PLACEHOLDER
    assert bodies["u1"] == "body-1"
