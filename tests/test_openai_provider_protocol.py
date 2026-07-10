"""OpenAIProvider conforms to the Provider protocol — same shape as
test_provider_protocol.py for Anthropic but instantiates OpenAI explicitly."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, QuestContext, Message, TextBlock
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.base import Provider
from imp.providers.openai import OpenAIAgentRunner, OpenAIProvider


def _inv(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=AgentDef(
            name="t", description="", tools=frozenset({"complete_step"}),
            subagents=(),
            output_schema=OutputSchema(
                fields=(OutputField(
                    name="status", type="enum",
                    values=("Success", "Error"), required=True,
                ),),
                source=SourceLoc(file=Path("a.md"), line=1),
            ),
            prompt_template_source="hi",
            source_path=Path("a.md"),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        provider_name="openai", model_name="gpt-4o-mini",
        system_prompt="hi",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(title="Test", project_root=tmp_path,
                                     workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "s",
events_path=tmp_path / "events.jsonl",
    )


def test_openai_provider_satisfies_protocol(tmp_path: Path) -> None:
    p: Provider = OpenAIProvider(model_name="gpt-4o-mini")
    assert p.name == "openai"
    assert p.model == "gpt-4o-mini"
    runner: AgentRunner = p.create_runner(_inv(tmp_path))
    assert isinstance(runner, OpenAIAgentRunner)
    # Public attrs only — anything private is forbidden by
    # tests/test_no_private_reachthroughs.py
    assert runner.model == "gpt-4o-mini"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_openai_runner_interrupt_is_idempotent() -> None:
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini")
    await runner.interrupt()
    await runner.interrupt()  # second call must not raise
