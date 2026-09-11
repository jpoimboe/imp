from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.engine.reincarnation import handoff_path_for
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import (
    FakeClaudeSDKClient,
    assistant,
    context_usage,
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
        tools=frozenset({"complete_step", "write_file"}), subagents=(),
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
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "wf"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log)


@pytest.mark.anyio
async def test_threshold_crossing_continues_with_instruction_increment(
        tmp_path: Path) -> None:
    """Crossing the live context threshold mid-turn signals interrupt(), lets the
    SDK close the turn balanced (error_during_execution), then continues the LIVE
    session with a plain handoff-instruction increment — NOT a _stream_history
    replay (the regression that doubled the context). The handoff event fires."""
    (tmp_path / "art").mkdir()
    hp = handoff_path_for(tmp_path / "step")
    log = InMemoryEventLog()

    scripted = [
        # Turn 1: an assistant turn carrying a (soon-dangling) tool_use; the live
        # context is 170000/200000 = 0.85 >= 0.8, so the runner interrupts here.
        assistant(text("working"),
                  tool_use("u1", "write_file", {"path": "scratch", "content": "x"})),
        # The SDK closes the interrupted turn balanced.
        result(subtype="error_during_execution", is_error=True),
        # Turn 2 (after the plain instruction increment): the agent emits its
        # handoff as plain text; the provider persists it before signalling.
        assistant(text("writing handoff")),
        result(),
    ]

    cu = context_usage(raw_max_tokens=200_000, percentage=0.85,
                       total_tokens=170_000)
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted,
                                     context_usage=cu)
        holder["client"] = client
        return client

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    assert not [e for e in events if e["type"] == "error"]
    client = holder["client"]
    assert client.interrupts == 1
    # Regression: the FIRST query streams history (a drained list); EVERY requery
    # after it is a plain increment string. If the handoff path re-streamed
    # history, queries[1:] would contain a list — assert it never does.
    assert isinstance(client.queries[0], list)
    assert all(isinstance(q, str) for q in client.queries[1:])
    # The handoff requery carried the instruction increment as a string.
    assert any("approaching the context limit" in q
               for q in client.queries if isinstance(q, str))
    # The interrupted turn's dangling tool_use ("u1") is balanced for the stash in
    # `messages` only — NOT logged as a real tool_result (it never ran).
    tool_results = [e for e in log.read() if e["type"] == "tool_result"]
    assert not any(e["payload"]["id"] == "u1" for e in tool_results)
    assert hp.exists()
    assert hp.read_text() == "writing handoff"


@pytest.mark.anyio
async def test_threshold_watchdog_does_not_refire_mid_interrupt(
        tmp_path: Path) -> None:
    """After a crossing sets handoff_phase = "interrupt_requested", a second
    assistant message arriving before the balanced close (still above threshold)
    must NOT trigger a second interrupt — interrupt() fires exactly once."""
    (tmp_path / "art").mkdir()
    hp = handoff_path_for(tmp_path / "step")
    log = InMemoryEventLog()

    scripted = [
        assistant(text("working"),
                  tool_use("u1", "write_file", {"path": "scratch", "content": "x"})),
        # A second assistant message lands before the balanced close, still above
        # threshold; the watchdog must stay quiet because a handoff is in flight.
        assistant(text("still working"),
                  tool_use("u1b", "write_file", {"path": "scratch2", "content": "y"})),
        result(subtype="error_during_execution", is_error=True),
        assistant(text("writing handoff")),
        result(),
    ]

    cu = context_usage(raw_max_tokens=200_000, percentage=0.85,
                       total_tokens=170_000)
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted,
                                     context_usage=cu)
        holder["client"] = client
        return client

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert holder["client"].interrupts == 1
    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert hp.exists()
    assert hp.read_text() == "writing handoff"


@pytest.mark.anyio
async def test_handoff_reported_even_when_note_missing_and_error(
        tmp_path: Path) -> None:
    """The runner always reports handoff_path on a Tier A handoff turn — it does
    NOT suppress on a missing note or an is_error close. (Failure detection is the
    engine's job, via note existence.)"""
    (tmp_path / "art").mkdir()
    hp = handoff_path_for(tmp_path / "step")
    log = InMemoryEventLog()

    scripted = [
        assistant(text("working"),
                  tool_use("u1", "write_file", {"path": "scratch", "content": "x"})),
        result(subtype="error_during_execution", is_error=True),
        # Turn 2: the agent produces no final text; the provider writes no note
        # and recovery degrades to transcript-only.
        assistant(text("")),
        result(is_error=True),
    ]

    cu = context_usage(raw_max_tokens=200_000, percentage=0.85,
                       total_tokens=170_000)
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted,
                                     context_usage=cu)
        holder["client"] = client
        return client

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    # The agent never wrote the note; the runner still reports it.
    assert not hp.exists()


@pytest.mark.anyio
async def test_below_threshold_completing_turn_interrupts_and_completes(
        tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()

    scripted = [
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    cu = context_usage(raw_max_tokens=200_000, percentage=0.50,
                       total_tokens=100_000)
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted,
                                     context_usage=cu)
        holder["client"] = client
        return client

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert not [e for e in events if e["type"] == "handoff"]
    assert any(e["type"] == "agent_result" for e in events)
    # Part A: a completing turn is interrupted regardless of the context
    # threshold — the completion-interrupt, not a handoff.
    assert holder["client"].interrupts == 1


@pytest.mark.anyio
async def test_completing_turn_above_threshold_completes_not_handoff(
        tmp_path: Path) -> None:
    """Completion wins: a turn that calls complete_step is never preempted into
    a handoff, even when live context is at/above the threshold. Under Part A it
    IS preempted — via the completion-interrupt — so it completes, not hands off.
    Drives the
    PRODUCTION tool name `mcp__imp__complete_step` (the SDK namespaces imp's
    tools); a watch guard comparing the bare name would miss it and wrongly
    interrupt+handoff a completing turn. Requires both the watch guard and the
    runner dual-write to canonicalize via _canon_tool_name."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()

    scripted = [
        assistant(text("done"),
                  tool_use("c1", "mcp__imp__complete_step", {"status": "Success"})),
        result(),
    ]
    cu = context_usage(raw_max_tokens=200_000, percentage=0.85,
                       total_tokens=170_000)
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted,
                                     context_usage=cu)
        holder["client"] = client
        return client

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert not [e for e in events if e["type"] == "handoff"]
    assert any(e["type"] == "agent_result" for e in events)
    # Part A: the completing turn is interrupted (completion path), not a handoff.
    assert holder["client"].interrupts == 1


@pytest.mark.anyio
async def test_handoff_note_turn_is_flagged_in_the_event_log(tmp_path: Path) -> None:
    # Only the note turn is flagged; the interrupted turn is flushed before the phase is set.
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()

    scripted = [
        # Turn 1: live context 170000/200000 = 0.85 >= 0.8 — the runner interrupts.
        assistant(text("working"),
                  tool_use("u1", "write_file", {"path": "scratch", "content": "x"})),
        result(subtype="error_during_execution", is_error=True),
        # Turn 2 (after the instruction increment) is the handoff note itself.
        assistant(text("HANDOFF-SENTINEL: where I left off")),
        result(),
    ]

    cu = context_usage(raw_max_tokens=200_000, percentage=0.85,
                       total_tokens=170_000)

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted,
                                   context_usage=cu)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert len([e for e in events if e["type"] == "handoff"]) == 1
    msgs = [e for e in log.read() if e["type"] == "agent_message"]
    assert len(msgs) >= 2
    # the flagged turn is the note itself, not merely the last message
    assert "HANDOFF-SENTINEL" in str(msgs[-1]["payload"]["raw"])
    assert msgs[-1]["payload"]["handoff"] is True
    assert all("handoff" not in m["payload"] for m in msgs[:-1])
