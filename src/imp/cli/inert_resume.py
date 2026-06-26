"""Inert resume of a change parked at a bare-turn-end yield (spec AC8) — cheap local reads only."""

from __future__ import annotations

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import Message, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.change import Change
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


def bare_turn_end_question(change: Change) -> str | None:
    """Pending question if `change` is paused at a bare-turn-end yield, else None."""
    state = State.load(change.state_dir)
    if state.status != "paused" or state.current_step is None:
        return None
    messages = Transcript(
        state.current_step.transcript_dir / "transcript.jsonl").load()
    if classify_resume(messages) is not ResumeState.AWAITING_USER:
        return None
    return render_pending_question(messages)
