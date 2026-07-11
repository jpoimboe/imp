"""Internal message shapes + AgentRunner protocol (spec §6.1, §6.3, §6.4).

All three V1 runners (Anthropic this plan; OpenAI + Claude Code in Plan 2)
share these types. Each runner converts to/from its provider-native shape
inside `run()`. Transcripts on disk are JSONL of these types.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from imp.engine.env import TestGates
from imp.engine.types import AgentDef


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str | list[ContentPart] | list[dict[str, Any]]
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"

    def __post_init__(self) -> None:
        # The Claude Code SDK can hand us is_error=None (an explicit null, not a
        # missing attr). None serializes to JSON null, which the Anthropic API
        # rejects on replay. Coerce once here — the constructor is the single
        # boundary every persistence/replay path funnels through. frozen+slots
        # forbids normal assignment, so use object.__setattr__.
        object.__setattr__(self, "is_error", bool(self.is_error))


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    # Anthropic extended-thinking block. Replayed unchanged (with its encrypted
    # `signature`) so a tool-use turn carrying thinking is accepted on resend.
    # `redacted_thinking` holds the base64 payload of the rare safety-redacted
    # variant (wire type "redacted_thinking"); None for an ordinary block.
    thinking: str = ""
    signature: str = ""
    redacted_thinking: str | None = None
    type: Literal["thinking"] = "thinking"


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ImagePart:
    # Structured image content part. `data` is the base64 of the PROCESSED
    # bytes (the on-wire/in-memory payload). `blob` is the content-addressed
    # filename "<sha256>.<ext>" under the change's blobs/ dir; `nbytes` is the
    # processed raw byte count. Together (blob, media_type, width, height,
    # nbytes) are exactly the lean reference persisted to events.jsonl (Part 5),
    # with the base64 `data` filtered out on write and rebuilt from the blob on
    # resume.
    media_type: str
    data: str
    width: int
    height: int
    blob: str
    nbytes: int
    type: Literal["image"] = "image"


ContentPart = TextPart | ImagePart

ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock | ThinkingBlock


_MCP_PREFIX = "mcp__imp__"


def canonicalize_tool_name(name: str) -> str:
    """Strip the provider-specific MCP namespace from a persisted tool name.

    claude_code exposes imp's tools as ``mcp__imp__<name>`` and persists that
    prefixed form in the transcript; anthropic and openai use the bare ``<name>``.
    Any logic that matches a persisted tool by bare name (resume-shape
    classification, dangling-`complete_step` repair) MUST canonicalize first, or
    it silently no-ops on claude_code transcripts (kibe87 incident, 2026-07-03)."""
    return name[len(_MCP_PREFIX):] if name.startswith(_MCP_PREFIX) else name


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["user", "assistant", "system"]
    content: tuple[ContentBlock, ...]


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """Render-side extraction from a provider response: block warnings to
    surface as runner_warning events, and extended-thinking contents to
    surface as thinking events. Not consulted by reconstruction."""
    block_warnings: tuple[str, ...] = ()
    thinkings: tuple[str, ...] = ()


# Engine → runner boundary value. Frozen to make it safe to pass to subagents.
@dataclass(frozen=True, slots=True)
class ChangeContext:
    title: str
    project_root: Path
    workflow_dir: Path
    id: str = ""
    change_home: str = ""
    project_facts: dict[str, str] = field(hash=False, default_factory=dict)
    guidance: str = ""
    persona_source: str = ""


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    agent_def: AgentDef
    provider_name: str                     # "anthropic" | "openai" | "claude_code"
    model_name: str
    system_prompt: str
    initial_messages: tuple[Message, ...]
    change_context: ChangeContext
    transcript_dir: Path                   # .imp/changes/<id>/steps/<phase>/<step>/
    events_path: Path
    subagent_defs: dict[str, AgentDef] = field(default_factory=dict)
    # resolved by engine (Task 27a/28); None means no tool-count cap (opt-in only)
    max_tool_calls: int | None = None
    # The bare-turn-end / AWAITING_USER resume input channel. Wired for attended
    # top-level agents; None for subagents (their bare-turn-end pause is headless).
    # May return either a `str` or an `Awaitable[str]`; call-sites await-if-awaitable.
    user_input_fn: Callable[[str], str | Awaitable[str]] | None = None
    # Set by the executor for the END-OF-TURN nudge — distinct from
    # user_input_fn, which serves the bare-turn-end / AWAITING_USER resume path.
    # Called with the agent's last text; returns the next user-message text to
    # inject and continue. None means "no end-of-turn nudge wired"
    # (legacy/test); providers then emit the immediate no_complete_step behavior.
    continuation_fn: (
        Callable[[str], tuple[str | Awaitable[str], bool]] | None
    ) = None
    # Stable identity of this agent invocation (step/gate/subagent), used as
    # the strict filter key for reconstruction. Derived by the executor /
    # dispatch_task; empty string only in legacy direct-construction tests.
    agent_id: str = ""
    # Shared EventLog instance (imp.engine.state.EventLog protocol). The runner
    # writes audit events through this; None means "construct a private
    # JsonlEventLog over events_path" (legacy/test fallback).
    event_log: Any = None
    # Live engine State for the rename_change tool to mutate. Any to avoid an
    # import cycle. None for subagents / legacy tests.
    change_state: Any = None
    # The LIVE, mutable Change for the rename_change tool. Any to avoid an import
    # cycle. None for subagents / legacy tests (no rename authority anyway).
    change: Any = None
    # parent_id override for this agent's FIRST emitted event (step_context):
    # the spawning step_started id (steps/gates) or task tool_call id (subagents).
    first_event_parent_id: str | None = None
    # Set by the executor when this invocation targets a step that just had a
    # gate fail: the already-formatted feedback string to inject on resume
    # (design §4). None on every fresh run and every non-targeted step.
    retry_feedback: str | None = None
    # When True, a resumed transcript that ends in a successful complete_step is
    # balanced with a SUCCESS-shaped tool_result (the agent's accepted work is
    # superseded, not failed). Set by the executor for every re-entry driven by
    # is_resume (a `resume:` edge). retry_feedback (above) now means "optional
    # wrapper text appended on that balancing user turn"; None = balance only.
    balance_on_resume: bool = False
    # True when a resume/respawn should deliver the continuation nudge (unioned
    # from State.interrupt_pending and State.retry_continue_pending) rather than
    # solicit an empty reply and park.
    resume_should_continue: bool = False
    # tier name → ProviderModel, the run's tier map. Threaded by the executor
    # into every AgentInvocation; copied onto ToolContext so dispatch_task can
    # place a subagent on its tier's resolved provider. Typed Any to avoid
    # importing ProviderModel here.
    resolved_tiers: dict[str, Any] | None = None
    # The effective, config tier menu for this run (ordered
    # cheapest->strongest). Copied onto ToolContext by the provider; surfaced to
    # the model as the `task` tool's optional `tier` enum. () = feature off.
    tiers: tuple[str, ...] = ()
    # The tier label this invocation resolved against ("low"|"medium"|"high"), so
    # a provider can set a per-tier posture (e.g. claude_code disables extended
    # thinking on "low" to match anthropic). None = no tier label (legacy/test, or
    # a subagent inheriting the parent's provider/model without an explicit tier).
    tier: str | None = None
    # Nesting bound's threading path: dispatch_task sets the child's depth to
    # parent + 1 and refuses past MAX_SUBAGENT_DEPTH. A field WITHOUT this
    # threading would read 0 forever and never trip. Each provider copies it
    # onto the child ToolContext (alongside resolved_tiers).
    subagent_depth: int = 0
    # The run's InterruptCoordinator. Copied onto ToolContext so dispatch_task
    # can drive a subagent with pause detection + interrupt attachment. Any to
    # avoid importing cli.signals here. None for legacy/test construction.
    coordinator: Any = None
    # Resolved tiered gate commands+timeouts (TestGates), threaded onto the child
    # ToolContext by each provider so the agent bash tool injects IMP_TEST_CMD_*.
    test_gates: TestGates | None = None
    # §10/§12: when True, the run is headless (no human at the keyboard) and the
    # subagent backoff loop applies a wall-clock ceiling instead of waiting forever.
    # Threaded from run_change → _route_agent → _run_agent_invocation →
    # build_agent_invocation → provider → child ToolContext → dispatch_task.
    headless: bool = False
    # True for an `interactive: true` workflow step (threshold, finalize, the
    # patent-workflow gates): human-gated on every turn, so the loop guard's
    # runaway detectors can only false-positive. Threaded step.interactive →
    # AgentInvocation → every provider's LoopGuard, where it disables both
    # detectors. Default False for subagents / legacy tests.
    interactive: bool = False
    # Resolved sandbox posture for this run. Threaded from run_change →
    # _route_agent → _run_agent_invocation → build_agent_invocation → provider →
    # ToolContext so every bash tool call applies the same posture. Any to avoid
    # importing SandboxPolicy here; None means no-op (bare sh -c, inactive policy).
    sandbox_policy: Any = None
    # The configured context-checkpoint threshold (a window fraction <= 1, or an
    # absolute token cap) for the Tier A proactive-handoff watch. Set from
    # State.checkpoint_threshold at the build sites; None disables the watch.
    checkpoint_threshold: float | int | None = None


Event = dict[str, Any]                     # {"type": str, "payload": dict}


class AgentRunner(Protocol):
    """Each provider's runner implements this. Engine consumes the event stream."""

    def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        """Yield events. Terminal event is `agent_output` (success) or `error`."""
        ...

    async def interrupt(self) -> None:
        """Cancel any in-flight LLM stream or tool subprocess. Idempotent.
        Called by `cli/signals.py` on ESC/Ctrl+C/Ctrl+D."""
        ...


# ── JSON serialization for transcript.jsonl ────────────────────────────────


def message_to_json(msg: Message) -> str:
    payload: dict[str, Any] = {
        "role": msg.role,
        "content": [_block_to_dict(b) for b in msg.content],
    }
    return json.dumps(payload, separators=(",", ":"))


def _content_part_to_dict(part: ContentPart) -> dict[str, Any]:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    # Persist a blob reference only; the base64 `data` is rebuilt from the blob on load.
    return {
        "type": "image", "media_type": part.media_type,
        "width": part.width, "height": part.height,
        "blob": part.blob, "nbytes": part.nbytes,
    }


def _content_part_from_dict(
    raw: dict[str, Any], blobs_dir: Path | None
) -> ContentPart:
    if raw["type"] == "text":
        return TextPart(text=raw["text"])
    blob = raw["blob"]
    if blobs_dir is not None:
        blob_path = blobs_dir / blob
        if blob_path.exists():
            data = base64.b64encode(blob_path.read_bytes()).decode("ascii")
            return ImagePart(
                media_type=raw["media_type"], data=data,
                width=raw["width"], height=raw["height"],
                blob=blob, nbytes=raw["nbytes"],
            )
    # Blob gone or no blobs dir: degrade to a text note keyed by the content sha.
    sha = blob.rsplit(".", 1)[0]
    return TextPart(text=f"[image {sha} no longer available]")


def _block_to_dict(b: ContentBlock) -> dict[str, Any]:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, ThinkingBlock):
        return {"type": "thinking", "thinking": b.thinking,
                "signature": b.signature, "redacted_thinking": b.redacted_thinking}
    if isinstance(b.content, str):
        content: Any = b.content
    elif b.content and all(isinstance(p, (TextPart, ImagePart)) for p in b.content):
        content = [_content_part_to_dict(p) for p in b.content]  # type: ignore[arg-type]
    else:
        content = b.content  # native Claude SDK list[dict]; persist verbatim
    return {
        "type": "tool_result", "tool_use_id": b.tool_use_id,
        "content": content, "is_error": b.is_error,
    }


def message_from_json(data: dict[str, Any], blobs_dir: Path | None = None) -> Message:
    blocks: list[ContentBlock] = []
    for raw in data["content"]:
        kind = raw["type"]
        if kind == "text":
            blocks.append(TextBlock(text=raw["text"]))
        elif kind == "tool_use":
            blocks.append(ToolUseBlock(id=raw["id"], name=raw["name"], input=raw["input"]))
        elif kind == "tool_result":
            raw_content = raw["content"]
            if isinstance(raw_content, str):
                content: Any = raw_content
            elif any(isinstance(p, dict) and p.get("type") == "image" and "blob" in p
                     for p in raw_content):
                content = [_content_part_from_dict(p, blobs_dir) for p in raw_content]
            else:
                content = raw_content  # native Claude SDK list[dict]; keep verbatim
            blocks.append(ToolResultBlock(
                tool_use_id=raw["tool_use_id"],
                content=content,
                is_error=raw.get("is_error", False),
            ))
        elif kind == "thinking":
            blocks.append(ThinkingBlock(
                thinking=raw.get("thinking", ""),
                signature=raw.get("signature", ""),
                redacted_thinking=raw.get("redacted_thinking"),
            ))
        else:
            raise ValueError(f"unknown block type {kind!r}")
    return Message(role=data["role"], content=tuple(blocks))
