from imp.engine.state import InMemoryEventLog
from imp.providers.codex_runner import process_event
from tests.codex_fakes import mcp_call


def test_mcp_tool_call_appends_call_and_result_events() -> None:
    elog = InMemoryEventLog()
    process_event(
        mcp_call(name="edit", args={"path": "a.txt", "text": "hi"},
                 result="ok", item_id="call-1"),
        elog=elog, agent_id="step:a", state={}, provider="codex", model="gpt-5.5",
    )
    types = [e["type"] for e in elog.read()]
    assert "tool_call" in types
    assert "tool_result" in types
    call = next(e for e in elog.read() if e["type"] == "tool_call")
    assert call["payload"]["name"] == "edit"
    assert call["payload"]["id"] == "call-1"
    result = next(e for e in elog.read() if e["type"] == "tool_result")
    assert result["payload"]["id"] == "call-1"
    assert "ok" in str(result["payload"].get("content", ""))


def test_mcp_tool_call_complete_step_records_args_on_state() -> None:
    """When the codex MCP item carries name=complete_step, runner captures args
    in state (idempotent secondary capture path alongside the in-process wrapper)."""
    elog = InMemoryEventLog()
    state = {"complete_step_args": None}
    process_event(
        mcp_call(name="complete_step",
                 args={"answer": "the-answer"}, result="step complete"),
        elog=elog, agent_id="step:a", state=state,
        provider="codex", model="gpt-5.5",
    )
    assert state["complete_step_args"] == {"answer": "the-answer"}
