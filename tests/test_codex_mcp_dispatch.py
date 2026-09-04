from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import ImagePart, TextPart
from imp.agents.subagents import make_task_tool
from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.engine.state import InMemoryEventLog
from imp.providers.codex_mcp import RunState, dispatch_tool
from tests._codex_mcp import call_protocol, call_via_mcp


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        project_root=tmp_path,
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


def _complete_step_tool() -> Tool:
    def _cs(ctx: Any, **kwargs: Any) -> str:
        raise AssertionError("complete_step body must not run inside the wrapper")

    return Tool(name="complete_step", description="",
                input_schema={"type": "object",
                              "properties": {
                                  "status": {"type": "string"},
                                  "phase": {"type": "string"},
                                  "complexity": {"type": "string"},
                                  "spec_path": {"type": "string"},
                              },
                              "required": ["status"]},
                fn=_cs)


def test_complete_step_args_never_carry_a_fabricated_none(tmp_path: Path) -> None:
    """An optional the agent did not send is ABSENT from the capture — the
    arg model never materialises it, so there is nothing to strip. Otherwise
    output validation rejects it with 'expected string, got NoneType'."""
    state = RunState(cap=50)
    out = asyncio.run(call_via_mcp(
        _complete_step_tool(), ctx=_ctx(tmp_path), state=state,
        arguments={"status": "Success", "phase": "drafting",
                   "complexity": "trivial"}))
    assert state.complete_step_called is True
    assert state.complete_step_args == {
        "status": "Success", "phase": "drafting", "complexity": "trivial"}
    assert "spec_path" not in state.complete_step_args
    assert out == "step complete"


def test_complete_step_preserves_non_none_optional_arg(tmp_path: Path) -> None:
    """A field the agent DID send survives with its value — the arg model omits
    only what was unset, never an optional that carries data."""
    state = RunState(cap=50)
    out = asyncio.run(call_via_mcp(
        _complete_step_tool(), ctx=_ctx(tmp_path), state=state,
        arguments={"status": "Success", "spec_path": "docs/my-spec.md"}))
    assert state.complete_step_called is True
    assert state.complete_step_args == {
        "status": "Success", "spec_path": "docs/my-spec.md"}
    assert out == "step complete"


# ── task tool special-case tests ─────────────────────────────────────────────


def test_task_dispatch_returns_output_and_appends_no_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """quest 2: dispatch_tool runs the subagent and returns its output; it does
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
    """A subagent handback propagates out of dispatch_tool so FastMCP wraps it as an
    isError tool result — dispatch_tool no longer captures it."""
    async def _fake(ctx2: Any, name: str, desc: str, *, tier: Any = None) -> str:
        raise RuntimeError("subagent 'impl' was suspended before finishing — "
                           "its provider or retry budget ran out. [suspended]")

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    state = RunState(cap=100)
    with pytest.raises(RuntimeError):
        asyncio.run(dispatch_tool(
            tool=make_task_tool(),
            args={"subagent_name": "impl", "task_description": "x"},
            ctx=ctx, state=state))


def test_task_dispatch_suspension_marks_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handback body's [interrupted] marker propagates UNCHANGED so FastMCP
    surfaces it for renderer classification — the handler no longer adds it."""
    async def _fake(ctx2: Any, name: str, desc: str, *, tier: Any = None) -> str:
        raise RuntimeError("subagent 'impl' was interrupted before finishing — "
                           "the run was paused. [interrupted]")

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    state = RunState(cap=100)
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(dispatch_tool(
            tool=make_task_tool(),
            args={"subagent_name": "impl", "task_description": "x"},
            ctx=ctx, state=state))
    assert str(ei.value).count("[interrupted]") == 1


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
        raise FatalToolError("config missing: IMP_TEST_CMD_FAST")
    bad = Tool(name="lint", description="",
               input_schema={"type": "object", "properties": {}}, fn=_fn)

    ctx = _ctx(tmp_path)
    state = RunState(cap=10)
    out = asyncio.run(dispatch_tool(tool=bad, args={}, ctx=ctx, state=state))

    assert state.fatal_tool_error == ("lint", "config missing: IMP_TEST_CMD_FAST")
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


def test_codex_has_no_wait_for_user_pause_intercept() -> None:
    """The dead live-run wait_for_user pause intercept is gone from codex: the
    RunState capture fields and CODEX_AWAIT_CAPTURE_TEXT constant are removed,
    the runner no longer reads state.awaiting_user, and the stale wait_for_user
    comment framing is refreshed (only the AwaitingUserInput import path keeps
    the module name). The live AWAITING_USER resume path is unaffected."""
    import dataclasses
    import inspect

    import imp.providers.codex_mcp as mcp_mod
    import imp.providers.codex_runner as runner_mod
    from imp.providers.codex_mcp import RunState

    names = {f.name for f in dataclasses.fields(RunState)}
    assert "awaiting_user" not in names
    assert "awaiting_prompt" not in names
    assert not hasattr(mcp_mod, "CODEX_AWAIT_CAPTURE_TEXT")

    runner_src = inspect.getsource(runner_mod)
    assert "state.awaiting_user" not in runner_src
    # The pause signal now imports from imp.engine.awaiting_user_input; no stale
    # wait_for_user module name remains in either source.
    assert runner_src.count("wait_for_user") == 0
    assert inspect.getsource(mcp_mod).count("wait_for_user") == 0


def test_dispatch_brackets_tool_watchdog(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator

    coord = InterruptCoordinator()
    seen = {"active": None}

    def _fn(ctx, text):
        seen["active"] = ctx.coordinator.tool_active
        return f"echo:{text}"

    rec = Tool(name="rec", description="",
               input_schema={"type": "object",
                             "properties": {"text": {"type": "string"}},
                             "required": ["text"]}, fn=_fn)

    ctx = _ctx(tmp_path)
    ctx.coordinator = coord
    state = RunState(cap=10)
    out = asyncio.run(dispatch_tool(
        tool=rec, args={"text": "hi"}, ctx=ctx, state=state))

    assert out == "echo:hi"
    assert seen["active"] is True       # watchdog frozen DURING the tool call
    assert coord.tool_active is False   # balanced after return -> window re-arms


def test_dispatch_brackets_tool_watchdog_on_raise(tmp_path: Path) -> None:
    """The exception propagates (FastMCP turns it into an isError result) AND the
    watchdog is both frozen during the call and balanced afterwards on that
    path — capturing `tool_active` from inside the raising body is what guards
    against `suspend_inactivity_watchdog` being dropped, which the trailing
    assertion alone would not catch."""
    from imp.cli.signals import InterruptCoordinator

    coord = InterruptCoordinator()
    seen = {"active": None}

    def _fn(ctx):
        seen["active"] = ctx.coordinator.tool_active
        raise RuntimeError("boom")

    boom = Tool(name="boom", description="",
                input_schema={"type": "object", "properties": {}}, fn=_fn)

    ctx = _ctx(tmp_path)
    ctx.coordinator = coord
    state = RunState(cap=10)
    with pytest.raises(RuntimeError, match="RuntimeError: boom"):
        asyncio.run(dispatch_tool(tool=boom, args={}, ctx=ctx, state=state))

    assert seen["active"] is True       # watchdog frozen DURING the raising call
    assert coord.tool_active is False   # balanced after the unwind -> re-arms


def _read_file_boom_tool() -> Tool:
    def _fn(ctx: Any, path: str) -> str:
        raise ValueError(f"no such file: {path}")

    return Tool(name="read_file", description="",
                input_schema={"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}, fn=_fn)


def test_raising_tool_yields_iserror_result_naming_the_exception_type(
    tmp_path: Path,
) -> None:
    """Through a real MCP client session: a raising tool must come back as an
    isError result whose text still names the original exception type."""
    res = asyncio.run(call_protocol(
        _read_file_boom_tool(), ctx=_ctx(tmp_path), state=RunState(cap=10),
        arguments={"path": "foo.py"}))

    assert res.isError is True
    assert res.content[0].text == (
        "Error executing tool read_file: ValueError: no such file: foo.py")


def _image_parts() -> list:
    return [
        TextPart(text="shot.png — 8×8 PNG"),
        ImagePart(media_type="image/png", data="aGVsbG8=", width=8, height=8,
                  blob="ab12.png", nbytes=5),
    ]


def _image_tool() -> Tool:
    parts = _image_parts()

    def _fn(ctx: Any) -> list:
        return parts

    return Tool(name="read_file", description="read",
                input_schema={"type": "object", "properties": {}}, fn=_fn)


def test_dispatch_list_content_returns_mcp_image_content(tmp_path: Path) -> None:
    from mcp.types import ImageContent, TextContent

    out = asyncio.run(dispatch_tool(
        tool=_image_tool(), args={}, ctx=_ctx(tmp_path), state=RunState(cap=10)))
    assert isinstance(out, list)
    assert isinstance(out[0], TextContent)
    assert out[0].text == "shot.png — 8×8 PNG"
    assert isinstance(out[1], ImageContent)
    assert out[1].mimeType == "image/png"
    assert out[1].data == "aGVsbG8="


def _plain_list_tool() -> Tool:
    def _fn(ctx: Any) -> list:
        return [1, 2, 3]

    return Tool(name="nums", description="nums",
                input_schema={"type": "object", "properties": {}}, fn=_fn)


def test_dispatch_plain_list_still_json_flattens(tmp_path: Path) -> None:
    # An ordinary (non-ContentPart) list keeps the json.dumps path unchanged.
    out = asyncio.run(dispatch_tool(
        tool=_plain_list_tool(), args={}, ctx=_ctx(tmp_path), state=RunState(cap=10)))
    assert out == "[1, 2, 3]"
