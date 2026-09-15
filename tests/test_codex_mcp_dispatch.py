from __future__ import annotations

import asyncio
import types
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.runner import ImagePart, TextPart
from imp.agents.subagents import make_task_tool
from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.engine.state import InMemoryEventLog
from imp.providers.codex_mcp import RunState, TaskStartCoordinator, dispatch_tool
from imp.providers.codex_runner import process_event
from tests._codex_mcp import call_protocol, call_via_mcp
from tests.async_wait import wait_until


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


@pytest.mark.anyio
async def test_task_start_before_request_admits_reserved_ticket() -> None:
    starts = TaskStartCoordinator()
    starts.task_started()

    ticket = starts.reserve()
    await starts.wait_for_registration(ticket)

    assert starts.reserved_count == 1


_TASK_ARGS = {"subagent_name": "impl", "task_description": "x"}


def _task_ctx(tmp_path: Path) -> tuple[ToolContext, InMemoryEventLog]:
    ctx = _ctx(tmp_path)
    elog = InMemoryEventLog()
    ctx.event_log = elog
    ctx.subagent_defs = {"impl": types.SimpleNamespace(label="implementing")}
    return ctx, elog


def _task_sdk_event(kind: str, arguments: dict[str, Any]) -> Any:
    return types.SimpleNamespace(
        type=kind,
        item=types.SimpleNamespace(
            type="mcp_tool_call", tool="sub_agent", id="sdk-task",
            arguments=arguments, result="sdk result", error=None,
            status="completed" if kind == "item.completed" else "in_progress"),
    )


def _process_task_sdk_event(
    kind: str, *, elog: InMemoryEventLog, state: RunState,
    proc_state: dict[str, Any], arguments: dict[str, Any] = _TASK_ARGS,
) -> None:
    process_event(
        _task_sdk_event(kind, arguments), elog=elog, agent_id=state.agent_id,
        state=proc_state, provider="codex", model="gpt-5.5", run_state=state)


@pytest.mark.anyio
@pytest.mark.parametrize("start_first", [False, True])
async def test_task_handler_owns_canonical_lifecycle_in_both_arrival_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start_first: bool,
) -> None:
    ctx, elog = _task_ctx(tmp_path)
    state = RunState(cap=100, agent_id="step:parent")
    proc_state: dict[str, Any] = {"prose_buffer": ["dispatching now"]}
    forwarded: list[str] = []

    async def _fake(
        ctx2: Any, name: str, desc: str, *, tool_call_event_id: str, tier: Any = None,
    ) -> str:
        forwarded.append(tool_call_event_id)
        elog.append(agent_id=f"sub:{tool_call_event_id}", type="child_event", payload={})
        return '{"status":"Success"}'

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    with anyio.fail_after(120):
        if start_first:
            _process_task_sdk_event(
                "item.started", elog=elog, state=state, proc_state=proc_state)
        request = asyncio.create_task(call_via_mcp(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS))
        await wait_until(lambda: state.task_starts.reserved_count == 1)
        if not start_first:
            _process_task_sdk_event(
                "item.started", elog=elog, state=state, proc_state=proc_state)
        assert await request == '{"status":"Success"}'

    before_completion = list(elog.read())
    _process_task_sdk_event(
        "item.completed", elog=elog, state=state, proc_state=proc_state)
    records = list(elog.read())
    lifecycle = [e for e in records if e["type"] in ("tool_call", "tool_result")]
    assert [e["type"] for e in before_completion] == [
        "agent_message", "tool_call", "child_event", "tool_result"]
    assert records == before_completion
    assert len(lifecycle) == 2
    assert lifecycle[0]["payload"]["id"] == lifecycle[1]["payload"]["id"]
    assert lifecycle[0]["payload"]["id_ns"] == "block"
    assert lifecycle[1]["payload"]["id_ns"] == "block"
    assert lifecycle[0]["payload"]["label"] == "implementing"
    assert forwarded == [lifecycle[0]["id"]]


@pytest.mark.anyio
async def test_invalid_request_releases_ticket_and_consumes_its_late_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, elog = _task_ctx(tmp_path)
    state = RunState(cap=100)
    proc_state: dict[str, Any] = {}

    async def _fake(*args: Any, **kwargs: Any) -> str:
        return "done"

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    with anyio.fail_after(120):
        invalid = await call_protocol(
            make_task_tool(), ctx=ctx, state=state,
            arguments={"subagent_name": "impl"})
        assert invalid.isError is True
        assert state.task_starts.pending_count == 1

        valid_task = asyncio.create_task(call_via_mcp(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS))
        await wait_until(lambda: state.task_starts.reserved_count == 2)
        _process_task_sdk_event(
            "item.started", elog=elog, state=state, proc_state=proc_state,
            arguments={"subagent_name": "impl"})
        assert valid_task.done() is False
        _process_task_sdk_event(
            "item.started", elog=elog, state=state, proc_state=proc_state)
        assert await valid_task == "done"


@pytest.mark.anyio
async def test_over_cap_request_releases_registration_for_later_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, elog = _task_ctx(tmp_path)
    state = RunState(cap=0)
    proc_state: dict[str, Any] = {}

    async def _fake(*args: Any, **kwargs: Any) -> str:
        return "done"

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    with anyio.fail_after(120):
        _process_task_sdk_event(
            "item.started", elog=elog, state=state, proc_state=proc_state)
        rejected = await call_protocol(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS)
        assert "tool_call_ceiling_exceeded" in rejected.content[0].text
        state.cap = 10
        later = asyncio.create_task(call_via_mcp(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS))
        await wait_until(lambda: state.task_starts.reserved_count == 2)
        _process_task_sdk_event(
            "item.started", elog=elog, state=state, proc_state=proc_state)
        assert await later == "done"


@pytest.mark.anyio
async def test_task_cancellation_before_start_releases_ticket(tmp_path: Path) -> None:
    ctx, _ = _task_ctx(tmp_path)
    state = RunState(cap=100)
    with anyio.fail_after(120):
        request = asyncio.create_task(call_via_mcp(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS))
        await wait_until(lambda: state.task_starts.reserved_count == 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert state.task_starts.pending_count == 1
        state.task_starts.task_started()
        assert state.task_starts.pending_count == 0


@pytest.mark.anyio
async def test_task_cancellation_while_waiting_for_registration_releases_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _ = _task_ctx(tmp_path)
    state = RunState(cap=100)
    first_can_register = asyncio.Event()
    second_is_waiting = asyncio.Event()
    original_wait = state.task_starts.wait_for_registration

    async def _controlled_wait(ticket: Any) -> None:
        await ticket.started.wait()
        if ticket.sequence == 0:
            await first_can_register.wait()
        else:
            second_is_waiting.set()
        await ticket.registration_turn.wait()

    state.task_starts.wait_for_registration = _controlled_wait  # type: ignore[method-assign]

    async def _fake(*args: Any, **kwargs: Any) -> str:
        return "done"

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    with anyio.fail_after(120):
        first = asyncio.create_task(call_via_mcp(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS))
        second = asyncio.create_task(call_via_mcp(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS))
        await wait_until(lambda: state.task_starts.reserved_count == 2)
        state.task_starts.task_started()
        state.task_starts.task_started()
        await second_is_waiting.wait()
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        first_can_register.set()
        assert await first == "done"
    state.task_starts.wait_for_registration = original_wait  # type: ignore[method-assign]


@pytest.mark.anyio
async def test_committed_cancellation_closes_only_cancelled_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, elog = _task_ctx(tmp_path)
    state = RunState(cap=100)
    gates = [asyncio.Event(), asyncio.Event()]
    entered: list[str] = []

    async def _fake(
        ctx2: Any, name: str, desc: str, *, tool_call_event_id: str, tier: Any = None,
    ) -> str:
        index = len(entered)
        entered.append(tool_call_event_id)
        await gates[index].wait()
        return f"done-{index}"

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    with anyio.fail_after(120):
        requests = [asyncio.create_task(call_via_mcp(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS))
            for _ in range(2)]
        await wait_until(lambda: state.task_starts.reserved_count == 2)
        state.task_starts.task_started()
        state.task_starts.task_started()
        await wait_until(lambda: len(entered) == 2)
        requests[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await requests[0]
        gates[1].set()
        assert await requests[1] == "done-1"

    results = [e["payload"] for e in elog.read() if e["type"] == "tool_result"]
    assert sorted((r["error"], r.get("kind")) for r in results) == [
        (False, None), (True, "interrupted")]


@pytest.mark.anyio
async def test_same_argument_requests_register_in_order_and_execute_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, elog = _task_ctx(tmp_path)
    state = RunState(cap=100)
    finish = [asyncio.Event(), asyncio.Event()]
    entered: list[str] = []

    async def _fake(
        ctx2: Any, name: str, desc: str, *, tool_call_event_id: str, tier: Any = None,
    ) -> str:
        index = len(entered)
        entered.append(tool_call_event_id)
        await finish[index].wait()
        return f"done-{index}"

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    with anyio.fail_after(120):
        first = asyncio.create_task(call_via_mcp(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS))
        await wait_until(lambda: state.task_starts.reserved_count == 1)
        second = asyncio.create_task(call_via_mcp(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS))
        await wait_until(lambda: state.task_starts.reserved_count == 2)
        state.task_starts.task_started()
        state.task_starts.task_started()
        await wait_until(lambda: len(entered) == 2)
        calls = [e for e in elog.read() if e["type"] == "tool_call"]
        assert entered == [e["id"] for e in calls]
        assert calls[0]["payload"]["id"] != calls[1]["payload"]["id"]
        finish[1].set()
        assert await second == "done-1"
        finish[0].set()
        assert await first == "done-0"
        results = [
            event for event in elog.read()
            if event["type"] == "tool_result"
        ]
        assert sorted(
            (result["payload"]["id"], result["payload"]["content"])
            for result in results
        ) == sorted(
            (calls[index]["payload"]["id"], f"done-{index}")
            for index in range(2)
        )


@pytest.mark.anyio
@pytest.mark.parametrize("exc", [
    RuntimeError("subagent suspended [suspended]"),
    RuntimeError("subagent interrupted [interrupted]"),
    ValueError("subagent failed"),
])
async def test_task_failure_returns_protocol_error_and_journals_canonical_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception,
) -> None:
    ctx, elog = _task_ctx(tmp_path)
    state = RunState(cap=100)
    forwarded: list[str] = []

    async def _fake(
        ctx2: Any, name: str, desc: str, *, tool_call_event_id: str, tier: Any = None,
    ) -> str:
        forwarded.append(tool_call_event_id)
        raise exc

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    state.task_starts.task_started()
    with anyio.fail_after(120):
        response = await call_protocol(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS)

    call = next(e for e in elog.read() if e["type"] == "tool_call")
    result = next(e for e in elog.read() if e["type"] == "tool_result")
    expected = f"{type(exc).__name__}: {exc}"
    assert response.isError is True
    assert response.content[0].text == f"Error executing tool sub_agent: {exc}"
    assert result["payload"]["content"] == expected
    assert result["payload"]["error"] is True
    assert "kind" not in result["payload"]
    assert forwarded == [call["id"]]


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
    # Sibling shape (claude.py): {id, name, signal}. codex args carry no
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


def test_codex_run_state_has_no_runtime_errors_field() -> None:
    """Its only writer was the swallow arm that now re-raises. claude has an
    independent field of the same name that must survive."""
    from dataclasses import fields

    from imp.providers.claude import _RunState as ClaudeRunState

    assert "runtime_errors" not in {f.name for f in fields(RunState)}
    assert "runtime_errors" in {f.name for f in fields(ClaudeRunState)}


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


@pytest.mark.anyio
async def test_task_result_over_cap_is_spilled_and_recorded_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 9(b)/AC 10: the codex MCP sub_agent arm wraps its success result, so the
    # model receives a preview + notice and the tool_result recorded in the event
    # log is byte-identical to it — never the raw result on one side and the
    # wrapped one on the other. The full result stays recoverable on disk.
    ctx, elog = _task_ctx(tmp_path)
    state = RunState(cap=100, agent_id="step:parent")
    proc_state: dict[str, Any] = {}
    big = "C" * 60_000

    async def _fake(
        ctx2: Any, name: str, desc: str, *, tool_call_event_id: str, tier: Any = None,
    ) -> str:
        return big

    monkeypatch.setattr("imp.agents.subagents.dispatch_task", _fake)
    with anyio.fail_after(120):
        request = asyncio.create_task(call_via_mcp(
            make_task_tool(), ctx=ctx, state=state, arguments=_TASK_ARGS))
        await wait_until(lambda: state.task_starts.reserved_count == 1)
        _process_task_sdk_event(
            "item.started", elog=elog, state=state, proc_state=proc_state)
        returned = await request

    assert len(returned) < len(big)                        # previewed, not the 60 KB
    assert "read with read_file offset=1" in returned
    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["payload"]["content"] == returned    # recorded == returned
    block_id = results[0]["payload"]["id"]
    assert (tmp_path / "tool-results" / block_id).read_text(encoding="utf-8") == big
