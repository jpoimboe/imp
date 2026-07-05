"""Workflow-agnostic slim digests of engine events for query_events.

One event -> one line: envelope (timestamp, type, agent_id) + a per-type
one-liner. Keyed strictly on ENGINE event types — references no workflow
step/phase/artifact names. Tolerant of missing envelope keys (raw
llm_response_usage_patch rows), malformed rows, and unparseable payloads.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

_TEXT_CAP = 200
_RESULT_CAP = 120


def _clip(text: str, cap: int) -> str:
    """Collapse whitespace (keeps the digest single-line) and cap length."""
    text = " ".join(text.split())
    return text if len(text) <= cap else text[:cap] + "…"


def _envelope(event: dict[str, Any]) -> str:
    ts = event.get("timestamp") or "-"
    etype = event.get("type") or "?"
    aid = event.get("agent_id") or "-"
    return f"{ts} {etype} [{aid}]"


def _message_text(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if isinstance(text, str):
        return text
    parts: list[str] = []
    for b in payload.get("content") or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(str(b.get("text", "")))
    return " ".join(parts)


def _parse_llm(payload: dict[str, Any]) -> tuple[str, int]:
    """Best-effort text + tool_use count from llm_response.raw. Never raises:
    a provider with no parser or a malformed raw degrades to ("", 0)."""
    try:
        from imp.engine.replay import parse_message
        msg = parse_message(payload.get("provider", "anthropic"),
                            payload.get("raw") or {})
        texts = " ".join(str(getattr(b, "text", "")) for b in msg.content
                         if getattr(b, "type", None) == "text")
        n_tools = sum(1 for b in msg.content
                      if getattr(b, "type", None) == "tool_use")
        return texts, n_tools
    except Exception:
        return "", 0


def _h_user(payload: dict[str, Any]) -> str:
    role = payload.get("role", "user")
    return f"{role}: {_clip(_message_text(payload), _TEXT_CAP)}"


def _h_awaiting(payload: dict[str, Any]) -> str:
    return _clip(str(payload.get("prompt", "")), _TEXT_CAP)


def _h_llm_response(payload: dict[str, Any]) -> str:
    texts, n_tools = _parse_llm(payload)
    usage = payload.get("usage") or {}
    return (f"stop={payload.get('stop_reason')} tools={n_tools} "
            f"usage={usage} {_clip(texts, _TEXT_CAP)}")


def _h_tool_call(payload: dict[str, Any]) -> str:
    summary = _clip(json.dumps(payload.get("input"), default=str), _RESULT_CAP)
    return f"{payload.get('name', '?')} {summary}"


def _h_tool_result(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    return (f"id={payload.get('id', '?')} error={payload.get('error')} "
            f"bytes={len(text.encode())} {_clip(text, _RESULT_CAP)}")


def _h_agent_output(payload: dict[str, Any]) -> str:
    return _clip(json.dumps(payload, default=str), _TEXT_CAP)


def _h_step_context(payload: dict[str, Any]) -> str:
    # Deliberately omits system_prompt + tools_schema (redaction by construction).
    return f"provider={payload.get('provider')} model={payload.get('model')}"


def _generic(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for k, v in payload.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            parts.append(f"{k}={v}")
        else:
            parts.append(f"{k}=<{type(v).__name__}>")
    return _clip(" ".join(parts), _TEXT_CAP)


def _h_warning(payload: dict[str, Any]) -> str:
    return f"warning: {_clip(str(payload.get('message', '')), _TEXT_CAP)}"


_HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "user_message": _h_user,
    "user_interjected": _h_user,
    "awaiting_user_input": _h_awaiting,
    "llm_response": _h_llm_response,
    "tool_call": _h_tool_call,
    "tool_result": _h_tool_result,
    "agent_output": _h_agent_output,
    "step_context": _h_step_context,
    "warning": _h_warning,
}


def _summarize(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    etype = event.get("type")
    handler = _HANDLERS.get(etype) if isinstance(etype, str) else None
    return handler(payload) if handler is not None else _generic(payload)


def digest_event(event: dict[str, Any]) -> str:
    """Render one event as a single slim digest line."""
    if event.get("_malformed"):
        return f"<malformed> {_clip(str(event.get('raw', '')), _RESULT_CAP)}"
    return f"{_envelope(event)} {_summarize(event)}"


_FULL_CAP = 2048
_TRUNC_MARK = "…[truncated — narrow the query]"
_REDACTED = "…[redacted]"


def _redact(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("type") != "step_context":
        return event
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return event
    redacted = dict(event)
    p = dict(payload)
    for key in ("system_prompt", "tools_schema"):
        if key in p:
            p[key] = _REDACTED
    redacted["payload"] = p
    return redacted


def full_line(event: dict[str, Any]) -> str:
    """One raw event object as JSON, step_context-redacted and byte-capped."""
    serialized = json.dumps(_redact(event), default=str)
    if len(serialized) > _FULL_CAP:
        return serialized[:_FULL_CAP] + _TRUNC_MARK
    return serialized
