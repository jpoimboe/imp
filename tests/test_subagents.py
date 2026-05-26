# tests/test_subagents.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
)
from imp.agents.subagents import (
    TASK_TOOL_SCHEMA,
    TaskMustBeAwaited,
    dispatch_task,
    make_task_tool,
)
from imp.agents.tools.context import ToolContext
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


def _agent(name: str) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi {{ change.id }}",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_task_tool_invoke_raises_sentinel(tmp_path: Path) -> None:
    """`.invoke()` must never be called — runner special-cases name=='task'."""
    ctx = ToolContext(
        project_root=tmp_path, artifacts_dir=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    tool = make_task_tool()
    with pytest.raises(TaskMustBeAwaited):
        tool.invoke(ctx, {"subagent_name": "x", "task_description": "y"})


def test_task_tool_schema_has_required_args() -> None:
    assert "subagent_name" in TASK_TOOL_SCHEMA["properties"]
    assert "task_description" in TASK_TOOL_SCHEMA["properties"]
    assert set(TASK_TOOL_SCHEMA["required"]) == {"subagent_name", "task_description"}


@pytest.mark.anyio
async def test_dispatch_task_awaits_spawn_subagent(tmp_path: Path) -> None:
    captured: dict = {}

    async def fake_spawn(sub_invocation: AgentInvocation):
        captured["invocation"] = sub_invocation

        async def _gen():
            yield {"type": "agent_output", "payload": {"status": "Success", "note": "done"}}

        return _gen()

    ctx = ToolContext(
        project_root=tmp_path, artifacts_dir=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.spawn_subagent = fake_spawn
    ctx.change_context = ChangeContext(
        change_id=42, project_root=tmp_path, artifacts_dir=tmp_path,
    )

    out = await dispatch_task(ctx, "reviewer", "please review")
    parsed = json.loads(out)
    assert parsed == {"status": "Success", "note": "done"}
    inv = captured["invocation"]
    assert inv.agent_def.name == "reviewer"
    assert inv.interactive is False
    assert inv.provider_name == "anthropic"
    assert inv.change_context.change_id == 42


@pytest.mark.anyio
async def test_dispatch_task_unknown_subagent_raises(tmp_path: Path) -> None:
    ctx = ToolContext(
        project_root=tmp_path, artifacts_dir=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.subagent_defs = {}
    ctx.spawn_subagent = lambda _inv: None  # type: ignore[assignment]
    with pytest.raises(ValueError, match="not declared"):
        await dispatch_task(ctx, "ghost", "hi")
