# src/imp/providers/message_eviction.py
"""In-memory cold-cache tool-result eviction for the direct-API providers.

anthropic and openai both re-send their own list[Message] each turn, so they
share this decode/re-encode adapter over the provider-neutral core in
history_compaction. The subprocess providers (claude_code, codex) rewrite their
own resume artifacts instead and do not use this module.

evicted_result_ids decodes a list[Message] into neutral HistoryRecords (turn
index increments per assistant message; a call and its result share that turn)
and returns the set of tool_use_ids whose bodies should be cleared. The provider
computes this set ONCE per invocation and reapplies it via apply_eviction on
every tool-round so the evicted prefix region stays byte-stable.
"""

from __future__ import annotations

from imp.agents.runner import Message, ToolResultBlock, ToolUseBlock
from imp.agents.tools.limits import KEEP_RECENT_TURNS
from imp.providers.history_compaction import HistoryRecord, results_to_evict


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
