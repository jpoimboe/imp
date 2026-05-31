"""Verify Claude Code tool wrapping behavior without driving `claude -p`.

Stubs the SDK's `tool` decorator with a passthrough that just returns the
wrapped function. The behavior under test is the *wrapper body* — captures
complete_step args, dispatches task, surfaces errors as is_error responses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
            operations=("read", "write"), paths=("/project/**",), mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.change_context = ChangeContext(
        slug="test-change", title="Test Change", project_root=tmp_path,
    )
    return ctx


def _passthrough_decorator(name: str, description: str, schema):  # type: ignore[no-untyped-def]
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
    out = await wrapped({"path": "/project/x.txt"})
    assert out["content"][0]["text"] == "hello"
    assert state.runtime_errors == []


@pytest.mark.anyio
async def test_wrap_tool_raises_surfaces_as_is_error(tmp_path: Path) -> None:
    from imp.agents.tools import get_registry

    state = _RunState()
    read = get_registry()["read_file"]

    wrapped = _wrap_one(
        "read_file", read, _ctx(tmp_path), state, _passthrough_decorator,
    )
    out = await wrapped({"path": "/project/nonexistent.txt"})
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
    )

    async def fake_spawn(_inv: object) -> object:
        async def _gen() -> object:
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        return _gen()

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
