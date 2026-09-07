"""Unit tests for imp's own codex exec event models and JSONL parser."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from imp.providers.codex_events import (
    CodexEventParseError,
    CommandExecutionItem,
    FileChangeItem,
    McpToolCallItem,
    UnknownThreadItem,
    _UnparsedThreadItem,
    parse_thread_item,
)
from imp.providers.errors import ConfigError

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


# The exact regression fixtures the deleted SDK patch pinned: well-formed JSON, every
# required field present, only the `status` value unmodelled.
_ITEM_MCP_UNKNOWN_STATUS = {
    "id": "i8",
    "type": "mcp_tool_call",
    "server": "imp",
    "tool": "bash",
    "arguments": {},
    "result": None,
    "status": "brand_new",
}
_ITEM_FILE_CHANGE_UNKNOWN_STATUS = {
    "id": "i9",
    "type": "file_change",
    "changes": [{"path": "a", "kind": "add"}],
    "status": "brand_new",
}


def test_unmodelled_status_on_a_consumed_type_raises_classified_parse_error() -> None:
    """mcp_tool_call carries imp's control flow, so drift there is fatal."""
    with pytest.raises(CodexEventParseError) as excinfo:
        parse_thread_item(_ITEM_MCP_UNKNOWN_STATUS)

    exc = excinfo.value
    assert exc.category == "config"
    assert str(exc) == (
        "codex event parse failure: item type 'mcp_tool_call' does not match imp's codex "
        "exec event model — update src/imp/providers/codex_events.py"
    )
    assert exc.detail["item_type"] == "mcp_tool_call"
    assert "brand_new" in str(exc.detail["validation_error"])


def test_the_parse_error_is_a_config_error() -> None:
    """The runner's existing ProviderError catch classifies it config → Pause."""
    assert issubclass(CodexEventParseError, ConfigError)


def test_unmodelled_status_on_an_unconsumed_type_degrades_instead_of_raising() -> None:
    """A file_change imp discards must not kill the stream."""
    item = parse_thread_item(_ITEM_FILE_CHANGE_UNKNOWN_STATUS)
    assert type(item) is _UnparsedThreadItem
    assert (item.id, item.type) == ("i9", "file_change")
    assert "brand_new" in item.imp_parse_error
    # The rest of the payload is dropped, not half-populated.
    assert not hasattr(item, "changes")


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "a1", "type": "agent_message"},
        {"id": "r1", "type": "reasoning"},
        {"id": "w1", "type": "web_search"},
        {"id": "e1", "type": "error"},
        {"id": "m1", "type": "mcp_tool_call", "server": "imp", "tool": "bash"},
    ],
    ids=["agent_message", "reasoning", "web_search", "error", "mcp_tool_call"],
)
def test_consumed_type_missing_a_required_field_raises(payload: dict[str, object]) -> None:
    with pytest.raises(CodexEventParseError) as excinfo:
        parse_thread_item(payload)
    assert excinfo.value.detail["item_type"] == payload["type"]


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "f1", "type": "file_change"},
        {"id": "c1", "type": "command_execution"},
        {"id": "t1", "type": "todo_list"},
    ],
    ids=["file_change", "command_execution", "todo_list"],
)
def test_unconsumed_type_missing_a_required_field_degrades(payload: dict[str, object]) -> None:
    item = parse_thread_item(payload)
    assert type(item) is _UnparsedThreadItem
    assert item.type == payload["type"]
    assert item.imp_parse_error != ""


@pytest.mark.parametrize("raw_id", [None, 7], ids=["absent", "non-str"])
def test_degradation_survives_a_payload_with_no_usable_id(raw_id: object) -> None:
    """The degradation constructor must never raise a second ValidationError."""
    payload: dict[str, object] = {"type": "file_change", "status": "brand_new", "changes": []}
    if raw_id is not None:
        payload["id"] = raw_id

    item = parse_thread_item(payload)
    assert type(item) is _UnparsedThreadItem
    assert item.id == ""


def test_an_unknown_item_type_never_degrades() -> None:
    """An unknown type is forward-compat, not drift: it stays UnknownThreadItem."""
    item = parse_thread_item({"id": "u2", "type": "future_type"})
    assert type(item) is UnknownThreadItem
