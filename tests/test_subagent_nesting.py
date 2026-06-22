from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import ChangeContext
from imp.agents.subagents import MAX_SUBAGENT_DEPTH, dispatch_task
from imp.agents.tools.context import ToolContext
from imp.cli.signals import InterruptCoordinator
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _agent(name: str, subagents=()) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset({"complete_step", "task"}),
        subagents=tuple(subagents),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


class _FakeRunner:
    def __init__(self, events, capture=None):
        self._events = events
        self._capture = capture
    async def run(self, inv):
        if self._capture is not None:
            self._capture["inv"] = inv
        for e in self._events:
            yield e
    async def interrupt(self) -> None: ...


def _ctx(tmp_path: Path, *, depth: int) -> ToolContext:
    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.subagent_depth = depth
    ctx.change_context = ChangeContext(title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")
    return ctx


@pytest.mark.anyio
async def test_nesting_threads_depth_and_child_subagents(tmp_path: Path) -> None:
    """A child that declares a grandchild gets depth+1 and the grandchild in its
    own subagent_defs (so it COULD dispatch it). Enables+threads nesting."""
    capture: dict = {}
    ctx = _ctx(tmp_path, depth=0)
    ctx.subagent_defs = {
        "child": _agent("child", subagents=("grandchild",)),
        "grandchild": _agent("grandchild"),
    }

    async def _spawn(_inv):
        return _FakeRunner(
            [{"type": "agent_output", "payload": {"status": "Success"}}],
            capture=capture)

    ctx.spawn_subagent = _spawn
    await dispatch_task(ctx, "child", "do nested work")
    inv = capture["inv"]
    assert inv.subagent_depth == 1
    assert "grandchild" in inv.subagent_defs


@pytest.mark.anyio
async def test_nesting_past_max_depth_raises_to_parent(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, depth=MAX_SUBAGENT_DEPTH)  # child would be MAX+1
    ctx.subagent_defs = {"child": _agent("child")}

    async def _spawn(_inv):
        return _FakeRunner([{"type": "agent_output", "payload": {"status": "Success"}}])

    ctx.spawn_subagent = _spawn
    with pytest.raises(ValueError, match="exceeds MAX_SUBAGENT_DEPTH"):
        await dispatch_task(ctx, "child", "too deep")
