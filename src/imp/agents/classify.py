"""Resume-state classifier (spec §6.8).

Examines the last persisted Message in transcript.jsonl and decides what
the runner should do before entering its main loop.
"""

from __future__ import annotations

from enum import Enum

from imp.agents.runner import Message, ToolUseBlock


class LegacyWaitForUserError(RuntimeError):
    """A transcript still carries a `wait_for_user` tool_use — it predates the
    tool's removal and must be migrated to the bare-turn-end shape before it can
    resume. (The one-shot migration lived at scripts/migrate_wfu_data.py; recover
    it from git history if it has since been removed.)"""


def _assert_no_legacy_wfu(messages: list[Message]) -> None:
    for m in messages:
        if m.role != "assistant":
            continue
        for b in m.content:
            if isinstance(b, ToolUseBlock) and b.name == "wait_for_user":
                raise LegacyWaitForUserError(
                    "transcript predates the wait_for_user removal and must be "
                    "migrated to the bare-turn-end shape before resuming")


class ResumeState(Enum):
    FRESH = "fresh"                            # no history; yield initial_messages
    SEND_TO_LLM = "send_to_llm"                # last message is user; LLM responds next
    AWAITING_USER = "awaiting_user"            # bare turn-end: assistant text, no complete_step
    MISSING_COMPLETE_STEP = "missing_complete" # agent stopped without complete_step; replay+nudge
    DANGLING_TOOL_USE = "dangling_tool_use"    # other dangling tool_use; repaired with placeholder


def classify_resume(messages: list[Message]) -> ResumeState:
    _assert_no_legacy_wfu(messages)
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
    return ResumeState.AWAITING_USER


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


__all__ = ["LegacyWaitForUserError", "ResumeState", "classify_resume", "count_tool_uses"]
