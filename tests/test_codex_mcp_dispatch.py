from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from imp.agents.subagents import make_task_tool
from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.engine.state import InMemoryEventLog
from imp.providers.codex_mcp import RunState, dispatch_tool


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl", subagent_defs={},
        provider_name="codex", model_name="gpt-5.5",
        parent_transcript_dir=tmp_path,
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
    assert state.complete_step_called is True
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


def test_complete_step_drops_none_valued_optional_args(tmp_path: Path) -> None:
    """codex's arg-model defaults absent optional fields to None via
    PydanticField(None); the capture must drop them so output validation
    doesn't reject a present-but-None optional field with 'expected string,
    got NoneType' (regression: spec-writer spec_path omitted by codex)."""
    ctx = _ctx(tmp_path)
    state = RunState(cap=50)

    def _cs(ctx: Any, **kwargs: Any) -> str:
        raise AssertionError("complete_step body must not run inside the wrapper")

    cs = Tool(name="complete_step", description="",
              input_schema={"type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "phase": {"type": "string"},
                                "complexity": {"type": "string"},
                                "spec_path": {"type": "string"},
                            },
                            "required": ["status"]},
              fn=_cs)

    # Simulate codex omitting the optional spec_path — the arg-model
    # materialises spec_path=None; the fix must strip it.
    out = asyncio.run(dispatch_tool(
        tool=cs,
        args={"status": "Success", "phase": "drafting",
              "complexity": "trivial", "spec_path": None},
        ctx=ctx, state=state,
    ))
    assert state.complete_step_called is True
    assert state.complete_step_args == {
        "status": "Success", "phase": "drafting", "complexity": "trivial"}
    assert "spec_path" not in state.complete_step_args
    assert out == "step complete"


def test_complete_step_preserves_non_none_optional_arg(tmp_path: Path) -> None:
    """A non-None optional field (e.g. spec_path provided) must NOT be dropped —
    the filter is None-only, not 'optional field' blanket removal."""
    ctx = _ctx(tmp_path)
    state = RunState(cap=50)

    def _cs(ctx: Any, **kwargs: Any) -> str:
        raise AssertionError("complete_step body must not run inside the wrapper")

    cs = Tool(name="complete_step", description="",
              input_schema={"type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "spec_path": {"type": "string"},
                            },
                            "required": ["status"]},
              fn=_cs)

    out = asyncio.run(dispatch_tool(
        tool=cs,
        args={"status": "Success", "spec_path": "docs/my-spec.md"},
        ctx=ctx, state=state,
    ))
    assert state.complete_step_called is True
    assert state.complete_step_args == {
        "status": "Success", "spec_path": "docs/my-spec.md"}
    assert out == "step complete"


# ── task tool special-case tests ─────────────────────────────────────────────


def test_task_dispatch_returns_output_and_appends_no_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Change 2: dispatch_tool runs the subagent and returns its output; it does
    NOT touch the event log for the task tool_call/result pair (process_event owns
    that now)."""
    elog = InMemoryEventLog()
    ctx = _ctx(tmp_path)
    ctx.event_log = elog  # type: ignore[attr-defined]

    async def _fake(ctx2: Any, name: str, desc: str, *, tier: Any = None) -> str:
        return '{"status": "Success"}'

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    state = RunState(cap=100)
    out = asyncio.run(dispatch_tool(
        tool=make_task_tool(),
        args={"subagent_name": "impl", "task_description": "x"},
        ctx=ctx, state=state))
    assert out == '{"status": "Success"}'
    assert [e for e in elog.read() if e["type"] == "tool_call"] == []


def test_task_dispatch_suspension_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Change 1: a subagent-origin pause propagates out of dispatch_tool so FastMCP
    wraps it as an isError tool result — dispatch_tool no longer captures it."""
    from imp.engine.agent_run import SubagentSuspended

    async def _fake(ctx2: Any, name: str, desc: str, *, tier: Any = None) -> str:
        raise SubagentSuspended("paused")

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    state = RunState(cap=100)
    with pytest.raises(SubagentSuspended):
        asyncio.run(dispatch_tool(
            tool=make_task_tool(),
            args={"subagent_name": "impl", "task_description": "x"},
            ctx=ctx, state=state))


def test_task_dispatch_generic_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subagent failure propagates out of dispatch_tool (FastMCP → isError)."""
    async def _fake(ctx2: Any, name: str, desc: str, *, tier: Any = None) -> str:
        raise RuntimeError("subagent 'impl' failed (api)")

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    state = RunState(cap=100)
    with pytest.raises(RuntimeError):
        asyncio.run(dispatch_tool(
            tool=make_task_tool(),
            args={"subagent_name": "impl", "task_description": "x"},
            ctx=ctx, state=state))


def test_fatal_tool_error_captured_on_state(tmp_path: Path) -> None:
    from imp.agents.tools.errors import FatalToolError

    def _fn(ctx: Any, **k: Any) -> str:
        raise FatalToolError("config missing: IMP_VERIFY_COMMAND")
    bad = Tool(name="lint", description="",
               input_schema={"type": "object", "properties": {}}, fn=_fn)

    ctx = _ctx(tmp_path)
    state = RunState(cap=10)
    out = asyncio.run(dispatch_tool(tool=bad, args={}, ctx=ctx, state=state))

    assert state.fatal_tool_error == ("lint", "config missing: IMP_VERIFY_COMMAND")
    assert "config missing" in out  # is_error-ish body, not silently dropped


def test_negative_rc_bash_records_killed_tool(tmp_path: Path) -> None:
    import json as _json

    def _fn(ctx: Any, command: str) -> str:
        return _json.dumps({"returncode": -9, "stdout": "", "stderr": "killed"})
    bash = Tool(name="bash", description="",
                input_schema={"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}, fn=_fn)
    ctx = _ctx(tmp_path)
    state = RunState(cap=10)
    asyncio.run(dispatch_tool(tool=bash, args={"command": "sleep 100"},
                              ctx=ctx, state=state))
    # Sibling shape (claude_code.py): {id, name, signal}. codex args carry no
    # __tool_use_id__, so id is "" (documented weak-id approximation).
    assert state.killed_tool_uses == [{"id": "", "name": "bash", "signal": 9}]


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


def test_dispatch_sweeps_bg_before_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 10 dispatch site, codex edition: every MCP dispatch sweeps —
    including cap-exceeded dispatches (the sweep precedes the cap RMW)."""
    import imp.agents.tools.bg as bg_mod

    calls = {"n": 0}
    real = bg_mod._sweep_bg

    async def counting(ctx: Any) -> None:
        calls["n"] += 1
        await real(ctx)

    monkeypatch.setattr(bg_mod, "_sweep_bg", counting)
    ctx = _ctx(tmp_path)
    state = RunState(cap=1)
    out = asyncio.run(dispatch_tool(
        tool=_echo_tool(), args={"text": "hi"}, ctx=ctx, state=state))
    assert out == "echo:hi"
    assert calls["n"] == 1
    # over-cap dispatch still sweeps (sweep precedes the RMW)
    asyncio.run(dispatch_tool(
        tool=_echo_tool(), args={"text": "hi"}, ctx=ctx, state=state))
    assert calls["n"] == 2


def test_awaiting_capture_text_carries_stop_guidance(tmp_path: Path) -> None:
    """A headless wait captured from a non-task tool returns the named awaiting
    constant carrying the same stop guidance (§2)."""
    from imp.agents.tools.wait_for_user import AwaitingUserInput
    from imp.providers.codex_mcp import CODEX_AWAIT_CAPTURE_TEXT

    async def _afn(ctx: Any) -> str:
        raise AwaitingUserInput("answer me")

    tool = Tool(name="wait_for_user", description="",
                input_schema={"type": "object", "properties": {}}, fn=_afn)
    state = RunState(cap=10)
    ctx = _ctx(tmp_path)
    out = asyncio.run(dispatch_tool(tool=tool, args={}, ctx=ctx, state=state))
    assert out == CODEX_AWAIT_CAPTURE_TEXT
    assert out.endswith("; do not dispatch further tasks")
    assert state.awaiting_user is True
    assert state.awaiting_prompt == "answer me"
