# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""/compact core: rewrite a step's transcript.jsonl to shed tool-result bodies.

Provider-agnostic core; delegates claude_code native-store cleanup to
ImpSessionStore. Keeps every tool_use call, all assistant/human text, and the
tool_use<->tool_result pairing; replaces only tool-result bodies with a fixed
placeholder and prepends a one-time engine note. Returns the count stubbed.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from imp.agents.runner import ContentBlock, Message, TextBlock, ToolResultBlock
from imp.agents.transcript import Transcript
from imp.providers.claude_code_session_store import session_store_for

RESULT_PLACEHOLDER = "[tool result omitted by compaction]"

# Prepended once, as role="user", at the START of the replayed history so the
# resume classifies as awaiting-user (re-solicit) rather than a trailing user
# turn that would force an agent turn.
ENGINE_NOTE = (
    '[compaction] Earlier tool results were replaced with "[tool result '
    'omitted by compaction]" to reclaim context; the tool calls that produced '
    'them remain. Re-run any tool whose output you still need.'
)


def compact_transcript(transcript_dir: Path) -> int:
    """Shed every not-yet-stubbed tool-result body in the step's transcript.jsonl,
    replacing it with RESULT_PLACEHOLDER; prepend ENGINE_NOTE as the first message
    unless it is already there. Returns the number of tool results newly stubbed.
    tool_use calls, assistant/human text, and tool_use<->tool_result pairing are
    preserved unchanged. Idempotent: a second call re-counts nothing and does not
    stack a duplicate note."""
    transcript = Transcript(transcript_dir / "transcript.jsonl")
    messages = transcript.load()
    already_noted = (
        bool(messages)
        and messages[0].role == "user"
        and messages[0].content == (TextBlock(text=ENGINE_NOTE),))
    stubbed = 0
    rewritten: list[Message] = []
    for msg in messages:
        blocks: list[ContentBlock] = []
        for block in msg.content:
            if isinstance(block, ToolResultBlock) and block.content != RESULT_PLACEHOLDER:
                blocks.append(replace(block, content=RESULT_PLACEHOLDER))
                stubbed += 1
            else:
                blocks.append(block)
        rewritten.append(replace(msg, content=tuple(blocks)))
    if already_noted:
        transcript.rewrite(rewritten)
    else:
        note = Message(role="user", content=(TextBlock(text=ENGINE_NOTE),))
        transcript.rewrite([note, *rewritten])
    # claude_code only: drop the native store so native resume rebuilds it slim (no-op elsewhere).
    session_store_for(transcript_dir).clear()
    return stubbed
