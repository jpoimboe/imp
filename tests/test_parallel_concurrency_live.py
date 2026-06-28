"""Gated live concurrency through CLI provider wiring (spec AC44). Billable.

Opt in via IMP_LIVE_PROVIDERS (`codex` and/or `claude_code`). Each runs ONE turn
that fans out two task calls and asserts the two subagent dispatches overlapped
in wall-clock (both tool_call events precede either tool_result event).
"""
from __future__ import annotations

import shutil

import pytest

from tests._live import assert_codex_toolchain, opted_in


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _assert_two_task_dispatches_overlap(events: list[dict]) -> None:
    calls = [e for e in events
             if e.get("type") == "tool_call"
             and (e.get("payload") or {}).get("name") == "sub_agent"]
    results = [e for e in events
               if e.get("type") == "tool_result"
               and (e.get("payload") or {}).get("id")
               in {(c.get("payload") or {}).get("id") for c in calls}]
    assert len(calls) >= 2, "expected >=2 task tool_call events"
    # Concurrency shape: BOTH task calls were emitted before EITHER task result.
    first_result_idx = min(events.index(r) for r in results)
    last_call_idx = max(events.index(c) for c in calls)
    assert last_call_idx < first_result_idx, (
        "task calls did not both precede the first task result (not concurrent)")


@pytest.mark.anyio
@pytest.mark.xfail(
    reason="codex 0.142.3 serializes MCP sub_agent dispatch: the installed CLI "
    "does not emit/execute parallel tool calls for the imp MCP server, despite "
    "imp annotating sub_agent readOnlyHint=true and codex source (Direct ToolMode "
    "+ model supports_parallel_tool_calls=true) indicating it should. Remove this "
    "marker once the installed codex gains parallel MCP tool-call support.",
    strict=False)
@pytest.mark.skipif(
    not opted_in("codex"),
    reason="codex not in IMP_LIVE_PROVIDERS")
async def test_codex_parallel_task_turn_is_concurrent() -> None:
    assert_codex_toolchain()
    from tests.live_helpers import run_parallel_task_turn

    events = await run_parallel_task_turn(provider="codex")
    _assert_two_task_dispatches_overlap(events)


@pytest.mark.anyio
@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS")
async def test_claude_code_parallel_task_turn_is_concurrent() -> None:
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI not on PATH"
    )
    from tests.live_helpers import run_parallel_task_turn

    events = await run_parallel_task_turn(provider="claude_code")
    _assert_two_task_dispatches_overlap(events)
