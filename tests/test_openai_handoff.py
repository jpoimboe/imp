from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.engine.reincarnation import handoff_path_for
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.openai import OpenAIAgentRunner
from tests.openai_fakes import (
    FakeResponsesClient,
    resp_function_call,
    resp_message,
    resp_response,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _agent(name: str, *, tools) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset(tools), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="go", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


def _invocation(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent("w", tools={"complete_step", "write_file"}),
        provider_name="openai", model_name="gpt-5.4",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(id="t", title="T", project_root=tmp_path,
                                    workflow_dir=tmp_path / "wf"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:impl/w")


@pytest.mark.anyio
async def test_threshold_crossing_elicits_handoff_and_signals(tmp_path: Path) -> None:
    hp = handoff_path_for(tmp_path / "step")
    fake = FakeResponsesClient([
        # Turn 1 reaches the 160k default-ceiling cutoff.
        resp_response(
            resp_function_call("u1", "write_file",
                               '{"path": "scratch.txt", "content": "x"}'),
            usage={"input_tokens": 160_000}),
        # Turn 2: agent replies with text + a write to a different file. The
        # provider captures the text and writes hp itself.
        resp_response(
            resp_message("# handoff from provider"),
            resp_function_call("u2", "write_file",
                               '{"path": "scratch2.txt", "content": "y"}')),
    ])
    runner = OpenAIAgentRunner(model_name="gpt-5.4", client_factory=lambda: fake)

    events = [e async for e in runner.run(_invocation(tmp_path))]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    transcript = (tmp_path / "step" / "transcript.jsonl").read_text()
    assert "approaching the context limit" in transcript   # instruction injected
    assert hp.exists()
    assert hp.read_text() == "# handoff from provider"


@pytest.mark.anyio
async def test_below_threshold_does_not_elicit_handoff(tmp_path: Path) -> None:
    fake = FakeResponsesClient([
        # One token below the 160k default-ceiling cutoff; no handoff.
        resp_response(
            resp_function_call("u1", "write_file",
                               '{"path": "scratch.txt", "content": "x"}'),
            usage={"input_tokens": 159_999}),
        resp_response(
            resp_function_call("c1", "complete_step", '{"status": "Success"}')),
    ])
    runner = OpenAIAgentRunner(model_name="gpt-5.4", client_factory=lambda: fake)

    events = [e async for e in runner.run(_invocation(tmp_path))]

    assert not [e for e in events if e["type"] == "handoff"]
    assert any(e["type"] == "agent_result" for e in events)
    transcript = (tmp_path / "step" / "transcript.jsonl").read_text()
    assert "approaching the context limit" not in transcript


@pytest.mark.anyio
async def test_empty_handoff_turn_does_not_write_note(tmp_path: Path) -> None:
    # When the handoff turn produces empty final text the note must NOT be
    # written (degrade to transcript-only recovery), but the handoff event still
    # fires so the engine can reincarnate.
    hp = handoff_path_for(tmp_path / "step")
    fake = FakeResponsesClient([
        # Turn 1 reaches the 160k default-ceiling cutoff and carries prose so a
        # walk-back helper would have stale text to pick up.
        resp_response(
            resp_message("I will write the files"),
            resp_function_call("u1", "write_file",
                               '{"path": "scratch.txt", "content": "x"}'),
            usage={"input_tokens": 160_000}),
        # Turn 2: agent replies with empty text and no tool (tool-less site 1).
        resp_response(resp_message("")),
    ])
    runner = OpenAIAgentRunner(model_name="gpt-5.4", client_factory=lambda: fake)

    events = [e async for e in runner.run(_invocation(tmp_path))]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    assert not hp.exists()


@pytest.mark.anyio
async def test_handoff_fires_on_tool_less_writing_turn(tmp_path: Path) -> None:
    # The handoff-writing turn ends WITHOUT a tool (the agent replies with prose):
    # the runner still emits exactly one terminal `handoff` and stops.
    hp = handoff_path_for(tmp_path / "step")
    fake = FakeResponsesClient([
        # Turn 1 reaches the 160k default-ceiling cutoff.
        resp_response(
            resp_function_call("u1", "write_file",
                               '{"path": "scratch.txt", "content": "x"}'),
            usage={"input_tokens": 160_000}),
        # Turn 2: agent responds to the handoff instruction with TEXT and no tool.
        resp_response(resp_message("Understood, stopping here.")),
    ])
    runner = OpenAIAgentRunner(model_name="gpt-5.4", client_factory=lambda: fake)

    events = [e async for e in runner.run(_invocation(tmp_path))]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    transcript = (tmp_path / "step" / "transcript.jsonl").read_text()
    assert "approaching the context limit" in transcript   # instruction injected
    assert hp.exists()
    assert hp.read_text() == "Understood, stopping here."
