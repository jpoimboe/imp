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
    tool_uses = [b for b in messages[-1].content if isinstance(b, ToolUseBlock)]
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


def inject_retry_feedback(
    messages: list[Message],
    transcript: Transcript,
    elog: "EventLog",
    agent_id: str,
    text: str,
) -> Message:
    """Balance the trailing dangling tool_use and append gate feedback as ONE
    user turn so `classify_resume` returns SEND_TO_LLM (design §5).

    The targeted step's transcript ends with a SUCCESSFUL `complete_step`
    tool_use. For that block synthesize a NON-error, success-shaped result —
    `repair_dangling_tool_use_on_resume`'s `is_error=True` "interrupted" content
    would tell the agent its own success failed. Any other trailing tool_use is
    a genuine interruption and gets the same error-flavored content the repair
    helper uses. The feedback `TextBlock` rides on the SAME user message (not a
    second consecutive user message) to preserve Anthropic role alternation.

    Emits a single `user_message` audit event for the injected turn.
    """
    last = messages[-1] if messages else None
    tool_uses = (
        [b for b in last.content if isinstance(b, ToolUseBlock)]
        if last is not None and last.role == "assistant"
        else []
    )
    result_blocks: list[ToolResultBlock] = []
    for tu in tool_uses:
        if tu.name == "complete_step":
            result_blocks.append(ToolResultBlock(
                tool_use_id=tu.id,
                content="previous step output recorded; superseded by the feedback below",
                is_error=False,
            ))
        else:
            result_blocks.append(ToolResultBlock(
                tool_use_id=tu.id,
                content=_synthetic_tool_result_content(tu.name),
                is_error=True,
            ))
    injected = Message(
        role="user",
        content=(*result_blocks, TextBlock(text=text)),
    )
    messages.append(injected)
    transcript.append(injected)
    elog.append_user_message(agent_id=agent_id, message=injected)
    return injected
