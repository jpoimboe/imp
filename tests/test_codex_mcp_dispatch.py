from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.providers.codex_mcp import RunState, dispatch_tool


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl", subagent_defs={},
        provider_name="codex", model_name="gpt-5.5",
        parent_transcript_dir=tmp_path, interactive=False,
    )


def _echo_tool() -> Tool:
    def _fn(ctx: Any, text: str) -> str:
        return f"echo:{text}"
    return Tool(name="echo", description="echo",
                input_schema={"type": "object",
                              "properties": {"text": {"type": "string"}},
                              "required": ["text"]}, fn=_fn)


def test_dispatch_invokes_imp_tool_and_returns_text(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    state = RunState(cap=10)
    out = asyncio.run(dispatch_tool(
        tool=_echo_tool(), args={"text": "hi"}, ctx=ctx, state=state))
    assert out == "echo:hi"
    assert state.dispatch_count == 1


def test_dispatch_complete_step_captures_args(tmp_path: Path) -> None:
    """The wrapper for `complete_step` writes args into state and returns OK
    without raising — the runner reads state at the next turn boundary."""
    ctx = _ctx(tmp_path)
    state = RunState(cap=10)

    def _cs(ctx: Any, **kwargs: Any) -> str:
        # Real complete_step raises StepComplete; the wrapper intercepts BEFORE
        # delegating, so this body is never reached.
        raise AssertionError("complete_step body must not run inside the wrapper")

    cs = Tool(name="complete_step", description="",
              input_schema={"type": "object", "properties": {"answer": {"type": "string"}},
                            "required": ["answer"]},
              fn=_cs)
    out = asyncio.run(dispatch_tool(
        tool=cs, args={"answer": "done"}, ctx=ctx, state=state))
    assert state.complete_step_args == {"answer": "done"}
    assert state.complete_step_called is True  # ADD THIS LINE
    assert "step complete" in out
    assert state.dispatch_count == 1


def test_dispatch_cap_trip_returns_cap_error_no_await_between(tmp_path: Path) -> None:
    """The cap RMW (`n += 1; if n > cap: cap-error`) must contain no `await`
    between read and decision. Confirmed by exceeding the cap and asserting
    the cap-error string is returned + the runner-side `abort_requested` flag
    is set."""
    ctx = _ctx(tmp_path)
    state = RunState(cap=2)
    t = _echo_tool()
    asyncio.run(dispatch_tool(tool=t, args={"text": "1"}, ctx=ctx, state=state))
    asyncio.run(dispatch_tool(tool=t, args={"text": "2"}, ctx=ctx, state=state))
    out = asyncio.run(dispatch_tool(tool=t, args={"text": "3"}, ctx=ctx, state=state))
    assert "cap" in out.lower()
    assert state.abort_requested is True
    assert state.dispatch_count == 3


def test_dispatch_awaits_async_tool(tmp_path: Path) -> None:
    """Verifies dispatch_tool uses `await tool.async_invoke(ctx, args)` not
    the sync path. An async tool that returns after an await would deadlock or
    raise under a sync call."""
    import asyncio

    ctx = _ctx(tmp_path)
    state = RunState(cap=10)

    async def _async_fn(ctx: Any, text: str) -> str:
        await asyncio.sleep(0)  # yield once to ensure async path is exercised
        return f"async:{text}"

    async_tool = Tool(name="async_echo", description="async echo",
                      input_schema={"type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"]},
                      fn=_async_fn)
    out = asyncio.run(dispatch_tool(
        tool=async_tool, args={"text": "hello"}, ctx=ctx, state=state))
    assert out == "async:hello"
    assert state.dispatch_count == 1


def test_cap_rmw_literal_has_no_await_between_read_and_decision() -> None:
    """AC #19 source-inspection guard: between the `dispatch_count += 1`
    increment and the `> state.cap` comparison there must be no `await`
    token."""
    import inspect as _inspect
    import re

    from imp.providers import codex_mcp

    src = _inspect.getsource(codex_mcp.dispatch_tool)
    lines = src.splitlines()
    inc_idx = next(i for i, ln in enumerate(lines)
                   if re.search(r"state\.dispatch_count\s*\+=\s*1", ln))
    cmp_idx = next(i for i, ln in enumerate(lines)
                   if re.search(r"state\.dispatch_count\s*>\s*state\.cap", ln))
    assert inc_idx < cmp_idx, "increment must precede comparison"
    window = "\n".join(lines[inc_idx:cmp_idx + 1])
    assert not re.search(r"\bawait\b", window), (
        f"cap RMW window contains an `await` between read and decision:\n{window}")
