"""One-shot old→new event-log transform (pre-triad → triad schema)."""

from __future__ import annotations

from typing import Any


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def migrate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        etype = ev.get("type")
        payload = ev.get("payload") or {}
        if etype == "llm_response":
            new = dict(ev)
            new["type"] = "agent_message"
            out.append(new)
        elif etype == "llm_response_usage_patch":
            new = dict(ev)
            new["type"] = "agent_message_usage_patch"
            out.append(new)
        elif etype == "agent_output":
            new = dict(ev)
            new["type"] = "agent_result"
            out.append(new)
        elif etype == "opening_intent":
            new = dict(ev)
            new["type"] = "user_message"
            new["payload"] = {
                "role": "user",
                "content": [_text_block(payload.get("text", ""))],
                "subtype": "intent",
            }
            out.append(new)
        elif etype == "engine_continuation":
            new = dict(ev)
            new["type"] = "engine_message"
            new["payload"] = {
                "role": "user",
                "content": [_text_block(payload.get("text", ""))],
                "subtype": "continuation",
            }
            out.append(new)
        else:
            out.append(ev)
    return out
