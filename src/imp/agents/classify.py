"""Resume-state classifier (spec §6.8).

Examines the last persisted Message in transcript.jsonl and decides what
the runner should do before entering its main loop.
"""

from __future__ import annotations

from enum import Enum

from imp.agents.runner import (
    Message,
    ToolResultBlock,
    ToolUseBlock,
    canonicalize_tool_name,
)


class ResumeState(Enum):
    FRESH = "fresh"                            # no history; yield initial_messages
    SEND_TO_LLM = "send_to_llm"                # last message is user; LLM responds next
    AWAITING_USER = "awaiting_user"            # bare turn-end: assistant text, no complete_step
    MISSING_COMPLETE_STEP = "missing_complete" # agent stopped without complete_step; replay+nudge
    DANGLING_TOOL_USE = "dangling_tool_use"    # other dangling tool_use; repaired with placeholder
    STEP_COMPLETED = "step_completed"          # resolved complete_step masked by trailing prose


def classify_resume(messages: list[Message]) -> ResumeState:
    if not messages:
        return ResumeState.FRESH

    last = messages[-1]
    if last.role == "system":
        # Replay artifact — recurse on the previous message.
        return classify_resume(messages[:-1])

    if last.role == "user":
        return ResumeState.SEND_TO_LLM

    # assistant: a dangling tool_use needs repair; a bare turn-end (assistant
    # text, no complete_step) IS the yield to the human -> AWAITING_USER.
    tool_uses = [b for b in last.content if isinstance(b, ToolUseBlock)]
    if tool_uses:
        return ResumeState.DANGLING_TOOL_USE
    if _trailing_prose_over_resolved_complete_step(messages):
        return ResumeState.STEP_COMPLETED
    return ResumeState.AWAITING_USER


def _trailing_prose_over_resolved_complete_step(messages: list[Message]) -> bool:
    """True when the tail is one-or-more trailing assistant text-only turns (and
    system replay artifacts) sitting directly on a `complete_step` tool_use whose
    tool_result immediately follows it. Bounded to the trailing run: the resolved
    complete_step must be immediately behind the prose, never buried behind other
    tool activity."""
    i = len(messages) - 1
    while i >= 0 and (
        messages[i].role == "system"
        or (
            messages[i].role == "assistant"
            and not any(isinstance(b, ToolUseBlock) for b in messages[i].content)
        )
    ):
        i -= 1
    if i <= 0 or messages[i].role != "user":
        return False
    result_ids = {
        b.tool_use_id for b in messages[i].content if isinstance(b, ToolResultBlock)
    }
    if not result_ids:
        return False
    prev = messages[i - 1]
    return prev.role == "assistant" and any(
        isinstance(b, ToolUseBlock)
        and canonicalize_tool_name(b.name) == "complete_step"
        and b.id in result_ids
        for b in prev.content
    )


def _contains_tool_result(msg: Message) -> bool:
    return any(isinstance(b, ToolResultBlock) for b in msg.content)


def count_tool_uses(messages: list[Message]) -> int:
    """Count tool_use blocks across all assistant messages in a transcript.

    Formerly seeded the per-resume tool-call ceiling; the ceiling is now opt-in
    and per-invocation, so no provider calls this internally. Retained as a
    utility (exported via ``imp.agents``) for diagnostic/external use.
    """
    return sum(
        1
        for m in messages
        if m.role == "assistant"
        for b in m.content
        if isinstance(b, ToolUseBlock)
    )


__all__ = ["ResumeState", "classify_resume", "count_tool_uses"]
