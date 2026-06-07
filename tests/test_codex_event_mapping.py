# tests/test_codex_event_mapping.py
from __future__ import annotations

from imp.engine.state import InMemoryEventLog
from imp.providers.codex_mcp import RunState
from imp.providers.codex_runner import process_event
from tests.codex_fakes import FakeItemEvent, FakeMcpToolCallItem


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

    calls = [e for e in list(elog.read()) if e.get("type") == "tool_call"]
    results = [e for e in list(elog.read()) if e.get("type") == "tool_result"]
    # No duplicate tool_call for task (dispatch_tool already minted it).
    assert calls == []
    # tool_result is keyed to the MINTED id, not the SDK item id.
    # Note: append_tool_result emits payload["id"] = block.tool_use_id.
    assert len(results) == 1
    assert results[0]["payload"]["id"] == "MINTED-1"
    assert run_state.minted_task_ids == []  # FIFO popped
