"""Fix 2: codex render/audit parser + turn reassembly."""
from __future__ import annotations

from imp.agents.runner import TextBlock, ToolUseBlock
from imp.engine.replay import parse_message


def test_parse_codex_text_only_message() -> None:
    raw = {"content": [{"type": "text", "text": "hello"}]}
    msg = parse_message("codex", raw)
    assert msg.role == "assistant"
    assert msg.content == (TextBlock(text="hello"),)


def test_parse_codex_message_with_tool_use_block() -> None:
    raw = {"content": [
        {"type": "text", "text": "calling tool"},
        {"type": "tool_use", "id": "call-1", "name": "edit_file",
         "input": {"path": "a.py"}},
    ]}
    msg = parse_message("codex", raw)
    assert msg.content == (
        TextBlock(text="calling tool"),
        ToolUseBlock(id="call-1", name="edit_file", input={"path": "a.py"}),
    )


def test_parse_codex_web_search_block_is_query_only() -> None:
    # web_search is represented as a tool_use carrying only the query (the
    # ungoverned/opaque limitation: codex exposes no results/citations).
    raw = {"content": [
        {"type": "tool_use", "id": "ws-1", "name": "web_search",
         "input": {"query": "python asyncio"}},
    ]}
    msg = parse_message("codex", raw)
    assert msg.content == (
        ToolUseBlock(id="ws-1", name="web_search",
                     input={"query": "python asyncio"}),
    )

from imp.providers.codex_runner import reassemble_codex_turn  # noqa: E402
from tests.codex_fakes import (  # noqa: E402
    FakeAgentMessageItem,
    FakeMcpToolCallItem,
    FakeWebSearchItem,
)


def test_reassemble_codex_turn_builds_full_turn_raw() -> None:
    items = [
        FakeAgentMessageItem(text="I'll edit the file"),
        FakeMcpToolCallItem(tool="edit_file", arguments={"path": "a.py"},
                            id="call-1"),
        FakeWebSearchItem(query="asyncio gather"),
    ]
    raw = reassemble_codex_turn(items)
    assert raw == {"content": [
        {"type": "text", "text": "I'll edit the file"},
        {"type": "tool_use", "id": "call-1", "name": "edit_file",
         "input": {"path": "a.py"}},
        {"type": "tool_use", "id": "", "name": "web_search",
         "input": {"query": "asyncio gather"}},
    ]}


def test_reassemble_task_tool_use_keeps_sdk_id() -> None:
    """Task tool_use blocks keep their SDK item id."""
    items = [FakeMcpToolCallItem(tool="sub_agent", arguments={"subagent_name": "impl"},
                                 id="sdk-task-9")]
    raw = reassemble_codex_turn(items)
    assert raw["content"][0]["id"] == "sdk-task-9"

