"""Verify _stream_history yields stream-json dicts of the right shape.

Spike 7 used three message types: assistant (with tool_use), user (with
tool_result), and user (with text). Each maps to a specific stream-json
envelope; this test pins the shape so future refactors don't break the
replay path.
"""

from __future__ import annotations

import asyncio
from typing import Any

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.providers.claude_code import _stream_history


async def _drain(async_iter: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async for x in async_iter:
        out.append(x)
    return out


def test_assistant_with_tool_use_envelope() -> None:
    msg = Message(
        role="assistant",
        content=(
            TextBlock(text="reading"),
            ToolUseBlock(id="t1", name="read_file", input={"path": "/p/x"}),
        ),
    )
    out = asyncio.run(_drain(_stream_history([msg])))
    assert len(out) == 1
    env = out[0]
    assert env["type"] == "assistant"
    assert "session_id" in env
    inner = env["message"]
    assert inner["role"] == "assistant"
    assert inner["content"][0] == {"type": "text", "text": "reading"}
    assert inner["content"][1] == {
        "type": "tool_use", "id": "t1", "name": "read_file",
        "input": {"path": "/p/x"},
    }


def test_user_with_tool_result_envelope() -> None:
    msg = Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t1", content="hello"),),
    )
    out = asyncio.run(_drain(_stream_history([msg])))
    env = out[0]
    assert env["type"] == "user"
    inner = env["message"]
    assert inner["role"] == "user"
    assert inner["content"][0]["type"] == "tool_result"
    assert inner["content"][0]["tool_use_id"] == "t1"


def test_user_text_envelope_inline() -> None:
    """Pure text user messages become inline strings, not block lists."""
    msg = Message(role="user", content=(TextBlock(text="hi"),))
    out = asyncio.run(_drain(_stream_history([msg])))
    inner = out[0]["message"]
    # Either inline string OR a single text block — both are accepted by claude.
    # We chose inline for compactness (Spike 7's "new user prompt" used this form).
    assert inner["content"] == "hi" or inner["content"] == [
        {"type": "text", "text": "hi"},
    ]
