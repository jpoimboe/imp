"""Resume-state classifier (spec §6.8).

Examines the last persisted Message in transcript.jsonl and decides what
the runner should do before entering its main loop.
"""

from __future__ import annotations

from enum import Enum

from imp.agents.runner import Message, ToolUseBlock


class ResumeState(Enum):
    FRESH = "fresh"                            # no history; yield initial_messages
    SEND_TO_LLM = "send_to_llm"                # last message is user; LLM responds next
    AWAITING_USER = "awaiting_user"            # last tool_use is wait_for_user; solicit + fill
    MISSING_COMPLETE_STEP = "missing_complete" # agent stopped without complete_step; replay+nudge
    DANGLING_TOOL_USE = "dangling_tool_use"    # other dangling tool_use; repaired with placeholder


def classify_resume(messages: list[Message]) -> ResumeState:
    if not messages:
        return ResumeState.FRESH

    last = messages[-1]
    if last.role == "system":
        # Replay artifact — recurse on the previous message.
        return classify_resume(messages[:-1])

    if last.role == "user":
        return ResumeState.SEND_TO_LLM

    # assistant
    tool_uses = [b for b in last.content if isinstance(b, ToolUseBlock)]
    if tool_uses:
        if any(b.name == "wait_for_user" for b in tool_uses):
            return ResumeState.AWAITING_USER
        return ResumeState.DANGLING_TOOL_USE
    return ResumeState.MISSING_COMPLETE_STEP


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
