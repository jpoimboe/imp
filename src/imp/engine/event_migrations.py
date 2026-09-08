# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""One-shot old→new event-log transforms."""

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


def migrate_id_ns_event_to_block(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One-shot: restamp the deleted "event" id_ns namespace as "block" (goza26).
    Gating the tool_call stamp on the set of event-stamped RESULT ids leaves a
    genuinely-legacy pre-id_ns log (unstamped on both sides) untouched, so it keeps
    taking the renderer's `id_ns is None` ordered probe. Returns a new list; a second
    run is a no-op, which the archive's `migrated != events` check depends on."""
    result_ids: set[str] = set()
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "tool_result" and payload.get("id_ns") == "event":
            rid = payload.get("id")
            if isinstance(rid, str):
                result_ids.add(rid)
    out: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") or {}
        etype = event.get("type")
        restamp = (
            (etype == "tool_result" and payload.get("id_ns") == "event")
            or (etype == "tool_call" and payload.get("name") == "sub_agent"
                and "id_ns" not in payload and payload.get("id") in result_ids))
        if restamp:
            new = dict(event)
            new["payload"] = {**payload, "id_ns": "block"}
            out.append(new)
        else:
            out.append(event)
    return out


def backfill_query_result_session_ids(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One-shot: stamp a synthetic session_id on every query_result row written
    before the field existed (vusi13). The rows are cumulative within one SDK client
    lifetime, so the accounting path takes the last row per id; a series ends when a
    step_started for that agent intervenes or the cumulative cost drops. A row whose
    cost is not a number neither opens nor closes a series — reading it as 0.0 would
    look like a drop and re-add an already-counted total. Returns a new list; a row
    that already carries the key is passed through, so a second run is a no-op, which
    both callers' `migrated != events` checks depend on."""
    out: list[dict[str, Any]] = []
    current: dict[str | None, str] = {}
    watermark: dict[str | None, float] = {}
    minted = 0
    for event in events:
        payload = event.get("payload") or {}
        etype = event.get("type")
        if etype == "step_started":
            # step_started is an executor row (agent_id None); the agent it starts
            # is the one the executor keys as step:<phase>/<name>.
            started = f"step:{payload.get('phase')}/{payload.get('step')}"
            current.pop(started, None)
            watermark.pop(started, None)
            out.append(event)
            continue
        if etype != "query_result" or "session_id" in payload:
            out.append(event)
            continue
        aid = event.get("agent_id")
        raw = payload.get("total_cost_usd")
        cost = float(raw) if isinstance(raw, int | float) else None
        if aid not in current or (cost is not None and cost < watermark.get(aid, 0.0)):
            minted += 1
            current[aid] = f"migrated-{minted}"
        if cost is not None:
            watermark[aid] = cost
        new = dict(event)
        new["payload"] = {"session_id": current[aid], **payload}
        out.append(new)
    return out


def backfill_step_round(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One-shot: stamp `round: 1` on every step_started row written before the engine
    emitted the field (mohi69). Historical rounds are not reconstructible from the
    stream — a `progress: true` goto resets the counter and leaves no trace in any
    payload — and 1 renders no round segment, so a migrated quest reads exactly as it
    did. A row that already carries the key is passed through, so a second run is a
    no-op, which both callers' `migrated != events` checks depend on."""
    out: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") != "step_started" or "round" in payload:
            out.append(event)
            continue
        new = dict(event)
        new["payload"] = {**payload, "round": 1}
        out.append(new)
    return out
