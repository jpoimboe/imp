from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from imp.agents.subagents import make_task_tool
from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.engine.agent_run import SubagentSuspended
from imp.providers.codex_mcp import RunState, _args_fingerprint, dispatch_tool


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


def test_dispatch_task_happy_path_returns_subagent_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dispatch_tool with tool.name=='task' must await dispatch_task directly
    and return its output — NOT go through async_invoke (which raises
    TaskMustBeAwaited by design)."""
    expected = '{"status": "done", "summary": "all tests passed"}'

    async def _stub_dispatch_task(
        ctx: Any, subagent_name: str, task_description: str,
        *, tool_call_event_id: str = "", tier: Any = None,
    ) -> str:
        assert subagent_name == "impl"
        assert task_description == "do it"
        return expected

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _stub_dispatch_task)

    from imp.engine.state import InMemoryEventLog
    task_tool = make_task_tool()
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    state = RunState(cap=50)

    result = asyncio.run(dispatch_tool(
        tool=task_tool,
        args={"subagent_name": "impl", "task_description": "do it", "tier": None},
        ctx=ctx,
        state=state,
    ))
    assert result == expected
    assert state.dispatch_count == 1


def test_dispatch_task_subagent_suspended_captured_not_reraised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SubagentSuspended must be captured onto RunState (for the runner to
    re-raise cleanly), NOT re-raised directly out of dispatch_tool — doing so
    would propagate through the in-process MCP boundary instead of letting the
    runner unwind the whole codex run in a controlled way."""

    async def _stub_dispatch_task(
        ctx: Any, subagent_name: str, task_description: str,
        *, tool_call_event_id: str = "", tier: Any = None,
    ) -> str:
        raise SubagentSuspended("paused by coordinator")

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _stub_dispatch_task)

    from imp.engine.state import InMemoryEventLog
    task_tool = make_task_tool()
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    state = RunState(cap=50)

    # Must NOT raise — must capture onto state and return benign text.
    out = asyncio.run(dispatch_tool(
        tool=task_tool,
        args={"subagent_name": "coder", "task_description": "write code"},
        ctx=ctx,
        state=state,
    ))
    assert isinstance(out, str)
    assert state.subagent_suspended is not None
    assert isinstance(state.subagent_suspended, SubagentSuspended)


def test_dispatch_task_generic_error_becomes_is_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-suspend exception from dispatch_task is converted to an error
    string (returned as the tool result) and recorded in state.runtime_errors."""

    async def _stub_dispatch_task(
        ctx: Any, subagent_name: str, task_description: str,
        *, tool_call_event_id: str = "", tier: Any = None,
    ) -> str:
        raise ValueError("boom")

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _stub_dispatch_task)

    from imp.engine.state import InMemoryEventLog
    task_tool = make_task_tool()
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    state = RunState(cap=50)

    result = asyncio.run(dispatch_tool(
        tool=task_tool,
        args={"subagent_name": "reviewer", "task_description": "review code"},
        ctx=ctx,
        state=state,
    ))
    assert "ValueError" in result
    assert "boom" in result
    assert any("ValueError" in e and "boom" in e for e in state.runtime_errors)


def test_task_dispatch_mints_parent_tool_call_and_passes_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dispatch_tool's task branch mints the parent tool_call event up front,
    pushes its id onto state.minted_task_ids (FIFO), and passes it to
    dispatch_task as tool_call_event_id."""
    from imp.engine.state import InMemoryEventLog

    seen: dict[str, Any] = {}

    async def _stub_dispatch_task(
        ctx: Any, subagent_name: str, task_description: str,
        *, tool_call_event_id: str = "", tier: Any = None,
    ) -> str:
        seen["tool_call_event_id"] = tool_call_event_id
        return '{"answer": "ok"}'

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _stub_dispatch_task)

    elog = InMemoryEventLog()
    ctx = _ctx(tmp_path)
    ctx.event_log = elog  # type: ignore[attr-defined]
    state = RunState(cap=50, agent_id="step:t")

    asyncio.run(dispatch_tool(
        tool=make_task_tool(),
        args={"subagent_name": "impl", "task_description": "do it"},
        ctx=ctx, state=state))

    # A tool_call event was minted, and its id was both queued and passed down.
    calls = [e for e in list(elog.read()) if e.get("type") == "tool_call"]
    assert len(calls) == 1
    minted_id = calls[0]["id"]
    assert seen["tool_call_event_id"] == minted_id
    # Mint now carries (event_id, fingerprint) so an out-of-order item can pair
    # by args, not by FIFO position (AC24).
    assert state.minted_task_ids == [
        (minted_id, _args_fingerprint({"subagent_name": "impl",
                                       "task_description": "do it"}))]


def test_task_subagent_suspended_captured_on_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SubagentSuspended from dispatch_task is captured onto RunState (so the
    runner can re-raise it) rather than swallowed into is_error text."""
    from imp.engine.state import InMemoryEventLog

    async def _stub(*a: Any, **k: Any) -> str:
        raise SubagentSuspended("paused by coordinator")

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _stub)
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    state = RunState(cap=50, agent_id="step:t")

    out = asyncio.run(dispatch_tool(
        tool=make_task_tool(),
        args={"subagent_name": "impl", "task_description": "x"},
        ctx=ctx, state=state))

    assert state.subagent_suspended is not None
    assert isinstance(state.subagent_suspended, SubagentSuspended)
    # Returns benign text to the SDK; the real signal is on state.
    assert isinstance(out, str)


def test_fatal_tool_error_captured_on_state(tmp_path: Path) -> None:
    from imp.agents.tools.errors import FatalToolError

    def _fn(ctx: Any, **k: Any) -> str:
        raise FatalToolError("config missing: IMP_LINT_COMMAND")
    bad = Tool(name="lint", description="",
               input_schema={"type": "object", "properties": {}}, fn=_fn)

    ctx = _ctx(tmp_path)
    state = RunState(cap=10)
    out = asyncio.run(dispatch_tool(tool=bad, args={}, ctx=ctx, state=state))

    assert state.fatal_tool_error == ("lint", "config missing: IMP_LINT_COMMAND")
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

    async def counting(ctx):
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


def test_args_fingerprint_drops_none_so_tier_omitted_matches_tier_none() -> None:
    """The mint side carries tier=None (FastMCP injects it for the omitted
    optional); the item side omits tier entirely. Dropping None-valued keys makes
    the two fingerprints equal — the load-bearing normalization for re-key (AC24)."""
    mint_side = _args_fingerprint(
        {"subagent_name": "impl", "task_description": "do it", "tier": None})
    item_side = _args_fingerprint(
        {"subagent_name": "impl", "task_description": "do it"})
    assert mint_side == item_side


def test_args_fingerprint_is_key_order_independent() -> None:
    """Fingerprint must not depend on dict insertion order — sorted canonical form."""
    a = _args_fingerprint({"subagent_name": "impl", "task_description": "x"})
    b = _args_fingerprint({"task_description": "x", "subagent_name": "impl"})
    assert a == b


def test_args_fingerprint_distinguishes_different_args() -> None:
    """Distinct task_description values yield distinct fingerprints — so two
    concurrent same-subagent dispatches with different descriptions pair correctly."""
    a = _args_fingerprint({"subagent_name": "impl", "task_description": "review A"})
    b = _args_fingerprint({"subagent_name": "impl", "task_description": "review B"})
    assert a != b


def test_task_dispatch_increments_inflight_during_and_clears_after(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """The in-flight counter reads 1 while the dispatch await is live and drops
    back to 0 once it returns — the drain gate's settle signal (AC25)."""
    from imp.engine.state import InMemoryEventLog
    state = RunState(cap=10)
    seen: dict[str, int] = {}

    async def _stub(ctx: Any, subagent_name: str, task_description: str,
                    **_kw: Any) -> str:
        seen["during"] = state.inflight_task_dispatches
        return "ok"

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _stub)
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    out = asyncio.run(dispatch_tool(
        tool=make_task_tool(),
        args={"subagent_name": "impl", "task_description": "do it"},
        ctx=ctx, state=state))
    assert out == "ok"
    assert seen["during"] == 1
    assert state.inflight_task_dispatches == 0


def test_task_dispatch_decrements_inflight_on_suspension_capture(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """A child that captures a suspension decrements its own in-flight slot in
    finally, so it is NOT counted as a draining sibling (AC25)."""
    from imp.engine.state import InMemoryEventLog
    state = RunState(cap=10)

    async def _stub(ctx: Any, subagent_name: str, task_description: str,
                    **_kw: Any) -> str:
        raise SubagentSuspended("paused")

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _stub)
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    asyncio.run(dispatch_tool(
        tool=make_task_tool(),
        args={"subagent_name": "impl", "task_description": "x"},
        ctx=ctx, state=state))
    assert state.subagent_suspended is not None
    assert state.inflight_task_dispatches == 0


def test_suspension_capture_text_carries_stop_guidance(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """On a captured suspension the wrapper returns the named constant whose text
    tells the model to stop dispatching (membership-freeze guidance, §2)."""
    from imp.engine.state import InMemoryEventLog
    from imp.providers.codex_mcp import CODEX_SUSPEND_CAPTURE_TEXT
    state = RunState(cap=10)

    async def _stub(ctx: Any, subagent_name: str, task_description: str,
                    **_kw: Any) -> str:
        raise SubagentSuspended("paused")

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _stub)
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    out = asyncio.run(dispatch_tool(
        tool=make_task_tool(),
        args={"subagent_name": "impl", "task_description": "x"},
        ctx=ctx, state=state))
    assert out == CODEX_SUSPEND_CAPTURE_TEXT
    assert out.endswith("; do not dispatch further tasks")


def test_awaiting_capture_text_carries_stop_guidance(tmp_path: Path) -> None:
    """A headless wait captured from a non-task tool returns the named awaiting
    constant carrying the same stop guidance (§2)."""
    from imp.agents.tools.wait_for_user import AwaitingUserInput
    from imp.engine.state import InMemoryEventLog
    from imp.providers.codex_mcp import CODEX_AWAIT_CAPTURE_TEXT

    async def _afn(ctx: Any) -> str:
        raise AwaitingUserInput("answer me")

    tool = Tool(name="wait_for_user", description="",
                input_schema={"type": "object", "properties": {}}, fn=_afn)
    state = RunState(cap=10)
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    out = asyncio.run(dispatch_tool(tool=tool, args={}, ctx=ctx, state=state))
    assert out == CODEX_AWAIT_CAPTURE_TEXT
    assert out.endswith("; do not dispatch further tasks")
    assert state.awaiting_user is True
    assert state.awaiting_prompt == "answer me"


def test_head_refusal_raises_sentinel_when_suspension_captured(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """AC25/AC29: a NEW task call while a suspension is captured is refused at the
    dispatch head — TaskRefused(sentinel) is raised BEFORE any mint or cap slot,
    and dispatch_task is never reached."""
    from imp.agents.subagents import TASK_REFUSAL_SENTINEL, TaskRefused
    from imp.engine.state import InMemoryEventLog
    state = RunState(cap=10)
    state.subagent_suspended = SubagentSuspended("a sibling already paused")
    called = {"dispatch_task": False}

    async def _stub(ctx: Any, subagent_name: str, task_description: str,
                    **_kw: Any) -> str:
        called["dispatch_task"] = True
        return "should not run"

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _stub)
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    with pytest.raises(TaskRefused) as ei:
        asyncio.run(dispatch_tool(
            tool=make_task_tool(),
            args={"subagent_name": "impl", "task_description": "new work"},
            ctx=ctx, state=state))
    assert str(ei.value) == TASK_REFUSAL_SENTINEL
    assert called["dispatch_task"] is False
    assert state.minted_task_ids == []          # nothing minted
    assert state.dispatch_count == 0            # no cap slot burned


def test_head_refusal_fires_on_should_pause_alone(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """AC29: an arm-entry refusal on should_pause alone burns no slot and mints
    nothing — the predicate also reads ctx.coordinator.should_pause."""
    from types import SimpleNamespace

    from imp.agents.subagents import TaskRefused
    from imp.engine.state import InMemoryEventLog
    state = RunState(cap=10)

    async def _stub(ctx: Any, subagent_name: str, task_description: str,
                    **_kw: Any) -> str:
        return "should not run"

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _stub)
    ctx = _ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()  # type: ignore[attr-defined]
    ctx.coordinator = SimpleNamespace(should_pause=True)
    with pytest.raises(TaskRefused):
        asyncio.run(dispatch_tool(
            tool=make_task_tool(),
            args={"subagent_name": "impl", "task_description": "x"},
            ctx=ctx, state=state))
    assert state.minted_task_ids == []
    assert state.dispatch_count == 0
