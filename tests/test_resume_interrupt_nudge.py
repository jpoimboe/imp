# tests/test_resume_interrupt_nudge.py
"""A no-message resume of a Ctrl-C-interrupted step must CONTINUE the step.

A mid-tool interrupt repairs the dangling call with a synthetic tool_result, so
the resume classifies as SEND_TO_LLM (never AWAITING_USER) and the
_resolve_wait_reply continuation nudge can't reach it. The providers append the
same nudge as the trailing user turn when invocation.resume_should_continue is
set and no human interject already drove continuation.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imp.agents.transcript import RESUME_CONTINUE_NUDGE
from imp.paths import project_state_dir


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


# claude-p's verbatim rejection body, as captured live (spike FINDINGS.md).
_REJECTION_BODY = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). STOP what "
    "you are doing and wait for the user to tell you how to proceed."
)
_MARKER = "[Request interrupted by user for tool use]"
# Prefix of the non-bash synthetic body; the full string is pinned verbatim in
# tests/test_transcript.py — here we only need to see the framing flip.
_INTERRUPTED_PREFIX = (
    "[interrupted] The run was paused before this call's result was captured")


def _seed_interrupt_marker_transcript(inv_dir: Path) -> None:
    """The live-observed claude_code tail after a cooperative mid-tool interrupt:
    claude-p's own 'rejected / STOP' tool_result, then its marker turn."""
    from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    transcript = Transcript(inv_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="go"),)))
    transcript.append(Message(role="assistant", content=(
        TextBlock(text="Dispatching the coder."),)))
    transcript.append(Message(role="assistant", content=(
        ToolUseBlock(id="toolu_1", name="mcp__imp__sub_agent",
                     input={"subagent_name": "coder", "task_description": "task 1"}),)))
    transcript.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="toolu_1", content=_REJECTION_BODY,
                        is_error=True),)))
    transcript.append(Message(role="user", content=(TextBlock(text=_MARKER),)))


async def _run_claude_code_resume(
    tmp_path: Path, *, seed: Any = _seed_interrupt_marker_transcript,
    queued: list[str] | None = None, subagent_depth: int = 0,
) -> tuple[Any, list[Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Drive a claude_code resume over a seeded transcript. Returns
    (event log, created SDK fakes, session-store records, yielded events).
    The runner is built without a user_input_fn — the dispatch default."""
    from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
    from imp.engine.state import InMemoryEventLog
    from imp.providers.claude import ClaudeCodeAgentRunner
    from imp.providers.claude_session_store import session_key, session_store_for
    from tests.cc_fakes import FakeClaudeSDKClient, assistant, result
    from tests.cc_fakes import text as cc_text
    from tests.cc_fakes import tool_use as cc_tool_use

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)
    seed(inv_dir)

    scripted = [
        assistant(cc_text("continuing"),
                  cc_tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    log = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    inv = AgentInvocation(
        agent_def=_agent_def(),
        provider_name="claude_code",
        model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow",
        ),
        transcript_dir=inv_dir,
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        coordinator=_Coord(queued),
        resume_should_continue=True,
        subagent_depth=subagent_depth,
    )

    events = [e async for e in runner.run(inv)]
    assert created, "client was never instantiated"
    store = session_store_for(inv_dir)
    records = await store.load(session_key(tmp_path, "step:p/s")) or []
    return log, created, records, events


def _shipped_blob(created: list[Any], records: list[dict[str, Any]]) -> str:
    """Everything the resume put in front of the model: the reconstructed session
    records (messages[:resume_base]) plus the streamed delta."""
    # ensure_ascii=False is load-bearing: the nudge copy carries an em dash, and
    # escaping it to — would make every `nudge not in blob` assertion vacuous.
    parts = [json.dumps(records, default=str, ensure_ascii=False)]
    for c in created:
        parts.append(json.dumps(c.queries, default=str, ensure_ascii=False))
    return "\n".join(parts)


def _continuations(log: Any) -> list[dict[str, Any]]:
    return [e for e in log.read()
            if e["type"] == "engine_message"
            and e["payload"].get("subtype") == "continuation"]


@pytest.mark.anyio
async def test_claude_code_interrupt_marker_resume_delivers_the_nudge(
    tmp_path: Path,
) -> None:
    """Criterion 8: with the marker turn repaired away, pending_human_tail is
    False and the existing guard delivers the continue nudge — exactly once."""
    log, _created, _records, events = await _run_claude_code_resume(tmp_path)

    assert events[-1]["type"] == "agent_result"
    conts = _continuations(log)
    assert len(conts) == 1, log.read()
    assert conts[0]["payload"]["content"][0]["text"] == RESUME_CONTINUE_NUDGE


@pytest.mark.anyio
async def test_claude_code_interrupt_marker_resume_ships_no_rejection_framing(
    tmp_path: Path,
) -> None:
    """Criterion 9: neither claude-p's rejection copy nor its marker reaches the
    model — the re-framed interrupted body takes their place."""
    _log, created, records, _events = await _run_claude_code_resume(tmp_path)

    blob = _shipped_blob(created, records)
    assert "The tool use was rejected" not in blob
    assert "[Request interrupted by user" not in blob
    assert _INTERRUPTED_PREFIX in blob


@pytest.mark.anyio
async def test_claude_code_interrupt_marker_resume_journals_no_tool_result(
    tmp_path: Path,
) -> None:
    """Criterion 10: the pause already journaled this call's outcome, so the
    repair adds nothing — a second entry for the same tool_use_id would falsify
    the history (spec Decisions summary 8)."""
    log, _created, _records, events = await _run_claude_code_resume(tmp_path)

    assert events[-1]["type"] == "agent_result"
    assert [e for e in log.read() if e["type"] == "tool_result"] == []


async def _run_anthropic(
    tmp_path: Path, *, resume_should_continue: bool, queued: list[str] | None,
    seed: Any = _seed_send_to_llm_transcript, resume_cause: str = "human",
) -> list[str]:
    from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
    from imp.engine.state import InMemoryEventLog
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)
    seed(inv_dir)

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
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow",
        ),
        transcript_dir=inv_dir,
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=InMemoryEventLog(),
        coordinator=_Coord(queued),
        resume_should_continue=resume_should_continue,
        resume_cause=resume_cause,
    )

    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_result"

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
    assert texts.count(RESUME_CONTINUE_NUDGE) == 1, texts


@pytest.mark.anyio
async def test_anthropic_non_interrupt_resume_omits_nudge(
    tmp_path: Path,
) -> None:
    texts = await _run_anthropic(
        tmp_path, resume_should_continue=False, queued=None)
    assert RESUME_CONTINUE_NUDGE not in texts, texts


@pytest.mark.anyio
async def test_anthropic_interject_wins_over_interrupt_nudge(
    tmp_path: Path,
) -> None:
    texts = await _run_anthropic(
        tmp_path, resume_should_continue=True, queued=["human says continue"])
    assert "human says continue" in texts, texts
    assert RESUME_CONTINUE_NUDGE not in texts, texts


@pytest.mark.anyio
async def test_claude_code_interrupt_resume_appends_continuation_nudge(
    tmp_path: Path,
) -> None:
    from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
    from imp.engine.state import InMemoryEventLog
    from imp.providers.claude import ClaudeCodeAgentRunner
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
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow",
        ),
        transcript_dir=inv_dir,
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=InMemoryEventLog(),
        coordinator=_Coord(),
        resume_should_continue=True,
    )

    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_result"
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
    assert all_texts.count(RESUME_CONTINUE_NUDGE) == 1, all_texts


@pytest.mark.anyio
async def test_claude_code_pending_human_tail_suppresses_nudge(
    tmp_path: Path,
) -> None:
    from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
    from imp.engine.state import InMemoryEventLog
    from imp.providers.claude import ClaudeCodeAgentRunner
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
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow",
        ),
        transcript_dir=inv_dir,
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        coordinator=_Coord(),
        resume_should_continue=True,
    )

    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_result"
    assert created, "client was never instantiated"

    # No engine continuation was journaled — the human reply was not buried.
    assert not any(
        e["type"] == "engine_message"
        and e["payload"].get("subtype") == "continuation"
        for e in log.read())

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
    assert RESUME_CONTINUE_NUDGE not in all_texts, all_texts
    assert "ignore" in all_texts, all_texts


@pytest.mark.anyio
async def test_anthropic_pending_human_tail_suppresses_nudge(
    tmp_path: Path,
) -> None:
    texts = await _run_anthropic(
        tmp_path, resume_should_continue=True, queued=None,
        seed=_seed_text_user_tail_transcript)
    assert RESUME_CONTINUE_NUDGE not in texts, texts
    assert "ignore" in texts, texts


async def _run_openai(
    tmp_path: Path, *, resume_should_continue: bool,
    seed: Any = _seed_send_to_llm_transcript, resume_cause: str = "human",
) -> tuple[list[str], Any]:
    from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
    from imp.engine.state import InMemoryEventLog
    from imp.providers.openai import OpenAIAgentRunner
    from tests.openai_fakes import (
        FakeResponsesClient,
        resp_function_call,
        resp_message,
        resp_response,
    )

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)
    seed(inv_dir)

    fake = FakeResponsesClient([
        resp_response(
            resp_message("done"),
            resp_function_call("c1", "complete_step", '{"status": "Success"}'),
        ),
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini", client_factory=lambda: fake,
    )
    inv = AgentInvocation(
        agent_def=_agent_def(),
        provider_name="openai",
        model_name="gpt-4o-mini",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow",
        ),
        transcript_dir=inv_dir,
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        coordinator=_Coord(),
        resume_should_continue=resume_should_continue,
        resume_cause=resume_cause,
    )

    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_result"

    texts: list[str] = []
    for call in fake.responses.calls:
        for item in call.get("input", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for block in item.get("content", []):
                if isinstance(block, dict) and block.get("type") == "input_text":
                    texts.append(block.get("text", ""))
    return texts, log


@pytest.mark.anyio
async def test_openai_interrupt_resume_appends_continuation_nudge(
    tmp_path: Path,
) -> None:
    texts, _log = await _run_openai(tmp_path, resume_should_continue=True)
    assert RESUME_CONTINUE_NUDGE in texts, texts


@pytest.mark.anyio
async def test_openai_pending_human_tail_suppresses_nudge(
    tmp_path: Path,
) -> None:
    texts, log = await _run_openai(
        tmp_path, resume_should_continue=True,
        seed=_seed_text_user_tail_transcript)
    assert not any(
        e["type"] == "engine_message"
        and e["payload"].get("subtype") == "continuation"
        for e in log.read())
    assert RESUME_CONTINUE_NUDGE not in texts, texts
    assert "ignore" in texts, texts


@pytest.mark.anyio
async def test_anthropic_provider_retry_resume_injects_the_retry_note(
    tmp_path: Path,
) -> None:
    """push_session's direct resume-continue site — the base class behind both
    registered push providers — selects the note from the cause."""
    from imp.agents.transcript import RESUME_RETRY_NUDGE
    texts = await _run_anthropic(
        tmp_path, resume_should_continue=True, queued=None,
        resume_cause="provider_retry")
    assert texts.count(RESUME_RETRY_NUDGE) == 1, texts
    assert RESUME_CONTINUE_NUDGE not in texts, texts


@pytest.mark.anyio
async def test_openai_provider_retry_resume_injects_the_retry_note(
    tmp_path: Path,
) -> None:
    from imp.agents.transcript import RESUME_RETRY_NUDGE
    texts, _log = await _run_openai(
        tmp_path, resume_should_continue=True, resume_cause="provider_retry")
    assert texts.count(RESUME_RETRY_NUDGE) == 1, texts
    assert RESUME_CONTINUE_NUDGE not in texts, texts


@pytest.mark.anyio
async def test_claude_code_provider_retry_resume_journals_the_retry_note(
    tmp_path: Path,
) -> None:
    """claude_code's resume-continue condition: the note is journaled as an
    engine_message/continuation and selected from the cause."""
    from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
    from imp.agents.transcript import RESUME_RETRY_NUDGE
    from imp.engine.state import InMemoryEventLog
    from imp.providers.claude import ClaudeCodeAgentRunner
    from tests.cc_fakes import FakeClaudeSDKClient, assistant, result
    from tests.cc_fakes import text as cc_text
    from tests.cc_fakes import tool_use as cc_tool_use

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)
    _seed_send_to_llm_transcript(inv_dir)
    log = InMemoryEventLog()
    scripted = [
        assistant(cc_text("done"),
                  cc_tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda opts: FakeClaudeSDKClient(
            opts, scripted_messages=scripted))
    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="claude_code",
        model_name="claude-sonnet-4-5", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"),
        transcript_dir=inv_dir, events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log, coordinator=_Coord(),
        resume_should_continue=True, resume_cause="provider_retry")

    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_result"
    conts = [e for e in log.read()
             if e["type"] == "engine_message"
             and e["payload"].get("subtype") == "continuation"]
    assert len(conts) == 1, log.read()
    assert conts[0]["payload"]["content"][0]["text"] == RESUME_RETRY_NUDGE


@pytest.mark.anyio
async def test_anthropic_dependency_resume_injects_the_dependency_note(
    tmp_path: Path,
) -> None:
    """The engine arms cause='dependency' before the wait; push_session's
    resume-continue site must deliver the dependency copy, not the pause copy."""
    from imp.agents.transcript import RESUME_DEPENDENCY_NUDGE, RESUME_RETRY_NUDGE
    texts = await _run_anthropic(
        tmp_path, resume_should_continue=True, queued=None,
        resume_cause="dependency")
    assert texts.count(RESUME_DEPENDENCY_NUDGE) == 1, texts
    assert RESUME_CONTINUE_NUDGE not in texts, texts
    assert RESUME_RETRY_NUDGE not in texts, texts


@pytest.mark.anyio
async def test_claude_code_dependency_resume_journals_the_dependency_note(
    tmp_path: Path,
) -> None:
    """claude_code is the provider the dependency wait actually runs under: the
    re-entered step's single continuation carries the dependency copy."""
    from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
    from imp.agents.transcript import RESUME_DEPENDENCY_NUDGE
    from imp.engine.state import InMemoryEventLog
    from imp.providers.claude import ClaudeCodeAgentRunner
    from tests.cc_fakes import FakeClaudeSDKClient, assistant, result
    from tests.cc_fakes import text as cc_text
    from tests.cc_fakes import tool_use as cc_tool_use

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)
    _seed_send_to_llm_transcript(inv_dir)
    log = InMemoryEventLog()
    scripted = [
        assistant(cc_text("done"),
                  cc_tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda opts: FakeClaudeSDKClient(
            opts, scripted_messages=scripted))
    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="claude_code",
        model_name="claude-sonnet-4-5", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"),
        transcript_dir=inv_dir, events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log, coordinator=_Coord(),
        resume_should_continue=True, resume_cause="dependency")

    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_result"
    conts = [e for e in log.read()
             if e["type"] == "engine_message"
             and e["payload"].get("subtype") == "continuation"]
    assert len(conts) == 1, log.read()
    assert conts[0]["payload"]["content"][0]["text"] == RESUME_DEPENDENCY_NUDGE


def _seed_between_turns_marker_transcript(inv_dir: Path) -> None:
    """An interrupt that landed BETWEEN turns: the marker sits on an assistant
    turn, with no rejection beneath it to re-frame. Left untouched by design."""
    from imp.agents.runner import Message, TextBlock
    from imp.agents.transcript import Transcript

    transcript = Transcript(inv_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="go"),)))
    transcript.append(Message(role="assistant", content=(
        TextBlock(text="partial thought, cut off"),)))
    transcript.append(Message(role="user", content=(TextBlock(text=_MARKER),)))


@pytest.mark.anyio
async def test_claude_code_subagent_interrupt_marker_resume_continues(
    tmp_path: Path,
) -> None:
    """Criterion 11(a): at subagent_depth > 0 with no user_input_fn the repaired
    resume delivers the nudge and runs to a real complete_step — not the
    no_user_input_fn error terminal, and not a bare EndOfTurn re-emitting the
    interrupted turn's partial text as the subagent's deliverable."""
    log, _created, _records, events = await _run_claude_code_resume(
        tmp_path, subagent_depth=1)

    assert not any(
        e["type"] == "error" and e["payload"].get("kind") == "no_user_input_fn"
        for e in events), events
    assert events[-1]["type"] == "agent_result"
    # A repair that dropped the tool_result turn too would leave an assistant
    # tail: EndOfTurn at depth, carrying "Dispatching the coder." here instead.
    assert events[-1]["payload"] == {"status": "Success"}
    conts = _continuations(log)
    assert len(conts) == 1, log.read()
    assert conts[0]["payload"]["content"][0]["text"] == RESUME_CONTINUE_NUDGE


@pytest.mark.anyio
async def test_claude_code_subagent_between_turns_marker_resume_unchanged(
    tmp_path: Path,
) -> None:
    """Criterion 11(b): the between-turns marker at subagent_depth > 0 resumes
    exactly as on main — the helper is a no-op, the marker stays on disk and
    streams as the delta, and pending_human_tail still suppresses the nudge."""
    from imp.agents.runner import TextBlock
    from imp.agents.transcript import Transcript

    log, created, _records, events = await _run_claude_code_resume(
        tmp_path, seed=_seed_between_turns_marker_transcript, subagent_depth=1)

    assert events[-1]["type"] == "agent_result"
    assert events[-1]["payload"] == {"status": "Success"}
    assert _continuations(log) == []
    loaded = Transcript(tmp_path / "step" / "transcript.jsonl").load()
    assert loaded[2].role == "user"
    assert loaded[2].content == (TextBlock(text=_MARKER),)
    assert _MARKER in _shipped_blob(created, [])


@pytest.mark.anyio
async def test_claude_code_interject_wins_over_repaired_interrupt_nudge(
    tmp_path: Path,
) -> None:
    """Criterion 13: a Ctrl-C steer's typed text is drained and delivered instead
    of the nudge, and the repair means it no longer contradicts a tool_result
    telling the model it was rejected."""
    _log, created, records, events = await _run_claude_code_resume(
        tmp_path, queued=["actually, run the tests first"])

    assert events[-1]["type"] == "agent_result"
    blob = _shipped_blob(created, records)
    assert "actually, run the tests first" in blob
    assert RESUME_CONTINUE_NUDGE not in blob
    assert "The tool use was rejected" not in blob
    assert _INTERRUPTED_PREFIX in blob
