# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""imp's models and parser for the `codex exec --json` event stream.

Mirrors codex-rs `exec/src/exec_events.rs`. An unknown field on a known type is
ignored; an unknown value for a declared field is a validation failure — that
asymmetry is what keeps codex schema drift visible instead of silently absorbed.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict


class _WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class AgentMessageItem(_WireModel):
    id: str
    type: Literal["agent_message"]
    text: str


class ReasoningItem(_WireModel):
    id: str
    type: Literal["reasoning"]
    text: str


class CommandExecutionItem(_WireModel):
    id: str
    type: Literal["command_execution"]
    command: str
    aggregated_output: str
    exit_code: int | None = None
    status: Literal["in_progress", "completed", "failed", "declined"]


class FileUpdateChange(_WireModel):
    path: str
    kind: Literal["add", "delete", "update"]


class FileChangeItem(_WireModel):
    id: str
    type: Literal["file_change"]
    changes: list[FileUpdateChange]
    status: Literal["in_progress", "completed", "failed"]


class McpToolCallResult(_WireModel):
    # rust sends Vec<JsonValue>: a content entry need not be an object.
    content: list[Any]
    structured_content: Any


class McpToolCallError(_WireModel):
    message: str


class McpToolCallItem(_WireModel):
    id: str
    type: Literal["mcp_tool_call"]
    server: str
    tool: str
    arguments: Any
    result: McpToolCallResult | None = None
    error: McpToolCallError | None = None
    status: Literal["in_progress", "completed", "failed"]


class WebSearchItem(_WireModel):
    id: str
    type: Literal["web_search"]
    query: str


class TodoItem(_WireModel):
    text: str
    completed: bool


class TodoListItem(_WireModel):
    id: str
    type: Literal["todo_list"]
    items: list[TodoItem]


class ErrorItem(_WireModel):
    id: str
    type: Literal["error"]
    message: str


class UnknownThreadItem(_WireModel):
    id: str
    type: str


ThreadItem = (
    AgentMessageItem
    | ReasoningItem
    | CommandExecutionItem
    | FileChangeItem
    | McpToolCallItem
    | WebSearchItem
    | TodoListItem
    | ErrorItem
    | UnknownThreadItem
)

_ITEM_MODELS: dict[str, type[BaseModel]] = {
    "agent_message": AgentMessageItem,
    "reasoning": ReasoningItem,
    "command_execution": CommandExecutionItem,
    "file_change": FileChangeItem,
    "mcp_tool_call": McpToolCallItem,
    "web_search": WebSearchItem,
    "todo_list": TodoListItem,
    "error": ErrorItem,
}


def parse_thread_item(data: Any) -> ThreadItem:
    if not isinstance(data, dict):
        raise ValueError(f"invalid codex item payload (expected object): {data!r}")
    item_type = data.get("type")
    if not isinstance(item_type, str):
        raise ValueError(f"invalid codex item payload (missing type): {data!r}")
    model = _ITEM_MODELS.get(item_type, UnknownThreadItem)
    return cast("ThreadItem", model.model_validate(data))
