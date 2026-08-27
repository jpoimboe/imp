# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""Shared rendering of agent_message / user_message payloads. Both the `imp log`
renderer and the live ChatRenderer derive the assistant Message from
agent_message.raw via parse_message, so factor the extraction here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from imp.agents.runner import TextBlock, ToolUseBlock
from imp.engine.replay import parse_message


@dataclass(frozen=True)
class RenderedMessage:
    texts: tuple[str, ...]
    tool_uses: tuple[ToolUseBlock, ...]
    stop_reason: Any
    usage: dict[str, Any]


def render_agent_message(payload: dict[str, Any]) -> RenderedMessage:
    provider = payload.get("provider", "anthropic")
    raw = payload.get("raw") or {}
    msg = parse_message(provider, raw)
    texts = tuple(b.text for b in msg.content if isinstance(b, TextBlock))
    tool_uses = tuple(b for b in msg.content if isinstance(b, ToolUseBlock))
    return RenderedMessage(
        texts=texts, tool_uses=tool_uses,
        stop_reason=payload.get("stop_reason"),
        usage=payload.get("usage") or {})


def user_message_text(payload: dict[str, Any]) -> str:
    return "".join(b.get("text", "") for b in (payload.get("content") or [])
                   if b.get("type") == "text")


def conversation_prose(events: list[Any]) -> str:
    parts: list[str] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            continue
        etype = ev.get("type")
        try:
            if etype == "user_message":
                text = user_message_text(payload)
            elif etype == "agent_message":
                text = "\n".join(render_agent_message(payload).texts)
            else:
                continue
        except Exception:
            continue
        if text:
            parts.append(text)
    return "\n".join(parts)


def _collect_strings(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_strings(v, out)


def event_text(event: Any) -> str:
    """Per-event payload strings, minus static step_context boilerplate."""
    if not isinstance(event, dict):
        return ""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return ""
    if event.get("type") == "step_context":
        payload = {k: v for k, v in payload.items()
                   if k not in ("system_prompt", "tools_schema")}
    parts: list[str] = []
    _collect_strings(payload, parts)
    return "\n".join(parts)


def events_content(events: list[Any]) -> str:
    """Full-quest text projection: every event's `event_text`, newline-joined."""
    return "\n".join(t for ev in events if (t := event_text(ev)))
