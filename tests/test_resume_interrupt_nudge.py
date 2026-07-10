# tests/test_resume_interrupt_nudge.py
"""A no-message resume of a Ctrl-C-interrupted step must CONTINUE the step.

A mid-tool interrupt repairs the dangling call with a synthetic tool_result, so
the resume classifies as SEND_TO_LLM (never AWAITING_USER) and the
_resolve_wait_reply continuation nudge can't reach it. The providers append the
same nudge as the trailing user turn when invocation.resume_should_continue is
set and no human interject already drove continuation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.engine.agent_run import _CONTINUATION_NUDGE


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def() -> Any:
    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc

    return AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step", "read_file"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


class _Coord:
    """Drains queued interjects in order; empty by default."""

    def __init__(self, queued: list[str] | None = None) -> None:
        self._q = list(queued or [])

    def drain_interject(self) -> str | None:
        return self._q.pop(0) if self._q else None

    def reset(self) -> None:
        pass

    def begin_tool_watchdog(self) -> None:
        pass

    def end_tool_watchdog(self) -> None:
        pass

    def note_activity(self) -> None:
        pass


def _seed_send_to_llm_transcript(inv_dir: Path) -> None:
    """A transcript ending on a user tool_result — SEND_TO_LLM, the shape a
    repaired mid-tool interrupt presents on resume."""
    from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    transcript = Transcript(inv_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="resume me"),)))
    transcript.append(Message(role="assistant", content=(
        ToolUseBlock(id="u1", name="read_file", input={"path": "/project/x"}),
    )))
    transcript.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="u1", content="prior contents"),
    )))


def _seed_text_user_tail_transcript(inv_dir: Path) -> None:
    """A transcript ending on a text-bearing user turn — an un-answered human
    reply pending at a gate: user 'ignore' is the tail."""
    from imp.agents.runner import Message, TextBlock
    from imp.agents.transcript import Transcript

    transcript = Transcript(inv_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="go"),)))
    transcript.append(Message(
        role="assistant",
        content=(TextBlock(text="At the merge gate. Merge now?"),)))
    transcript.append(Message(role="user", content=(TextBlock(text="ignore"),)))


async def _run_anthropic(
    tmp_path: Path, *, resume_should_continue: bool, queued: list[str] | None,
) -> list[str]:
    from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
    from imp.agents.tools import get_registry  # noqa: F401 — ensures tools registered
    from imp.engine.state import InMemoryEventLog
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)
    _seed_send_to_llm_transcript(inv_dir)

    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"status": "Success", "note": "ok"})),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    inv = AgentInvocation(
        agent_def=_agent_def(),
        provider_name="anthropic",
        model_name="claude-sonnet-4-6",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=inv_dir,
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=InMemoryEventLog(),
        coordinator=_Coord(queued),
        resume_should_continue=resume_should_continue,
    )

    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"

    texts: list[str] = []
    for call in fake.messages.calls:
        for m in call["messages"]:
            content = m["content"] if isinstance(m, dict) else m.content
            if isinstance(content, str):
                texts.append(content)
            else:
                for block in content:
                    text = block.get("text") if isinstance(block, dict) else None
                    if text is not None:
                        texts.append(text)
    return texts


@pytest.mark.anyio
async def test_anthropic_interrupt_resume_appends_continuation_nudge(
    tmp_path: Path,
) -> None:
    texts = await _run_anthropic(
        tmp_path, resume_should_continue=True, queued=None)
    assert texts.count(_CONTINUATION_NUDGE) == 1, texts


@pytest.mark.anyio
async def test_anthropic_non_interrupt_resume_omits_nudge(
    tmp_path: Path,
) -> None:
    texts = await _run_anthropic(
        tmp_path, resume_should_continue=False, queued=None)
    assert _CONTINUATION_NUDGE not in texts, texts


@pytest.mark.anyio
async def test_anthropic_interject_wins_over_interrupt_nudge(
    tmp_path: Path,
) -> None:
    texts = await _run_anthropic(
        tmp_path, resume_should_continue=True, queued=["human says continue"])
    assert "human says continue" in texts, texts
    assert _CONTINUATION_NUDGE not in texts, texts


@pytest.mark.anyio
async def test_claude_code_interrupt_resume_appends_continuation_nudge(
    tmp_path: Path,
) -> None:
    from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
    from imp.agents.tools import get_registry  # noqa: F401 — ensures tools registered
    from imp.engine.state import InMemoryEventLog
    from imp.providers.claude_code import ClaudeCodeAgentRunner
    from tests.cc_fakes import FakeClaudeSDKClient, assistant, result
    from tests.cc_fakes import text as cc_text
    from tests.cc_fakes import tool_use as cc_tool_use

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)
    _seed_send_to_llm_transcript(inv_dir)

    scripted = [
        assistant(
            cc_text("done"),
            cc_tool_use("c1", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    inv = AgentInvocation(
        agent_def=_agent_def(),
        provider_name="claude_code",
        model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=inv_dir,
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=InMemoryEventLog(),
        coordinator=_Coord(),
        resume_should_continue=True,
    )

    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"
    assert created, "client was never instantiated"

    def _texts_in_query(q: list) -> list[str]:
        texts: list[str] = []
        for d in q:
            if not isinstance(d, dict) or d.get("type") != "user":
                continue
            content = d.get("message", {}).get("content", "")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
        return texts

    all_texts: list[str] = []
    for q in created[0].queries:
        if isinstance(q, list):
            all_texts.extend(_texts_in_query(q))
        elif isinstance(q, str):
            all_texts.append(q)
    assert all_texts.count(_CONTINUATION_NUDGE) == 1, all_texts


@pytest.mark.anyio
async def test_claude_code_pending_human_tail_suppresses_nudge(
    tmp_path: Path,
) -> None:
    from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
    from imp.agents.tools import get_registry  # noqa: F401 — ensures tools registered
    from imp.engine.state import InMemoryEventLog
    from imp.providers.claude_code import ClaudeCodeAgentRunner
    from tests.cc_fakes import FakeClaudeSDKClient, assistant, result
    from tests.cc_fakes import text as cc_text
    from tests.cc_fakes import tool_use as cc_tool_use

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)
    _seed_text_user_tail_transcript(inv_dir)

    scripted = [
        assistant(
            cc_text("acting on the reply"),
            cc_tool_use("c1", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    log = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    inv = AgentInvocation(
        agent_def=_agent_def(),
        provider_name="claude_code",
        model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=inv_dir,
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        coordinator=_Coord(),
        resume_should_continue=True,
    )

    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"
    assert created, "client was never instantiated"

    # No engine continuation was journaled — the human reply was not buried.
    assert not any(e["type"] == "engine_continuation" for e in log.read())

    def _texts_in_query(q: list) -> list[str]:
        texts: list[str] = []
        for d in q:
            if not isinstance(d, dict) or d.get("type") != "user":
                continue
            content = d.get("message", {}).get("content", "")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
        return texts

    all_texts: list[str] = []
    for q in created[0].queries:
        if isinstance(q, list):
            all_texts.extend(_texts_in_query(q))
        elif isinstance(q, str):
            all_texts.append(q)
    assert _CONTINUATION_NUDGE not in all_texts, all_texts
    assert "ignore" in all_texts, all_texts
