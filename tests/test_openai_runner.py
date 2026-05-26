# tests/test_openai_runner.py
from __future__ import annotations

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
from imp.agents.transcript import Transcript
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.openai import OpenAIAgentRunner
from tests.openai_fakes import FakeAsyncOpenAI, completion, message, tool_call


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
        provider_name="openai", model_name="gpt-4o-mini",
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
    fake = FakeAsyncOpenAI([
        completion(message(
            content="calling complete_step",
            tool_calls=[tool_call("c1", "complete_step",
                                  {"status": "Success", "note": "ok"})],
        )),
    ])

    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
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
async def test_resume_from_existing_transcript(tmp_path: Path) -> None:
    """If transcript.jsonl exists, runner reloads it and continues mid-step."""
    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    (inv.transcript_dir).mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(
        role="assistant",
        content=(
            TextBlock(text="reading first…"),
            ToolUseBlock(id="t0", name="read_file", input={"path": "/project/x"}),
        ),
    ))
    t.append(Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t0", content="some content"),),
    ))

    # The runner sees the SEND_TO_LLM resume state; next call is the agent's
    # next turn — and it completes the step.
    fake = FakeAsyncOpenAI([
        completion(message(
            content="done",
            tool_calls=[tool_call("c1", "complete_step", {"status": "Success"})],
        )),
    ])
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
    )

    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    kinds = [e["type"] for e in events]
    assert kinds[-1] == "agent_output"
    # Initial-messages were NOT re-appended because transcript was non-empty.
    loaded = t.load()
    # Original 3 + new assistant turn = 4 messages.
    assert len(loaded) == 4


@pytest.mark.anyio
async def test_refusal_emits_runner_warning(tmp_path: Path) -> None:
    """OpenAI's `refusal` field is not part of imp Message; should warn, not fail."""
    (tmp_path / "art").mkdir()

    fake = FakeAsyncOpenAI([
        # First response: only a refusal. No tool_calls → runner falls through
        # to "agent finished without complete_step" error (autonomous mode).
        completion(message(content=None, refusal="I can't help with that.")),
    ])
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
    )
    events = []
    async for ev in runner.run(_inv(tmp_path)):
        events.append(ev)

    warnings = [e for e in events if e["type"] == "runner_warning"]
    assert any("refusal" in w["payload"]["message"] for w in warnings)
    # No fatal — the final event is `error` (no complete_step), not `error` for
    # an unknown block.
    final = events[-1]
    assert final["type"] == "error"
    assert final["payload"]["kind"] == "no_complete_step"
