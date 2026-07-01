from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import (
    FakeClaudeSDKClient,
    assistant,
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


def _inv(tmp_path: Path, log: InMemoryEventLog, *,
         threshold: float | int | None = None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(), provider_name="claude_code",
        model_name="claude-sonnet-4-5", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "wf"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log, checkpoint_threshold=threshold)


@pytest.mark.anyio
async def test_completing_turn_is_interrupted_then_completes(tmp_path: Path) -> None:
    """Part A: a turn that calls complete_step is preempted via cooperative
    interrupt. The SDK closes the turn balanced (error_during_execution); the
    step still emits agent_output and returns Success — the completion-interrupt
    is neither a handoff nor an error/pause, and it never requeries."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    scripted = [
        assistant(text("Recording the outcome."),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(is_error=True, subtype="error_during_execution"),
    ]
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        holder["client"] = client
        return client

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    assert holder["client"].interrupts == 1
    assert [e["type"] for e in events] == ["agent_output"]
    assert events[-1]["payload"] == {"status": "Success"}
    assert not [e for e in events if e["type"] in ("error", "handoff")]
    # The complete arm falls through to agent_output — it never requeries
    # (unlike the steer/handoff arms), so only the initial query was sent.
    assert len(holder["client"].queries) == 1
    # The interrupt branch re-emits the result-less complete_step tool_call to
    # the event log (the `continue` skips the normal append path).
    tool_calls = [e for e in log.read() if e["type"] == "tool_call"]
    assert any(e["payload"]["name"] == "complete_step" for e in tool_calls)


@pytest.mark.anyio
async def test_completing_turn_interrupt_is_unconditional(tmp_path: Path) -> None:
    """Part A: the completion-interrupt fires from the complete_step detection
    itself — independent of any checkpoint threshold (threshold=None here) and
    distinct from the handoff mechanism. A plain (non-error_during_execution)
    close still resets the flag and yields agent_output."""
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    scripted = [
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    holder: dict[str, Any] = {}

    def factory(opts: Any) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        holder["client"] = client
        return client

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)

    events = [ev async for ev in runner.run(_inv(tmp_path, log, threshold=None))]

    assert holder["client"].interrupts == 1
    assert [e["type"] for e in events] == ["agent_output"]
    assert events[-1]["payload"] == {"status": "Success"}
    # The interrupt branch re-emits the result-less complete_step tool_call to
    # the event log (the `continue` skips the normal append path).
    tool_calls = [e for e in log.read() if e["type"] == "tool_call"]
    assert any(e["payload"]["name"] == "complete_step" for e in tool_calls)
