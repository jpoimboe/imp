"""Shared raw → assistant Message parse dispatch for the LLM event audit trail.

parse_message is the single shared raw→assistant-Message function, also used
by each runner — so runner and replay can never drift. Provider modules export
_parse_*_message; this module wires them into the dispatch table. (Providers do
not import replay, so there is no cycle.)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from imp.agents.runner import Message
from imp.providers.anthropic import _parse_anthropic_message
from imp.providers.claude_code import _parse_claude_code_message
from imp.providers.codex_runner import _parse_codex_message
from imp.providers.openai import _parse_responses_message

_MESSAGE_PARSERS: dict[str, Callable[[dict[str, Any]], Message]] = {
    "anthropic": _parse_anthropic_message,
    "openai": _parse_responses_message,
    "claude_code": _parse_claude_code_message,
    "codex": _parse_codex_message,
    "scripted": _parse_anthropic_message,
}


def parse_message(provider: str, raw: dict[str, Any]) -> Message:
    """raw → assistant Message. The shared parse dispatch."""
    parser = _MESSAGE_PARSERS.get(provider)
    if parser is None:
        raise KeyError(f"no message parser for provider {provider!r}")
    return parser(raw)
