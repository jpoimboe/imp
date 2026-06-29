# src/imp/providers/message_eviction.py
"""In-memory cold-cache tool-result eviction for the direct-API providers.

anthropic and openai both re-send their own list[Message] each turn, so they
share this decode/re-encode adapter over the provider-neutral core in
history_compaction. claude_code also reuses this module's evicted_result_ids
(to compute the set over its full message history) and apply_eviction (for its
streamed delta tail), while rewriting its session-store resume artifact
separately; codex rewrites its rollout artifact and does not use this module.

evicted_result_ids decodes a list[Message] into neutral HistoryRecords (turn
index increments per assistant message; a call and its result share that turn)
and returns the set of tool_use_ids whose bodies should be cleared. The provider
computes this set ONCE per invocation and reapplies it via apply_eviction on
every tool-round so the evicted prefix region stays byte-stable.
"""

from __future__ import annotations

from imp.agents.runner import Message, ToolResultBlock, ToolUseBlock
from imp.agents.tools.limits import KEEP_RECENT_TURNS
from imp.providers.history_compaction import (
    EVICTION_PLACEHOLDER,
    HistoryRecord,
    results_to_evict,
)


def evicted_result_ids(
    messages: list[Message],
    *,
    cold: bool,
    keep_recent_turns: int = KEEP_RECENT_TURNS,
) -> frozenset[str]:
    return results_to_evict(
        _messages_to_records(messages),
        cold=cold,
        keep_recent_turns=keep_recent_turns,
    )


def _messages_to_records(messages: list[Message]) -> list[HistoryRecord]:
    records: list[HistoryRecord] = []
    turn = -1
    for m in messages:
        if m.role == "assistant":
            turn += 1
        for b in m.content:
            if isinstance(b, ToolUseBlock):
                records.append(
                    HistoryRecord(turn=turn, call_id=b.id, call_name=b.name))
            elif isinstance(b, ToolResultBlock):
                records.append(
                    HistoryRecord(turn=turn, result_id=b.tool_use_id,
                                  result_body=b.content))
    return records


def apply_eviction(
    messages: list[Message], to_evict: frozenset[str],
) -> list[Message]:
    if not to_evict:
        return messages
    return [_evict_message(m, to_evict) for m in messages]


def _evict_message(m: Message, to_evict: frozenset[str]) -> Message:
    if not any(
        isinstance(b, ToolResultBlock) and b.tool_use_id in to_evict
        for b in m.content
    ):
        return m
    new_content = tuple(
        ToolResultBlock(
            tool_use_id=b.tool_use_id,
            content=EVICTION_PLACEHOLDER,
            is_error=b.is_error,
        )
        if isinstance(b, ToolResultBlock) and b.tool_use_id in to_evict
        else b
        for b in m.content
    )
    return Message(role=m.role, content=new_content)
