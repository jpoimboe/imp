"""Verify _stream_history yields stream-json dicts of the right shape.

Spike 7 used three message types: assistant (with tool_use), user (with
tool_result), and user (with text). Each maps to a specific stream-json
envelope; this test pins the shape so future refactors don't break the
replay path.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.providers.claude_code import _merge_assistant_fragments, _stream_history


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


def test_merge_collapses_consecutive_user_messages() -> None:
    """An interrupted ToolSearch turn left three consecutive user-role messages
    (tool_result + "[Request interrupted]" + complete_step nudge). The Anthropic
    API requires strict role alternation; replaying three user turns in a row is
    rejected with a 400 ("tool use concurrency issues"). The replay builder must
    fold consecutive same-role messages so the replayed history alternates.

    Mirrors the exact on-disk transcript shape from change 2026-06-06-06x3ml.
    """
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=()),  # empty thinking-only fragment
        Message(role="assistant",
                content=(ToolUseBlock(id="t1", name="ToolSearch", input={}),)),
        Message(role="user",
                content=(ToolResultBlock(tool_use_id="t1", content="schemas"),)),
        Message(role="user",
                content=(TextBlock(text="[Request interrupted by user]"),)),
        Message(role="user",
                content=(TextBlock(text="You ended your turn without complete_step"),)),
    ]
    out = _merge_assistant_fragments(messages)
    roles = [m.role for m in out]
    assert all(a != b for a, b in itertools.pairwise(roles)), (
        f"replayed history has consecutive same-role messages: {roles}")
    # The three folded user blocks survive in order, tool_result first (the API
    # requires the tool_result to lead the user turn that answers a tool_use).
    last = out[-1]
    assert last.role == "user"
    kinds = [type(b).__name__ for b in last.content]
    assert kinds == ["ToolResultBlock", "TextBlock", "TextBlock"], kinds


def test_merge_still_drops_empty_assistant_and_folds_assistant_fragments() -> None:
    """Existing behavior: empty assistant fragments are dropped and consecutive
    assistant fragments fold into one turn."""
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=()),  # dropped
        Message(role="assistant", content=(TextBlock(text="think"),)),
        Message(role="assistant",
                content=(ToolUseBlock(id="t1", name="read_file", input={}),)),
    ]
    out = _merge_assistant_fragments(messages)
    assert [m.role for m in out] == ["user", "assistant"]
    kinds = [type(b).__name__ for b in out[1].content]
    assert kinds == ["TextBlock", "ToolUseBlock"], kinds
