"""raw → assistant Message parsers, one per provider wire format.

They live outside the provider modules so imp.engine.replay can build its
dispatch table — on the read-only CLI path behind `imp list` / `imp log` /
`imp events` — without importing a provider module, and with it httpx and the
rest of the agent stack. Parsing needs nothing but imp.agents.runner's blocks.
"""

from __future__ import annotations

import json
from typing import Any, cast

from imp.agents.runner import Message, TextBlock, ThinkingBlock, ToolUseBlock


def _parse_anthropic_message(raw: dict[str, Any]) -> Message:
    """raw → assistant Message. Reconstruction-critical; shared with replay.py.
    Thinking blocks (signed + redacted) are PRESERVED as ThinkingBlocks so a
    tool-use turn carrying thinking replays unchanged; unknown blocks are
    dropped (they're not conversation history)."""
    content: list[Any] = []
    for block in raw.get("content", []):
        kind = block.get("type")
        if kind == "text":
            content.append(TextBlock(text=block.get("text", "")))
        elif kind == "tool_use":
            content.append(ToolUseBlock(id=block["id"], name=block["name"],
                                        input=block.get("input", {})))
        elif kind == "thinking":
            content.append(ThinkingBlock(
                thinking=block.get("thinking", ""),
                signature=block.get("signature", ""),
            ))
        elif kind == "redacted_thinking":
            content.append(ThinkingBlock(redacted_thinking=block.get("data", "")))
        # other unknown blocks: skipped (see diagnostics)
    return Message(role="assistant", content=tuple(content))


def _parse_responses_message(raw: dict[str, Any]) -> Message:
    """Reconstruct the assistant Message from Responses `output[]`.

    message → output_text parts become TextBlocks; function_call items become
    ToolUseBlocks. web_search_call / reasoning items are NOT conversation
    content (web events are emitted separately in Task 18)."""
    blocks: list[Any] = []
    for item in raw.get("output", []) or []:
        itype = item.get("type")
        if itype == "message":
            text = "".join(
                part.get("text", "")
                for part in item.get("content", []) or []
                if part.get("type") == "output_text")
            if text:
                blocks.append(TextBlock(text=text))
        elif itype == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            blocks.append(ToolUseBlock(
                id=item.get("call_id", ""),
                name=cast(str, item.get("name")), input=args))
    return Message(role="assistant", content=tuple(blocks))


def _parse_claude_code_message(raw: dict[str, Any]) -> Message:
    content: list[Any] = []
    for block in raw.get("content", []):
        kind = block.get("type")
        if kind == "text":
            content.append(TextBlock(text=block.get("text", "")))
        elif kind == "tool_use":
            content.append(ToolUseBlock(id=block["id"], name=block["name"],
                                        input=block.get("input", {})))
    return Message(role="assistant", content=tuple(content))


def _parse_codex_message(raw: dict[str, Any]) -> Message:
    """raw → assistant Message for codex.  A codex `agent_message.raw` now
    carries prose text blocks only (tool_use lives in the standalone
    `tool_call` events); this parser also tolerates a tool_use block for the
    hand-built audit/parse fixtures.  A web_search appears as a query-only
    tool_use block.  Unknown blocks are dropped.
    """
    content: list[Any] = []
    for block in raw.get("content", []):
        kind = block.get("type")
        if kind == "text":
            content.append(TextBlock(text=block.get("text", "")))
        elif kind == "tool_use":
            content.append(ToolUseBlock(
                id=block["id"], name=block["name"],
                input=block.get("input", {})))
        # unknown: skipped
    return Message(role="assistant", content=tuple(content))
