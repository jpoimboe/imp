# tests/test_runner_types.py
from __future__ import annotations

import json

from imp.agents.runner import (
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    message_from_json,
    message_to_json,
)


def test_message_immutable() -> None:
    msg = Message(role="user", content=(TextBlock(text="hi"),))
    assert msg.role == "user"
    assert msg.content[0].text == "hi"
    try:
        msg.role = "assistant"  # type: ignore[misc]
        raise AssertionError("Message should be frozen")
    except AttributeError:
        pass


def test_round_trip_text_only() -> None:
    msg = Message(role="assistant", content=(TextBlock(text="hello"),))
    raw = message_to_json(msg)
    back = message_from_json(json.loads(raw))
    assert back == msg
    assert isinstance(back.content, tuple)  # JSON list → tuple


def test_round_trip_tool_use_and_result() -> None:
    msg = Message(
        role="assistant",
        content=(
            TextBlock(text="reading…"),
            ToolUseBlock(id="t1", name="read_file", input={"path": "/p/x"}),
        ),
    )
    raw = message_to_json(msg)
    back = message_from_json(json.loads(raw))
    assert back == msg

    result = Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t1", content="contents", is_error=False),),
    )
    raw = message_to_json(result)
    back = message_from_json(json.loads(raw))
    assert back == result
