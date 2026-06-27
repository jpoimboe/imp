"""Classified terminal-error turns are rolled back so retries re-query."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner, _roll_back_error_turn
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use


def test_roll_back_removes_trailing_assistant_turn(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    seed = Message(role="user", content=(TextBlock(text="go"),))
    err = Message(
        role="assistant",
        content=(TextBlock(text="Failed to authenticate. API Error: 401"),),
    )
    messages = [seed, err]
    t.append(seed)
    t.append(err)

    _roll_back_error_turn(messages, t)

    # In-memory list ends on the prior user turn.
    assert [m.role for m in messages] == ["user"]
    block = messages[0].content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "go"

    # Persisted transcript is truncated to match.
    loaded = t.load()
    assert [m.role for m in loaded] == ["user"]
    loaded_block = loaded[0].content[0]
    assert isinstance(loaded_block, TextBlock)
    assert loaded_block.text == "go"


def test_roll_back_removes_only_most_recent_assistant_turn(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    user1 = Message(role="user", content=(TextBlock(text="go"),))
    a1 = Message(role="assistant", content=(TextBlock(text="first assistant turn"),))
    user2 = Message(role="user", content=(TextBlock(text="continue"),))
    a2 = Message(role="assistant", content=(TextBlock(text="second assistant turn - error"),))
    messages = [user1, a1, user2, a2]
    for m in messages:
        t.append(m)

    _roll_back_error_turn(messages, t)

    # Only the last assistant turn (A2) is removed; A1 is intact.
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    a1_block = messages[1].content[0]
    assert isinstance(a1_block, TextBlock)
    assert a1_block.text == "first assistant turn"

    # Persisted transcript matches in-memory list.
    loaded = t.load()
    assert [m.role for m in loaded] == ["user", "assistant", "user"]
    loaded_a1_block = loaded[1].content[0]
    assert isinstance(loaded_a1_block, TextBlock)
    assert loaded_a1_block.text == "first assistant turn"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}), subagents=(),
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


def _inv(tmp_path: Path, log: InMemoryEventLog) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="claude_code", model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
    )


@pytest.mark.anyio
async def test_termination_401_rolls_back_and_keeps_llm_event(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)

    scripted = [
        assistant(text(
            "Failed to authenticate. API Error: 401 Invalid authentication credentials")),
        result(is_error=True, api_error_status=401),
    ]

    def factory(_opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(_opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1, events
    assert errs[0]["payload"]["category"] == "auth"

    # AC1: transcript rolled back to the prior user turn -> SEND_TO_LLM on resume.
    loaded = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    assert [m.role for m in loaded] == ["user"]
    assert classify_resume(loaded) is ResumeState.SEND_TO_LLM

    # AC4: the error turn's llm_response event remains queryable.
    assert any(e["type"] == "llm_response" for e in log.read())


@pytest.mark.anyio
async def test_classified_text_branch_rolls_back(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)

    # No is_error / api_error_status -> _termination_from_result returns None,
    # so the auth-prose turn is caught by the _classify_agent_text branch.
    scripted = [
        assistant(text("Invalid authentication credentials")),
        result(),
    ]

    def factory(_opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(_opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1, events
    assert errs[0]["payload"]["category"] == "auth"

    loaded = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    assert [m.role for m in loaded] == ["user"]
    assert classify_resume(loaded) is ResumeState.SEND_TO_LLM
