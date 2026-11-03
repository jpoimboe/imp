# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The `tool_killed` audit event: a bash result whose returncode is a negative signal."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from imp.agents.runner import TextPart, ToolResultBlock, canonicalize_tool_name

if TYPE_CHECKING:
    from imp.engine.state import EventLog


def emit_tool_killed(elog: EventLog, agent_id: str, name: str, block: ToolResultBlock) -> None:
    """Append `tool_killed` after `block` was journaled, if it is a signal-killed bash result."""
    if canonicalize_tool_name(name) != "bash" or block.is_error:
        return
    text = _text(block.content)
    if text is None:
        return
    try:
        parsed = json.loads(text)
    except ValueError:
        return
    rc = parsed.get("returncode") if isinstance(parsed, dict) else None
    if isinstance(rc, int) and rc < 0:
        elog.append(agent_id=agent_id, type="tool_killed",
                    payload={"id": block.tool_use_id, "name": "bash", "signal": -rc})


def _text(content: str | list[Any]) -> str | None:
    if isinstance(content, str):
        return content
    texts: list[str] = []
    for p in content:
        if isinstance(p, TextPart):
            texts.append(p.text)
        elif isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
            texts.append(p["text"])
        else:
            return None
    return "".join(texts)
