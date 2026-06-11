"""Per-step transcript.jsonl owned by the runner (spec §6.8).

Append-discipline rules (load-bearing for replay correctness):
  1. Only complete, well-formed messages are appended. Partial / interrupted
     assistant messages go to events.jsonl, NOT here.
  2. Every tool_use block in an assistant message gets a matching tool_result
     before the next assistant turn (caller's responsibility).

Rule 2 is best-effort: mid-tool Ctrl+C (spec §6.15) can leave the last
assistant message with a tool_use whose tool_result was never persisted.
For claude_code this is unavoidable — the SDK spawns claude-p in our
process group, so SIGINT kills the subprocess before it can emit the
tool_result back to our receive loop. For anthropic / openai the same
shape occurs when the executor's `should_pause` check fires after we
yielded `tool_result` but before the user message was appended. Each
provider calls `repair_dangling_tool_use_on_resume` after `load()` so the
SEND_TO_LLM resume path has a balanced transcript.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from imp.agents.runner import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    message_from_json,
    message_to_json,
)

if TYPE_CHECKING:
    from imp.engine.state import EventLog

RESUME_SUPERSEDED_NOTE = "previous step output recorded; superseded by the feedback below"


class Transcript:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def exists_and_nonempty(self) -> bool:
        return self._path.exists() and self._path.stat().st_size > 0

    def load(self) -> list[Message]:
        if not self._path.exists():
            return []
        out: list[Message] = []
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(message_from_json(json.loads(line)))
        return out

    def append(self, msg: Message) -> None:
        with self._path.open("a") as f:
            f.write(message_to_json(msg) + "\n")


_INTERRUPTED_BASH_BODY = (
    '{"stdout": "", '
    '"stderr": "interrupted before result captured", '
    '"returncode": -2}'
)


def _synthetic_tool_result_content(tool_name: str) -> str:
    """Per-tool synthetic body for an interrupted tool_use.

    For bash, mimic the real tool's JSON shape so the agent can parse
    `returncode` and reason about the kill (rc=-2 → SIGINT). Other tools
    get a plain explanatory string; the agent need only see *some* result
    to satisfy the API's tool_use ↔ tool_result pairing.
    """
    if tool_name in ("bash", "mcp__imp__bash"):
        return _INTERRUPTED_BASH_BODY
    return f"tool {tool_name!r} interrupted before result was captured"


def repair_dangling_tool_use_on_resume(
    messages: list[Message], transcript: Transcript,
) -> Message | None:
    """Restore the tool_use ↔ tool_result invariant before resuming.

    Inspects the final message: if it's an assistant message containing
    one or more tool_use blocks, synthesize a user message with a
    placeholder tool_result for each one, append it to both `messages`
    (in-place) and `transcript`, and return it. Returns None when no
    repair was needed — the caller can use that as the "transcript was
    already balanced" signal without re-walking the list.

    Caller is responsible for emitting any user-facing event (e.g. a
    `tool_result` event so `imp log` shows the synthetic result) — this
    helper only touches the in-memory and on-disk transcript.
    """
    if not messages or messages[-1].role != "assistant":
        return None
    tool_uses = [
        b for b in messages[-1].content
        if isinstance(b, ToolUseBlock) and b.name != "wait_for_user"
    ]
    if not tool_uses:
        return None
    blocks = tuple(
        ToolResultBlock(
            tool_use_id=tu.id,
            content=_synthetic_tool_result_content(tu.name),
            is_error=True,
        )
        for tu in tool_uses
    )
    repair = Message(role="user", content=blocks)
    messages.append(repair)
    transcript.append(repair)
    return repair


def balance_success_on_resume(
    messages: list[Message], transcript: Transcript, elog: EventLog,
    agent_id: str, wrapper_text: str | None = None,
) -> Message | None:
    """Balance a resumed transcript that ends in a successful complete_step.

    The targeted step's transcript ends with a SUCCESSFUL `complete_step`
    tool_use (no tool_result). Synthesize a NON-error, success-shaped result for
    it (the repair helper's is_error=True 'interrupted' content would tell the
    agent its own accepted work failed). Any *other* trailing tool_use is a real
    interruption and gets error-flavored content. When `wrapper_text` is given,
    it rides on the SAME user message (Anthropic role-alternation). Returns the
    appended Message, or None when there is no trailing tool_use to balance AND
    no wrapper to add (caller then falls back to repair / plain resume).

    This is a property of (resume target, previously completed): it fires on the
    default feedback-true backward goto, a branch-fired goto, and the
    feedback:false handback alike -- independent of whether wrapper_text is set.
    """
    last = messages[-1] if messages else None
    tool_uses = (
        [b for b in last.content
         if isinstance(b, ToolUseBlock) and b.name != "wait_for_user"]
        if last is not None and last.role == "assistant" else []
    )
    result_blocks: list[ToolResultBlock] = []
    for tu in tool_uses:
        if tu.name == "complete_step":
            result_blocks.append(ToolResultBlock(
                tool_use_id=tu.id,
                content=RESUME_SUPERSEDED_NOTE
                        if wrapper_text else "previous step output recorded; resuming",
                is_error=False))
        else:
            result_blocks.append(ToolResultBlock(
                tool_use_id=tu.id,
                content=_synthetic_tool_result_content(tu.name), is_error=True))
    if not result_blocks and not wrapper_text:
        return None
    blocks: list[TextBlock | ToolResultBlock] = list(result_blocks)
    if wrapper_text:
        blocks.append(TextBlock(text=wrapper_text))
    injected = Message(role="user", content=tuple(blocks))
    messages.append(injected)
    transcript.append(injected)
    elog.append_user_message(agent_id=agent_id, message=injected)
    return injected


def resolve_wait_for_user_on_resume(
    messages: list[Message], transcript: Transcript, elog: EventLog,
    agent_id: str, reply: str,
) -> Message | None:
    """Resolve a paused wait_for_user with the REAL human reply (C1).

    The transcript ends with an assistant message whose last tool_use is
    `wait_for_user`. Append a user message with a SUCCESS-shaped tool_result
    (is_error=False) carrying `reply` against that id — the inverse of the
    placeholder repair, which is deliberately skipped for wait_for_user. Returns
    the injected message, or None if there is no dangling wait_for_user.
    """
    last = messages[-1] if messages else None
    if last is None or last.role != "assistant":
        return None
    wfu = next((b for b in last.content
                if isinstance(b, ToolUseBlock) and b.name == "wait_for_user"), None)
    if wfu is None:
        return None
    block = ToolResultBlock(tool_use_id=wfu.id, content=reply, is_error=False)
    injected = Message(role="user", content=(block,))
    messages.append(injected)
    transcript.append(injected)
    elog.append_user_message(agent_id=agent_id, message=injected)
    return injected
