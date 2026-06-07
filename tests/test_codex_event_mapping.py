# tests/test_codex_event_mapping.py
from __future__ import annotations

from imp.engine.state import InMemoryEventLog
from imp.providers.codex_mcp import RunState
from imp.providers.codex_runner import process_event
from tests.codex_fakes import (
    FakeAgentMessageItem,
    FakeItemEvent,
    FakeMcpToolCallItem,
    FakeTurnCompletedEvent,
    FakeUsage,
)


def test_task_completion_dedupes_and_rekeys_to_minted_id() -> None:
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    run_state.minted_task_ids.append("MINTED-1")  # as if dispatch_tool minted it

    ev = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="task", arguments={"subagent_name": "impl"},
        result='{"answer": "ok"}', id="sdk-item-9"))

    process_event(ev, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    events = list(elog.read())
    calls = [e for e in events if e.get("type") == "tool_call"]
    results = [e for e in events if e.get("type") == "tool_result"]
    # No duplicate tool_call for task (dispatch_tool already minted it).
    assert calls == []
    # tool_result is keyed to the MINTED id, not the SDK item id.
    # Note: append_tool_result emits payload["id"] = block.tool_use_id.
    assert len(results) == 1
    assert results[0]["payload"]["id"] == "MINTED-1"
    assert run_state.minted_task_ids == []  # FIFO popped


def test_two_task_calls_one_turn_map_results_in_fifo_order() -> None:
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    run_state.minted_task_ids.extend(["MINT-A", "MINT-B"])  # dispatch order

    for sdk_id, ans in (("sdk-1", "first"), ("sdk-2", "second")):
        ev = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
            tool="task", arguments={}, result=ans, id=sdk_id))
        process_event(ev, elog=elog, agent_id="step:t", state=state,
                      provider="codex", model="gpt-5.5", run_state=run_state)

    results = [e for e in list(elog.read()) if e.get("type") == "tool_result"]
    assert [r["payload"]["id"] for r in results] == ["MINT-A", "MINT-B"]
    assert run_state.minted_task_ids == []


def test_turn_completed_emits_reassembled_llm_response_before_usage() -> None:
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")

    # agent_message then a tool call, then turn.completed with usage.
    msg = FakeItemEvent(type="item.completed", item=FakeAgentMessageItem(text="hello"))
    call = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="write_file", arguments={"path": "x"}, result="ok", id="c1"))
    done = FakeTurnCompletedEvent(usage=FakeUsage(input_tokens=10, output_tokens=5))

    for ev in (msg, call, done):
        process_event(ev, elog=elog, agent_id="step:t", state=state,
                      provider="codex", model="gpt-5.5", run_state=run_state)

    llm = [e for e in list(elog.read()) if e.get("type") == "llm_response"]
    assert len(llm) == 1  # ONE full-turn response, not per-agent_message
    content = llm[0]["payload"]["raw"]["content"]
    kinds = [b["type"] for b in content]
    assert "text" in kinds and "tool_use" in kinds  # carries tool_use now
    # Usage attached to that same response.
    assert llm[0]["payload"]["usage"]  # non-empty
