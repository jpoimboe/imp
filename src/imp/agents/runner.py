"""Internal message shapes + AgentRunner protocol (spec §6.1, §6.3, §6.4).

All three V1 runners (Anthropic this plan; OpenAI + Claude Code in Plan 2)
share these types. Each runner converts to/from its provider-native shape
inside `run()`. Transcripts on disk are JSONL of these types.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from imp.engine.types import AgentDef, FsRule


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["user", "assistant", "system"]
    content: tuple[ContentBlock, ...]


# Engine → runner boundary value. Frozen to make it safe to pass to subagents.
@dataclass(frozen=True, slots=True)
class ChangeContext:
    change_id: int
    project_root: Path
    artifacts_dir: Path


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    agent_def: AgentDef
    provider_name: str                     # "anthropic" | "openai" | "claude_code"
    model_name: str
    system_prompt: str
    initial_messages: tuple[Message, ...]
    change_context: ChangeContext
    interactive: bool
    transcript_dir: Path                   # .imp/changes/N/steps/<phase>/<step>/
    fs_permissions: tuple[FsRule, ...]
    events_path: Path
    subagent_defs: dict[str, AgentDef] = field(default_factory=dict)
    max_tool_calls: int | None = None      # resolved by engine (Task 27a/28); None means "use executor default of 50"


Event = dict[str, Any]                     # {"type": str, "payload": dict}


class AgentRunner(Protocol):
    """Each provider's runner implements this. Engine consumes the event stream."""

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        """Yield events. Terminal event is `agent_output` (success) or `error`."""
        ...

    async def interrupt(self) -> None:
        """Cancel any in-flight LLM stream or tool subprocess. Idempotent.
        Called by `cli/signals.py` on ESC/Ctrl+C/Ctrl+D."""
        ...


# ── JSON serialization for transcript.jsonl ────────────────────────────────


def message_to_json(msg: Message) -> str:
    payload: dict[str, Any] = {
        "role": msg.role,
        "content": [_block_to_dict(b) for b in msg.content],
    }
    return json.dumps(payload, separators=(",", ":"))


def _block_to_dict(b: ContentBlock) -> dict[str, Any]:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {
        "type": "tool_result", "tool_use_id": b.tool_use_id,
        "content": b.content, "is_error": b.is_error,
    }


def message_from_json(data: dict[str, Any]) -> Message:
    blocks: list[ContentBlock] = []
    for raw in data["content"]:
        kind = raw["type"]
        if kind == "text":
            blocks.append(TextBlock(text=raw["text"]))
        elif kind == "tool_use":
            blocks.append(ToolUseBlock(id=raw["id"], name=raw["name"], input=raw["input"]))
        elif kind == "tool_result":
            blocks.append(ToolResultBlock(
                tool_use_id=raw["tool_use_id"],
                content=raw["content"],
                is_error=raw.get("is_error", False),
            ))
        else:
            raise ValueError(f"unknown block type {kind!r}")
    return Message(role=data["role"], content=tuple(blocks))
