# tests/test_anthropic_runner.py
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from imp.agents.runner import (
    AgentInvocation, ChangeContext, Message, TextBlock, ToolResultBlock, ToolUseBlock,
)
from imp.agents.transcript import Transcript
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicAgentRunner
from tests.anthropic_fakes import FakeAsyncAnthropic, response, text, tool_use


def _agent_def(tools=frozenset({"complete_step", "read_file"})) -> AgentDef:
    return AgentDef(
        name="t", description="", tools=tools, subagents=(),
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


def _inv(tmp_path: Path, tools=None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(tools or frozenset({"complete_step", "read_file"})),
        provider_name="anthropic", model_name="claude-sonnet-4-6",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            change_id=1, project_root=tmp_path, artifacts_dir=tmp_path / "art",
        ),
        interactive=False, transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",), mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
    )


@pytest.mark.anyio
async def test_runs_to_complete_step(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([
        response(
            text("calling complete_step"),
            tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
        ),
    ])

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda: fake,
    )
    events = []
    async for ev in runner.run(_inv(tmp_path)):
        events.append(ev)

    kinds = [e["type"] for e in events]
    assert "agent_message" in kinds
    assert kinds[-1] == "agent_output"
    assert events[-1]["payload"] == {"status": "Success", "note": "ok"}


@pytest.mark.anyio
async def test_dispatches_read_file_then_completes(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    (tmp_path / "data.txt").write_text("payload")
    fake = FakeAsyncAnthropic([
        response(tool_use("u1", "read_file", {"path": "/project/data.txt"})),
        response(
            tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
        ),
    ])

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    events = [e async for e in runner.run(_inv(tmp_path))]
    kinds = [e["type"] for e in events]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    # Tool result was passed back to the LLM in the next .create() call.
    second_call = fake.messages.calls[1]
    last_msg = second_call["messages"][-1]
    assert last_msg["role"] == "user"
    # The user message carries a tool_result block.
    assert any(
        block.get("type") == "tool_result" for block in last_msg["content"]
    )


@pytest.mark.anyio
async def test_transcript_persists_each_turn(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    fake = FakeAsyncAnthropic([
        response(
            tool_use("c1", "complete_step", {"status": "Success", "note": "n"}),
        ),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    inv = _inv(tmp_path)
    async for _ in runner.run(inv):
        pass
    assert (inv.transcript_dir / "transcript.jsonl").exists()
    lines = (inv.transcript_dir / "transcript.jsonl").read_text().splitlines()
    # at least: initial user + assistant
    assert len(lines) >= 2


@pytest.mark.anyio
async def test_resume_uses_existing_transcript(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    # Seed a transcript with: user msg → assistant tool_use → user tool_result
    inv = _inv(tmp_path)
    transcript = Transcript(inv.transcript_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="resume me"),)))
    transcript.append(Message(role="assistant", content=(
        ToolUseBlock(id="u1", name="read_file", input={"path": "/project/x"}),
    )))
    transcript.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="u1", content="prior contents"),
    )))

    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"status": "Success", "note": "resumed"})),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"
    assert events[-1]["payload"]["note"] == "resumed"

    # The single LLM call should have been issued with all three prior messages.
    only_call = fake.messages.calls[0]
    assert len(only_call["messages"]) == 3


@pytest.fixture
def anyio_backend():
    return "asyncio"
