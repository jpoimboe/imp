from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, QuestContext, Message, TextBlock
from imp.engine.reincarnation import handoff_path_for
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicAgentRunner
from tests.anthropic_fakes import FakeAsyncAnthropic, response, text, tool_use


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


def _invocation(tmp_path: Path, *, threshold: float | int | None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent("w", tools={"complete_step", "write_file"}),
        provider_name="anthropic", model_name="sonnet",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(title="T", project_root=tmp_path,
                                     workflow_dir=tmp_path / "wf"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:impl/w",
        checkpoint_threshold=threshold)


@pytest.mark.anyio
async def test_threshold_crossing_elicits_handoff_and_signals(tmp_path: Path) -> None:
    hp = handoff_path_for(tmp_path / "step")
    fake = FakeAsyncAnthropic([
        # Turn 1 crosses 0.8 * 200000 = 160000 (170000) on a tool-using turn.
        response(tool_use("u1", "write_file",
                          {"path": "scratch.txt", "content": "x"}),
                 usage={"input_tokens": 170_000}),
        # Turn 2: the agent writes the handoff note as instructed, then the
        # runner emits the terminal `handoff` signal.
        response(tool_use("u2", "write_file",
                          {"path": str(hp), "content": "# handoff\n"})),
    ])
    runner = AnthropicAgentRunner(model_name="sonnet", client_factory=lambda: fake)

    events = [e async for e in runner.run(_invocation(tmp_path, threshold=0.8))]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    transcript = (tmp_path / "step" / "transcript.jsonl").read_text()
    assert "approaching the context limit" in transcript   # instruction injected
    assert hp.exists()                                      # turn 2 wrote the note


@pytest.mark.anyio
async def test_below_threshold_does_not_elicit_handoff(tmp_path: Path) -> None:
    fake = FakeAsyncAnthropic([
        # 100000 / 200000 = 0.50 < 0.8 -> no crossing.
        response(tool_use("u1", "write_file",
                          {"path": "scratch.txt", "content": "x"}),
                 usage={"input_tokens": 100_000}),
        response(tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    runner = AnthropicAgentRunner(model_name="sonnet", client_factory=lambda: fake)

    events = [e async for e in runner.run(_invocation(tmp_path, threshold=0.8))]

    assert not [e for e in events if e["type"] == "handoff"]
    assert any(e["type"] == "agent_output" for e in events)
    transcript = (tmp_path / "step" / "transcript.jsonl").read_text()
    assert "approaching the context limit" not in transcript


@pytest.mark.anyio
async def test_handoff_fires_on_tool_less_writing_turn(tmp_path: Path) -> None:
    # The handoff-writing turn ends WITHOUT a tool (the agent replies with prose):
    # the runner still emits exactly one terminal `handoff` and stops.
    hp = handoff_path_for(tmp_path / "step")
    fake = FakeAsyncAnthropic([
        # Turn 1 crosses 0.8 * 200000 = 160000 (170000) on a tool-using turn.
        response(tool_use("u1", "write_file",
                          {"path": "scratch.txt", "content": "x"}),
                 usage={"input_tokens": 170_000}),
        # Turn 2: agent responds to the handoff instruction with TEXT and no tool.
        response(text("Understood, stopping here."), stop_reason="end_turn"),
    ])
    runner = AnthropicAgentRunner(model_name="sonnet", client_factory=lambda: fake)

    events = [e async for e in runner.run(_invocation(tmp_path, threshold=0.8))]

    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["payload"]["handoff_path"] == str(hp)
    transcript = (tmp_path / "step" / "transcript.jsonl").read_text()
    assert "approaching the context limit" in transcript   # instruction injected
