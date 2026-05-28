from __future__ import annotations

from pathlib import Path

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.engine.replay import (
    build_messages_from_events,
    parse_message,
    reconstruct_request_for_response,
)
from imp.engine.state import InMemoryEventLog


def _agent_stream(log: InMemoryEventLog, agent_id: str) -> dict:
    """Emit a step_context, initial user_message, one llm_response with a
    tool_use, a tool_result, then a final llm_response. Return key event ids."""
    log.append_step_context(
        agent_id=agent_id, parent_id_override="seed",
        provider="anthropic", model="m", system_prompt="SYS",
        tools_schema=[{"name": "read_file"}], params={"max_tokens": 8192},
        max_tool_calls=50)
    log.append_user_message(
        agent_id=agent_id,
        message=Message(role="user", content=(TextBlock(text="begin"),)))
    resp1 = log.append_llm_response(
        agent_id=agent_id, provider="anthropic", model="m",
        raw={"content": [{"type": "tool_use", "id": "t1",
                          "name": "read_file", "input": {"p": "x"}}],
             "stop_reason": "tool_use", "usage": {}},
        usage={}, stop_reason="tool_use")
    log.append_tool_call(agent_id=agent_id,
                         block=ToolUseBlock(id="t1", name="read_file", input={"p": "x"}))
    log.append_tool_result(agent_id=agent_id,
                           block=ToolResultBlock(tool_use_id="t1", content="FILE"))
    resp2 = log.append_llm_response(
        agent_id=agent_id, provider="anthropic", model="m",
        raw={"content": [{"type": "text", "text": "done"}],
             "stop_reason": "end_turn", "usage": {}},
        usage={}, stop_reason="end_turn")
    return {"resp1": resp1["id"], "resp2": resp2["id"]}


def test_parse_message_dispatches_by_provider() -> None:
    msg = parse_message("anthropic",
                        {"content": [{"type": "text", "text": "x"}]})
    assert msg == Message(role="assistant", content=(TextBlock(text="x"),))


def test_build_messages_reconstructs_full_turn_sequence() -> None:
    log = InMemoryEventLog()
    _agent_stream(log, "step:p/s")
    msgs = build_messages_from_events(list(log.read()), agent_id="step:p/s")
    roles = [m.role for m in msgs]
    assert roles == ["user", "assistant", "user", "assistant"]
    # The synthesized tool-result wrapper is a user message of ToolResultBlocks
    assert isinstance(msgs[2].content[0], ToolResultBlock)
    assert msgs[2].content[0].content == "FILE"


def test_build_messages_filters_by_agent_id() -> None:
    log = InMemoryEventLog()
    _agent_stream(log, "step:p/s")
    _agent_stream(log, "sub:abc")
    only = build_messages_from_events(list(log.read()), agent_id="sub:abc")
    assert [m.role for m in only] == ["user", "assistant", "user", "assistant"]


def test_until_event_id_is_exclusive() -> None:
    log = InMemoryEventLog()
    ids = _agent_stream(log, "step:p/s")
    msgs = build_messages_from_events(
        list(log.read()), agent_id="step:p/s", until_event_id=ids["resp2"])
    # everything up to (not including) resp2: user, assistant, user(tool_result)
    assert [m.role for m in msgs] == ["user", "assistant", "user"]


def test_reconstruct_request_includes_system_tools_messages() -> None:
    log = InMemoryEventLog()
    ids = _agent_stream(log, "step:p/s")
    req = reconstruct_request_for_response(list(log.read()), ids["resp2"])
    assert req["system"] == "SYS"
    assert req["tools"] == [{"name": "read_file"}]
    assert req["model"] == "m"
    assert [m.role for m in req["messages"]] == ["user", "assistant", "user"]
