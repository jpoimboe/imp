from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

import imp.agents.dispatch as dispatch_mod
from imp.agents.dispatch import TurnState, dispatch_task_batch
from imp.agents.parallel import reset_parallelism_cache
from imp.agents.runner import ChangeContext, ToolUseBlock
from imp.agents.subagents import SubagentSuspended
from imp.agents.tools.context import ToolContext
from imp.cli.signals import InterruptCoordinator
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _agent(name: str) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


def _ctx(tmp_path: Path, *, spawn, event_log=None) -> ToolContext:
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer"),
                         "good": _agent("good"), "bad": _agent("bad")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = spawn
    ctx.event_log = event_log
    ctx.change_context = ChangeContext(
        slug="change-42", title="Change 42", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    return ctx


def _task_tu(tu_id: str, name: str = "reviewer", desc: str = "go") -> ToolUseBlock:
    return ToolUseBlock(id=tu_id, name="task",
                        input={"subagent_name": name, "task_description": desc})


class _SlowRunner:
    """Records peak concurrency: bumps a shared gate while the child body runs."""

    def __init__(self, gate: dict, delay: float) -> None:
        self._gate = gate
        self._delay = delay

    async def run(self, _inv):
        self._gate["active"] += 1
        self._gate["peak"] = max(self._gate["peak"], self._gate["active"])
        try:
            await anyio.sleep(self._delay)
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        finally:
            self._gate["active"] -= 1

    async def interrupt(self) -> None: ...


def _slow_spawn(gate: dict, delay: float):
    async def _spawn(_inv):
        return _SlowRunner(gate, delay)
    return _spawn


@pytest.mark.anyio
async def test_batch_children_overlap_in_wallclock(tmp_path: Path) -> None:
    """AC2: two task tus in one batch run CONCURRENTLY (peak concurrency 2)."""
    reset_parallelism_cache()
    gate = {"active": 0, "peak": 0}
    ctx = _ctx(tmp_path, spawn=_slow_spawn(gate, 0.05))
    elog = InMemoryEventLog()
    tool_results: list = []
    state = TurnState(tool_calls_used=0)
    tus = [_task_tu("t1"), _task_tu("t2")]
    hbs = [ev async for ev in dispatch_task_batch(
        ctx=ctx, elog=elog, aid="step:p/s", task_tus=tus,
        tool_results=tool_results, state=state, ceiling=None)]
    assert gate["peak"] == 2                      # genuinely concurrent
    assert len(hbs) == 2                          # one heartbeat per task tu
    assert all(h["type"] == "tool_heartbeat" for h in hbs)
    assert {b.tool_use_id for b in tool_results} == {"t1", "t2"}
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_batch_assembles_results_in_document_order(tmp_path: Path) -> None:
    """AC2: results land in DOCUMENT order even when COMPLETION order differs.
    t1 ('slow') finishes after t2 ('fast'); the assembled tool_results are still
    [t1, t2] — proving the batch keys assembly off task_tus order, not arrival."""
    reset_parallelism_cache()

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        await anyio.sleep(0.04 if desc == "slow" else 0.0)
        return json.dumps({"status": "Success", "echo": desc})

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
        elog = InMemoryEventLog()
        tool_results: list = []
        state = TurnState(tool_calls_used=0)
        tus = [_task_tu("t1", desc="slow"), _task_tu("t2", desc="fast")]
        async for _ in dispatch_task_batch(
                ctx=ctx, elog=elog, aid="step:p/s", task_tus=tus,
                tool_results=tool_results, state=state, ceiling=None):
            pass
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    assert [b.tool_use_id for b in tool_results] == ["t1", "t2"]
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_batch_failure_isolation(tmp_path: Path) -> None:
    """AC3: child 'bad' raises, child 'good' succeeds -> bad is_error, good intact."""
    reset_parallelism_cache()

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        if name == "bad":
            raise RuntimeError("boom")
        return json.dumps({"status": "Success"})

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
        elog = InMemoryEventLog()
        tool_results: list = []
        state = TurnState(tool_calls_used=0)
        tus = [_task_tu("tg", name="good"), _task_tu("tb", name="bad")]
        async for _ in dispatch_task_batch(
                ctx=ctx, elog=elog, aid="step:p/s", task_tus=tus,
                tool_results=tool_results, state=state, ceiling=None):
            pass
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    by_id = {b.tool_use_id: b for b in tool_results}
    assert by_id["tg"].is_error is False
    assert by_id["tb"].is_error is True
    assert "RuntimeError: boom" in by_id["tb"].content
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_batch_over_ceiling_backfill_shape(tmp_path: Path) -> None:
    """AC11: an over-ceiling task tu emits ONLY its is_error tool_result event
    (no tool_call event); the under-ceiling tu emits a tool_call THEN a
    tool_result; both tool_call events precede either tool_result (pre-group)."""
    reset_parallelism_cache()

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        return json.dumps({"status": "Success"})

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
        elog = InMemoryEventLog()
        tool_results: list = []
        state = TurnState(tool_calls_used=0)
        tus = [_task_tu("t1"), _task_tu("t2")]  # ceiling 1 -> t1 runs, t2 backfilled
        async for _ in dispatch_task_batch(
                ctx=ctx, elog=elog, aid="step:p/s", task_tus=tus,
                tool_results=tool_results, state=state, ceiling=1):
            pass
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    rows = [e for e in elog.read() if e["agent_id"] == "step:p/s"]
    calls = [e for e in rows if e["type"] == "tool_call"]
    results = [e for e in rows if e["type"] == "tool_result"]
    # exactly one tool_call (for t1); none for the backfilled t2
    assert [c["payload"]["id"] for c in calls] == ["t1"]
    # t2's only event is its is_error tool_result
    t2_res = [r for r in results if r["payload"]["id"] == "t2"]
    assert len(t2_res) == 1 and t2_res[0]["payload"]["error"] is True
    # every tool_call precedes every tool_result in the log (pre-group minting)
    last_call = max(rows.index(c) for c in calls)
    first_result = min(rows.index(r) for r in results)
    assert last_call < first_result
    assert state.ceiling_tripped is True
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_batch_width_cap_bounds_concurrency(tmp_path: Path) -> None:
    """AC14: a batch wider than MAX_PARALLEL_TASKS runs <= cap concurrently and
    all children complete (the Part-2 limiter waits, never errors)."""
    reset_parallelism_cache()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("IMP_MAX_PARALLEL_TASKS", "2")
    try:
        gate = {"active": 0, "peak": 0}
        ctx = _ctx(tmp_path, spawn=_slow_spawn(gate, 0.03))
        elog = InMemoryEventLog()
        tool_results: list = []
        state = TurnState(tool_calls_used=0)
        tus = [_task_tu(f"t{i}") for i in range(4)]
        async for _ in dispatch_task_batch(
                ctx=ctx, elog=elog, aid="step:p/s", task_tus=tus,
                tool_results=tool_results, state=state, ceiling=None):
            pass
        assert gate["peak"] == 2                       # never exceeds the cap
        assert len(tool_results) == 4                  # all complete
    finally:
        monkeypatch.undo()
        reset_parallelism_cache()


@pytest.mark.anyio
async def test_batch_children_get_distinct_subagent_ids(tmp_path: Path) -> None:
    """AC20: concurrent children mint distinct `sub:` agent_ids / transcript dirs."""
    reset_parallelism_cache()
    seen: list = []

    async def _spawn(inv):
        seen.append((inv.agent_id, str(inv.transcript_dir)))
        return _SlowRunner({"active": 0, "peak": 0}, 0.0)

    ctx = _ctx(tmp_path, spawn=_spawn)
    elog = InMemoryEventLog()
    tool_results: list = []
    state = TurnState(tool_calls_used=0)
    tus = [_task_tu("t1"), _task_tu("t2")]
    async for _ in dispatch_task_batch(
            ctx=ctx, elog=elog, aid="step:p/s", task_tus=tus,
            tool_results=tool_results, state=state, ceiling=None):
        pass
    agent_ids = [a for a, _ in seen]
    dirs = [d for _, d in seen]
    assert len(set(agent_ids)) == 2 and all(a.startswith("sub:") for a in agent_ids)
    assert len(set(dirs)) == 2
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_batch_suspension_surfaces_bare(tmp_path: Path) -> None:
    """A child SubagentSuspended unwinds the turn as a BARE SubagentSuspended (the
    single-exception group is unwrapped), never as an ExceptionGroup."""
    reset_parallelism_cache()

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        raise SubagentSuspended("child paused")

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
        elog = InMemoryEventLog()
        tool_results: list = []
        state = TurnState(tool_calls_used=0)
        with pytest.raises(SubagentSuspended):
            async for _ in dispatch_task_batch(
                    ctx=ctx, elog=elog, aid="step:p/s", task_tus=[_task_tu("t1")],
                    tool_results=tool_results, state=state, ceiling=None):
                pass
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    assert tool_results == []                  # no batch tool_results on suspension
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_batch_child_body_bug_not_wrapped_in_group(tmp_path: Path) -> None:
    """AC19: a BaseException escaping a child wrapper re-raises as today's bare
    exception form, never an unhandled ExceptionGroup."""
    reset_parallelism_cache()

    class _Boom(BaseException):
        pass

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        raise _Boom("wrapper bug")

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
        elog = InMemoryEventLog()
        with pytest.raises(_Boom):
            async for _ in dispatch_task_batch(
                    ctx=ctx, elog=elog, aid="step:p/s", task_tus=[_task_tu("t1")],
                    tool_results=[], state=TurnState(tool_calls_used=0), ceiling=None):
                pass
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    reset_parallelism_cache()
