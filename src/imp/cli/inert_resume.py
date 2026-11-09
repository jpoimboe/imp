# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Inert resume of a quest parked at a bare-turn-end yield (spec AC8) — cheap local reads only."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from imp.cli.exit_codes import ExitCode
from imp.engine.quest import Quest
from imp.engine.resume_gate import pending_question
from imp.remote.session.protocol import SkillRequest


def render_inert_question(text: str, *, width: int, color: bool) -> str:
    """Render the pending bare-turn-end question for the inert resume preview
    through `_render_markdown_block` — the same renderer every other agent turn
    uses — so the preview shows the bold `• ` turn bullet and a 2-column
    hang-indent, and reads as prose (markdown→ANSI) rather than raw markdown
    source. Called at depth 0, so the gutter is empty. The text is agent-authored,
    so it is prose-escaped here, exactly as the scrollback escapes an agent turn."""
    from imp.cli.render_text import _caret_escape_prose, _render_markdown_block
    return "\n".join(_render_markdown_block(_caret_escape_prose(text), width, 0, color))


def bare_turn_end_question(quest: Quest) -> str | None:
    """Last unanswered bare-turn-end gate's `last_agent_message`, else None."""
    return pending_question(list(quest.event_log().read()))


def make_buffered_solicit(
    first_line: str, inner: Callable[[str, bool], Awaitable[str | SkillRequest]],
) -> Callable[[str, bool], Awaitable[str | SkillRequest]]:
    """Return first_line at the first gate when it is a bare turn-end yield; an interrupt
    gate spends the latch without delivering, so the line can never answer a later gate."""
    spent = False

    async def _solicit(prompt: str, interrupt: bool = False) -> str | SkillRequest:
        nonlocal spent
        if not spent:
            spent = True
            if not interrupt:
                return first_line
        return await inner(prompt, interrupt)

    return _solicit


async def run_inert_resume(
    *,
    question: str,
    ask_line: Callable[[], Awaitable[str | None]],
    launch: Callable[[str], Awaitable[int]],
) -> int:
    """Print the question; loop until a non-blank line (launch it) or None (the
    user left: USER_ENDED).

    The question is blank-padded on both sides so it neither fuses with the
    `<id> · resuming` header above nor with the input prompt below."""
    print(f"\n{question}\n")
    while True:
        line = await ask_line()
        if line is None:
            return ExitCode.USER_ENDED
        if line.strip():
            return await launch(line)
