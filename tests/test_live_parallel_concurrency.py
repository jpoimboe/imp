"""Gated live concurrency through CLI provider wiring (spec AC44). Billable.

codex: IMP_RUN_CODEX=1. claude_code: IMP_RUN_DOGFOOD=1. Each runs ONE turn that
fans out two task calls and asserts the two subagent dispatches overlapped in
wall-clock (both tool_call events precede either tool_result event).
"""
from __future__ import annotations

import os

import pytest


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
@pytest.mark.skipif(
    os.environ.get("IMP_RUN_CODEX") != "1",
    reason="live codex concurrency; set IMP_RUN_CODEX=1 to opt in")
async def test_codex_parallel_task_turn_is_concurrent() -> None:
    from tests.live_helpers import run_parallel_task_turn

    events = await run_parallel_task_turn(provider="codex")
    _assert_two_task_dispatches_overlap(events)


@pytest.mark.anyio
@pytest.mark.skipif(
    os.environ.get("IMP_RUN_DOGFOOD") != "1",
    reason="live claude_code dogfood concurrency; set IMP_RUN_DOGFOOD=1 to opt in")
async def test_claude_code_parallel_task_turn_is_concurrent() -> None:
    from tests.live_helpers import run_parallel_task_turn

    events = await run_parallel_task_turn(provider="claude_code")
    _assert_two_task_dispatches_overlap(events)
