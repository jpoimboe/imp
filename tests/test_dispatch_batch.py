from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import anyio
import pytest

import imp.agents.dispatch as dispatch_mod
from imp.agents.dispatch import TurnState, _dispatch_one_task_inline, dispatch_task_batch
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
    ctx = ToolContext(project_root=tmp_path,
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
        title="Change 42", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    return ctx


def _task_tu(tu_id: str, name: str = "reviewer", desc: str = "go") -> ToolUseBlock:
    return ToolUseBlock(id=tu_id, name="sub_agent",
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


@pytest.mark.anyio
async def test_dispatch_one_tool_use_rejects_task_tu(tmp_path: Path) -> None:
    """AC16: a `task` tu reaching dispatch_one_tool_use raises RuntimeError (the
    load-bearing raise-not-assert guard — tasks must route through the batch)."""
    from imp.agents.dispatch import dispatch_one_tool_use

    ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
    with pytest.raises(RuntimeError, match="dispatch_task_batch"):
        await dispatch_one_tool_use(
            ctx=ctx, elog=InMemoryEventLog(), aid="step:p/s", invocation=None,
            tools={}, tu=_task_tu("t1"), tool_results=[],
            state=TurnState(tool_calls_used=0), ceiling=None)



@pytest.mark.anyio
async def test_batch_child_suspension_becomes_is_error(tmp_path: Path) -> None:
    reset_parallelism_cache()

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None, **kwargs):
        if name == "paused":
            raise SubagentSuspended("child paused")
        return json.dumps({"status": "Success"})

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
        elog = InMemoryEventLog()
        tool_results: list = []
        state = TurnState(tool_calls_used=0)
        tus = [_task_tu("t1", name="paused"), _task_tu("t2", name="ok")]
        collected: list = []
        async for ev in dispatch_task_batch(
                ctx=ctx, elog=elog, aid="step:p/s", task_tus=tus,
                tool_results=tool_results, state=state, ceiling=None):
            collected.append(ev)
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    by_id = {b.tool_use_id: b for b in tool_results}
    assert by_id["t1"].is_error is True
    assert "SubagentSuspended" in by_id["t1"].content
    assert by_id["t2"].is_error is False                # sibling intact
    assert len(collected) == 2                          # one heartbeat per tu
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_batch_cancellation_cancels_children_cleanly(tmp_path: Path) -> None:
    """AC18: a surrounding cancel (watchdog/aclose) during the batch group await
    cancels all in-flight children via the task group; each child's finally runs
    (clean teardown) and no result is appended. Falsifiable: a regression that
    swallowed cancellation in a child wrapper (e.g. catching BaseException) would
    leave finally un-run or append results."""
    reset_parallelism_cache()
    finals = {"count": 0}

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        try:
            await anyio.sleep(10)
            return json.dumps({"status": "Success"})
        finally:
            finals["count"] += 1

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
        elog = InMemoryEventLog()
        tool_results: list = []
        state = TurnState(tool_calls_used=0)
        tus = [_task_tu("t1"), _task_tu("t2")]
        with anyio.move_on_after(0.05):
            async for _ in dispatch_task_batch(
                    ctx=ctx, elog=elog, aid="step:p/s", task_tus=tus,
                    tool_results=tool_results, state=state, ceiling=None):
                pass
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    assert finals["count"] == 2                     # both children cancelled cleanly
    assert tool_results == []                       # no results from cancelled children
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_batch_success_and_error_results_stamp_id_ns_block(tmp_path: Path) -> None:
    # AC38 (production): a dispatched child's success result AND a child-error
    # result each carry id_ns="block" (fold opened by a non-empty block id).
    reset_parallelism_cache()

    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        if desc == "boom":
            raise ValueError("kaboom")
        return json.dumps({"status": "Success", "echo": desc})

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
        elog = InMemoryEventLog()
        tool_results: list = []
        state = TurnState(tool_calls_used=0)
        tus = [_task_tu("t1", desc="ok"), _task_tu("t2", desc="boom")]
        async for _ in dispatch_task_batch(
                ctx=ctx, elog=elog, aid="step:p/s", task_tus=tus,
                tool_results=tool_results, state=state, ceiling=None):
            pass
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]

    results = [e for e in elog.read() if e["type"] == "tool_result"]
    ns = {e["payload"]["id"]: e["payload"].get("id_ns") for e in results}
    assert ns["t1"] == "block"          # success child
    assert ns["t2"] == "block"          # error child
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_batch_over_ceiling_backfill_result_is_unstamped(tmp_path: Path) -> None:
    # AC11/AC38: an over-ceiling tu opens no fold, so its backfill is_error
    # tool_result must NOT be stamped (an id_ns="block" stamp on a fold-less
    # result would render a malformed-log warning in narrative). ceiling=0 → the
    # first (and only) tu is backfilled, never dispatched.
    reset_parallelism_cache()
    ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
    elog = InMemoryEventLog()
    tool_results: list = []
    state = TurnState(tool_calls_used=0)
    async for _ in dispatch_task_batch(
            ctx=ctx, elog=elog, aid="step:p/s", task_tus=[_task_tu("t1")],
            tool_results=tool_results, state=state, ceiling=0):
        pass
    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["payload"]["error"] is True       # backfill is_error
    assert "id_ns" not in results[0]["payload"]         # unstamped → orphan arm
    reset_parallelism_cache()


@pytest.mark.anyio
async def test_inline_kill_switch_task_result_stamps_id_ns_block(tmp_path: Path) -> None:
    # AC21/AC38: the kill-switch inline single-task path also opens a fold by a
    # non-empty block id, so its success result stamps id_ns="block".
    from imp.agents.dispatch import _dispatch_one_task_inline
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
        await _dispatch_one_task_inline(
            ctx=ctx, elog=elog, aid="step:p/s", tu=_task_tu("t1"),
            tool_results=tool_results, state=state, ceiling=None)
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]
    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["payload"]["id"] == "t1"
    assert results[0]["payload"]["id_ns"] == "block"
    reset_parallelism_cache()


class _NestingRunner:
    """Holds a parent-level limiter slot while running its own nested dispatch_task_batch."""

    def __init__(self, child_root: Path, completed: list, leaf_gate: dict) -> None:
        self._root = child_root
        self._completed = completed
        self._gate = leaf_gate

    async def run(self, _inv):
        child_ctx = _ctx(self._root, spawn=_slow_spawn(self._gate, 0.03))
        leaf_results: list = []
        async for _ in dispatch_task_batch(
                ctx=child_ctx, elog=InMemoryEventLog(), aid="step:c",
                task_tus=[_task_tu("c1"), _task_tu("c2")],
                tool_results=leaf_results, state=TurnState(tool_calls_used=0),
                ceiling=None):
            pass
        self._completed.append(len(leaf_results))
        yield {"type": "agent_output", "payload": {"status": "Success"}}

    async def interrupt(self) -> None: ...


def _nesting_spawn(tmp_path: Path, completed: list, leaf_gate: dict):
    counter = {"i": 0}

    async def _spawn(_inv):
        idx = counter["i"]
        counter["i"] += 1
        return _NestingRunner(tmp_path / f"child{idx}", completed, leaf_gate)

    return _spawn


@pytest.mark.anyio
async def test_depth2_nested_dispatch_uses_fresh_limiters_no_deadlock(
    tmp_path: Path,
) -> None:
    """AC15: depth-2 nested dispatch uses fresh per-invocation limiters; no deadlock."""
    reset_parallelism_cache()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("IMP_MAX_PARALLEL_TASKS", "2")
    try:
        completed: list = []
        leaf_gate = {"active": 0, "peak": 0}
        ctx = _ctx(tmp_path, spawn=_nesting_spawn(tmp_path, completed, leaf_gate))
        elog = InMemoryEventLog()
        tool_results: list = []
        state = TurnState(tool_calls_used=0)
        tus = [_task_tu("p1"), _task_tu("p2")]   # 2 == cap -> parent limiter full
        with anyio.fail_after(5):
            async for _ in dispatch_task_batch(
                    ctx=ctx, elog=elog, aid="step:p/s", task_tus=tus,
                    tool_results=tool_results, state=state, ceiling=None):
                pass
        assert len(tool_results) == 2            # both parent tasks settled
        assert len(completed) == 2               # both children ran a nested batch
        assert sum(completed) == 4               # every nested leaf completed
    finally:
        monkeypatch.undo()
        reset_parallelism_cache()


@pytest.mark.anyio
async def test_batch_stamps_subagent_label_on_task_tool_call(tmp_path: Path) -> None:
    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        return json.dumps({"status": "Success"})

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
        ctx.subagent_defs = {
            "explorer": dataclasses.replace(_agent("explorer"),
                                            label="exploring codebase"),
            "reviewer": _agent("reviewer"),   # no label
        }
        elog = InMemoryEventLog()
        state = TurnState(tool_calls_used=0)
        tus = [_task_tu("t1", "explorer"), _task_tu("t2", "reviewer"), _task_tu("t3", "ghost")]
        [ev async for ev in dispatch_task_batch(
            ctx=ctx, elog=elog, aid="step:p/s", task_tus=tus,
            tool_results=[], state=state, ceiling=None)]
        calls = {e["payload"]["id"]: e["payload"]
                 for e in elog.read() if e["type"] == "tool_call"}
        assert calls["t1"]["label"] == "exploring codebase"
        assert "label" not in calls["t2"]            # unlabeled agent: omitted
        assert "label" not in calls["t3"]            # unknown agent name: omitted
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]


@pytest.mark.anyio
async def test_inline_stamps_subagent_label_on_task_tool_call(tmp_path: Path) -> None:
    async def _fake(ctx, name, desc, *, tool_call_event_id="", tier=None):
        return json.dumps({"status": "Success"})

    monkey = dispatch_mod.dispatch_task
    dispatch_mod.dispatch_task = _fake  # type: ignore[assignment]
    try:
        ctx = _ctx(tmp_path, spawn=_slow_spawn({"active": 0, "peak": 0}, 0.0))
        ctx.subagent_defs = {
            "explorer": dataclasses.replace(_agent("explorer"),
                                            label="exploring codebase"),
        }
        elog = InMemoryEventLog()
        await _dispatch_one_task_inline(
            ctx=ctx, elog=elog, aid="step:p/s", tu=_task_tu("t1", "explorer"),
            tool_results=[], state=TurnState(tool_calls_used=0), ceiling=None)
        call = next(e["payload"] for e in elog.read() if e["type"] == "tool_call")
        assert call["label"] == "exploring codebase"
    finally:
        dispatch_mod.dispatch_task = monkey  # type: ignore[assignment]


def test_spawn_tool_offered_as_sub_agent() -> None:
    from imp.agents.subagents import make_task_tool
    assert make_task_tool().name == "sub_agent"

