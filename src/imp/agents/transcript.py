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

from imp.agents.classify import (
    ResumeState,
    classify_resume,
)
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

RESUME_CONTINUE_NUDGE = (
    "You stopped before calling complete_step. Continue exactly where you left "
    "off and finish the step — call complete_step when the work is done."
)

# Injected once onto every synthesized resume turn. On resume the transcript is
# replayed to the provider, and reconstructed tool_results can surface to the
# model as errors ("internal error") even when the original action succeeded —
# observed under claude_code's stream-json replay, and a genuine shape under the
# is_error placeholders this module writes for interrupted/superseded tool_uses.
# The note keeps the model from redoing durable work it can simply verify.
RESUME_DURABILITY_NOTE = (
    "[resume] The tool results above were reconstructed from before this resume "
    "and may display as errors even when the original action succeeded. Any files "
    "you have already written and commits you have already made are durable on "
    "disk — do not blindly redo them. Before re-running any write, edit, or "
    "commit, verify the current state with a read-only check (read the file, or "
    "run git log / git status)."
)

RESUME_MANIFEST_HEADER = (
    "[resume] This step is resuming. The tool results above were reconstructed "
    "from before the resume and may display as errors — that is a replay "
    "artifact, not a real failure. Every tool call listed below SUCCEEDED; do "
    "not redo this grounded work. Any files written and commits made are durable "
    "on disk."
)

_MANIFEST_WRITE_TOOLS = {"write_file", "edit_file"}


def build_resume_manifest(
    messages: list[Message], *,
    exclude_tool_use_ids: frozenset[str] = frozenset(),
) -> str | None:
    """Summarize prior tool activity for a stream-json fallback resume.

    Names each prior tool call as having SUCCEEDED and frames the (possibly
    error-shaped) replayed tool_results as a replay artifact, so the model does
    not discard grounded work. For durable write calls echoes the path and the
    content/replacement that reached disk. Tool_use ids in
    `exclude_tool_use_ids` are skipped — used for a genuinely-dangling last
    tool_use (killed between call and result) that must NOT be claimed
    succeeded. Returns None when the history holds no non-control tool calls to
    summarize.
    """
    lines: list[str] = []
    for m in messages:
        if m.role != "assistant":
            continue
        for b in m.content:
            if not isinstance(b, ToolUseBlock):
                continue
            if b.id in exclude_tool_use_ids:
                continue
            name = b.name[len("mcp__imp__"):] \
                if b.name.startswith("mcp__imp__") else b.name
            if name == "complete_step":
                continue
            line = f"- {name} — succeeded"
            if name in _MANIFEST_WRITE_TOOLS:
                path = b.input.get("path")
                body = b.input.get("content", b.input.get("new"))
                if path is not None:
                    line += f" (wrote {path})"
                if body is not None:
                    line += f"\n{body}"
            lines.append(line)
    if not lines:
        return None
    return RESUME_MANIFEST_HEADER + "\n\n" + "\n".join(lines)


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

    def rewrite(self, messages: list[Message]) -> None:
        """Overwrite the transcript with `messages` — repairs an existing turn in place."""
        with self._path.open("w") as f:
            for m in messages:
                f.write(message_to_json(m) + "\n")


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
) -> tuple[ToolResultBlock, ...] | None:
    """Restore the tool_use ↔ tool_result invariant before resuming.

    Two shapes are repaired:
      1. The last message is an ASSISTANT turn with one or more unmatched tool_use
         blocks (the mid-tool interrupt left no following user turn): synthesize a
         NEW trailing user message with a placeholder tool_result for each plus the
         durability note, append it to `messages` (in place) and `transcript`.
      2. The last message is a USER turn that immediately follows an assistant turn
         carrying unmatched tool_use blocks — e.g. a claude_code
         '[Request interrupted by user]' message persisted after the tool_use.
         Anthropic requires the tool_result to live in the user turn directly after
         the tool_use, so inject the placeholder result(s) for the UNMATCHED
         tool_uses INTO that trailing user turn (in place) and rewrite the
         transcript.

    Returns the tuple of ToolResultBlocks this call SYNTHESIZED — shape-1: every
    result block it created; shape-2: only the `unmatched`-derived blocks, never a
    pre-existing matched tool_result already in the trailing user turn. Returns
    None when no repair is needed (already balanced); the returned tuple is never
    empty, so `is not None` is still the 'transcript was already balanced' signal.
    The Message is mutated in place in `messages` and `transcript`; callers iterate
    the returned tuple to emit one tool_result event per synthesized block.
    """
    if not messages:
        return None
    last = messages[-1]
    if last.role == "assistant":
        tool_uses = [
            b for b in last.content
            if isinstance(b, ToolUseBlock)
        ]
        if not tool_uses:
            return None
        result_blocks = tuple(
            ToolResultBlock(
                tool_use_id=tu.id,
                content=_synthetic_tool_result_content(tu.name),
                is_error=True,
            )
            for tu in tool_uses
        )
        repair = Message(
            role="user",
            content=(*result_blocks, TextBlock(text=RESUME_DURABILITY_NOTE)),
        )
        messages.append(repair)
        transcript.append(repair)
        return result_blocks
    if last.role == "user" and len(messages) >= 2 and messages[-2].role == "assistant":
        tool_uses = [
            b for b in messages[-2].content
            if isinstance(b, ToolUseBlock)
        ]
        if not tool_uses:
            return None
        matched = {
            b.tool_use_id for b in last.content if isinstance(b, ToolResultBlock)
        }
        unmatched = [tu for tu in tool_uses if tu.id not in matched]
        if not unmatched:
            return None
        result_blocks = tuple(
            ToolResultBlock(
                tool_use_id=tu.id,
                content=_synthetic_tool_result_content(tu.name),
                is_error=True,
            )
            for tu in unmatched
        )
        # Anthropic requires tool_result blocks at the head of the user turn; the
        # original interrupt text follows, and the durability note frames the
        # (error-shaped) reconstructed results last.
        repaired = Message(
            role="user",
            content=(
                *result_blocks,
                *last.content,
                TextBlock(text=RESUME_DURABILITY_NOTE),
            ),
        )
        messages[-1] = repaired
        transcript.rewrite(messages)
        return result_blocks
    return None


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
         if isinstance(b, ToolUseBlock)]
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
    # Resume-durability note frames the (possibly error-shaped) results above;
    # the actionable wrapper feedback, if any, stays last so it reads as the
    # most recent instruction.
    blocks.append(TextBlock(text=RESUME_DURABILITY_NOTE))
    if wrapper_text:
        blocks.append(TextBlock(text=wrapper_text))
    injected = Message(role="user", content=tuple(blocks))
    messages.append(injected)
    transcript.append(injected)
    elog.append_user_message(agent_id=agent_id, message=injected)
    return injected


def deliver_reply_on_resume(
    messages: list[Message], transcript: Transcript, elog: EventLog,
    agent_id: str, reply: str,
) -> Message:
    """Deliver a human reply (or drained interject) as the next user message.

    A bare-turn-end pause leaves the transcript ending on assistant text with no
    tool_use — nothing to repair, the reply is simply the next user turn. Append
    it to `messages`, persist it, and journal a user_message event; the now
    user-terminated transcript drives the next LLM turn.
    """
    injected = Message(role="user", content=(TextBlock(text=reply),))
    messages.append(injected)
    transcript.append(injected)
    elog.append_user_message(agent_id=agent_id, message=injected)
    return injected



