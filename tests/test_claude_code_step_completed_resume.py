from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

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
from imp.agents.transcript import RESUME_REDO_NUDGE, Transcript
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers import claude_code
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use


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
        agent_def=_agent_def(), provider_name="claude_code",
        model_name="claude-sonnet-4-5", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "wf"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log)


def _seed_incident_transcript(inv: AgentInvocation) -> Transcript:
    """Tail: resolved complete_step masked by one trailing assistant prose turn."""
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="cs", name="complete_step", input={"status": "Success"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="cs", content="ok", is_error=False),)))
    t.append(Message(role="assistant", content=(TextBlock(text="All done — summary."),)))
    return t


@pytest.mark.anyio
async def test_step_completed_rework_delivers_feedback_as_user_turn_natively(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """goto(resume, rework) over a STEP_COMPLETED tail: the retry_feedback rides
    as a trailing USER TextBlock (never a rewritten is_error result), no
    awaiting_user_input is solicited, and the resume takes the NATIVE path
    (single-turn delta, no replay runner_warning)."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    inv = replace(inv, balance_on_resume=True, retry_feedback="Redo: fix the claim.")
    t = _seed_incident_transcript(inv)

    scripted = [
        assistant(text("Reworked."),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]

    # Spy on the first-query history stream to prove the native single-turn delta.
    streamed: list[list[Message]] = []
    _orig_stream = claude_code._stream_history
    monkeypatch.setattr(
        claude_code, "_stream_history",
        lambda messages: (streamed.append(list(messages)), _orig_stream(messages))[1])

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    assert events[-1]["type"] == "agent_output"
    log_types = [e["type"] for e in log.read()]
    # Harm 2 killed: no phantom human solicit.
    assert "awaiting_user_input" not in log_types
    # Native path: no stream-json replay fallback warning.
    assert "runner_warning" not in log_types
    # The handback is a plain user TextBlock, NOT a rewritten is_error result.
    loaded = t.load()
    assert loaded[4].role == "user"
    block = loaded[4].content[0]
    assert isinstance(block, TextBlock)
    assert not isinstance(block, ToolResultBlock)
    assert block.text == "Redo: fix the claim."
    # Native single-turn delta: the first query streamed exactly the delivered
    # handback turn, not the whole transcript a replay fallback would send.
    assert len(streamed) == 1
    assert len(streamed[0]) == 1 and streamed[0][0].role == "user"
    assert streamed[0][0].content[0].text == "Redo: fix the claim."


@pytest.mark.anyio
async def test_step_completed_bare_goto_delivers_redo_nudge_natively(
        tmp_path: Path) -> None:
    """goto(resume) with no feedback over a STEP_COMPLETED tail: RESUME_REDO_NUDGE
    is delivered as the trailing user turn, no solicit, and the resume drives the
    next LLM turn to completion."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    inv = replace(inv, balance_on_resume=True)  # retry_feedback stays None
    t = _seed_incident_transcript(inv)

    scripted = [
        assistant(text("Redone."),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    assert events[-1]["type"] == "agent_output"
    log_types = [e["type"] for e in log.read()]
    assert "awaiting_user_input" not in log_types
    assert "runner_warning" not in log_types
    loaded = t.load()
    assert loaded[4].role == "user"
    block = loaded[4].content[0]
    assert isinstance(block, TextBlock)
    assert not isinstance(block, ToolResultBlock)
    assert block.text == RESUME_REDO_NUDGE
