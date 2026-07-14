"""Workflow-agnostic slim digests of engine events for the imp events command.

One event -> one line: envelope (timestamp, type, agent_id) + a per-type
one-liner. Keyed strictly on ENGINE event types — references no workflow
step/phase/artifact names. Tolerant of missing envelope keys (raw
llm_response_usage_patch rows), malformed rows, and unparseable payloads.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def _collapse(text: str) -> str:
    """Collapse whitespace to keep the digest one logical line; length is clipped in digest_line."""
    return " ".join(text.split())


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


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(b.get("text", "")) for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        if parts:
            return " ".join(parts)
    return json.dumps(content, default=str)


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
    return f"{role}: {_collapse(_message_text(payload))}"


def _h_awaiting(payload: dict[str, Any]) -> str:
    return _collapse(str(payload.get("steer_hint", "")))


def _h_llm_response(payload: dict[str, Any]) -> str:
    texts, n_tools = _parse_llm(payload)
    usage = payload.get("usage") or {}
    return (f"stop={payload.get('stop_reason')} tools={n_tools} "
            f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
            f"{_collapse(texts)}")


def _h_tool_call(payload: dict[str, Any]) -> str:
    summary = _collapse(json.dumps(payload.get("input"), default=str))
    return f"{payload.get('name', '?')} {summary}"


def _h_tool_result(payload: dict[str, Any]) -> str:
    text = _content_text(payload.get("content"))
    return (f"error={payload.get('error')} bytes={len(text.encode())} "
            f"{_collapse(text)}")


def _h_agent_output(payload: dict[str, Any]) -> str:
    return _collapse(json.dumps(payload, default=str))


def _h_step_context(payload: dict[str, Any]) -> str:
    # Deliberately omits system_prompt + tools_schema (redaction by construction).
    return f"provider={payload.get('provider')} model={payload.get('model')}"


def _h_step_started(payload: dict[str, Any]) -> str:
    # model/provider absent on script/gate steps; None output is acceptable.
    return (f"step={payload.get('step')} phase={payload.get('phase')} "
            f"kind={payload.get('kind')} model={payload.get('model')} "
            f"provider={payload.get('provider')}")


def _h_query_result(payload: dict[str, Any]) -> str:
    # Positive-select five fields; every other payload key drops by construction.
    cost = payload.get("total_cost_usd")
    dur = payload.get("duration_ms")
    cost_s = f"${cost:.2f}" if isinstance(cost, (int, float)) else f"${cost}"
    dur_s = f"{dur / 1000:.1f}s" if isinstance(dur, (int, float)) else f"{dur}s"
    return (f"cost={cost_s} dur={dur_s} turns={payload.get('num_turns')} "
            f"stop={payload.get('stop_reason')} subtype={payload.get('subtype')}")


def _generic(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for k, v in payload.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            parts.append(f"{k}={v}")
        else:
            parts.append(f"{k}=<{type(v).__name__}>")
    return _collapse(" ".join(parts))


def _h_warning(payload: dict[str, Any]) -> str:
    return f"warning: {_collapse(str(payload.get('message', '')))}"


_HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "user_message": _h_user,
    "user_interjected": _h_user,
    "awaiting_user_input": _h_awaiting,
    "llm_response": _h_llm_response,
    "step_started": _h_step_started,
    "query_result": _h_query_result,
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


def summarize(event: dict[str, Any]) -> str:
    """Public type-aware one-line summary (the digest tail, no envelope) —
    reused by `imp events`' compact `#N` digest line."""
    return _summarize(event)


def digest_event(event: dict[str, Any]) -> str:
    """Render one event as a single slim digest line."""
    if event.get("_malformed"):
        return f"<malformed> {_collapse(str(event.get('raw', '')))}"
    return f"{_envelope(event)} {_summarize(event)}"
