"""Inert resume of a change parked at a bare-turn-end yield (spec AC8) — cheap local reads only."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import Message, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.quest import Quest
from imp.engine.state import State


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


def render_inert_question(text: str, *, width: int, color: bool) -> str:
    """Render the pending bare-turn-end question for the inert resume preview
    through `_render_markdown_block` — the same renderer every other agent turn
    uses — so the preview shows the bold `• ` turn bullet and a 2-column
    hang-indent, and reads as prose (markdown→ANSI) rather than raw markdown
    source. Called at depth 0, so the gutter is empty."""
    from imp.cli.render_text import _render_markdown_block
    return "\n".join(_render_markdown_block(text, width, 0, color))


def bare_turn_end_question(change: Quest) -> str | None:
    """Pending question if `change` is paused at a bare-turn-end yield, else None."""
    state = State.load(change.state_dir)
    if state.status != "paused" or state.current_step is None:
        return None
    messages = Transcript(
        state.current_step.transcript_dir / "transcript.jsonl").load()
    if classify_resume(messages) is not ResumeState.AWAITING_USER:
        return None
    return render_pending_question(messages)


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
