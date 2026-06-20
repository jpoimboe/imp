from __future__ import annotations

from pathlib import Path

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.agents.tools.context import ToolContext
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


def _agent() -> AgentDef:
    return AgentDef(
        name="a", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


def test_invocation_subagent_depth_and_coordinator_default() -> None:
    inv = AgentInvocation(
        agent_def=_agent(), provider_name="p", model_name="m",
        system_prompt="s", initial_messages=(),
        change_context=ChangeContext(title="t",
                                     project_root=Path("/tmp"),
                                     workflow_dir=Path("/tmp/wf")),
        transcript_dir=Path("/tmp/td"),
        fs_permissions=(), events_path=Path("/tmp/e.jsonl"))
    assert inv.subagent_depth == 0
    assert inv.coordinator is None


def test_toolcontext_subagent_depth_and_coordinator_default() -> None:
    ctx = ToolContext(project_root=Path("/tmp"), fs_permissions=(),
                      events_path=Path("/tmp/e.jsonl"))
    assert ctx.subagent_depth == 0
    assert ctx.coordinator is None
