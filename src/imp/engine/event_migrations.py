# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""One-shot old→new event-log transform (pre-triad → triad schema)."""

from __future__ import annotations

import uuid
from typing import Any


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def migrate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # agent_ids whose next engine-injected user_message is the completion-nudge
    # injection (collapsed into the engine_message the nudge already emitted).
    suppress_completion_user: set[str | None] = set()
    # agent_ids whose next engine-injected user_message is the agent_prompt.
    prompt_pending: set[str | None] = set()
    n = len(events)
    for i, ev in enumerate(events):
        etype = ev.get("type")
        aid = ev.get("agent_id")
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
        elif etype == "completion_nudge":
            new = dict(ev)
            new["type"] = "engine_message"
            new["payload"] = {
                "role": "user",
                "content": [_text_block(payload.get("message", ""))],
                "subtype": "completion",
            }
            out.append(new)
            suppress_completion_user.add(aid)
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
                    "agent_id": aid,
                    "timestamp": ev.get("timestamp"),
                    "type": "engine_message",
                    "payload": {
                        "role": "system",
                        "content": [_text_block(payload["system_prompt"])],
                        "subtype": "system_prompt",
                    },
                })
            prompt_pending.add(aid)
        elif etype == "user_interjected":
            continue
        elif etype == "user_message" and "human" in payload:
            if payload.get("human"):
                nxt = events[i + 1] if i + 1 < n else None
                interject = (
                    nxt is not None
                    and nxt.get("type") == "user_interjected"
                    and nxt.get("agent_id") == aid
                )
                new = dict(ev)
                new["payload"] = {
                    "role": payload.get("role", "user"),
                    "content": payload.get("content", []),
                    "subtype": "interject" if interject else "reply",
                }
                out.append(new)
            elif aid in suppress_completion_user:
                suppress_completion_user.discard(aid)
            else:
                if aid in prompt_pending:
                    prompt_pending.discard(aid)
                    subtype = "agent_prompt"
                else:
                    subtype = "continuation"
                new = dict(ev)
                new["type"] = "engine_message"
                new["payload"] = {
                    "role": payload.get("role", "user"),
                    "content": payload.get("content", []),
                    "subtype": subtype,
                }
                out.append(new)
        else:
            out.append(ev)
    return out
