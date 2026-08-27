# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""Inert resume of a quest parked at a bare-turn-end yield (spec AC8) — cheap local reads only."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from imp.engine.quest import Quest
from imp.remote.session.protocol import SkillRequest


def render_inert_question(text: str, *, width: int, color: bool) -> str:
    """Render the pending bare-turn-end question for the inert resume preview
    through `_render_markdown_block` — the same renderer every other agent turn
    uses — so the preview shows the bold `• ` turn bullet and a 2-column
    hang-indent, and reads as prose (markdown→ANSI) rather than raw markdown
    source. Called at depth 0, so the gutter is empty."""
    from imp.cli.render_text import _render_markdown_block
    return "\n".join(_render_markdown_block(text, width, 0, color))


_ANSWER_TRANSITIONS = frozenset({"step_started", "step_completed"})
_HUMAN_ANSWER_SUBTYPES = frozenset({"reply", "interject"})


def _is_gate_answer(event: dict[str, Any]) -> bool:
    """A wait is consumed by a solicited human turn (user_message reply/interject)
    or a step transition. An opening intent is not a solicited answer."""
    etype = event.get("type")
    if etype in _ANSWER_TRANSITIONS:
        return True
    if etype == "user_message":
        return (event.get("payload") or {}).get("subtype") in _HUMAN_ANSWER_SUBTYPES
    return False


def bare_turn_end_question(quest: Quest) -> str | None:
    """Last unanswered bare-turn-end gate's `last_agent_message`, else None."""
    events = list(quest.event_log().read())
    for i in range(len(events) - 1, -1, -1):
        if events[i].get("type") == "awaiting_user_input":
            gate = events[i]
            following = events[i + 1:]
            break
    else:
        return None
    payload = gate.get("payload") or {}
    if payload.get("interrupt"):
        return None
    if any(_is_gate_answer(e) for e in following):
        return None
    return payload.get("last_agent_message") or None


def make_buffered_solicit(
    first_line: str, inner: Callable[[str, bool], Awaitable[str | SkillRequest]],
) -> Callable[[str, bool], Awaitable[str | SkillRequest]]:
    """Return first_line on the first call; delegate to inner(prompt) on every later call."""
    delivered = False

    async def _solicit(prompt: str, interrupt: bool = False) -> str | SkillRequest:
        nonlocal delivered
        if not delivered:
            delivered = True
            return first_line
        return await inner(prompt, interrupt)

    return _solicit


async def run_inert_resume(
    *,
    question: str,
    ask_line: Callable[[], Awaitable[str | None]],
    launch: Callable[[str], Awaitable[int]],
) -> int:
    """Print the question; loop until a non-blank line (launch it) or None (return 0).

    The question is blank-padded on both sides so it neither fuses with the
    `<id> · resuming` header above nor with the input prompt below."""
    print(f"\n{question}\n")
    while True:
        line = await ask_line()
        if line is None:
            return 0
        if line.strip():
            return await launch(line)
