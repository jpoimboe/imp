"""One-shot old→new event-log transform (pre-triad → triad schema)."""

from __future__ import annotations

import uuid
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
        elif etype == "step_context":
            has_sys = "system_prompt" in payload
            new = dict(ev)
            if has_sys:
                new["payload"] = {
                    k: v for k, v in payload.items() if k != "system_prompt"
                }
            out.append(new)
            if has_sys:
                out.append({
                    "id": uuid.uuid4().hex,
                    "parent_id": ev.get("id"),
                    "agent_id": ev.get("agent_id"),
                    "timestamp": ev.get("timestamp"),
                    "type": "engine_message",
                    "payload": {
                        "role": "system",
                        "content": [_text_block(payload["system_prompt"])],
                        "subtype": "system_prompt",
                    },
                })
        else:
            out.append(ev)
    return out
