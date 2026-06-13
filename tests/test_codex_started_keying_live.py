"""Gated codex regression (promoted from spike_codex_started_timing.py): one live
parallel-task turn confirms the Change-2 keying — each `task` tool_call is keyed on
the SDK item id and opens before its result; the two task results pair to the two
task calls by id (out-of-order safe). Billable; set IMP_RUN_CODEX=1 to opt in.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.skipif(
    os.environ.get("IMP_RUN_CODEX") != "1",
    reason="live codex started-keying regression; set IMP_RUN_CODEX=1 to opt in")
async def test_codex_task_lifecycle_keyed_on_sdk_item_id() -> None:
    from tests.live_helpers import run_parallel_task_turn

    events = await run_parallel_task_turn(provider="codex")
    calls = [e for e in events
             if e.get("type") == "tool_call"
             and (e.get("payload") or {}).get("name") == "task"]
    results = [e for e in events
               if e.get("type") == "tool_result"
               and (e.get("payload") or {}).get("id_ns") == "event"]
    call_ids = [(c.get("payload") or {}).get("id") for c in calls]
    result_ids = [(r.get("payload") or {}).get("id") for r in results]
    assert len(calls) >= 2, "expected >=2 task tool_call events"
    assert len(results) >= 2, "expected >=2 task tool_result events"
    # Each task tool_call carries a non-empty SDK item id (opened at item.started).
    assert all(cid for cid in call_ids), "task tool_call must carry the SDK item id"
    assert len(set(call_ids)) == len(call_ids), "task item ids are distinct"
    # Every task result pairs to a task call by id (no fingerprint, no re-key).
    assert set(result_ids) <= set(call_ids), "task results pair to calls by SDK id"
    # Concurrency shape: both calls precede either result (fold opened at start).
    first_result_idx = min(events.index(r) for r in results)
    last_call_idx = max(events.index(c) for c in calls)
    assert last_call_idx < first_result_idx, "task calls did not both precede results"
