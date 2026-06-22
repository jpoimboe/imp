"""Each provider must build the task tool with the run's tier menu and copy it
onto ToolContext. Verified at the _build_tools / construction layer so the test
needs no live model."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


def _invocation(tmp_path: Path, provider: str) -> AgentInvocation:
    schema = OutputSchema(
        fields=(OutputField(name="status", type="enum",
                            values=("Success", "Error")),),
        source=SourceLoc(file=Path("a.md"), line=1))
    agent = AgentDef(
        name="parent", description="", tools=frozenset({"task", "complete_step"}),
        subagents=("reviewer",), output_schema=schema,
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))
    return AgentInvocation(
        agent_def=agent, provider_name=provider, model_name="m",
        system_prompt="hi",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            tmp_path, tmp_path, tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "t",
        events_path=tmp_path / "events.jsonl",
        tiers=("low", "high"))


@pytest.mark.parametrize("module,provider", [
    ("imp.providers.anthropic", "anthropic"),
    ("imp.providers.openai", "openai"),
    ("imp.providers.claude_code", "claude_code"),
])
def test_build_tools_threads_tiers(module, provider, tmp_path: Path) -> None:
    import importlib
    mod = importlib.import_module(module)
    inv = _invocation(tmp_path, provider)
    tools = mod._build_tools(inv)
    schema = tools["task"].input_schema
    assert schema["properties"]["tier"]["enum"] == ["low", "high"]
