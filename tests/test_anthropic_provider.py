from __future__ import annotations

from pathlib import Path

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicProvider


def _agent() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}),
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


def _inv(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent(),
        provider_name="anthropic", model_name="claude-sonnet-4-6",
        system_prompt="be terse", initial_messages=(),
        change_context=ChangeContext(
            change_id=1, project_root=tmp_path, artifacts_dir=tmp_path,
        ),
        interactive=False, transcript_dir=tmp_path / "step",
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
    )


def test_provider_name_and_model() -> None:
    p = AnthropicProvider(model_name="claude-sonnet-4-6")
    assert p.name == "anthropic"
    assert p.model == "claude-sonnet-4-6"  # public; engine uses this


def test_create_runner_returns_runner(tmp_path: Path) -> None:
    p = AnthropicProvider(model_name="claude-sonnet-4-6")
    runner = p.create_runner(_inv(tmp_path))
    assert hasattr(runner, "run")
    assert hasattr(runner, "interrupt")
