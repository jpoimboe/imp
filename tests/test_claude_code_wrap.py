"""Verify Claude Code tool wrapping behavior without driving `claude -p`.

Stubs the SDK's `tool` decorator with a passthrough that just returns the
wrapped function. The behavior under test is the *wrapper body* — captures
complete_step args, dispatches task, surfaces errors as is_error responses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.parallel import reset_parallelism_cache
from imp.agents.runner import ChangeContext
from imp.agents.tools.complete_step import make_complete_step_tool
from imp.agents.tools.context import ToolContext
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import _RunState, _wrap_one


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=(FsRule(
            operations=("read", "write"), paths=("**",), mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.change_context = ChangeContext(
        slug="test-change", title="Test Change", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow",
    )
    return ctx


def _passthrough_decorator(name, description, schema, annotations=None):  # type: ignore[no-untyped-def]
    """Stub `claude_agent_sdk.tool` — returns the function unchanged."""
    def _inner(fn):  # type: ignore[no-untyped-def]
        fn.__sdk_tool_name__ = name  # marker for assertions
        fn.__sdk_tool_description__ = description
        fn.__sdk_tool_schema__ = schema
        return fn
    return _inner


@pytest.mark.anyio
async def test_wrap_complete_step_captures_args(tmp_path: Path) -> None:
    state = _RunState()
    schema = OutputSchema(
        fields=(OutputField(
            name="status", type="enum",
            values=("Success", "Error"), required=True,
        ),),
        source=SourceLoc(file=Path("a.md"), line=1),
    )
    tool = make_complete_step_tool(schema)

    wrapped = _wrap_one(
        "complete_step", tool, _ctx(tmp_path), state, _passthrough_decorator,
    )

    out = await wrapped({"status": "Success"})
    assert state.complete_step_called is True
    assert state.complete_step_args == {"status": "Success"}
    assert out["content"][0]["text"] == "step complete"
    assert "isError" not in out  # success path


@pytest.mark.anyio
async def test_wrap_regular_tool_returns_invoke_result(tmp_path: Path) -> None:
    from imp.agents.tools import get_registry

    state = _RunState()
    read = get_registry()["read_file"]
    (tmp_path / "x.txt").write_text("hello")

    wrapped = _wrap_one(
        "read_file", read, _ctx(tmp_path), state, _passthrough_decorator,
    )
    out = await wrapped({"path": "x.txt"})
    assert out["content"][0]["text"] == "     1\thello"
    assert state.runtime_errors == []


@pytest.mark.anyio
async def test_wrap_tool_raises_surfaces_as_is_error(tmp_path: Path) -> None:
    from imp.agents.tools import get_registry

    state = _RunState()
    read = get_registry()["read_file"]

    wrapped = _wrap_one(
        "read_file", read, _ctx(tmp_path), state, _passthrough_decorator,
    )
    out = await wrapped({"path": "nonexistent.txt"})
    assert out.get("isError") is True
    assert "FileNotFoundError" in out["content"][0]["text"]
    assert state.runtime_errors  # error was recorded


@pytest.mark.anyio
async def test_wrap_task_routes_to_dispatch_task(tmp_path: Path) -> None:
    from imp.agents.subagents import make_task_tool

    sub_def = AgentDef(
        name="sub", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(
                name="status", type="enum",
                values=("Success", "Error"), required=True,
            ),),
            source=SourceLoc(file=Path("sub.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("sub.md"),
        source=SourceLoc(file=Path("sub.md"), line=1),
    )

    ctx = _ctx(tmp_path)
    ctx.subagent_defs = {"sub": sub_def}
    ctx.parent_transcript_dir = tmp_path / "parent"
    ctx.change_context = ChangeContext(
        slug="test-change", title="Test Change", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow",
    )

    class _FakeRunner:
        async def run(self, inv):
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        async def interrupt(self) -> None: ...

    async def fake_spawn(_inv: object) -> object:
        return _FakeRunner()

    ctx.coordinator = None  # no coordinator needed for this test
    ctx.spawn_subagent = fake_spawn

    state = _RunState()
    wrapped = _wrap_one(
        "task", make_task_tool(), ctx, state, _passthrough_decorator,
    )
    out = await wrapped({"subagent_name": "sub", "task_description": "do"})
    assert state.complete_step_called is False  # routed to dispatch_task, not capture
    text = out["content"][0]["text"]
    # dispatch_task returns JSON-stringified agent_output.
    assert json.loads(text) == {"status": "Success"}


@pytest.mark.anyio
async def test_wrap_task_emits_matching_call_and_result(tmp_path: Path) -> None:
    # Part 1: dispatching `task` via _impl must mint a real block id and emit
    # EXACTLY ONE task tool_call AND EXACTLY ONE task tool_result, BOTH carrying
    # the SAME non-empty block id (not the empty __tool_use_id__). The matching id
    # is what lets narrative's _h_tool_result close the fold carrying the outcome.
    from imp.agents.subagents import make_task_tool
    from imp.engine.state import InMemoryEventLog

    sub_def = AgentDef(
        name="sub", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(
                name="status", type="enum",
                values=("Success", "Error"), required=True,
            ),),
            source=SourceLoc(file=Path("sub.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("sub.md"),
        source=SourceLoc(file=Path("sub.md"), line=1),
    )

    ctx = _ctx(tmp_path)
    ctx.subagent_defs = {"sub": sub_def}
    ctx.parent_transcript_dir = tmp_path / "parent"
    ctx.change_context = ChangeContext(
        slug="test-change", title="Test Change", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow",
    )
    elog = InMemoryEventLog()
    ctx.event_log = elog

    class _FakeRunner:
        async def run(self, inv):
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        async def interrupt(self) -> None: ...

    async def fake_spawn(_inv: object) -> object:
        return _FakeRunner()

    ctx.coordinator = None
    ctx.spawn_subagent = fake_spawn

    state = _RunState()
    state.agent_id = "step:p/task-lead"
    wrapped = _wrap_one(
        "task", make_task_tool(), ctx, state, _passthrough_decorator,
    )
    # The SDK passes the (empty) __tool_use_id__; the mint must ignore it.
    out = await wrapped({"subagent_name": "sub", "task_description": "do",
                         "__tool_use_id__": ""})
    assert json.loads(out["content"][0]["text"]) == {"status": "Success"}

    events = elog._events
    calls = [e for e in events
             if e["type"] == "tool_call" and e["payload"]["name"] == "task"]
    assert len(calls) == 1, "expected exactly one task tool_call"
    call_id = calls[0]["payload"]["id"]
    assert call_id, "minted block id must be non-empty"
    # Exactly one tool_result keyed on the SAME minted id as the call.
    matching = [e for e in events
                if e["type"] == "tool_result" and e["payload"]["id"] == call_id]
    assert len(matching) == 1, "expected exactly one task tool_result with matching id"
    assert matching[0]["payload"]["error"] is False


@pytest.mark.anyio
async def test_wrap_bash_killed_records_state(tmp_path: Path) -> None:
    """_impl wrapper records signal-killed bash invocations on _RunState
    so the receive loop can emit tool_killed events later."""
    import json

    from imp.agents.tools import get_registry
    from imp.providers.claude_code import _RunState, _wrap_one

    state = _RunState()
    bash_tool = get_registry()["bash"]

    # Stub: the tool's fn returns a JSON string with returncode -15 (SIGTERM).
    # We can't actually kill a real sleep here without timing flakiness, so
    # monkey-patch the registry entry's fn to return the canonical kill shape.
    from dataclasses import replace as dc_replace
    killed_tool = dc_replace(
        bash_tool,
        fn=lambda ctx, **kw: json.dumps({
            "stdout": "", "stderr": "", "returncode": -15,
        }),
    )

    wrapped = _wrap_one(
        "bash", killed_tool, _ctx(tmp_path), state, _passthrough_decorator,
    )
    out = await wrapped({"command": "sleep 10"})
    # Tool returns successfully (the runner records the kill, doesn't error).
    assert out.get("isError") is not True
    # killed_tool_uses captured the signal.
    assert len(state.killed_tool_uses) == 1
    assert state.killed_tool_uses[0]["name"] == "bash"
    assert state.killed_tool_uses[0]["signal"] == 15


@pytest.mark.anyio
async def test_wrapper_sweeps_bg_on_every_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 10 dispatch site, claude_code edition: every MCP tool dispatch
    sweeps the background registry — module-attr call so one monkeypatch
    covers all sites."""
    import imp.agents.tools.bg as bg_mod
    from imp.agents.tools import get_registry

    calls = {"n": 0}
    real = bg_mod._sweep_bg

    async def counting(ctx):
        calls["n"] += 1
        await real(ctx)

    monkeypatch.setattr(bg_mod, "_sweep_bg", counting)
    ctx = _ctx(tmp_path)
    state = _RunState()
    (tmp_path / "f.txt").write_text("data")
    wrapped = _wrap_one("read_file", get_registry()["read_file"], ctx, state,
                        _passthrough_decorator)
    out = await wrapped({"path": "f.txt"})
    assert "data" in out["content"][0]["text"]
    assert calls["n"] == 1


def test_cc_task_tool_annotated_readonly_when_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC23: claude_code wraps `task` with ToolAnnotations(readOnlyHint=True) via the
    real claude_agent_sdk.tool annotations= parameter, at width > 1."""
    from claude_agent_sdk import tool as sdk_tool

    from imp.agents.subagents import make_task_tool
    monkeypatch.delenv("IMP_MAX_PARALLEL_TASKS", raising=False)
    reset_parallelism_cache()
    try:
        wrapped = _wrap_one(
            "task", make_task_tool(), _ctx(tmp_path), _RunState(), sdk_tool)
        assert wrapped.annotations is not None
        assert wrapped.annotations.readOnlyHint is True
    finally:
        reset_parallelism_cache()


def test_cc_non_task_tool_not_annotated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC23: ONLY `task` is annotated — read_file wraps with annotations=None."""
    from claude_agent_sdk import tool as sdk_tool

    from imp.agents.tools import get_registry
    monkeypatch.delenv("IMP_MAX_PARALLEL_TASKS", raising=False)
    reset_parallelism_cache()
    try:
        wrapped = _wrap_one(
            "read_file", get_registry()["read_file"], _ctx(tmp_path),
            _RunState(), sdk_tool)
        assert wrapped.annotations is None
    finally:
        reset_parallelism_cache()


def test_cc_task_annotation_dropped_by_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC21 (bridge half): IMP_MAX_PARALLEL_TASKS=1 drops the readOnlyHint at the
    claude_code registration site."""
    from claude_agent_sdk import tool as sdk_tool

    from imp.agents.subagents import make_task_tool
    monkeypatch.setenv("IMP_MAX_PARALLEL_TASKS", "1")
    reset_parallelism_cache()
    try:
        wrapped = _wrap_one(
            "task", make_task_tool(), _ctx(tmp_path), _RunState(), sdk_tool)
        assert wrapped.annotations is None
    finally:
        reset_parallelism_cache()


def test_cc_task_annotation_derived_from_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC23: the annotation is DERIVED from the allowlist-mode flag, not hardcoded —
    flip CLAUDE_CODE_ALLOWLIST_MODE off and the hint disappears even at width > 1."""
    from claude_agent_sdk import tool as sdk_tool

    from imp.agents.subagents import make_task_tool
    monkeypatch.delenv("IMP_MAX_PARALLEL_TASKS", raising=False)
    reset_parallelism_cache()
    monkeypatch.setattr(
        "imp.providers.claude_code.CLAUDE_CODE_ALLOWLIST_MODE", False)
    try:
        wrapped = _wrap_one(
            "task", make_task_tool(), _ctx(tmp_path), _RunState(), sdk_tool)
        assert wrapped.annotations is None
    finally:
        reset_parallelism_cache()


def _sub_def() -> AgentDef:
    return AgentDef(
        name="sub", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("sub.md"), line=1)),
        prompt_template_source="hi", source_path=Path("sub.md"),
        source=SourceLoc(file=Path("sub.md"), line=1))


@pytest.mark.anyio
async def test_wrap_task_success_result_stamps_id_ns_block(tmp_path: Path) -> None:
    # AC38 (production): _impl's canonical SUCCESS task tool_result (keyed on the
    # minted non-empty block id) stamps id_ns="block".
    from imp.agents.subagents import make_task_tool
    from imp.engine.state import InMemoryEventLog

    ctx = _ctx(tmp_path)
    ctx.subagent_defs = {"sub": _sub_def()}
    ctx.parent_transcript_dir = tmp_path / "parent"
    elog = InMemoryEventLog()
    ctx.event_log = elog

    class _FakeRunner:
        async def run(self, inv):
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        async def interrupt(self) -> None: ...

    async def fake_spawn(_inv: object) -> object:
        return _FakeRunner()

    ctx.coordinator = None
    ctx.spawn_subagent = fake_spawn

    state = _RunState()
    state.agent_id = "step:p/task-lead"
    wrapped = _wrap_one("task", make_task_tool(), ctx, state, _passthrough_decorator)
    out = await wrapped({"subagent_name": "sub", "task_description": "do",
                         "__tool_use_id__": ""})
    assert json.loads(out["content"][0]["text"]) == {"status": "Success"}

    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["payload"]["error"] is False
    assert results[0]["payload"]["id_ns"] == "block"


@pytest.mark.anyio
async def test_wrap_task_error_result_stamps_id_ns_block(tmp_path: Path) -> None:
    # AC38 (production): _impl's error-close task tool_result (dispatch raises an
    # ordinary exception) also stamps id_ns="block" on the same minted block id.
    from imp.agents.subagents import make_task_tool
    from imp.engine.state import InMemoryEventLog

    ctx = _ctx(tmp_path)
    ctx.subagent_defs = {"sub": _sub_def()}
    ctx.parent_transcript_dir = tmp_path / "parent"
    elog = InMemoryEventLog()
    ctx.event_log = elog

    class _BoomRunner:
        async def run(self, inv):
            raise ValueError("kaboom")
            yield  # pragma: no cover  (make it an async generator)
        async def interrupt(self) -> None: ...

    async def fake_spawn(_inv: object) -> object:
        return _BoomRunner()

    ctx.coordinator = None
    ctx.spawn_subagent = fake_spawn

    state = _RunState()
    state.agent_id = "step:p/task-lead"
    wrapped = _wrap_one("task", make_task_tool(), ctx, state, _passthrough_decorator)
    out = await wrapped({"subagent_name": "sub", "task_description": "do",
                         "__tool_use_id__": ""})
    assert out.get("isError") is True

    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["payload"]["error"] is True
    assert results[0]["payload"]["id_ns"] == "block"


@pytest.mark.anyio
async def test_impl_inflight_counter_peaks_at_one_then_drains(tmp_path: Path) -> None:
    # AC26: the inflight counter is 1 WHILE dispatch_task is awaiting and 0 after it
    # returns. A slow fake spawn lets us observe the in-flight value mid-dispatch.
    import anyio

    from imp.agents.subagents import make_task_tool
    from imp.engine.state import InMemoryEventLog

    ctx = _ctx(tmp_path)
    ctx.subagent_defs = {"sub": _sub_def()}
    ctx.parent_transcript_dir = tmp_path / "parent"
    ctx.event_log = InMemoryEventLog()

    observed: dict[str, int] = {}

    class _SlowRunner:
        async def run(self, inv):
            observed["during"] = state.inflight_task_dispatches
            await anyio.sleep(0)
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        async def interrupt(self) -> None: ...

    async def fake_spawn(_inv: object) -> object:
        return _SlowRunner()

    ctx.coordinator = None
    ctx.spawn_subagent = fake_spawn
    state = _RunState()
    state.agent_id = "step:p/task-lead"

    wrapped = _wrap_one("task", make_task_tool(), ctx, state, _passthrough_decorator)
    assert state.inflight_task_dispatches == 0      # before
    await wrapped({"subagent_name": "sub", "task_description": "do",
                  "__tool_use_id__": ""})
    assert observed["during"] == 1                  # incremented around the await
    assert state.inflight_task_dispatches == 0      # decremented in finally


@pytest.mark.anyio
async def test_impl_inflight_counter_decrements_on_dispatch_error(tmp_path: Path) -> None:
    # AC26: a dispatch that raises an ordinary exception still decrements the counter
    # (finally on the error path) — the degraded is_error response is returned.
    from imp.agents.subagents import make_task_tool
    from imp.engine.state import InMemoryEventLog

    ctx = _ctx(tmp_path)
    ctx.subagent_defs = {"sub": _sub_def()}
    ctx.parent_transcript_dir = tmp_path / "parent"
    ctx.event_log = InMemoryEventLog()

    class _BoomRunner:
        async def run(self, inv):
            raise ValueError("kaboom")
            yield  # pragma: no cover
        async def interrupt(self) -> None: ...

    async def fake_spawn(_inv: object) -> object:
        return _BoomRunner()

    ctx.coordinator = None
    ctx.spawn_subagent = fake_spawn
    state = _RunState()
    state.agent_id = "step:p/task-lead"

    wrapped = _wrap_one("task", make_task_tool(), ctx, state, _passthrough_decorator)
    out = await wrapped({"subagent_name": "sub", "task_description": "do",
                        "__tool_use_id__": ""})
    assert out.get("isError") is True
    assert state.inflight_task_dispatches == 0      # decremented despite the raise


@pytest.mark.anyio
async def test_impl_refuses_task_when_membership_frozen(tmp_path: Path) -> None:
    # AC29 (claude_code): with awaiting_user already set, a NEW task is refused
    # WITHOUT dispatch — _impl emits the bare TASK_REFUSAL_SENTINEL as the minted
    # fold-closing is_error result and returns it, producing exactly one
    # tool_call + sentinel tool_result pair.
    from imp.agents.subagents import TASK_REFUSAL_SENTINEL, make_task_tool
    from imp.engine.state import InMemoryEventLog

    ctx = _ctx(tmp_path)
    ctx.subagent_defs = {"sub": _sub_def()}
    ctx.parent_transcript_dir = tmp_path / "parent"
    elog = InMemoryEventLog()
    ctx.event_log = elog

    spawned = {"n": 0}

    class _Runner:
        async def run(self, inv):
            spawned["n"] += 1
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        async def interrupt(self) -> None: ...

    async def fake_spawn(_inv: object) -> object:
        return _Runner()

    ctx.coordinator = None
    ctx.spawn_subagent = fake_spawn
    state = _RunState()
    state.agent_id = "step:p/task-lead"
    state.awaiting_user = True       # membership frozen by a prior wait_for_user

    wrapped = _wrap_one("task", make_task_tool(), ctx, state, _passthrough_decorator)
    out = await wrapped({"subagent_name": "sub", "task_description": "do",
                        "__tool_use_id__": ""})

    assert spawned["n"] == 0                                    # never dispatched
    assert out.get("isError") is True
    assert out["content"][0]["text"] == TASK_REFUSAL_SENTINEL   # bare, unwrapped
    calls = [e for e in elog.read() if e["type"] == "tool_call"]
    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert len(calls) == 1 and len(results) == 1               # one pair
    assert results[0]["payload"]["content"] == TASK_REFUSAL_SENTINEL
    assert results[0]["payload"]["error"] is True
    assert results[0]["payload"]["id_ns"] == "block"
    assert state.inflight_task_dispatches == 0                 # finally still ran


@pytest.mark.anyio
async def test_impl_raises_when_dispatched_off_the_runner_loop(
        tmp_path: Path) -> None:
    # AC34: _impl raises if it runs on a loop other than the one the runner
    # recorded (state.runner_loop). A production check, exercised by setting a
    # mismatched runner_loop.
    import asyncio

    from imp.agents.subagents import make_task_tool

    foreign = asyncio.new_event_loop()
    try:
        ctx = _ctx(tmp_path)
        ctx.subagent_defs = {"sub": _sub_def()}
        ctx.parent_transcript_dir = tmp_path / "parent"
        state = _RunState()
        state.agent_id = "step:p/task-lead"
        state.runner_loop = foreign      # not the loop this test runs on
        wrapped = _wrap_one("task", make_task_tool(), ctx, state,
                            _passthrough_decorator)
        with pytest.raises(RuntimeError, match="runner loop"):
            await wrapped({"subagent_name": "sub", "task_description": "do",
                          "__tool_use_id__": ""})
    finally:
        foreign.close()
