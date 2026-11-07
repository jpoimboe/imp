# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Per-step transcript.jsonl owned by the runner (spec §6.8).

Append-discipline rules (load-bearing for replay correctness):
  1. Only complete, well-formed messages are appended. Partial / interrupted
     assistant messages go to events.jsonl, NOT here.
  2. Every tool_use block in an assistant message gets a matching tool_result
     before the next assistant turn (caller's responsibility).

Rule 2 is best-effort: mid-tool Ctrl+C (spec §6.15) can leave the last
assistant message with a tool_use whose tool_result was never persisted.
For claude this is unavoidable — the SDK spawns claude-p in our
process group, so SIGINT kills the subprocess before it can emit the
tool_result back to our receive loop. For anthropic / openai the same
shape occurs when the executor's `should_pause` check fires after we
yielded `tool_result` but before the user message was appended. On resume,
claude calls `answer_unanswered_tool_uses_on_resume` and the push providers call
`repair_dangling_tool_use_on_resume`, so the resumed transcript is balanced.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from imp.agents.runner import (
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    canonicalize_tool_name,
    message_from_json,
    message_to_json,
)
from imp.agents.tool_killed import emit_tool_killed

if TYPE_CHECKING:
    from imp.engine.state import EventLog

# Rework-resume frame for is_error complete_step result; SINGLE unbroken line (prompt-embedded).
REWORK_RESULT_FRAME = (
    "[revision required] Your previous complete_step output was recorded, then "
    "sent back for revision — this is NOT a system failure. Address the feedback "
    "below and re-run the step; call complete_step again when the work is done."
)

# Matched by equality on resume. SINGLE unbroken line (prompt-embedded).
COMPLETE_STEP_BATCHED = (
    "complete_step was not run: it must be called on its own, after you have read "
    "the results of your other tool calls. Check those results, then call "
    "complete_step again."
)

RESUME_CONTINUE_NUDGE = (
    "The run was paused and is now resuming — this is not a "
    "rejection and nothing failed. Continue the step from where you left off "
    "and call complete_step when it is done. Do not ask the user how to "
    "proceed unless you genuinely need input you do not already have."
)

RESUME_RETRY_NUDGE = (
    "The previous provider call failed and the engine is retrying it — this is "
    "not a rejection and nothing you did failed. Anything you already said above "
    "has reached the user's screen; continue from there rather than repeating it. "
    "Continue the step from where you left off and call complete_step when it is "
    "done. Do not ask the user how to proceed unless you genuinely need input you "
    "do not already have."
)

RESUME_DEPENDENCY_NUDGE = (
    "The quest this step was waiting on has finished, so the run is resuming — "
    "nothing failed. Retry the action that was waiting and continue the step from "
    "there, then call `complete_step` when it is done. Do not ask the user how to "
    "proceed unless you genuinely need input you do not already have."
)

# Injected once onto every synthesized resume turn. On resume the transcript is
# replayed to the provider, and reconstructed tool_results can surface to the
# model as errors ("internal error") even when the original action succeeded —
# observed under claude's stream-json replay, and a genuine shape under the
# is_error placeholders this module writes for interrupted/superseded tool_uses.
# The note keeps the model from redoing durable work it can simply verify.
RESUME_DURABILITY_NOTE = (
    "[resume] The run was paused and is now resuming. Any tool "
    "results above shown as errors were cut off by that pause — they were not "
    "rejected or failed. The tool results above were reconstructed from before "
    "this resume and may display as errors even when the original action "
    "succeeded. Any files you have already written and commits you have already "
    "made are durable on disk — do not blindly redo them. Before re-running any "
    "write, edit, or commit, verify the current state with a read-only check "
    "(read the file, or run git log / git status)."
)

RESUME_REDO_NUDGE = (
    "This step is resuming. Your previous output was recorded and is now "
    "superseded; re-run the step from the current state and call complete_step "
    "when the work is done."
)

# Stream-only: the CLI splices a "No response requested." turn after a user-tail body at load.
RESUME_STUB_NOTE = (
    '[imp] The "No response requested." turn just above was inserted by the '
    "Claude Code CLI when it reloaded this session. You did not write it and "
    "nothing went wrong, so don't apologise for it or mention it. Carry on from "
    "the turn before it."
)


@dataclass(frozen=True, slots=True)
class SkillReply:
    """A daemon-resolved human skill load: the command line the human typed, the
    skill name, and the already-loaded body (body + trailing args). Distinct from
    the wire frame SkillRequest — this carries the LOADED body back through the
    continuation contract to the delivery helper."""
    command: str
    name: str
    body: str


@dataclass(frozen=True, slots=True)
class ContinuationNote:
    """A framework-authored line injected into a parked solicit. Distinct from a
    plain str so the delivery helper can journal it as an engine_message rather
    than putting words in the human's mouth."""
    text: str


SolicitReply = str | SkillReply | ContinuationNote


class Transcript:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def exists_and_nonempty(self) -> bool:
        return self._path.exists() and self._path.stat().st_size > 0

    def load(self, blobs_dir: Path | None = None) -> list[Message]:
        if not self._path.exists():
            return []
        out: list[Message] = []
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(message_from_json(json.loads(line), blobs_dir))
        return out

    def append(self, msg: Message) -> None:
        with self._path.open("a") as f:
            f.write(message_to_json(msg) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def rewrite(self, messages: list[Message]) -> None:
        """Overwrite the transcript with `messages` — repairs an existing turn in place."""
        with self._path.open("w") as f:
            for m in messages:
                f.write(message_to_json(m) + "\n")


_INTERRUPTED_BASH_BODY = (
    '{"stdout": "", '
    '"stderr": "interrupted by a pause before result captured — not a '
    'failure; re-run if needed", '
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
    return (
        "[interrupted] The run was paused before this call's result was "
        "captured — this was NOT a rejection, denial, or failure. On resume, "
        "re-run or re-dispatch this call if its work is still needed, then "
        "continue the step."
    )


_UNRESOLVED_SENTENCE = (
    "imp interrupted this turn; this call was still running and its outcome was "
    "not recorded. Verify the current state before relying on it."
)

_UNRESOLVED_BASH_BODY = (
    '{"stdout": "", '
    f'"stderr": "{_UNRESOLVED_SENTENCE}", '
    '"returncode": null}'
)


def unresolved_tool_result_content(tool_name: str) -> str:
    """Body for a call still running when the bounded post-interrupt wait expired.

    Distinct from _synthetic_tool_result_content, whose "re-run or re-dispatch this
    call" advice is unsafe for a call that may still be executing; and `returncode`
    is null rather than -2, since no kill has been established here.
    """
    if tool_name in ("bash", "mcp__imp__bash"):
        return _UNRESOLVED_BASH_BODY
    return _UNRESOLVED_SENTENCE


def captured_result_key(name: str, tool_input: dict[str, Any]) -> str:
    """Reconciliation key matching a captured result to a dangling tool_use by name +
    canonical input (no usable execution-time id exists for in-process MCP tools); the
    mcp__imp__ prefix is stripped and input sorted-key canonicalized so a bare-name
    capture matches a prefixed transcript block regardless of field order.
    """
    bare = (name[len("mcp__imp__"):]
            if name.startswith("mcp__imp__") else name)
    return bare + "\x00" + json.dumps(tool_input, sort_keys=True, default=str)


def repair_dangling_tool_use_on_resume(
    messages: list[Message], transcript: Transcript,
    captured: dict[str, tuple[str, bool]] | None = None,
) -> tuple[ToolResultBlock, ...] | None:
    """Restore the tool_use ↔ tool_result invariant before resuming.

    Two shapes are repaired:
      1. The last message is an ASSISTANT turn with one or more unmatched tool_use
         blocks (the mid-tool interrupt left no following user turn): synthesize a
         NEW trailing user message with a placeholder tool_result for each plus the
         durability note, append it to `messages` (in place) and `transcript`.
      2. The last message is a USER turn that immediately follows an assistant turn
         carrying unmatched tool_use blocks — e.g. a claude
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
    def _all_captured_success(tus: list[ToolUseBlock]) -> bool:
        if captured is None:
            return False
        for tu in tus:
            key = captured_result_key(tu.name, tu.input)
            if key not in captured or captured[key][1]:
                return False
        return True

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
        result_blocks = tuple(_resume_result(tu, captured) for tu in tool_uses)
        note = ((TextBlock(text=RESUME_DURABILITY_NOTE),)
                if not _all_captured_success(tool_uses) else ())
        repair = Message(role="user", content=(*result_blocks, *note))
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
        result_blocks = tuple(_resume_result(tu, captured) for tu in unmatched)
        # Anthropic requires tool_result blocks at the head of the user turn; the
        # original interrupt text follows, and the durability note frames the
        # (error-shaped) reconstructed results last.
        note = ((TextBlock(text=RESUME_DURABILITY_NOTE),)
                if not _all_captured_success(unmatched) else ())
        repaired = Message(
            role="user",
            content=(*result_blocks, *last.content, *note),
        )
        messages[-1] = repaired
        transcript.rewrite(messages)
        return result_blocks
    return None


# claude-p's own interrupt marker, matching the pattern the SDK writes
# (claude_agent_sdk/_internal/sessions.py). The rejection body it pairs with is
# CLI-version-fragile and is deliberately never matched.
_INTERRUPT_MARKER_RE = re.compile(r"^\[Request interrupted by user[^\]]*\]$")


def _is_interrupt_marker(msg: Message) -> bool:
    if msg.role != "user" or len(msg.content) != 1:
        return False
    block = msg.content[0]
    return (isinstance(block, TextBlock)
            and _INTERRUPT_MARKER_RE.match(block.text.strip()) is not None)


def _trailing_user_start(messages: list[Message]) -> int:
    i = len(messages)
    while i > 0 and messages[i - 1].role == "user":
        i -= 1
    return i


def _has_durability_note(msg: Message) -> bool:
    return any(isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE
               for b in msg.content)


def unanswered_tool_uses(messages: list[Message]) -> list[ToolUseBlock]:
    """Every tool_use in `messages` whose id has no tool_result anywhere in `messages`."""
    answered = {b.tool_use_id for m in messages for b in m.content
                if isinstance(b, ToolResultBlock)}
    return [b for m in messages if m.role == "assistant" for b in m.content
            if isinstance(b, ToolUseBlock) and b.id not in answered]


def complete_step_tail(messages: list[Message]) -> ToolUseBlock | None:
    """The complete_step tool_use in the last assistant turn when only user turns
    follow it: dangling, under its stored "step complete" result, or already
    answered by an earlier resume. A rejected call recorded no output, so it is no
    tail. Read after the marker repair has dropped claude's interrupt marker."""
    start = _trailing_user_start(messages)
    if start == 0 or messages[start - 1].role != "assistant":
        return None
    cs = next((b for b in messages[start - 1].content
               if isinstance(b, ToolUseBlock)
               and canonicalize_tool_name(b.name) == "complete_step"), None)
    if cs is not None and any(
            isinstance(b, ToolResultBlock) and b.tool_use_id == cs.id
            and b.content == COMPLETE_STEP_BATCHED
            for m in messages[start:] for b in m.content):
        return None
    return cs


def _resume_result(
    tu: ToolUseBlock, captured: dict[str, tuple[str, bool]] | None,
) -> ToolResultBlock:
    hit = (captured.get(captured_result_key(tu.name, tu.input))
           if captured is not None else None)
    if hit is not None:
        return ToolResultBlock(tool_use_id=tu.id, content=hit[0], is_error=hit[1])
    return ToolResultBlock(tool_use_id=tu.id,
                           content=_synthetic_tool_result_content(tu.name),
                           is_error=True)


def repair_interrupt_marker_on_resume(
    messages: list[Message], transcript: Transcript,
    captured: dict[str, tuple[str, bool]] | None = None,
) -> None:
    """Strip claude-p's interrupt framing before resume classification.

    On an interrupt claude-p writes its own turns, one Message per block: an is_error
    "rejected, STOP" tool_result per outstanding call, then a text-only
    '[Request interrupted by user …]' marker, possibly followed by real user text.
    The marker makes the tail look like a human turn and the rejection copy is the
    model's only framing.

    Within the trailing user run, find the last marker; R is the run's non-marker
    turns before it. Unless R holds no tool_result and every tool_use is answered
    (a lone marker over prose), drop every marker up to the last and restate each
    is_error result in R as an interruption — imp's captured result when the call
    really completed, else the synthetic body; tool_uses resolve by id anywhere.
    complete_step results are left for the complete_step-tail answer. Turns after
    the last marker are untouched. Journals nothing: the pause already recorded
    these results in the event log, so a second entry would falsify it.

    The durability note is appended to R's last turn only when some result was
    restated as a placeholder or a captured error and R does not already hold it.
    """
    start = _trailing_user_start(messages)
    markers = [i for i in range(start, len(messages))
               if _is_interrupt_marker(messages[i])]
    if not markers:
        return
    last_marker = markers[-1]
    run = [m for m in messages[start:last_marker] if not _is_interrupt_marker(m)]
    if not any(isinstance(b, ToolResultBlock) for m in run for b in m.content) \
            and not unanswered_tool_uses(messages):
        return
    tool_uses = {b.id: b for m in messages if m.role == "assistant"
                 for b in m.content if isinstance(b, ToolUseBlock)}
    cut_off = False
    repaired: list[Message] = []
    for m in run:
        blocks: list[ContentBlock] = []
        for b in m.content:
            tu = (tool_uses.get(b.tool_use_id)
                  if isinstance(b, ToolResultBlock) and b.is_error else None)
            # A real failure's text is lost — only successes are captured; a re-run resurfaces it.
            if tu is not None and canonicalize_tool_name(tu.name) != "complete_step":
                b = _resume_result(tu, captured)
                cut_off = cut_off or b.is_error
            blocks.append(b)
        repaired.append(Message(role="user", content=tuple(blocks)))
    if cut_off and not any(_has_durability_note(m) for m in repaired):
        repaired[-1] = Message(role="user", content=(
            *repaired[-1].content, TextBlock(text=RESUME_DURABILITY_NOTE)))
    messages[start:last_marker + 1] = repaired
    transcript.rewrite(messages)


def answer_unanswered_tool_uses_on_resume(
    messages: list[Message], transcript: Transcript, elog: EventLog,
    agent_id: str, captured: dict[str, tuple[str, bool]] | None = None,
) -> None:
    """Give every unanswered tool_use a result before a claude resume.

    claude transcripts hold one Message per content block, so parallel and
    interleaved calls leave unanswered tool_uses in any assistant run, not just the
    last. Each gets imp's captured result, else the synthetic placeholder. With no
    trailing user run the results form one new user turn; otherwise they head the
    run's first turn, ahead of its original content — `_project_for_cli` regroups
    each behind its tool_use by id. The durability note is appended to that turn
    unless every result is a captured success or the run already holds it. Each
    result is journaled as a tool_result event.
    """
    unanswered = unanswered_tool_uses(messages)
    if not unanswered:
        return
    results = tuple(_resume_result(tu, captured) for tu in unanswered)
    start = _trailing_user_start(messages)
    note = ((TextBlock(text=RESUME_DURABILITY_NOTE),)
            if any(r.is_error for r in results)
            and not any(_has_durability_note(m) for m in messages[start:]) else ())
    if start == len(messages):
        repair = Message(role="user", content=(*results, *note))
        messages.append(repair)
        transcript.append(repair)
    else:
        messages[start] = Message(
            role="user", content=(*results, *messages[start].content, *note))
        transcript.rewrite(messages)
    for tu, rb in zip(unanswered, results, strict=True):
        elog.append_tool_result(agent_id=agent_id, block=rb)
        emit_tool_killed(elog, agent_id, tu.name, rb)


def answer_complete_step_on_resume(
    messages: list[Message], transcript: Transcript, elog: EventLog,
    agent_id: str, cs: ToolUseBlock, feedback: str | None,
) -> None:
    """Answer a complete_step tail on a claude rework or backward-goto resume.

    Rework (`feedback`): cs's result becomes the framed feedback, is_error. Goto: an
    existing success or framed-feedback result is kept (a paused rework resumed
    plainly), else cs gets a success result. A changed result replaces the old one
    in place, else heads the trailing user run, else forms a new user turn, and is
    journaled as an engine continuation, never a tool_result event. No durability
    note. Then RESUME_REDO_NUDGE trails, unless it already does.
    """
    start = _trailing_user_start(messages)
    hit = next(((i, j, b) for i in range(start, len(messages))
                for j, b in enumerate(messages[i].content)
                if isinstance(b, ToolResultBlock) and b.tool_use_id == cs.id), None)
    old = hit[2] if hit is not None else None
    if feedback:
        new = ToolResultBlock(tool_use_id=cs.id, is_error=True,
                              content=REWORK_RESULT_FRAME + "\n\n" + feedback)
    elif old is not None and (not old.is_error or (
            isinstance(old.content, str)
            and old.content.startswith(REWORK_RESULT_FRAME))):
        new = old
    else:
        new = ToolResultBlock(tool_use_id=cs.id, is_error=False,
                              content="previous step output recorded; resuming")
    if new != old:
        if hit is not None:
            i, j, _ = hit
            content = list(messages[i].content)
            content[j] = new
            messages[i] = Message(role="user", content=tuple(content))
            transcript.rewrite(messages)
        elif start < len(messages):
            messages[start] = Message(
                role="user", content=(new, *messages[start].content))
            transcript.rewrite(messages)
        else:
            turn = Message(role="user", content=(new,))
            messages.append(turn)
            transcript.append(turn)
        elog.append_engine_message(
            agent_id=agent_id, message=Message(role="user", content=(new,)),
            subtype="continuation")
    if messages[-1] != Message(role="user", content=(TextBlock(text=RESUME_REDO_NUDGE),)):
        deliver_engine_continuation(
            messages, transcript, elog, agent_id, RESUME_REDO_NUDGE)


def balance_success_on_resume(
    messages: list[Message], transcript: Transcript, elog: EventLog,
    agent_id: str, wrapper_text: str | None = None,
) -> Message | None:
    """Balance a resumed transcript that ends in a successful complete_step.

    The targeted step's transcript ends with a SUCCESSFUL `complete_step`
    tool_use (no tool_result). Synthesize a NON-error, success-shaped result for
    it (the repair helper's is_error=True 'interrupted' content would tell the
    agent its own accepted work failed). Any *other* trailing tool_use is a real
    interruption and gets error-flavored content. When `wrapper_text` (rework
    feedback) is given, it becomes the `is_error=True` content of the complete_step
    result itself -- framed as a revision request -- so the model reads its own
    completion sent back, rather than a success result plus a separate feedback
    text block. If there is no complete_step to carry it, the wrapper falls back to
    a trailing text block so it is never dropped. Returns the appended Message, or
    None when there is no trailing tool_use to balance AND no wrapper to add.

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
    feedback_delivered = False
    for tu in tool_uses:
        if canonicalize_tool_name(tu.name) == "complete_step":
            if wrapper_text:
                # Rework: deliver feedback as is_error complete_step result.
                result_blocks.append(ToolResultBlock(
                    tool_use_id=tu.id,
                    content=REWORK_RESULT_FRAME + "\n\n" + wrapper_text,
                    is_error=True))
                feedback_delivered = True
            else:
                result_blocks.append(ToolResultBlock(
                    tool_use_id=tu.id,
                    content="previous step output recorded; resuming",
                    is_error=False))
        else:
            result_blocks.append(ToolResultBlock(
                tool_use_id=tu.id,
                content=_synthetic_tool_result_content(tu.name), is_error=True))
    if not result_blocks and not wrapper_text:
        return None
    blocks: list[TextBlock | ToolResultBlock] = list(result_blocks)
    # Resume-durability note frames the (possibly error-shaped) results above.
    blocks.append(TextBlock(text=RESUME_DURABILITY_NOTE))
    # Fallback: no complete_step to carry it, so append as trailing text.
    if wrapper_text and not feedback_delivered:
        blocks.append(TextBlock(text=wrapper_text))
    injected = Message(role="user", content=tuple(blocks))
    messages.append(injected)
    transcript.append(injected)
    elog.append_engine_message(agent_id=agent_id, message=injected, subtype="continuation")
    return injected


def deliver_reply_on_resume(
    messages: list[Message], transcript: Transcript, elog: EventLog,
    agent_id: str, reply: str, *, human: bool,
) -> Message:
    """Deliver a human reply (or drained interject) as the next user message.

    A bare-turn-end pause leaves the transcript ending on assistant text with no
    tool_use — nothing to repair, the reply is simply the next user turn. Append
    it to `messages`, persist it, and journal a user_message/reply event (human)
    or engine_message/continuation (framework injection); the now
    user-terminated transcript drives the next LLM turn.
    """
    injected = Message(role="user", content=(TextBlock(text=reply),))
    messages.append(injected)
    transcript.append(injected)
    if human:
        elog.append_user_message(agent_id=agent_id, message=injected, subtype="reply")
    else:
        elog.append_engine_message(agent_id=agent_id, message=injected, subtype="continuation")
    return injected


def deliver_continuation_reply(
    messages: list[Message] | None,
    transcript: Transcript | None,
    elog: EventLog,
    agent_id: str,
    reply: SolicitReply,
    *,
    human: bool,
) -> str:
    """Deliver a bare-turn-end / resume continuation reply as the next user turn.

    A plain str reply journals a user_message/reply (human=True) or
    engine_message/continuation (human=False) and appends it to
    messages/transcript. A ContinuationNote is a framework injection: it always
    journals engine_message/continuation, whatever `human` the provider passed.
    A SkillReply journals the command as user_message/reply
    (event-only, step agent_id) + a skill_load(source="human") event + the body
    as engine_message/continuation, appending ONLY the body — so the command
    provenance renders `>` and consumes the awaiting-reply arm while the body
    lands non-interactive (dropped in lean, `injected message:` under --full).

    Returns the text that drives the next agent turn: the body for a SkillReply,
    the reply text otherwise. messages/transcript are None for codex's
    string-prompt model, which keeps no message-list transcript here.
    """
    if isinstance(reply, SkillReply):
        command_msg = Message(
            role="user", content=(TextBlock(text=reply.command),))
        elog.append_user_message(
            agent_id=agent_id, message=command_msg, subtype="reply")
        elog.append(agent_id=None, type="skill_load",
                    payload={"name": reply.name, "source": "human"})
        body_msg = Message(role="user", content=(TextBlock(text=reply.body),))
        if messages is not None:
            messages.append(body_msg)
        if transcript is not None:
            transcript.append(body_msg)
        elog.append_engine_message(
            agent_id=agent_id, message=body_msg, subtype="continuation")
        return reply.body
    text = reply.text if isinstance(reply, ContinuationNote) else reply
    msg = Message(role="user", content=(TextBlock(text=text),))
    if messages is not None:
        messages.append(msg)
    if transcript is not None:
        transcript.append(msg)
    # A framework note is never the human's words, whatever `human` providers pass.
    if human and not isinstance(reply, ContinuationNote):
        elog.append_user_message(agent_id=agent_id, message=msg, subtype="reply")
    else:
        elog.append_engine_message(agent_id=agent_id, message=msg, subtype="continuation")
    return text


def deliver_engine_continuation(
    messages: list[Message], transcript: Transcript, elog: EventLog,
    agent_id: str, reply: str,
) -> Message:
    """Deliver an engine-generated continuation nudge as the next user turn.

    Same transcript effect as deliver_reply_on_resume — append the nudge to
    `messages`/`transcript` so it drives the next LLM turn — but journal an
    `engine_message`/`continuation` event instead of a human `user_message`.
    The nudge is the engine self-continuing a retry, not a person speaking,
    so it must NOT render with the `>` caret.
    """
    injected = Message(role="user", content=(TextBlock(text=reply),))
    messages.append(injected)
    transcript.append(injected)
    elog.append_engine_message(agent_id=agent_id, message=injected, subtype="continuation")
    return injected

