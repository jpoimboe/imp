"""Shared rendering of llm_response / user_message payloads. Both the `imp log`
renderer and the live ChatRenderer derive the assistant Message from
llm_response.raw via parse_message, so factor the extraction here."""

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


def render_llm_response(payload: dict[str, Any]) -> RenderedMessage:
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
            if etype == "opening_intent":
                text = str(payload.get("text", ""))
            elif etype == "user_message":
                if not payload.get("human"):
                    continue
                text = user_message_text(payload)
            elif etype == "llm_response":
                text = "\n".join(render_llm_response(payload).texts)
            else:
                continue
        except Exception:
            continue
        if text:
            parts.append(text)
    return "\n".join(parts)
