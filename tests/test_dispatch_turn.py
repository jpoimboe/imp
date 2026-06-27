from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

import imp.agents.dispatch as dispatch_mod
from imp.agents.dispatch import TurnState, dispatch_turn_tool_uses
from imp.agents.parallel import reset_parallelism_cache
from imp.agents.runner import ChangeContext, ToolUseBlock
from imp.agents.subagents import SubagentSuspended
from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.cli.signals import InterruptCoordinator
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _agent(name: str) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.event_log = None
    ctx.change_context = ChangeContext(
        title="C", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    return ctx


def _task_tu(tu_id: str, desc: str = "go") -> ToolUseBlock:
    return ToolUseBlock(id=tu_id, name="sub_agent",
                        input={"subagent_name": "reviewer", "task_description": desc})


def _noop_tu(tu_id: str) -> ToolUseBlock:
    return ToolUseBlock(id=tu_id, name="noop", input={})


_NOOP_TOOL = Tool(name="noop", description="", input_schema={"type": "object",
                  "properties": {}, "required": []},
                  fn=lambda ctx, **kw: json.dumps({"ok": True}))


async def _drain(ctx, elog, tus, tool_results, state, ceiling):
    async for _ in dispatch_turn_tool_uses(
            ctx=ctx, elog=elog, aid="step:p/s", invocation=None,
            tools={"noop": _NOOP_TOOL}, tool_uses=tus, tool_results=tool_results,
            state=state, ceiling=ceiling):
        pass


@pytest.mark.anyio
async def test_mixed_turn_results_in_document_order(tmp_path: Path) -> None:
    """AC13: [task1, noop, task2] -> tasks batched at the first-task position,
    noop runs after the batch, and the assembled tool_results are in DOCUMENT
    order [task1, noop, task2] even though task2 completes before task1."""
    reset_parallelism_cache()

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        await anyio.sleep(0.04 if desc == "slow" else 0.0)
        return json.dumps({"status": "Success", "echo": desc})

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path)
        elog = InMemoryEventLog()
        tool_results: list = []
        tus = [_task_tu("t1", "slow"), _noop_tu("n1"), _task_tu("t2", "fast")]
        await _drain(ctx, elog, tus, tool_results, TurnState(tool_calls_used=0), None)
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    assert [b.tool_use_id for b in tool_results] == ["t1", "n1", "t2"]
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_leading_non_task_completes_before_batch(tmp_path: Path) -> None:
    """AC13: [noop, task1, task2] -> the leading noop completes through
    dispatch_one_tool_use BEFORE the batch starts (its tool_result event is
    logged before either task's tool_call event)."""
    reset_parallelism_cache()

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        return json.dumps({"status": "Success"})

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path)
        elog = InMemoryEventLog()
        tool_results: list = []
        tus = [_noop_tu("n1"), _task_tu("t1"), _task_tu("t2")]
        await _drain(ctx, elog, tus, tool_results, TurnState(tool_calls_used=0), None)
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    rows = [e for e in elog.read() if e["agent_id"] == "step:p/s"]
    noop_result_idx = next(i for i, e in enumerate(rows)
                           if e["type"] == "tool_result" and e["payload"]["id"] == "n1")
    first_task_call_idx = min(
        i for i, e in enumerate(rows)
        if e["type"] == "tool_call" and e["payload"]["id"] in ("t1", "t2"))
    assert noop_result_idx < first_task_call_idx
    assert [b.tool_use_id for b in tool_results] == ["n1", "t1", "t2"]
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_mixed_turn_ceiling_reordering(tmp_path: Path) -> None:
    """AC12: [task1, noop, task2] with TWO ceiling slots free -> task1+task2 claim
    them at the first-task position and the trailing noop is BACKFILLED
    (over-ceiling), the pinned slot-reordering vs today's task1+noop."""
    reset_parallelism_cache()

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        return json.dumps({"status": "Success"})

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path)
        elog = InMemoryEventLog()
        tool_results: list = []
        state = TurnState(tool_calls_used=0)
        tus = [_task_tu("t1"), _noop_tu("n1"), _task_tu("t2")]
        await _drain(ctx, elog, tus, tool_results, state, 2)  # ceiling=2
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    by_id = {b.tool_use_id: b for b in tool_results}
    assert by_id["t1"].is_error is False          # ran
    assert by_id["t2"].is_error is False          # ran (claimed the 2nd slot)
    assert by_id["n1"].is_error is True           # backfilled (over ceiling)
    assert "tool_call_ceiling_exceeded" in by_id["n1"].content
    assert [b.tool_use_id for b in tool_results] == ["t1", "n1", "t2"]
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_kill_switch_inline_is_sequential(tmp_path: Path) -> None:
    """AC21: with IMP_MAX_PARALLEL_TASKS=1 a two-task turn takes the inline path —
    NO task group, so the children do NOT overlap (peak concurrency 1) — and
    results land in document order."""
    reset_parallelism_cache()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("IMP_MAX_PARALLEL_TASKS", "1")
    gate = {"active": 0, "peak": 0}

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        gate["active"] += 1
        gate["peak"] = max(gate["peak"], gate["active"])
        try:
            await anyio.sleep(0.02)
            return json.dumps({"status": "Success", "echo": desc})
        finally:
            gate["active"] -= 1

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path)
        elog = InMemoryEventLog()
        tool_results: list = []
        tus = [_task_tu("t1", "a"), _task_tu("t2", "b")]
        await _drain(ctx, elog, tus, tool_results, TurnState(tool_calls_used=0), None)
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
        monkeypatch.undo()
        reset_parallelism_cache()
    assert gate["peak"] == 1                       # sequential, no overlap
    assert [b.tool_use_id for b in tool_results] == ["t1", "t2"]


@pytest.mark.anyio
async def test_trailing_non_task_dispatched_after_suspended_task(
    tmp_path: Path,
) -> None:
    """[task1, noop] where task1's SubagentSuspended degrades to
    is_error -> the batch settles normally, the trailing noop IS dispatched, and
    t1 has an is_error result. Falsifiable: old code raised SubagentSuspended from
    the batch, preventing the noop from ever running."""
    reset_parallelism_cache()

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        raise SubagentSuspended("t1 paused")

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path)
        elog = InMemoryEventLog()
        tool_results: list = []
        tus = [_task_tu("t1"), _noop_tu("n1")]
        await _drain(ctx, elog, tus, tool_results, TurnState(tool_calls_used=0), None)
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    by_id = {b.tool_use_id: b for b in tool_results}
    assert by_id["t1"].is_error is True              # suspension degraded to is_error
    assert "SubagentSuspended" in by_id["t1"].content
    assert by_id["n1"].is_error is False             # noop ran and succeeded
    reset_parallelism_cache()
