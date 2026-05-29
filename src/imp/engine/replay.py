"""Reconstruction helpers for the LLM event audit trail (design 2026-05-28).

The reconstruction surface is strictly deduped: user_message events,
llm_response.raw (parsed to assistant Messages), and tool_result events.
tool_call / thinking events are render/lifecycle conveniences and are NOT
consulted here.

parse_message is the single shared raw→assistant-Message function, also used
by each runner — so runner and replay can never drift. Provider modules export
_parse_*_message; this module wires them into the dispatch table. (Providers do
not import replay, so there is no cycle.)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any, cast

from imp.agents.runner import (
    Diagnostics,
    Message,
    TextBlock,
    ToolResultBlock,
)
from imp.providers.anthropic import (
    _extract_anthropic_diagnostics,
    _parse_anthropic_message,
)
from imp.providers.claude_code import (
    _extract_claude_code_diagnostics,
    _parse_claude_code_message,
)
from imp.providers.openai import (
    _extract_openai_diagnostics,
    _parse_openai_message,
)

_MESSAGE_PARSERS: dict[str, Callable[[dict[str, Any]], Message]] = {
    "anthropic": _parse_anthropic_message,
    "openai": _parse_openai_message,
    "claude_code": _parse_claude_code_message,
}

_DIAGNOSTICS_EXTRACTORS: dict[str, Callable[[dict[str, Any]], Diagnostics]] = {
    "anthropic": _extract_anthropic_diagnostics,
    "openai": _extract_openai_diagnostics,
    "claude_code": _extract_claude_code_diagnostics,
}


def parse_message(provider: str, raw: dict[str, Any]) -> Message:
    """raw → assistant Message. Reconstruction's single source of truth."""
    parser = _MESSAGE_PARSERS.get(provider)
    if parser is None:
        raise KeyError(f"no message parser for provider {provider!r}")
    return parser(raw)


def extract_diagnostics(provider: str, raw: dict[str, Any]) -> Diagnostics:
    """Render-only: (block_warnings, thinkings). Not used by reconstruction."""
    extractor = _DIAGNOSTICS_EXTRACTORS.get(provider)
    if extractor is None:
        return Diagnostics()
    return extractor(raw)


def _user_message_from_event(payload: dict[str, Any]) -> Message:
    blocks = tuple(
        TextBlock(text=b.get("text", ""))
        for b in payload.get("content", [])
        if b.get("type") == "text"
    )
    return Message(role="user", content=blocks)


def build_messages_from_events(
    events: Iterable[dict[str, Any]],
    *,
    agent_id: str,
    until_event_id: str | None = None,
) -> tuple[Message, ...]:
    """Reconstruct the agent's messages array from its deduped surface."""
    msgs: list[Message] = []
    pending_results: list[ToolResultBlock] = []

    def flush() -> None:
        if pending_results:
            msgs.append(Message(role="user", content=tuple(pending_results)))
            pending_results.clear()

    for e in events:
        if e.get("agent_id") != agent_id:
            continue
        if until_event_id is not None and e.get("id") == until_event_id:
            break
        t = e.get("type")
        p = e.get("payload") or {}
        if t == "user_message":
            flush()
            msgs.append(_user_message_from_event(p))
        elif t == "llm_response":
            flush()
            msgs.append(parse_message(p["provider"], p["raw"]))
        elif t == "tool_result":
            pending_results.append(ToolResultBlock(
                tool_use_id=p["id"], content=p.get("content", ""),
                is_error=bool(p.get("error", False))))
    flush()
    return tuple(msgs)


def reconstruct_request_for_response(
    events: Sequence[dict[str, Any]],
    response_event_id: str,
) -> dict[str, Any]:
    """Return {system, tools, model, params, messages} that produced the given
    llm_response. For provider='claude_code' this is a normalized approximation
    (claude_code makes no interceptable per-turn API call)."""
    response = next((e for e in events if e.get("id") == response_event_id), None)
    if response is None:
        raise KeyError(f"no event with id {response_event_id!r}")
    agent_id = response.get("agent_id")

    ctx: dict[str, Any] | None = None
    for e in events:
        if e.get("id") == response_event_id:
            break
        if e.get("agent_id") == agent_id and e.get("type") == "step_context":
            ctx = e.get("payload") or {}
    if ctx is None:
        raise ValueError(f"no step_context for agent {agent_id!r} before response")

    messages = build_messages_from_events(
        events, agent_id=cast(str, agent_id), until_event_id=response_event_id)
    return {
        "system": ctx.get("system_prompt"),
        "tools": ctx.get("tools_schema"),
        "model": ctx.get("model"),
        "params": ctx.get("params"),
        "messages": messages,
    }
