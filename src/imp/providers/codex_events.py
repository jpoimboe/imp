# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""imp's models and parser for the `codex exec --json` event stream.

Mirrors codex-rs `exec/src/exec_events.rs`. An unknown field on a known type is
ignored; an unknown value for a declared field is a validation failure — that
asymmetry is what keeps codex schema drift visible instead of silently absorbed.
"""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from imp.providers.errors import ConfigError


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


class _UnparsedThreadItem(UnknownThreadItem):
    imp_parse_error: str


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


class Usage(_WireModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0


class ThreadStartedEvent(_WireModel):
    type: Literal["thread.started"]
    thread_id: str


class TurnStartedEvent(_WireModel):
    type: Literal["turn.started"]


class TurnCompletedEvent(_WireModel):
    type: Literal["turn.completed"]
    usage: Usage


class TurnFailedEvent(_WireModel):
    type: Literal["turn.failed"]
    # Raw so categorize_turn_failed reads `status`/`kind`; codex's least stable payload.
    error: dict[str, Any]


class ItemStartedEvent(_WireModel):
    type: Literal["item.started"]
    item: ThreadItem


class ItemUpdatedEvent(_WireModel):
    type: Literal["item.updated"]
    item: ThreadItem


class ItemCompletedEvent(_WireModel):
    type: Literal["item.completed"]
    item: ThreadItem


class ThreadErrorEvent(_WireModel):
    type: Literal["error"]
    message: str


class UnknownThreadEvent(_WireModel):
    type: str


ThreadEvent = (
    ThreadStartedEvent
    | TurnStartedEvent
    | TurnCompletedEvent
    | TurnFailedEvent
    | ItemStartedEvent
    | ItemUpdatedEvent
    | ItemCompletedEvent
    | ThreadErrorEvent
    | UnknownThreadEvent
)


class CodexEventParseError(ConfigError):
    """A codex item type imp consumes did not match imp's exec event models."""


# The item types process_event dispatches on: their payloads carry imp's control
# flow, so a parse failure there is fatal rather than skippable.
_CONSUMED_ITEM_TYPES = frozenset(
    {"mcp_tool_call", "agent_message", "reasoning", "web_search", "error"}
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

_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "thread.started": ThreadStartedEvent,
    "turn.started": TurnStartedEvent,
    "turn.completed": TurnCompletedEvent,
    "turn.failed": TurnFailedEvent,
    "item.started": ItemStartedEvent,
    "item.updated": ItemUpdatedEvent,
    "item.completed": ItemCompletedEvent,
    "error": ThreadErrorEvent,
}


def parse_thread_item(data: Any) -> ThreadItem:
    if not isinstance(data, dict):
        raise ValueError(f"invalid codex item payload (expected object): {data!r}")
    item_type = data.get("type")
    if not isinstance(item_type, str):
        raise ValueError(f"invalid codex item payload (missing type): {data!r}")
    model = _ITEM_MODELS.get(item_type, UnknownThreadItem)
    try:
        return cast("ThreadItem", model.model_validate(data))
    except ValidationError as exc:
        if item_type in _CONSUMED_ITEM_TYPES:
            raise CodexEventParseError(
                f"codex event parse failure: item type {item_type!r} does not match imp's codex "
                f"exec event model — update src/imp/providers/codex_events.py",
                detail={"item_type": item_type, "validation_error": str(exc)},
            ) from exc
        item_id = data.get("id")
        return _UnparsedThreadItem(
            id=item_id if isinstance(item_id, str) else "", type=item_type, imp_parse_error=str(exc)
        )


def parse_thread_event(data: Any) -> ThreadEvent:
    if not isinstance(data, dict):
        raise ValueError(f"invalid codex event payload (expected object): {data!r}")
    event_type = data.get("type")
    if not isinstance(event_type, str):
        raise ValueError(f"invalid codex event payload (missing type): {data!r}")
    model = _EVENT_MODELS.get(event_type)
    if model is None:
        return UnknownThreadEvent.model_validate(data)
    if event_type.startswith("item."):
        data = {**data, "item": parse_thread_item(data.get("item"))}
    return cast("ThreadEvent", model.model_validate(data))


def _first_key_wins(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # codex flattens a second `id` into web_search items; the first is the item_N one.
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key not in out:
            out[key] = value
    return out


def parse_thread_event_line(line: str) -> ThreadEvent:
    try:
        data = json.loads(line, object_pairs_hook=_first_key_wins)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid codex event line: {line!r}") from exc
    return parse_thread_event(data)
