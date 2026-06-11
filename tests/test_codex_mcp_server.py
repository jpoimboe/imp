from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from imp.agents.parallel import reset_parallelism_cache
from imp.agents.subagents import make_task_tool
from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.providers.codex_mcp import RunState, _register_tool, start_server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        project_root=tmp_path,
        fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
        subagent_defs={},
        provider_name="codex",
        model_name="gpt-5.5",
        parent_transcript_dir=tmp_path,
    )


def _echo_tool() -> Tool:
    def _fn(ctx, text):
        return f"echo:{text}"
    return Tool(name="echo", description="echo",
                input_schema={"type": "object",
                              "properties": {"text": {"type": "string"}},
                              "required": ["text"]}, fn=_fn)


@pytest.mark.anyio
async def test_server_binds_ephemeral_port_and_returns_url(tmp_path: Path) -> None:
    state = RunState(cap=10)
    handle = await start_server(
        tools={"echo": _echo_tool()}, ctx=_ctx(tmp_path), state=state,
        bearer_token="secret-token-123", host="127.0.0.1",
    )
    try:
        # Returns a real listening port (NOT zero).
        assert handle.port > 0
        assert handle.url.startswith("http://127.0.0.1:")
        # Server task is running.
        assert handle.task is not None and not handle.task.done()
    finally:
        await handle.shutdown()


@pytest.mark.anyio
async def test_server_rejects_request_without_bearer_token(tmp_path: Path) -> None:
    state = RunState(cap=10)
    handle = await start_server(
        tools={"echo": _echo_tool()}, ctx=_ctx(tmp_path), state=state,
        bearer_token="t-correct", host="127.0.0.1",
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(handle.url, json={"jsonrpc": "2.0", "id": 1,
                                                    "method": "initialize",
                                                    "params": {}})
            # No bearer → 401 Unauthorized.
            assert r.status_code == 401
    finally:
        await handle.shutdown()


@pytest.mark.anyio
async def test_server_rejects_request_with_wrong_bearer(tmp_path: Path) -> None:
    state = RunState(cap=10)
    handle = await start_server(
        tools={"echo": _echo_tool()}, ctx=_ctx(tmp_path), state=state,
        bearer_token="t-correct", host="127.0.0.1",
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                handle.url, json={"jsonrpc": "2.0", "id": 1,
                                  "method": "initialize", "params": {}},
                headers={"Authorization": "Bearer t-wrong"})
            assert r.status_code == 401
    finally:
        await handle.shutdown()


@pytest.mark.anyio
async def test_server_accepts_request_with_correct_bearer(tmp_path: Path) -> None:
    state = RunState(cap=10)
    handle = await start_server(
        tools={"echo": _echo_tool()}, ctx=_ctx(tmp_path), state=state,
        bearer_token="t-good", host="127.0.0.1",
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                handle.url, json={"jsonrpc": "2.0", "id": 1,
                                  "method": "initialize", "params": {}},
                headers={"Authorization": "Bearer t-good"})
            assert r.status_code != 401 and r.status_code != 403
    finally:
        await handle.shutdown()


def _register(tool, ctx):
    from mcp.server.fastmcp import FastMCP
    app = FastMCP("imp")
    _register_tool(app, tool, ctx=ctx, state=RunState(cap=10))
    return app._tool_manager._tools[tool.name]


def test_codex_task_tool_annotated_readonly_when_parallel(
    tmp_path: Path, monkeypatch
) -> None:
    """AC23: with the bypass posture on and width > 1 (default 8), the registered
    `task` tool carries ToolAnnotations(readOnlyHint=True) — the CLI MCP-parallelism
    unlock."""
    monkeypatch.delenv("IMP_MAX_PARALLEL_TASKS", raising=False)
    reset_parallelism_cache()
    try:
        mcp_tool = _register(make_task_tool(), _ctx(tmp_path))
        assert mcp_tool.annotations is not None
        assert mcp_tool.annotations.readOnlyHint is True
    finally:
        reset_parallelism_cache()


def test_codex_non_task_tool_not_annotated(tmp_path: Path, monkeypatch) -> None:
    """AC23: ONLY `task` is annotated — a non-task tool (echo) registers with
    annotations=None even at width > 1."""
    monkeypatch.delenv("IMP_MAX_PARALLEL_TASKS", raising=False)
    reset_parallelism_cache()
    try:
        mcp_tool = _register(_echo_tool(), _ctx(tmp_path))
        assert mcp_tool.annotations is None
    finally:
        reset_parallelism_cache()


def test_codex_task_annotation_dropped_by_kill_switch(
    tmp_path: Path, monkeypatch
) -> None:
    """AC21 (bridge half): IMP_MAX_PARALLEL_TASKS=1 drops the readOnlyHint at the
    codex registration site, re-serializing dispatch end-to-end."""
    monkeypatch.setenv("IMP_MAX_PARALLEL_TASKS", "1")
    reset_parallelism_cache()
    try:
        mcp_tool = _register(make_task_tool(), _ctx(tmp_path))
        assert mcp_tool.annotations is None
    finally:
        reset_parallelism_cache()


def test_codex_task_annotation_derived_from_bypass(
    tmp_path: Path, monkeypatch
) -> None:
    """AC23: the annotation is DERIVED from the bypass posture, not hardcoded — flip
    CODEX_APPROVAL_POLICY off the bypass value and the hint disappears even at width
    > 1 (the honesty-note derivation pin)."""
    monkeypatch.delenv("IMP_MAX_PARALLEL_TASKS", raising=False)
    reset_parallelism_cache()
    monkeypatch.setattr(
        "imp.providers.codex_mcp.CODEX_APPROVAL_POLICY", "untrusted")
    try:
        mcp_tool = _register(make_task_tool(), _ctx(tmp_path))
        assert mcp_tool.annotations is None
    finally:
        reset_parallelism_cache()


@pytest.mark.anyio
async def test_fastmcp_roundtrip_refusal_is_error_suffix_sentinel(
    tmp_path: Path,
) -> None:
    """AC27: a refused task, driven through the real FastMCP Tool.run, surfaces
    as a ToolError whose wire text suffix-equals TASK_REFUSAL_SENTINEL (FastMCP
    wraps the raise as 'Error executing tool task: <sentinel>')."""
    from types import SimpleNamespace

    from mcp.server.fastmcp.exceptions import ToolError

    from imp.agents.subagents import TASK_REFUSAL_SENTINEL
    ctx = _ctx(tmp_path)
    ctx.coordinator = SimpleNamespace(should_pause=True)  # freeze membership
    mcp_tool = _register(make_task_tool(), ctx)
    with pytest.raises(ToolError) as ei:
        await mcp_tool.run(
            {"subagent_name": "impl", "task_description": "x"})
    assert str(ei.value).endswith(TASK_REFUSAL_SENTINEL)
    assert str(ei.value).startswith("Error executing tool task:")


@pytest.mark.anyio
async def test_start_server_raises_off_the_runner_loop(tmp_path: Path) -> None:
    # AC34: start_server refuses to run when state.runner_loop is a DIFFERENT loop
    # than the one it is invoked on (the foreign-loop topology guard). A production
    # check, exercised here by setting a mismatched runner_loop.
    import asyncio

    foreign = asyncio.new_event_loop()
    try:
        state = RunState(cap=10)
        state.runner_loop = foreign   # not the loop this test runs on
        with pytest.raises(RuntimeError, match="runner"):
            await start_server(
                tools={"echo": _echo_tool()}, ctx=_ctx(tmp_path), state=state,
                bearer_token="secret-token-123", host="127.0.0.1")
    finally:
        foreign.close()
