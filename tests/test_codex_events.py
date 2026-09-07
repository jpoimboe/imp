"""Unit tests for imp's own codex exec event models and JSONL parser."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from imp.providers.codex_events import (
    CommandExecutionItem,
    FileChangeItem,
    McpToolCallItem,
    UnknownThreadItem,
    parse_thread_item,
)

# Real codex 0.153 wire payloads (codex-rs exec/src/exec_events.rs at rust-v0.153.2).
_ITEM_FILE_CHANGE_STARTED = {
    "id": "i1",
    "type": "file_change",
    "changes": [{"path": "a", "kind": "add"}],
    "status": "in_progress",
}
_ITEM_COMMAND_DECLINED = {
    "id": "i2",
    "type": "command_execution",
    "command": "ls",
    "aggregated_output": "",
    "exit_code": None,
    "status": "declined",
}
_ITEM_MCP_STRING_CONTENT = {
    "id": "i3",
    "type": "mcp_tool_call",
    "server": "imp",
    "tool": "bash",
    "arguments": {},
    "result": {"content": ["plain string"], "structured_content": None},
    "status": "completed",
}


def test_real_wire_item_payloads_parse_with_their_values() -> None:
    """The three payloads the pinned SDK's own models reject must parse here."""
    changed = parse_thread_item(_ITEM_FILE_CHANGE_STARTED)
    assert type(changed) is FileChangeItem
    assert changed.status == "in_progress"
    assert [(c.path, c.kind) for c in changed.changes] == [("a", "add")]

    declined = parse_thread_item(_ITEM_COMMAND_DECLINED)
    assert type(declined) is CommandExecutionItem
    assert declined.status == "declined"
    assert declined.command == "ls"
    assert declined.exit_code is None

    mcp = parse_thread_item(_ITEM_MCP_STRING_CONTENT)
    assert type(mcp) is McpToolCallItem
    assert mcp.result is not None
    assert mcp.result.content == ["plain string"]
    assert mcp.result.structured_content is None
    assert mcp.error is None


@pytest.mark.parametrize("status", ["in_progress", "completed", "failed"])
def test_file_change_accepts_every_rust_patch_status(status: str) -> None:
    item = parse_thread_item({"id": "i", "type": "file_change", "changes": [], "status": status})
    assert item.status == status


@pytest.mark.parametrize("status", ["in_progress", "completed", "failed", "declined"])
def test_command_execution_accepts_declined_alongside_the_rest(status: str) -> None:
    item = parse_thread_item(
        {
            "id": "i",
            "type": "command_execution",
            "command": "ls",
            "aggregated_output": "",
            "exit_code": 0,
            "status": status,
        }
    )
    assert item.status == status


@pytest.mark.parametrize("status", ["in_progress", "completed", "failed"])
def test_mcp_tool_call_accepts_every_rust_status(status: str) -> None:
    item = parse_thread_item(
        {
            "id": "i",
            "type": "mcp_tool_call",
            "server": "imp",
            "tool": "bash",
            "arguments": {},
            "result": None,
            "status": status,
        }
    )
    assert item.status == status


def test_remaining_item_types_parse_to_their_own_models() -> None:
    assert parse_thread_item({"id": "a1", "type": "agent_message", "text": "hi"}).text == "hi"
    assert parse_thread_item({"id": "r1", "type": "reasoning", "text": "why"}).text == "why"
    assert parse_thread_item({"id": "w1", "type": "web_search", "query": "q"}).query == "q"
    assert parse_thread_item({"id": "e1", "type": "error", "message": "boom"}).message == "boom"
    todos = parse_thread_item(
        {"id": "t1", "type": "todo_list", "items": [{"text": "one", "completed": False}]}
    )
    assert [(t.text, t.completed) for t in todos.items] == [("one", False)]


def test_unknown_item_type_parses_to_the_unknown_form_dropping_extras() -> None:
    item = parse_thread_item({"id": "u1", "type": "future_type", "whatever": 1})
    assert type(item) is UnknownThreadItem
    assert (item.id, item.type) == ("u1", "future_type")
    assert not hasattr(item, "whatever")


def test_known_item_keeps_its_fields_and_drops_an_undeclared_one() -> None:
    item = parse_thread_item({"id": "a1", "type": "agent_message", "text": "hi", "future_field": 2})
    assert item.text == "hi"
    assert not hasattr(item, "future_field")


@pytest.mark.parametrize(
    "payload",
    [["not", "an", "object"], "text", None, {"id": "x"}, {"id": "x", "type": 7}],
    ids=["list", "str", "none", "no-type", "non-str-type"],
)
def test_malformed_item_payload_raises_value_error(payload: object) -> None:
    with pytest.raises(ValueError, match="invalid codex item payload"):
        parse_thread_item(payload)


def test_parsed_items_are_frozen() -> None:
    item = parse_thread_item(_ITEM_MCP_STRING_CONTENT)
    with pytest.raises(ValidationError):
        item.status = "failed"
