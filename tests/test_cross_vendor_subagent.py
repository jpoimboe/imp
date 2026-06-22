# tests/test_cross_vendor_subagent.py
"""Cross-vendor subagent seam (real runner).

A real AnthropicAgentRunner parent dispatches a `task` to a `reviewer` subagent
whose tier (`medium`) resolves to openai. The child must run on the
openai recording runner (vendor B != the anthropic parent), and its agent_output
must flow back as the parent's task tool_result.

This is NOT a literal reuse of test_anthropic_runner.py's same-vendor dispatch
test: there the child draws from the parent's shared FakeAsyncAnthropic queue;
here the child runs on a RECORDING provider that consumes nothing from the parent
queue. The parent queue therefore holds exactly two responses (task tool_use,
then complete_step) — adding a third (child) turn would leave it unconsumed and
desync the parent.

Fails if any single hop regresses: resolution not collecting the subagent role,
dispatch_task not reading ctx.resolved_tiers, or spawn_subagent_via_registry not
building the child from the registry by provider_name.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicAgentRunner, AnthropicProvider
from imp.providers.config import ProviderModel
from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

_SRC = SourceLoc(file=Path("a.md"), line=1)


class _RecordingRunner:
    """A child runner that consumes nothing from the parent's queue and yields a
    single agent_output reporting the vendor/model it actually ran on."""

    def __init__(self, name: str, model: str) -> None:
        self.name = name
        self.model = model

    async def run(self, invocation):
        yield {
            "type": "agent_output",
            "payload": {"status": "Success", "ran_on": self.name,
                        "model": self.model},
        }

    async def interrupt(self) -> None:
        pass


class _RecordingProvider:
    def __init__(self, name: str, model: str) -> None:
        self.name = name
        self.model = model

    def create_runner(self, invocation):
        return _RecordingRunner(self.name, self.model)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _status_schema() -> OutputSchema:
    return OutputSchema(
        fields=(OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),),
        source=_SRC)


@pytest.mark.anyio
async def test_cross_vendor_subagent_runs_on_child_vendor(
    tmp_path: Path, provider_registry,
) -> None:
    (tmp_path / "art").mkdir()

    # Delta 3: reviewer subagent declares tier medium (-> openai).
    reviewer_def = AgentDef(
        name="reviewer", description="",
        tools=frozenset({"complete_step"}), subagents=(),
        output_schema=_status_schema(),
        prompt_template_source="review",
        source_path=Path("r.md"), source=_SRC,
        tier="medium",
    )
    parent_def = AgentDef(
        name="coder", description="",
        tools=frozenset({"complete_step", "task"}), subagents=("reviewer",),
        output_schema=_status_schema(),
        prompt_template_source="code",
        source_path=Path("c.md"), source=_SRC,
    )

    # Delta 1: PARENT-ONLY queue with exactly two responses. The child draws from
    # the recording runner, NOT this queue — do not add a third turn.
    parent_fake = FakeAsyncAnthropic([
        response(tool_use("t1", "task", {
            "subagent_name": "reviewer",
            "task_description": "review the spec",
        })),
        response(tool_use("pc", "complete_step", {"status": "Success"})),
    ])

    # Register openai as a RECORDING provider; anthropic as the real provider over
    # the parent fake. Teardown is owned by the provider_registry fixture.
    provider_registry(
        "openai", env_check=lambda: True,
        factory=lambda m: _RecordingProvider("openai", m))
    provider_registry(
        "anthropic", env_check=lambda: True,
        factory=lambda m: AnthropicProvider(
            model_name=m, client_factory=lambda: parent_fake))

    log = InMemoryEventLog()
    inv = AgentInvocation(
        agent_def=parent_def,
        provider_name="anthropic", model_name="claude-sonnet-4-6",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        subagent_defs={"reviewer": reviewer_def},
        # Delta 2: parent sets resolved_tiers so the subagent's medium tier
        # places the child on openai.
        resolved_tiers={
            "low": ProviderModel("anthropic", "claude-sonnet-4-6"),
            "medium": ProviderModel("openai", "gpt-5.2"),
        },
    )

    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: parent_fake)
    events = [e async for e in runner.run(inv)]

    # Parent completed.
    assert events[-1]["type"] == "agent_output"
    assert events[-1]["payload"]["status"] == "Success"

    # The child's agent_output flowed back as the parent's `task` tool_result,
    # and it ran on the RESOLVED child vendor/model (openai/gpt-5.2) — not the
    # anthropic parent. This is the cross-vendor assertion.
    task_result_ev = next(
        e for e in log.read()
        if e["type"] == "tool_result" and e["payload"].get("id") == "t1")
    parsed = json.loads(task_result_ev["payload"]["content"])
    assert parsed["ran_on"] == "openai"
    assert parsed["model"] == "gpt-5.2"
    assert parsed["status"] == "Success"

    # The parent fake's TWO turns were both consumed; the child consumed none.
    assert len(parent_fake.messages.calls) == 2
