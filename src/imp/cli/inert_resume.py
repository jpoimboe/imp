"""Inert resume of a change parked at a bare-turn-end yield (spec AC8) — cheap local reads only."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from imp.engine.quest import Quest


def render_inert_question(text: str, *, width: int, color: bool) -> str:
    """Render the pending bare-turn-end question for the inert resume preview
    through `_render_markdown_block` — the same renderer every other agent turn
    uses — so the preview shows the bold `• ` turn bullet and a 2-column
    hang-indent, and reads as prose (markdown→ANSI) rather than raw markdown
    source. Called at depth 0, so the gutter is empty."""
    from imp.cli.render_text import _render_markdown_block
    return "\n".join(_render_markdown_block(text, width, 0, color))


_ANSWER_MARKERS = frozenset({"user_message", "step_started", "step_completed"})


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
    if any(e.get("type") in _ANSWER_MARKERS for e in following):
        return None
    return payload.get("last_agent_message") or None


def make_buffered_solicit(
    first_line: str, inner: Callable[[str], Awaitable[str]],
) -> Callable[[str], Awaitable[str]]:
    """Return first_line on the first call; delegate to inner(prompt) on every later call."""
    delivered = False

    async def _solicit(prompt: str) -> str:
        nonlocal delivered
        if not delivered:
            delivered = True
            return first_line
        return await inner(prompt)

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
