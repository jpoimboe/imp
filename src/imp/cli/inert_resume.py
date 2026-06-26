"""Inert resume of a change parked at a bare-turn-end yield (spec AC8) — cheap local reads only."""

from __future__ import annotations

from imp.agents.runner import Message, TextBlock


def render_pending_question(messages: list[Message]) -> str | None:
    """Last assistant text in `messages`, else nearest preceding prose; None if none."""
    for msg in reversed(messages):
        if msg.role != "assistant":
            continue
        text = "\n".join(
            b.text for b in msg.content if isinstance(b, TextBlock)
        ).strip()
        if text:
            return text
    return None
