# tests/test_provider_emit_taxonomy.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicAgentRunner
from tests.anthropic_fakes import FakeAsyncAnthropic, response, text, tool_use


@pytest.fixture
def anyio_backend():
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
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


def _inv(tmp_path: Path, log: InMemoryEventLog) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(), provider_name="anthropic",
        model_name="claude-sonnet-4-6", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="T", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log)


@pytest.mark.anyio
async def test_anthropic_fresh_entry_emits_triad_order(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path, log)):
        pass

    evs = [e for e in log.read() if e.get("agent_id") == "step:p/s"]
    sc = next(e for e in evs if e["type"] == "step_context")
    assert "system_prompt" not in sc["payload"]
    ems = [e for e in evs if e["type"] == "engine_message"]
    assert [e["payload"]["subtype"] for e in ems][:2] == ["system_prompt", "agent_prompt"]
    assert ems[0]["payload"]["content"][0]["text"] == "be terse"
    assert ems[1]["payload"]["content"][0]["text"] == "go"
    assert not [e for e in evs if e["type"] == "user_message"]
    types = [e["type"] for e in evs]
    assert types.index("engine_message") < types.index("agent_message")


@pytest.mark.anyio
async def test_anthropic_handoff_injection_emits_engine_message_continuation(
        tmp_path: Path) -> None:
    # When the runner crosses the context threshold it must inject the handoff
    # instruction as engine_message/continuation, not user_message/human=False.
    log = InMemoryEventLog()
    fake = FakeAsyncAnthropic([
        # Turn 1 reports 170_000 input tokens → crosses 0.8 × 200_000 = 160_000.
        # Uses an undeclared tool so dispatch returns a non-terminal error result
        # and the loop continues to the crossed_now injection site.
        response(tool_use("u1", "bash", {"command": "echo hi", "timeout": 5}),
                 usage={"input_tokens": 170_000}),
        # Turn 2: agent responds to the injected instruction with text and no tool;
        # runner writes the handoff note, emits handoff, and stops.
        response(text("# handoff note"), stop_reason="end_turn"),
    ])
    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="anthropic",
        model_name="claude-sonnet-4-6", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="T", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/h", event_log=log,
        checkpoint_threshold=0.8)
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake)

    async for _ in runner.run(inv):
        pass

    evs = [e for e in log.read() if e.get("agent_id") == "step:p/h"]
    continuations = [
        e for e in evs
        if e["type"] == "engine_message" and e["payload"].get("subtype") == "continuation"
    ]
    assert len(continuations) == 1
    assert "approaching the context limit" in continuations[0]["payload"]["content"][0]["text"]
    assert not [e for e in evs if e["type"] == "user_message"]
