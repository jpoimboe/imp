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
    PROMPT_USER = "prompt_user"                # last assistant text+interactive; read user input
    MISSING_COMPLETE_STEP = "missing_complete" # autonomous agent stopped without complete_step
    DANGLING_TOOL_USE = "dangling_tool_use"    # invalid; should not happen given append discipline


def classify_resume(messages: list[Message], *, interactive: bool) -> ResumeState:
    if not messages:
        return ResumeState.FRESH

    last = messages[-1]
    if last.role == "system":
        # Replay artifact — recurse on the previous message.
        return classify_resume(messages[:-1], interactive=interactive)

    if last.role == "user":
        return ResumeState.SEND_TO_LLM

    # assistant
    has_tool_use = any(isinstance(b, ToolUseBlock) for b in last.content)
    if has_tool_use:
        return ResumeState.DANGLING_TOOL_USE
    if interactive:
        return ResumeState.PROMPT_USER
    return ResumeState.MISSING_COMPLETE_STEP


def count_tool_uses(messages: list[Message]) -> int:
    """For the tool-call ceiling guard (spec §6.5)."""
    return sum(
        1
        for m in messages
        if m.role == "assistant"
        for b in m.content
        if isinstance(b, ToolUseBlock)
    )


__all__ = ["ResumeState", "classify_resume", "count_tool_uses"]
